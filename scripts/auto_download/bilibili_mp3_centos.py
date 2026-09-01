"""Synchronize a Bilibili collection as cleaned MP3 files on CentOS."""

from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from .cli import run_profile
from .profiles import BILIBILI_MP3


if __name__ == "__main__":
    raise SystemExit(run_profile(BILIBILI_MP3, needs_node=False))
