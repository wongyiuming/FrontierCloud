"""Shared dependency checks, concise logging, and download summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from pathlib import Path
import shutil
import sys
import time
from types import ModuleType
from typing import TextIO


def load_yt_dlp() -> ModuleType:
    """Import the third-party yt-dlp package and validate its public API."""
    try:
        module = importlib.import_module("yt_dlp")
    except ImportError as exc:
        raise RuntimeError(
            "未安装 yt-dlp，请在项目 .venv 中执行：python -m pip install -e ."
        ) from exc
    if not hasattr(module, "YoutubeDL"):
        raise RuntimeError("当前解释器加载到的不是完整的第三方 yt-dlp 包。")
    return module


def resolve_executable(command: str, configured_path: str | None) -> str:
    """Resolve a configured executable or find it on PATH."""
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if candidate.is_dir():
            candidate /= f"{command}.exe" if sys.platform == "win32" else command
        if candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(f"找不到 {command}：{candidate}")
    if discovered := shutil.which(command):
        return str(Path(discovered).resolve())
    raise RuntimeError(f"PATH 中找不到 {command}，请安装或显式指定路径。")


class ConciseYtdlpLogger:
    """Discard routine yt-dlp chatter while retaining warnings and errors."""

    def __init__(self, stream: TextIO = sys.stdout, *, debug: bool = False):
        self.stream = stream
        self.debug_enabled = debug

    def debug(self, message: str) -> None:
        if self.debug_enabled and message.startswith("[debug]"):
            print(message, file=self.stream)

    def info(self, message: str) -> None:
        if message.startswith("[download] Destination:"):
            print(message, file=self.stream)

    def warning(self, message: str) -> None:
        print(f"[警告] {message}", file=self.stream)

    def error(self, message: str) -> None:
        print(f"[错误] {message}", file=self.stream)


class DownloadProgress:
    """Print only transfer speed while a media item is downloading."""

    def __init__(
        self,
        stream: TextIO = sys.stdout,
        *,
        minimum_interval: float = 1.0,
    ):
        self.stream = stream
        self.minimum_interval = minimum_interval
        self._last_update = 0.0

    @staticmethod
    def _format_speed(bytes_per_second: float | None) -> str:
        if not bytes_per_second:
            return "计算中"
        units = ("B/s", "KiB/s", "MiB/s", "GiB/s")
        value = float(bytes_per_second)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
        return "计算中"

    def __call__(self, status: dict) -> None:
        state = status.get("status")
        now = time.monotonic()
        if state == "downloading" and now - self._last_update >= self.minimum_interval:
            self._last_update = now
            print(
                f"[速度] {self._format_speed(status.get('speed'))}",
                file=self.stream,
            )


@dataclass
class SyncReport:
    """Collect and print the result of one synchronization run."""

    skipped: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def print(self, stream: TextIO = sys.stdout) -> None:
        print("\n同步汇总", file=stream)
        self._print_group(stream, "已存在，跳过", self.skipped)
        self._print_group(stream, "本次新增", self.added)
        self._print_group(stream, "远端已无，本地删除", self.deleted)
        self._print_group(stream, "失败", self.failed)

    @staticmethod
    def _print_group(stream: TextIO, title: str, values: list[str]) -> None:
        print(f"{title}（{len(values)}）", file=stream)
        for value in values:
            print(f"  - {value}", file=stream)
