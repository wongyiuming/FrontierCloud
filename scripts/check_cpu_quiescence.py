#!/usr/bin/env python3
"""Require a one-core CI container to return to low CPU after business tests."""
from __future__ import annotations

import argparse
import subprocess
import time


def cpu_percent(container: str) -> float:
    value = subprocess.check_output(
        ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container],
        text=True,
        timeout=5,
    ).strip()
    return float(value.removesuffix("%"))


def wait_for_quiescence(
    container: str,
    *,
    threshold: float = 20.0,
    consecutive: int = 5,
    timeout: float = 15.0,
) -> bool:
    deadline = time.monotonic() + timeout
    low_samples = 0
    samples: list[float] = []
    while time.monotonic() < deadline:
        value = cpu_percent(container)
        samples.append(value)
        low_samples = low_samples + 1 if value < threshold else 0
        if low_samples >= consecutive:
            print(f"CPU recovered below {threshold:.0f}% for {consecutive} samples: {samples}")
            return True
        time.sleep(1)
    print(f"CPU did not recover below {threshold:.0f}%: {samples}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("container")
    parser.add_argument("--threshold", type=float, default=20.0)
    parser.add_argument("--consecutive", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    return 0 if wait_for_quiescence(
        args.container,
        threshold=args.threshold,
        consecutive=args.consecutive,
        timeout=args.timeout,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
