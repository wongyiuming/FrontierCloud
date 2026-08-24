"""在 CentOS 上增量下载 YouTube 播放列表并转换为 MP3。"""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import ModuleType


DEFAULT_CHANNEL_URL = "https://www.youtube.com/@%E7%BE%BD%E6%B1%9F-f4k/playlists"
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MEDIA_DIR = BASE_DIR / "data" / "media"


def load_yt_dlp() -> ModuleType:
    """加载第三方 yt-dlp，并对项目内同名目录给出明确提示。"""
    try:
        module = importlib.import_module("yt_dlp")
    except ImportError as exc:
        raise RuntimeError(
            "未安装 yt-dlp。请执行："
            "uv pip install --python .venv/bin/python -U 'yt-dlp[default]'"
        ) from exc

    if not hasattr(module, "YoutubeDL"):
        raise RuntimeError(
            "当前 Python 未安装第三方 yt-dlp，加载到的是项目内的同名目录。"
            "请使用 .venv/bin/python 运行，或先安装 yt-dlp。"
        )
    return module


def resolve_executable(
    command: str, configured_path: str | None, environment_name: str
) -> str:
    """优先使用参数/环境变量，否则从 CentOS 的 PATH 中查找。"""
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if candidate.is_dir():
            candidate /= command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise RuntimeError(f"找不到可执行的 {command}：{candidate}")

    discovered = shutil.which(command)
    if discovered:
        return str(Path(discovered).resolve())
    raise RuntimeError(
        f"CentOS 的 PATH 中找不到 {command}。请先安装它，或设置 "
        f"{environment_name} 指向可执行文件。"
    )


def require_supported_node(node_path: str) -> str:
    """当前 yt-dlp EJS 要求 Node.js 22 或更高版本。"""
    try:
        result = subprocess.run(
            [node_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法运行 Node.js：{exc}") from exc

    version = result.stdout.strip()
    match = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}", version)
    if not match:
        raise RuntimeError(f"无法识别 Node.js 版本：{version!r}")
    if int(match.group(1)) < 22:
        raise RuntimeError(f"Node.js 版本过低（{version}），需要 22 或更高版本。")
    return version


def resolve_node(configured_path: str | None) -> tuple[str, str]:
    """查找可运行的 Node，兼容服务环境未包含 ~/.local/bin 的情况。"""
    if configured_path:
        node_path = resolve_executable("node", configured_path, "NODE_PATH")
        return node_path, require_supported_node(node_path)

    candidates = [Path.home() / ".local" / "bin" / "node"]
    if discovered := shutil.which("node"):
        candidates.append(Path(discovered))
    candidates.extend((Path("/usr/local/bin/node"), Path("/usr/bin/node")))

    errors = []
    visited = set()
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        node_path = str(candidate.resolve())
        if node_path in visited:
            continue
        visited.add(node_path)
        try:
            return node_path, require_supported_node(node_path)
        except RuntimeError as exc:
            errors.append(str(exc))

    detail = f" 已发现但不可用：{'；'.join(errors)}" if errors else ""
    raise RuntimeError(
        "找不到可用的 Node.js 22+。请安装它，或设置 NODE_PATH 指向可执行文件。"
        + detail
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.getenv("YOUTUBE_URL", DEFAULT_CHANNEL_URL),
        help="YouTube 频道、播放列表或视频地址",
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=Path(os.getenv("MEDIA_DIR", DEFAULT_MEDIA_DIR)),
        help="MP3 输出根目录",
    )
    parser.add_argument(
        "--ffmpeg",
        default=os.getenv("FFMPEG_PATH"),
        help="ffmpeg 可执行文件或其目录；默认从 PATH 查找",
    )
    parser.add_argument(
        "--node",
        default=os.getenv("NODE_PATH"),
        help="Node.js 可执行文件或其目录；默认从 PATH 查找",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查依赖，不访问 YouTube 或下载文件",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    yt_dlp = load_yt_dlp()
    ffmpeg_path = resolve_executable("ffmpeg", args.ffmpeg, "FFMPEG_PATH")
    node_path, node_version = resolve_node(args.node)

    if args.check:
        print(f"yt-dlp: {Path(yt_dlp.__file__).resolve()}")
        print(f"ffmpeg: {ffmpeg_path}")
        print(f"node: {node_path} ({node_version})")
        print(f"媒体目录: {args.media_dir.resolve()}")
        return 0

    media_dir = args.media_dir.expanduser().resolve()
    media_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "ffmpeg_location": ffmpeg_path,
        "outtmpl": str(media_dir / "%(playlist_title)s" / "%(title)s.%(ext)s"),
        "download_archive": str(media_dir / "archive.txt"),
        "noplaylist": False,
        "no_plugins": True,
        "remote_components": {"ejs:github"},
        "js_runtimes": {"node": {"path": node_path}},
        "rm_cachedir": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "191",
            }
        ],
        "ignoreerrors": True,
        "quiet": False,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.download([args.url]) or 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"依赖检查失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
