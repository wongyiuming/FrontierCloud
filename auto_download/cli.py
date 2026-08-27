"""Command-line runner shared by every automatic media synchronization script."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .download_support import load_yt_dlp, resolve_executable
from .media_sync import (
    MediaSynchronizer,
    SyncProfile,
    build_yt_dlp_downloader,
    discover_remote_items,
)
from .profiles import with_source_url


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MEDIA_DIR = BASE_DIR / "data" / "media"


def build_parser(profile: SyncProfile, *, needs_node: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将远端播放列表强一致同步到清洗后的 media 目录。"
    )
    parser.add_argument("--url", default=profile.source_url, help="频道或播放列表地址")
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=Path(os.getenv("MEDIA_DIR", DEFAULT_MEDIA_DIR)),
        help="最终媒体目录；文件名在写入时已经清洗",
    )
    parser.add_argument(
        "--ffmpeg",
        default=os.getenv("FFMPEG_PATH"),
        help="ffmpeg 可执行文件或其目录；默认从 PATH 查找",
    )
    if needs_node:
        parser.add_argument(
            "--node",
            default=os.getenv("NODE_PATH"),
            help="Node.js 可执行文件或其目录；默认从 PATH 查找",
        )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查解释器和外部依赖，不访问远端、不下载",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取远端元数据并汇报计划，不下载、不删除、不写清单",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="额外显示 yt-dlp 的关键 debug 消息",
    )
    return parser


def run_profile(profile: SyncProfile, *, needs_node: bool) -> int:
    args = build_parser(profile, needs_node=needs_node).parse_args()
    try:
        yt_dlp = load_yt_dlp()
        ffmpeg_path = resolve_executable("ffmpeg", args.ffmpeg)
        node_path = resolve_executable("node", args.node) if needs_node else None
        effective_profile = with_source_url(profile, args.url)

        if args.check:
            print(f"Python: {sys.executable}")
            print(f"yt-dlp: {Path(yt_dlp.__file__).resolve()}")
            print(f"ffmpeg: {ffmpeg_path}")
            if node_path:
                print(f"node: {node_path}")
            print(f"media: {args.media_dir.expanduser().resolve()}")
            return 0

        print(f"[元数据] 正在读取：{effective_profile.source_url}")
        remote_items = discover_remote_items(
            yt_dlp,
            effective_profile,
            ffmpeg_path=ffmpeg_path,
            node_path=node_path,
            debug=args.debug,
        )
        if not remote_items:
            print("[错误] 远端没有返回任何可同步项目。", file=sys.stderr)
            return 1
        print(f"[元数据] 共发现 {len(remote_items)} 项")

        synchronizer = MediaSynchronizer(effective_profile, args.media_dir)
        downloader = build_yt_dlp_downloader(
            yt_dlp,
            effective_profile,
            ffmpeg_path=ffmpeg_path,
            node_path=node_path,
            debug=args.debug,
        )
        report = synchronizer.synchronize(
            remote_items,
            downloader,
            dry_run=args.dry_run,
        )
        report.print()
        return 1 if report.failed else 0
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
