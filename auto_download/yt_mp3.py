"""Synchronize YouTube playlists as cleaned MP3 files on Windows."""

from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_download.cli import run_profile
from auto_download.profiles import YOUTUBE_MP3


if __name__ == "__main__":
    raise SystemExit(run_profile(YOUTUBE_MP3, needs_node=True))
