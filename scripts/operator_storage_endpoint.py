#!/usr/bin/env python3
"""Linux Docker-host wrapper for offline endpoint moves. Leaves maintenance on."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

# A direct script invocation has scripts/ rather than the repository on sys.path.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.operator_backup import BackupError, Docker, Plan, absolute, operation_lock, private_write


def handover(plan, command, *, confirm, previous, endpoint, relationship=""):
    # Validate all destructive/downtime preconditions before creating a fence.
    for value in (confirm, previous, endpoint, relationship):
        if not isinstance(value, str) or "\0" in value or len(value) > 512:
            raise BackupError("invalid handover arguments")
    import re
    if not re.fullmatch("[a-f0-9]{32}", confirm) or (command == "storage-rebind" and not re.fullmatch("[a-f0-9]{32}", relationship)):
        raise BackupError("exact NodeID and relationship confirmation are required")
    if command == "storage-endpoint" and relationship:
        raise BackupError("storage-endpoint accepts no relationship")
    if command not in {"storage-endpoint", "storage-rebind"}:
        raise BackupError("unknown offline storage operation")
    plan.assert_cold(allow_project=True)
    root = plan.resources["control"].root
    marker = root / ".frontiercloud-native-maintenance"
    if marker.exists() or marker.is_symlink():
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError("unsafe existing maintenance fence")
    else:
        private_write(marker, b"offline-storage-handover\n")
        os.chown(marker, 10001, 10001, follow_symlinks=False)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    services = sorted(set(plan.config["services"]) - ({"mysql"} if plan.engine == "mysql" else set()))
    plan.docker.run(plan.docker.base + ["--profile", "*", "stop", "--timeout", "30", *services])
    plan.assert_cold(allow_services=frozenset({"mysql"}) if plan.engine == "mysql" else frozenset())
    arguments = [command, "--confirm-node-id", confirm, "--previous-endpoint", previous, "--endpoint", endpoint]
    if relationship:
        arguments += ["--relationship", relationship]
    # Uses the Web service's existing mounts/env/UID and existing DB secrets.
    # No ports published, no dependencies started, no host Docker socket in Web.
    return plan.docker.run(plan.docker.base + ["run", "--rm", "--no-deps", "--entrypoint", "python", "web", "-m", "app.services.offline_storage_endpoint", *arguments])


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("storage-endpoint", "storage-rebind"))
    parser.add_argument("--project-directory", required=True, type=Path)
    parser.add_argument("--compose-file", action="append", required=True, type=Path)
    parser.add_argument("--confirm-node-id", required=True)
    parser.add_argument("--previous-endpoint", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--relationship", default="")
    options = parser.parse_args(arguments)
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise BackupError("run the operator wrapper as root on its local Linux Docker host")
        plan = Plan.load(Docker(absolute(options.project_directory), options.compose_file))
        with operation_lock(plan.name):
            print(handover(plan, options.command, confirm=options.confirm_node_id, previous=options.previous_endpoint,
                          endpoint=options.endpoint, relationship=options.relationship))
        return 0
    except (BackupError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, BackupError) else type(error).__name__
        print("Offline operator wrapper failed (maintenance retained): " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
