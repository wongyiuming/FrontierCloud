"""Experimentally infer and download reusable LRC files from local media names."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://lrclib.net/api/search"
DEFAULT_MUSIC_ROOT = Path("/data/media/music")
DEFAULT_USER_AGENT = (
    "FrontierCloud-LRCLIB-Experimental/1.0 "
    "(https://github.com/wongyiuming/FrontierCloud)"
)
AUDIO_SUFFIXES = {".flac", ".m4a", ".mp3", ".wav"}
TIMESTAMP_RE = re.compile(r"(?m)^\[\d{1,3}:\d{2}(?:\.\d{1,3})?]")
NUMBERED_TRACK_RE = re.compile(
    r"(?:^|_)(?:第)?\d{1,2}(?:首)?_(.+?)(?="
    r"(?:_(?:第)?\d{1,2}(?:首)?_)|_(?:附歌词|歌词|作词|作曲|演唱|live|mv)|$)",
    re.IGNORECASE,
)
COMPOUND_MARKER_RE = re.compile(r"(?:medley|串烧|组曲|联唱|连唱)", re.IGNORECASE)
NOISE_RE = re.compile(
    r"^(?:official|官方|完整版?|歌词(?:版)?|附歌词|lyrics?|lyric|mv|musicvideo|"
    r"video|audio|hq|hd|uhd|无动画|伴奏|纯音乐|作词|填词|作曲|演唱|主唱|"
    r"现场|live|version|版|中字|字幕|karaoke|ktv|remaster(?:ed)?|"
    r"\d{3,4}p(?:\d+)?|[248]k|\d{2,3}kbps|(?:19|20)\d{2})$",
    re.IGNORECASE,
)
GENERIC_DIRECTORY_KEYS = {
    "audio",
    "download",
    "downloads",
    "favorite",
    "favorites",
    "media",
    "music",
    "音乐",
    "歌曲",
    "收藏",
    "合集",
    "默认分类",
}
GENERIC_TITLE_FRAGMENTS = {
    "演唱会",
    "音乐会",
    "精选",
    "专场",
    "合集",
}
_converter = None


@dataclass(frozen=True)
class SongCandidate:
    media_path: str
    title: str
    artist: str | None
    compound: bool
    evidence: str


@dataclass(frozen=True)
class LyricsRecord:
    record_id: int
    track_name: str
    artist_name: str
    album_name: str
    duration: float | None
    synced_lyrics: str


@dataclass(frozen=True)
class ScoredRecord:
    record: LyricsRecord
    score: float
    title_score: float
    artist_score: float
    duration_delta: float | None


@dataclass(frozen=True)
class PlanEntry:
    media_path: str
    inferred_title: str
    inferred_artist: str
    lrclib_id: int | None
    lrclib_track: str | None
    lrclib_artist: str | None
    lrclib_album: str | None
    output_path: str | None
    status: str
    reason: str
    score: float | None


class RequestBudgetExceeded(RuntimeError):
    """Stop the experiment before it can create an unbounded API workload."""


def simplify(value: str) -> str:
    """Convert text to Simplified Chinese with the tools-only OpenCC dependency."""
    global _converter
    if _converter is None:
        try:
            from opencc import OpenCC
        except ImportError as exc:
            raise RuntimeError(
                'OpenCC is required; install it with '
                '`python -m pip install -e ".[tools]"`.'
            ) from exc
        _converter = OpenCC("t2s")
    return _converter.convert(unicodedata.normalize("NFKC", str(value or "")))


def semantic_key(value: str) -> str:
    """Return a punctuation-insensitive matching key in Simplified Chinese."""
    return "".join(character for character in simplify(value).casefold() if character.isalnum())


def similarity(left: str, right: str) -> float:
    left_key = semantic_key(left)
    right_key = semantic_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    ratio = SequenceMatcher(None, left_key, right_key).ratio()
    if left_key in right_key or right_key in left_key:
        ratio = max(ratio, min(len(left_key), len(right_key)) / max(len(left_key), len(right_key)))
    return ratio


def clean_field(value: str) -> str:
    """Create one compact portable filename field without losing Unicode letters."""
    normalized = simplify(value).strip()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in normalized
        if character != "\0"
    )
    return re.sub(r"_+", "_", normalized).strip("_. ")


def canonical_lrc(value: str) -> str:
    """Normalize LRCLIB line endings while preserving timestamp and text content."""
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"


def lyric_digest(value: str) -> str:
    return hashlib.sha256(canonical_lrc(value).encode("utf-8")).hexdigest()


def valid_synced_lyrics(value: str) -> bool:
    normalized = canonical_lrc(value)
    return bool(normalized.strip() and TIMESTAMP_RE.search(normalized))


def _is_generic_directory(value: str) -> bool:
    key = semantic_key(value)
    return (
        not key
        or key in GENERIC_DIRECTORY_KEYS
        or bool(re.fullmatch(r"(?:cd|disc|disk|album)\d*", key, re.IGNORECASE))
        or any(fragment in key for fragment in {"专辑", "演唱会", "音乐会"})
    )


def infer_artist(media_path: Path, music_root: Path) -> str | None:
    """Prefer the stable artist directory rather than guessing from arbitrary tokens."""
    relative = media_path.relative_to(music_root)
    for component in relative.parts[:-1]:
        candidate = simplify(component).strip(" _-.")
        if not _is_generic_directory(candidate) and len(semantic_key(candidate)) >= 2:
            return candidate
    return None


def _meaningful_tokens(value: str, artist: str | None) -> list[str]:
    artist_key = semantic_key(artist or "")
    tokens: list[str] = []
    for raw_token in re.split(r"[_\s]+", simplify(value)):
        token = raw_token.strip(" .-()[]{}【】（）")
        key = semantic_key(token)
        if not key or key.isdigit() or NOISE_RE.fullmatch(token):
            continue
        if any(fragment in key for fragment in GENERIC_TITLE_FRAGMENTS):
            continue
        if artist_key and (key == artist_key or (len(key) >= 3 and key in artist_key)):
            continue
        if len(key) < 2:
            continue
        tokens.append(token)
    return tokens


def _deduplicate_titles(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        candidate = value.strip(" _-.")
        key = semantic_key(candidate)
        if len(key) < 2 or key in seen or NOISE_RE.fullmatch(candidate):
            continue
        seen.add(key)
        result.append(candidate)
    return result


def infer_song_candidates(media_path: Path, music_root: Path) -> list[SongCandidate]:
    """Produce conservative song candidates and explicitly recognize numbered medleys."""
    stem = simplify(media_path.stem)
    artist = infer_artist(media_path, music_root)
    numbered = [match.group(1) for match in NUMBERED_TRACK_RE.finditer(stem)]
    numbered_titles = _deduplicate_titles(
        [" ".join(_meaningful_tokens(value, artist)) for value in numbered]
    )
    if len(numbered_titles) >= 2:
        return [
            SongCandidate(
                media_path=media_path.relative_to(music_root).as_posix(),
                title=title,
                artist=artist,
                compound=True,
                evidence="numbered-track-list",
            )
            for title in numbered_titles
        ]

    explicit_parts = re.split(r"\s*(?:\+|、|／|;|；)\s*", stem)
    if COMPOUND_MARKER_RE.search(stem) and len(explicit_parts) > 1:
        compound_titles = _deduplicate_titles(
            [" ".join(_meaningful_tokens(value, artist)) for value in explicit_parts]
        )
        if len(compound_titles) >= 2:
            return [
                SongCandidate(
                    media_path=media_path.relative_to(music_root).as_posix(),
                    title=title,
                    artist=artist,
                    compound=True,
                    evidence="medley-delimiter",
                )
                for title in compound_titles
            ]

    tokens = _meaningful_tokens(stem, artist)
    titles: list[str] = []
    if tokens:
        titles.append(" ".join(tokens))
        titles.extend(tokens)
        titles.extend(" ".join(tokens[index:index + 2]) for index in range(len(tokens) - 1))
    return [
        SongCandidate(
            media_path=media_path.relative_to(music_root).as_posix(),
            title=title,
            artist=artist,
            compound=False,
            evidence="filename-and-directory" if artist else "filename-only",
        )
        for title in _deduplicate_titles(titles)[:8]
    ]


def probe_duration(media_path: Path, ffprobe: str | None) -> float | None:
    """Read duration without decoding media; absence or malformed media stays non-fatal."""
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        duration = float(completed.stdout.strip())
        return duration if duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class LrclibClient:
    """Small sequential LRCLIB client with caching, retries, and a request budget."""

    def __init__(
        self,
        *,
        timeout: float,
        request_delay: float,
        max_requests: int,
        user_agent: str,
    ):
        self.timeout = timeout
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.user_agent = user_agent
        self.request_count = 0
        self._cache: dict[tuple[str, str | None], list[LyricsRecord]] = {}
        self._last_request_at = 0.0

    def search(self, title: str, artist: str | None) -> list[LyricsRecord]:
        key = (semantic_key(title), semantic_key(artist or "") or None)
        if key in self._cache:
            return self._cache[key]
        if self.request_count >= self.max_requests:
            raise RequestBudgetExceeded(
                f"LRCLIB request budget exhausted ({self.max_requests})"
            )
        parameters = {"track_name": title}
        if artist:
            parameters["artist_name"] = artist
        records = self._request(parameters)
        self._cache[key] = records
        return records

    def broad_search(self, query: str) -> list[LyricsRecord]:
        key = (f"q:{semantic_key(query)}", None)
        if key in self._cache:
            return self._cache[key]
        if self.request_count >= self.max_requests:
            raise RequestBudgetExceeded(
                f"LRCLIB request budget exhausted ({self.max_requests})"
            )
        records = self._request({"q": query})
        self._cache[key] = records
        return records

    def _request(self, parameters: dict[str, str]) -> list[LyricsRecord]:
        remaining_delay = self.request_delay - (time.monotonic() - self._last_request_at)
        if remaining_delay > 0:
            time.sleep(remaining_delay)
        request = Request(
            f"{API_URL}?{urlencode(parameters)}",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        last_error: Exception | None = None
        for attempt in range(3):
            if self.request_count >= self.max_requests:
                raise RequestBudgetExceeded(
                    f"LRCLIB request budget exhausted ({self.max_requests})"
                )
            self.request_count += 1
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read(8 * 1024 * 1024 + 1)
                self._last_request_at = time.monotonic()
                if len(payload) > 8 * 1024 * 1024:
                    raise RuntimeError("LRCLIB response exceeded 8 MiB")
                document = json.loads(payload.decode("utf-8"))
                if not isinstance(document, list):
                    raise RuntimeError("LRCLIB search returned a non-list response")
                return [record for item in document if (record := parse_record(item))]
            except HTTPError as exc:
                last_error = exc
                if exc.code != 429 and exc.code < 500:
                    break
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = min(10.0, float(retry_after)) if retry_after else 1.5 * (attempt + 1)
                except ValueError:
                    delay = 1.5 * (attempt + 1)
                time.sleep(delay)
            except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LRCLIB request failed: {last_error}")


def parse_record(item: object) -> LyricsRecord | None:
    if not isinstance(item, dict) or item.get("instrumental"):
        return None
    synced = item.get("syncedLyrics")
    if not isinstance(synced, str) or not valid_synced_lyrics(synced):
        return None
    try:
        record_id = int(item["id"])
    except (KeyError, TypeError, ValueError):
        return None
    duration_value = item.get("duration")
    try:
        duration = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration = None
    return LyricsRecord(
        record_id=record_id,
        track_name=str(item.get("trackName") or item.get("name") or "").strip(),
        artist_name=str(item.get("artistName") or "").strip(),
        album_name=str(item.get("albumName") or "").strip(),
        duration=duration,
        synced_lyrics=canonical_lrc(synced),
    )


def score_record(
    candidate: SongCandidate,
    record: LyricsRecord,
    media_duration: float | None,
) -> ScoredRecord | None:
    title_score = similarity(candidate.title, record.track_name)
    if title_score < 0.84:
        return None

    source_key = semantic_key(Path(candidate.media_path).stem)
    if candidate.artist:
        artist_score = similarity(candidate.artist, record.artist_name)
        if artist_score < 0.55:
            return None
    else:
        artist_score = 1.0 if semantic_key(record.artist_name) in source_key else 0.0
        if not artist_score or semantic_key(record.track_name) not in source_key:
            return None

    duration_delta: float | None = None
    if not candidate.compound and media_duration and record.duration:
        duration_delta = abs(media_duration - record.duration)
        if duration_delta > 15:
            return None
        duration_score = max(0.0, 1.0 - duration_delta / 15)
        score = 0.55 * title_score + 0.30 * artist_score + 0.15 * duration_score
    else:
        score = 0.65 * title_score + 0.35 * artist_score
    return ScoredRecord(record, score, title_score, artist_score, duration_delta)


def select_unambiguous(scored: list[ScoredRecord]) -> tuple[ScoredRecord | None, str]:
    """Accept one strong result only when a different lyric is not nearly tied."""
    by_record: dict[int, ScoredRecord] = {}
    for item in scored:
        previous = by_record.get(item.record.record_id)
        if previous is None or item.score > previous.score:
            by_record[item.record.record_id] = item
    ranked = sorted(by_record.values(), key=lambda item: item.score, reverse=True)
    if not ranked or ranked[0].score < 0.82:
        return None, "no-confident-synced-match"
    best = ranked[0]
    best_digest = lyric_digest(best.record.synced_lyrics)
    competing = next(
        (
            item
            for item in ranked[1:]
            if lyric_digest(item.record.synced_lyrics) != best_digest
        ),
        None,
    )
    if competing and best.score - competing.score < 0.06:
        return None, "ambiguous-versions"
    return best, "matched"


def output_name(record: LyricsRecord) -> str:
    fields = [clean_field(record.track_name)]
    album = clean_field(record.album_name)
    if album and album not in {"_", "-"}:
        fields.append(album)
    fields.append(clean_field(record.artist_name))
    fields = [field for field in fields if field]
    return "_".join(fields) + ".lrc"


def existing_lyrics(lyrics_dir: Path) -> tuple[dict[str, Path], set[str]]:
    hashes: dict[str, Path] = {}
    names: set[str] = set()
    if not lyrics_dir.is_dir():
        return hashes, names
    for path in sorted(lyrics_dir.rglob("*.lrc")):
        if not path.is_file() or path.is_symlink():
            continue
        names.add(path.name.casefold())
        try:
            hashes.setdefault(lyric_digest(path.read_text(encoding="utf-8")), path)
        except (OSError, UnicodeDecodeError):
            continue
    return hashes, names


def allocate_output_path(
    lyrics_dir: Path,
    record: LyricsRecord,
    occupied_names: set[str],
) -> Path:
    name = output_name(record)
    if name.casefold() in occupied_names:
        path = Path(name)
        name = f"{path.stem}_lrclib_{record.record_id}{path.suffix}"
    occupied_names.add(name.casefold())
    return lyrics_dir / name


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def media_files(
    music_root: Path,
    max_files: int,
    include_patterns: list[str],
) -> list[Path]:
    paths = [
        path
        for path in sorted(music_root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in AUDIO_SUFFIXES
        and (
            not include_patterns
            or any(
                fnmatch.fnmatch(path.relative_to(music_root).as_posix(), pattern)
                for pattern in include_patterns
            )
        )
    ]
    return paths[:max_files] if max_files else paths


def match_candidates(
    candidates: list[SongCandidate],
    client: LrclibClient,
    media_duration: float | None,
) -> list[tuple[SongCandidate, ScoredRecord | None, str]]:
    if not candidates:
        return []
    is_compound = any(candidate.compound for candidate in candidates)
    if candidates[0].artist is None:
        records = client.broad_search(Path(candidates[0].media_path).stem)
        scored_with_candidate = [
            (candidate, result)
            for candidate in candidates
            for record in records
            if (result := score_record(candidate, record, media_duration))
        ]
        best, reason = select_unambiguous([item[1] for item in scored_with_candidate])
        if best is None:
            return [(candidates[0], None, reason)]
        selected_candidate = next(
            candidate
            for candidate, result in scored_with_candidate
            if result.record.record_id == best.record.record_id and result.score == best.score
        )
        return [(selected_candidate, best, reason)]

    if is_compound:
        matched: list[tuple[SongCandidate, ScoredRecord | None, str]] = []
        for candidate in candidates:
            scored = [
                result
                for record in client.search(candidate.title, candidate.artist)
                if (result := score_record(candidate, record, None))
            ]
            best, reason = select_unambiguous(scored)
            matched.append((candidate, best, reason))
        return matched

    scored_with_candidate: list[tuple[SongCandidate, ScoredRecord]] = []
    for candidate in candidates:
        for record in client.search(candidate.title, candidate.artist):
            result = score_record(candidate, record, media_duration)
            if result:
                scored_with_candidate.append((candidate, result))
    best, reason = select_unambiguous([item[1] for item in scored_with_candidate])
    if best is None:
        return [(candidates[0], None, reason)]
    selected_candidate = next(
        candidate
        for candidate, result in scored_with_candidate
        if result.record.record_id == best.record.record_id and result.score == best.score
    )
    return [(selected_candidate, best, reason)]


def run(args: argparse.Namespace) -> tuple[list[PlanEntry], int]:
    music_root = args.music_root.expanduser().resolve()
    lyrics_dir = args.lyrics_dir.expanduser().resolve()
    if not music_root.is_dir():
        raise RuntimeError(f"Music root does not exist: {music_root}")
    if music_root == lyrics_dir or music_root in lyrics_dir.parents:
        raise RuntimeError("Lyrics output must not be inside the music tree")

    ffprobe = shutil.which(args.ffprobe) if args.ffprobe else None
    client = LrclibClient(
        timeout=args.timeout,
        request_delay=args.request_delay,
        max_requests=args.max_requests,
        user_agent=args.user_agent,
    )
    known_hashes, occupied_names = existing_lyrics(lyrics_dir)
    plan: list[PlanEntry] = []
    failures = 0

    for media_path in media_files(music_root, args.max_files, args.include):
        candidates = infer_song_candidates(media_path, music_root)
        relative = media_path.relative_to(music_root).as_posix()
        if not candidates:
            plan.append(PlanEntry(relative, "", "", None, None, None, None, None,
                                  "skipped", "insufficient-filename-evidence", None))
            continue
        duration = probe_duration(media_path, ffprobe)
        try:
            matches = match_candidates(candidates, client, duration)
        except RuntimeError as exc:
            failures += 1
            plan.append(PlanEntry(relative, candidates[0].title,
                                  candidates[0].artist or "", None, None, None, None,
                                  None, "error", str(exc), None))
            if isinstance(exc, RequestBudgetExceeded):
                break
            continue

        for candidate, match, reason in matches:
            if match is None:
                plan.append(PlanEntry(relative, candidate.title, candidate.artist or "",
                                      None, None, None, None, None, "skipped", reason, None))
                continue
            record = match.record
            digest = lyric_digest(record.synced_lyrics)
            existing = known_hashes.get(digest)
            if existing:
                output = existing
                status = "reused-existing" if output.is_file() else "reused-planned"
            else:
                output = allocate_output_path(lyrics_dir, record, occupied_names)
                status = "would-create"
                if args.write:
                    write_atomic(output, record.synced_lyrics)
                    status = "created"
                known_hashes[digest] = output
            plan.append(PlanEntry(
                relative,
                candidate.title,
                candidate.artist or "",
                record.record_id,
                record.track_name,
                record.artist_name,
                record.album_name,
                str(output),
                status,
                reason,
                round(match.score, 4),
            ))

    print(json.dumps([asdict(entry) for entry in plan], ensure_ascii=False, indent=2))
    created = sum(entry.status in {"created", "would-create"} for entry in plan)
    reused = sum(entry.status in {"reused-existing", "reused-planned"} for entry in plan)
    skipped = sum(entry.status == "skipped" for entry in plan)
    print(
        f"[summary] media={len({entry.media_path for entry in plan})} "
        f"new={created} reused={reused} skipped={skipped} "
        f"errors={failures} requests={client.request_count} mode="
        f"{'write' if args.write else 'preview'}",
        file=sys.stderr,
    )
    return plan, 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "实验性地从存量音乐文件名推断曲目，并从 LRCLIB 规划或下载去重的同步歌词。"
        )
    )
    parser.add_argument("--music-root", type=Path, default=DEFAULT_MUSIC_ROOT)
    parser.add_argument("--lyrics-dir", type=Path)
    parser.add_argument("--write", action="store_true", help="实际写入；省略时只输出计划")
    parser.add_argument("--max-files", type=int, default=0, help="最多扫描文件数；0 表示全部")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="只处理匹配相对 POSIX 路径 glob 的文件；可重复指定",
    )
    parser.add_argument("--max-requests", type=int, default=200, help="LRCLIB 请求硬上限")
    parser.add_argument("--request-delay", type=float, default=0.35, help="请求之间的最短秒数")
    parser.add_argument("--timeout", type=float, default=12.0, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe 命令；留空则不读取时长")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--report", type=Path, help="可选：把同一 JSON 计划写入指定文件")
    return parser


def configure_console_streams() -> None:
    """Keep Chinese plans readable in Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    configure_console_streams()
    args = build_parser().parse_args()
    if args.lyrics_dir is None:
        args.lyrics_dir = args.music_root.parent / "lyrics"
    if args.max_files < 0 or args.max_requests < 1:
        print("[error] Limits must be non-negative and max-requests must be positive.", file=sys.stderr)
        return 2
    try:
        plan, exit_code = run(args)
        if args.report:
            write_atomic(
                args.report.expanduser().resolve(),
                json.dumps([asdict(entry) for entry in plan], ensure_ascii=False, indent=2) + "\n",
            )
        return exit_code
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
