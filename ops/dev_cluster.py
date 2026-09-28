"""Provision persistent isolated Docker hosts for the ten-node development cluster.

This is operator tooling, never a business-service runtime component. Each node
owns a Docker daemon so old production Updaters retain their normal container
names and cannot replace another node's services. All commands run as root.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


NAMES = ["master", *[f"direct-{i}" for i in range(1, 4)], *[f"relay-{i}" for i in range(1, 7)]]
REPOSITORY = "https://github.com/wongyiuming/FrontierCloud.git"
DIND_IMAGE = "docker:29.1.3-dind"


def run(*args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args!r}\n{result.stderr[-6000:]}")
    return result.stdout.strip()


def inner(name, *args):
    return run("docker", "exec", "fc-dev-host-" + name, "docker", *args)


def compose(name, *args):
    return inner(name, "compose", "--project-directory", "/node/repo", "-p", "frontiercloud",
                 "-f", "/node/repo/docker-compose.yaml", "-f", "/node/override.json", *args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/root/frontiercloud-dev"))
    parser.add_argument("--address", required=True)
    parser.add_argument("--initial-sha", required=True)
    parser.add_argument("--images", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("root is required")
    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (root / "inventory.json").exists():
        raise RuntimeError("Persistent cluster already exists; use CD, never reprovision it")
    ca = root / "ca"
    ca.mkdir(mode=0o700, exist_ok=True)
    if not (ca / "root.crt").exists():
        run("openssl", "req", "-x509", "-nodes", "-days", "3650", "-newkey", "rsa:3072",
            "-keyout", str(ca / "root.key"), "-out", str(ca / "root.crt"),
            "-subj", "/CN=FrontierCloud persistent development CA",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        (ca / "root.key").chmod(0o600)
    (ca / "bundle.crt").write_bytes(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes() + b"\n" + (ca / "root.crt").read_bytes())
    network = "fc-dev-hosts"
    existing = run("docker", "network", "ls", "--filter", "name=^" + network + "$", "--format", "{{.Name}}")
    if not existing:
        run("docker", "network", "create", "--subnet", "172.29.252.0/24", network)
    inventory = {"root": str(root), "ca": str(ca / "root.crt"), "address": args.address, "nodes": []}
    for index, name in enumerate(NAMES):
        node = root / "nodes" / name
        node.mkdir(mode=0o700, parents=True, exist_ok=True)
        repo = node / "repo"
        if not (repo / ".git").exists():
            run("git", "clone", REPOSITORY, str(repo))
            run("git", "-C", str(repo), "checkout", "--detach", args.initial_sha)
        if run("git", "-C", str(repo), "rev-parse", "HEAD") != args.initial_sha:
            raise RuntimeError(f"{name}: existing checkout differs; refusing reset")
        certs = repo / "certs"
        certs.mkdir(exist_ok=True)
        (certs / "extensions.conf").write_text(
            f"subjectAltName=IP:{args.address}\nbasicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        run("openssl", "req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(certs / "privkey.pem"),
            "-out", str(certs / "request.pem"), "-subj", f"/CN={args.address}")
        run("openssl", "x509", "-req", "-in", str(certs / "request.pem"), "-CA", str(ca / "root.crt"),
            "-CAkey", str(ca / "root.key"), "-CAcreateserial", "-days", "365", "-out", str(certs / "fullchain.pem"),
            "-extfile", str(certs / "extensions.conf"))
        (repo / ".env").write_text(
            f"TLS_ENABLED=true\nSERVER_NAME={args.address}\nINSTANCE_NAME=dev-{name}\n"
            f"HTTP_PORT=80\nHTTPS_PORT=443\nWEBRTC_STUN_PORT={3478 + index}\n")
        override = {"services": {service: {"volumes": [
            {"type": "bind", "source": "/dev-ca/bundle.crt", "target": "/etc/ssl/certs/ca-certificates.crt", "read_only": True}]
        } for service in ("web", "nginx")}}
        override["services"]["web"]["environment"] = {"SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"}
        (node / "override.json").write_text(json.dumps(override))
        daemon = "fc-dev-host-" + name
        exists = run("docker", "ps", "-a", "--filter", "name=^/" + daemon + "$", "--format", "{{.Names}}")
        if not exists:
            run("docker", "run", "-d", "--privileged", "--restart", "unless-stopped", "--name", daemon,
                "--network", network,
                "-v", f"{node}:/node", "-v", f"{node / 'docker'}:/var/lib/docker",
                "-v", f"{ca}:/dev-ca:ro", "-v", f"{args.images.resolve()}:/seed-images.tar:ro",
                "-p", f"{args.address}:{14443 + index}:443", "-p", f"{args.address}:{18080 + index}:80",
                "-p", f"{args.address}:{3478 + index}:{3478 + index}/udp",
                "-p", f"{args.address}:{3478 + index}:{3478 + index}/tcp",
                DIND_IMAGE, "dockerd", "--host=unix:///var/run/docker.sock", "--storage-driver=overlay2")
        for attempt in range(60):
            try:
                inner(name, "info", "--format", "{{.ServerVersion}}")
                break
            except RuntimeError:
                time.sleep(2)
        else:
            raise RuntimeError(f"Docker daemon not ready: {name}")
        inner(name, "load", "-i", "/seed-images.tar")
        compose(name, "up", "-d", "--no-build", "--wait", "--wait-timeout", "240")
        compose(name, "exec", "-T", "nginx", "nginx", "-t")
        inventory["nodes"].append({"name": name, "mode": "Master" if index == 0 else "Direct" if index < 4 else "Relay",
                                    "endpoint": f"https://{args.address}:{14443 + index}", "daemon": daemon})
        (root / "provision-progress.json").write_text(json.dumps(inventory, indent=2))
        print(json.dumps({"ready": name, "initial_sha": args.initial_sha}), flush=True)
    (root / "inventory.json").write_text(json.dumps(inventory, indent=2))
    print("PERSISTENT_CLUSTER_PROVISIONED", flush=True)


if __name__ == "__main__":
    main()
