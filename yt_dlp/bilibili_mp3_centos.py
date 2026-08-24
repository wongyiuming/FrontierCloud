"""在 CentOS 上增量下载 Bilibili 收藏夹/合集并转换为 MP3。"""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import shutil
import sys
from types import ModuleType


DEFAULT_CHANNEL_URL = (
    "https://space.bilibili.com/50687441/favlist?fid=4086690941&ftype=create"
)
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MEDIA_DIR = BASE_DIR / "data" / "media"

BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.bilibili.com/",
}


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


def resolve_executable(command: str, configured_path: str | None) -> str:
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
        f"{command.upper()}_PATH 指向可执行文件。"
    )


def clean_filename(name: str) -> str:
    """过滤会改变 Linux 路径含义或妨碍跨平台复制的字符。"""
    return "".join(c for c in name if c not in r'/\:*?"<>|' and c != "\0").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.getenv("BILIBILI_URL", DEFAULT_CHANNEL_URL),
        help="Bilibili 收藏夹、合集或视频地址",
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
        "--check",
        action="store_true",
        help="只检查依赖，不访问 Bilibili 或下载文件",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    yt_dlp = load_yt_dlp()
    ffmpeg_path = resolve_executable("ffmpeg", args.ffmpeg)

    if args.check:
        print(f"yt-dlp: {Path(yt_dlp.__file__).resolve()}")
        print(f"ffmpeg: {ffmpeg_path}")
        print(f"媒体目录: {args.media_dir.resolve()}")
        return 0

    media_dir = args.media_dir.expanduser().resolve()
    media_dir.mkdir(parents=True, exist_ok=True)

    extract_opts = {
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "http_headers": BILIBILI_HEADERS,
    }

    print("正在获取 Bilibili 合集/列表信息...")
    with yt_dlp.YoutubeDL(extract_opts) as ydl:
        info = ydl.extract_info(args.url, download=False)

    if not info:
        print("未能获取到页面信息。", file=sys.stderr)
        return 1

    playlists = []
    for entry in info.get("entries") or []:
        if entry and (
            entry.get("_type") == "playlist"
            or entry.get("extractor_key")
            in {"Bilibili", "BilibiliPlaylist", "BilibiliSpace"}
        ):
            playlists.append(entry)

    if not playlists and info.get("_type") == "playlist":
        playlists = [info]

    if not playlists:
        playlists = [
            {
                "title": info.get("title") or "Default_Collection",
                "url": args.url,
            }
        ]

    exit_code = 0
    for playlist in playlists:
        playlist_title = clean_filename(
            playlist.get("title") or "Default_Collection"
        ) or "Default_Collection"
        playlist_dir = media_dir / playlist_title
        playlist_dir.mkdir(parents=True, exist_ok=True)

        playlist_url = (
            playlist.get("url") or playlist.get("webpage_url") or args.url
        )
        archive_file = playlist_dir / "archive.txt"

        print("\n========================================")
        print(f"开始增量下载合集/收藏夹: {playlist_title}")
        print("========================================")

        ydl_opts = {
            "format": "bestaudio/best",
            "http_headers": BILIBILI_HEADERS,
            "ffmpeg_location": ffmpeg_path,
            "outtmpl": str(playlist_dir / "%(title)s.%(ext)s"),
            "download_archive": str(archive_file),
            "noplaylist": False,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }
            ],
            "ignoreerrors": True,
            "quiet": False,
            "retries": 10,
            "fragment_retries": 10,
            "buffer_size": 1024 * 1024,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            exit_code = max(exit_code, ydl.download([playlist_url]) or 0)

    return exit_code


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"依赖检查失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
