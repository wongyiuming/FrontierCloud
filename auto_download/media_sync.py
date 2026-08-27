"""Synchronize remote playlists into a cleaned local media tree."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Callable, Iterable

from .download_support import ConciseYtdlpLogger, DownloadProgress, SyncReport
from .nama_clean import allocate_unique_stem, sanitize_component


@dataclass(frozen=True)
class SyncProfile:
    """Describe one remote source and its desired local media format."""

    name: str
    source_url: str
    media_kind: str
    extension: str
    format_selector: str
    headers: dict[str, str]
    retries: int = 3


@dataclass(frozen=True)
class RemoteItem:
    """Represent one downloadable item discovered in a remote playlist."""

    media_id: str
    extractor: str
    original_title: str
    original_playlist: str
    webpage_url: str


@dataclass(frozen=True)
class ManifestItem:
    """Persist remote identity, original names, and the clean local path."""

    media_id: str
    extractor: str
    original_title: str
    original_playlist: str
    clean_title: str
    clean_playlist: str
    relative_path: str


DownloadCallback = Callable[[RemoteItem, Path], bool]


class MediaSynchronizer:
    """Reconcile a remote item list with one local managed media collection."""

    def __init__(self, profile: SyncProfile, media_dir: Path):
        self.profile = profile
        self.media_dir = media_dir.expanduser().resolve()
        self.metadata_dir = self.media_dir / ".sync"
        self.manifest_path = self.metadata_dir / f"{profile.name}.json"

    def load_manifest(self) -> dict[str, ManifestItem]:
        if not self.manifest_path.is_file():
            return {}
        try:
            document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return {
                media_id: ManifestItem(**value)
                for media_id, value in document.get("items", {}).items()
            }
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"无法读取同步清单：{self.manifest_path}: {exc}") from exc

    def save_manifest(self, items: dict[str, ManifestItem]) -> None:
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "version": 1,
            "profile": self.profile.name,
            "source_url": self.profile.source_url,
            "media_kind": self.profile.media_kind,
            "items": {
                media_id: asdict(item)
                for media_id, item in sorted(items.items())
            },
        }
        temporary_path = self.manifest_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.manifest_path)

    def plan_items(self, remote_items: Iterable[RemoteItem]) -> dict[str, ManifestItem]:
        planned: dict[str, ManifestItem] = {}
        occupied_by_playlist: dict[str, set[str]] = {}
        for remote in remote_items:
            if not remote.media_id or remote.media_id in planned:
                continue
            clean_playlist = sanitize_component(
                remote.original_playlist,
                fallback="Default_Collection",
            )
            occupied = occupied_by_playlist.setdefault(clean_playlist.casefold(), set())
            clean_title = allocate_unique_stem(
                remote.original_title,
                remote.media_id,
                occupied,
            )
            relative_path = str(
                Path(clean_playlist) / f"{clean_title}.{self.profile.extension}"
            )
            planned[remote.media_id] = ManifestItem(
                media_id=remote.media_id,
                extractor=remote.extractor,
                original_title=remote.original_title,
                original_playlist=remote.original_playlist,
                clean_title=clean_title,
                clean_playlist=clean_playlist,
                relative_path=relative_path,
            )
        return planned

    def synchronize(
        self,
        remote_items: Iterable[RemoteItem],
        downloader: DownloadCallback,
        *,
        dry_run: bool = False,
    ) -> SyncReport:
        remote_by_id = {item.media_id: item for item in remote_items if item.media_id}
        planned = self.plan_items(remote_by_id.values())
        previous = self.load_manifest()
        report = SyncReport()

        if not dry_run:
            self.media_dir.mkdir(parents=True, exist_ok=True)

        stale_paths = self._remove_stale(previous, planned, report, dry_run=dry_run)

        completed: dict[str, ManifestItem] = {}
        for media_id, manifest_item in planned.items():
            target = self.media_dir / manifest_item.relative_path
            previous_item = previous.get(media_id)
            if self._adopt_existing(previous_item, manifest_item, target, dry_run=dry_run):
                report.skipped.append(self._display_name(manifest_item))
                completed[media_id] = manifest_item
                continue

            if dry_run:
                report.added.append(self._display_name(manifest_item))
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            if downloader(remote_by_id[media_id], target):
                if target.is_file():
                    report.added.append(self._display_name(manifest_item))
                    completed[media_id] = manifest_item
                else:
                    report.failed.append(
                        f"{self._display_name(manifest_item)}（下载器未生成目标文件）"
                    )
            else:
                report.failed.append(self._display_name(manifest_item))

        self._remove_untracked(
            planned,
            previous,
            report,
            ignored_paths=stale_paths,
            dry_run=dry_run,
        )
        if not dry_run:
            self.save_manifest(completed)
        return report

    def _adopt_existing(
        self,
        previous: ManifestItem | None,
        current: ManifestItem,
        target: Path,
        *,
        dry_run: bool,
    ) -> bool:
        if target.is_file():
            return True

        candidates: list[Path] = []
        if previous:
            candidates.append(self.media_dir / previous.relative_path)

        playlist_dir = target.parent
        if playlist_dir.is_dir():
            for candidate in playlist_dir.glob(f"*.{self.profile.extension}"):
                if sanitize_component(candidate.stem) == current.clean_title:
                    candidates.append(candidate)

        for candidate in candidates:
            if not candidate.is_file() or candidate == target:
                continue
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                candidate.replace(target)
            return True
        return False

    def _remove_stale(
        self,
        previous: dict[str, ManifestItem],
        planned: dict[str, ManifestItem],
        report: SyncReport,
        *,
        dry_run: bool,
    ) -> set[str]:
        stale_paths: set[str] = set()
        for media_id in sorted(previous.keys() - planned.keys()):
            item = previous[media_id]
            path = self.media_dir / item.relative_path
            if path.is_file():
                stale_paths.add(Path(item.relative_path).as_posix().casefold())
                if not dry_run:
                    path.unlink()
                    self._remove_empty_parents(path.parent)
                report.deleted.append(self._display_name(item))
        return stale_paths

    def _remove_untracked(
        self,
        planned: dict[str, ManifestItem],
        previous: dict[str, ManifestItem],
        report: SyncReport,
        *,
        ignored_paths: set[str],
        dry_run: bool,
    ) -> None:
        desired_paths = {
            Path(item.relative_path).as_posix().casefold() for item in planned.values()
        }
        protected_paths = self._other_manifest_paths()
        playlist_dirs = {
            self.media_dir / item.clean_playlist
            for item in (*planned.values(), *previous.values())
        }
        for playlist_dir in sorted(playlist_dirs):
            if not playlist_dir.is_dir():
                continue
            for path in sorted(playlist_dir.glob(f"*.{self.profile.extension}")):
                relative = path.relative_to(self.media_dir).as_posix()
                normalized = relative.casefold()
                if (
                    normalized in desired_paths
                    or normalized in protected_paths
                    or normalized in ignored_paths
                ):
                    continue
                if not dry_run:
                    path.unlink()
                    self._remove_empty_parents(path.parent)
                report.deleted.append(relative)

    def _other_manifest_paths(self) -> set[str]:
        protected: set[str] = set()
        if not self.metadata_dir.is_dir():
            return protected
        for manifest_path in self.metadata_dir.glob("*.json"):
            if manifest_path == self.manifest_path:
                continue
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
                for item in document.get("items", {}).values():
                    if relative_path := item.get("relative_path"):
                        protected.add(Path(relative_path).as_posix().casefold())
            except (OSError, ValueError, TypeError):
                continue
        return protected

    def _remove_empty_parents(self, directory: Path) -> None:
        while directory != self.media_dir and directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent

    @staticmethod
    def _display_name(item: ManifestItem) -> str:
        return f"{item.original_playlist} / {item.original_title}"


def discover_remote_items(
    yt_dlp,
    profile: SyncProfile,
    *,
    ffmpeg_path: str | None = None,
    node_path: str | None = None,
    debug: bool = False,
) -> list[RemoteItem]:
    """Expand a channel or playlist URL into a stable list of remote items."""
    logger = ConciseYtdlpLogger(debug=debug)
    options = {
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "logger": logger,
        "http_headers": profile.headers,
    }
    if ffmpeg_path:
        options["ffmpeg_location"] = ffmpeg_path
    if node_path:
        options.update(
            {
                "no_plugins": True,
                "remote_components": {"ejs:github"},
                "js_runtimes": {"node": {"path": node_path}},
            }
        )
    with yt_dlp.YoutubeDL(options) as ydl:
        root = ydl.extract_info(profile.source_url, download=False)
        return _expand_remote_info(ydl, root, profile.source_url)


def _expand_remote_info(ydl, root: dict | None, fallback_url: str) -> list[RemoteItem]:
    if not root:
        return []
    discovered: dict[str, RemoteItem] = {}

    def visit(info: dict, playlist_title: str | None = None) -> None:
        entries = [entry for entry in info.get("entries") or [] if entry]
        current_playlist = info.get("title") or playlist_title or "Default_Collection"
        if entries:
            for entry in entries:
                entry_key = str(entry.get("ie_key") or entry.get("extractor_key") or "")
                is_nested = entry.get("_type") == "playlist" or any(
                    marker in entry_key.lower() for marker in ("playlist", "tab", "space")
                )
                if is_nested and (entry.get("url") or entry.get("webpage_url")):
                    nested = ydl.extract_info(
                        entry.get("url") or entry.get("webpage_url"),
                        download=False,
                    )
                    if nested:
                        visit(nested, entry.get("title") or current_playlist)
                    continue
                add_entry(entry, current_playlist)
            return
        add_entry(info, playlist_title or current_playlist)

    def add_entry(info: dict, playlist_title: str) -> None:
        media_id = str(info.get("id") or "").strip()
        if not media_id:
            return
        extractor = str(
            info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or "media"
        )
        unique_id = f"{extractor.casefold()}:{media_id}"
        webpage_url = str(info.get("webpage_url") or info.get("url") or fallback_url)
        discovered[unique_id] = RemoteItem(
            media_id=unique_id,
            extractor=extractor,
            original_title=str(info.get("title") or media_id),
            original_playlist=str(playlist_title or "Default_Collection"),
            webpage_url=webpage_url,
        )

    visit(root)
    return list(discovered.values())


def build_yt_dlp_downloader(
    yt_dlp,
    profile: SyncProfile,
    *,
    ffmpeg_path: str,
    node_path: str | None = None,
    debug: bool = False,
) -> DownloadCallback:
    """Build a one-item downloader that writes directly to a cleaned target."""
    logger = ConciseYtdlpLogger(debug=debug)
    progress = DownloadProgress()

    def download(item: RemoteItem, target: Path) -> bool:
        print(f"[下载] {item.original_playlist} / {item.original_title}")
        options = {
            "format": profile.format_selector,
            "ffmpeg_location": ffmpeg_path,
            "outtmpl": str(target.with_suffix(".%(ext)s")),
            "noplaylist": True,
            "ignoreerrors": False,
            "quiet": True,
            "no_warnings": True,
            "logger": logger,
            "progress_hooks": [progress],
            "http_headers": profile.headers,
            "retries": profile.retries,
            "fragment_retries": profile.retries,
        }
        if profile.media_kind == "audio":
            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": profile.extension,
                    "preferredquality": "0",
                }
            ]
        else:
            options["merge_output_format"] = profile.extension
            options["postprocessors"] = [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": profile.extension,
                }
            ]
        if node_path:
            options.update(
                {
                    "no_plugins": True,
                    "remote_components": {"ejs:github"},
                    "js_runtimes": {"node": {"path": node_path}},
                }
            )
        with yt_dlp.YoutubeDL(options) as ydl:
            return (ydl.download([item.webpage_url]) or 0) == 0

    return download
