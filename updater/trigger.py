#!/usr/bin/env python3
"""Operator entry point for a controlled Master update."""
from __future__ import annotations

import json
import re
import socket
import sys

SOCKET_PATH = "/run/frontiercloud-updater/control.sock"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    if len(sys.argv) != 2 or not SHA_RE.fullmatch(sys.argv[1]):
        print("usage: python3 /workspace/updater/trigger.py <40-char-dev-commit-sha>", file=sys.stderr)
        return 2
    payload = json.dumps({"version": sys.argv[1], "propagate": True}, separators=(",", ":")) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(SOCKET_PATH)
        client.sendall(payload.encode())
        response = client.makefile("rb").readline(4096).decode().strip()
    print(response)
    result = json.loads(response)
    return 0 if result.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
