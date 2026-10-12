"""Validated, crash-safe generations. Only the controller writes these volumes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import ssl
import stat
import warnings
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.verification import PolicyBuilder, Store
from cryptography.utils import CryptographyDeprecationWarning

PRIVATE = Path("/var/lib/frontiercloud-certificate")
PUBLISHED = Path("/run/frontiercloud-tls")
RUNTIME = Path("/run/frontiercloud-certificate")
GENERATION = re.compile(r"^[a-f0-9]{32}$")


def bounded(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("certificate asset must be a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("certificate asset changed while opening")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("certificate asset exceeds its size limit")
    return data


def atomic(path: Path, data: bytes, mode=0o640) -> None:
    temporary = path.with_name(path.name + ".next-" + secrets.token_hex(16))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def trust_store() -> Store:
    context = ssl.create_default_context()
    roots = []
    for value in context.get_ca_certs(binary_form=True):
        with warnings.catch_warnings():
            warnings.simplefilter("error", CryptographyDeprecationWarning)
            try:
                roots.append(x509.load_der_x509_certificate(value))
            except (ValueError, CryptographyDeprecationWarning):
                # Malformed/legacy non-RFC roots cannot anchor product certificates.
                continue
    return Store(roots)


def validate(host: str, certificate: bytes, key: bytes, *, roots: Store | None = None, now=None):
    if not 0 < len(certificate) <= 65536 or not 0 < len(key) <= 16384:
        raise ValueError("invalid certificate pair size")
    chain = x509.load_pem_x509_certificates(certificate)
    if not 1 <= len(chain) <= 8:
        raise ValueError("invalid certificate chain")
    private_key = serialization.load_pem_private_key(key, password=None)
    public = lambda value: value.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(private_key.public_key()) != public(chain[0].public_key()):
        raise ValueError("certificate and private key mismatch")
    verifier = PolicyBuilder().store(roots or trust_store()).time(now or datetime.now(timezone.utc)).build_server_verifier(x509.DNSName(host))
    verifier.verify(chain[0], chain[1:])
    return chain[0]


def fingerprint(leaf) -> str:
    return hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()


def pointer(directory: Path = PUBLISHED) -> dict:
    value = json.loads(bounded(directory / "current.json", 512))
    if not isinstance(value, dict) or not GENERATION.fullmatch(value.get("generation", "")):
        raise ValueError("invalid TLS generation pointer")
    if value.get("previous") and not GENERATION.fullmatch(value["previous"]):
        raise ValueError("invalid previous TLS generation")
    return value


def material(directory: Path = PUBLISHED):
    value = pointer(directory)
    base = directory / value["generation"]
    if not stat.S_ISDIR(base.lstat().st_mode):
        raise ValueError("invalid TLS generation directory")
    return value["generation"], bounded(base / "fullchain.pem", 65536), bounded(base / "privkey.pem", 16384)


def publish(host: str, certificate: bytes, key: bytes, *, directory=PUBLISHED, roots=None, now=None):
    leaf = validate(host, certificate, key, roots=roots, now=now)
    try:
        previous = pointer(directory)["generation"]
    except (OSError, ValueError):
        previous = ""
    generation = secrets.token_hex(16)
    base = directory / generation
    base.mkdir(mode=0o750)
    os.chmod(base, 0o750)
    try:
        atomic(base / "fullchain.pem", certificate)
        atomic(base / "privkey.pem", key)
        atomic(base / "verified.json", json.dumps({
            "hostname": host, "fingerprint": fingerprint(leaf),
            "not_before": int(leaf.not_valid_before_utc.timestamp()),
            "not_after": int(leaf.not_valid_after_utc.timestamp()),
        }).encode())
        atomic(directory / "current.json", json.dumps({"generation": generation, "previous": previous}).encode())
    except BaseException:
        # Preserve a generation if its pointer became visible before an fsync failure.
        try:
            committed = pointer(directory)["generation"] == generation
        except (OSError, ValueError):
            committed = False
        if not committed:
            for name in ("fullchain.pem", "privkey.pem", "verified.json"):
                (base / name).unlink(missing_ok=True)
            base.rmdir()
        raise
    return leaf


def initialize() -> None:
    for directory, mode in ((PRIVATE, 0o700), (RUNTIME, 0o710), (PUBLISHED, 0o750)):
        directory.mkdir(mode=mode, exist_ok=True)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("certificate volumes must not be symlinks")
        os.chmod(directory, mode)
        os.chown(directory, 0, 10001)


def prune(directory: Path = PUBLISHED) -> None:
    """Only a confirmed serving generation authorizes reclaiming old pairs."""
    current = pointer(directory)
    keep = {current["generation"], current.get("previous", "")}
    expected = {"fullchain.pem", "privkey.pem", "verified.json"}
    for base in directory.iterdir():
        if base.name in keep or not GENERATION.fullmatch(base.name) or not stat.S_ISDIR(base.lstat().st_mode):
            continue
        children = list(base.iterdir())
        if {child.name for child in children} != expected or any(not stat.S_ISREG(child.lstat().st_mode) for child in children):
            continue
        for child in children:
            child.unlink()
        base.rmdir()
