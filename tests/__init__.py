"""FrontierCloud test modules and repository-level source contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _assert_runtime_data_is_untracked() -> None:
    # Source-contract jobs have the repository metadata; runtime images do not.
    if not (ROOT / ".git").exists():
        return
    result = subprocess.run(
        ["git", "ls-files", "--", "data/"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    if tracked:
        raise RuntimeError(
            "Runtime data must stay outside Git; bootstrap required files at runtime instead: "
            + ", ".join(tracked)
        )


_assert_runtime_data_is_untracked()
