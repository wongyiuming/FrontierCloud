"""Move legacy mixed media into the type-owned bounded directory layout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv"}
TYPE_DIRECTORIES = {"audio": "music", "video": "vido"}


def media_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in AUDIO_EXTS:
        return "music"
    if suffix in VIDEO_EXTS:
        return "vido"
    return None


def plan_moves(media_root: Path) -> list[tuple[Path, Path]]:
    """Build a collision-free migration plan before changing any files."""
    media_root = media_root.resolve()
    moves: list[tuple[Path, Path]] = []
    layouts: dict[tuple[str, str], set[str]] = {}
    errors: list[str] = []

    for category in media_root.iterdir():
        if (
            category.is_symlink()
            or not category.is_dir()
            or category.name in {"music", "vido", ".sync"}
        ):
            continue
        candidates: list[Path] = []
        for child in category.iterdir():
            if child.is_symlink():
                continue
            if child.is_file():
                candidates.append(child)
                continue
            if child.is_dir():
                for nested in child.iterdir():
                    if nested.is_symlink():
                        continue
                    if nested.is_dir():
                        errors.append(
                            f"unsupported directory depth: {nested.relative_to(media_root).as_posix()}"
                        )
                    elif nested.is_file():
                        candidates.append(nested)

        for source in candidates:
            relative = source.relative_to(media_root)
            target_type = media_type(source)
            if not target_type:
                continue
            layout = "direct" if len(relative.parts) == 2 else "nested"
            layouts.setdefault((target_type, relative.parts[0]), set()).add(layout)
            target = media_root / target_type / relative
            if target.exists():
                errors.append(f"target exists: {target.relative_to(media_root).as_posix()}")
            moves.append((source, target))

    for (target_type, category), shapes in layouts.items():
        if len(shapes) > 1:
            errors.append(f"mixed direct/nested layout: {target_type}/{category}")

    manifest_root = media_root / ".sync"
    if manifest_root.is_dir():
        for source in manifest_root.glob("*.json"):
            try:
                kind = str(json.loads(source.read_text(encoding="utf-8")).get("media_kind", ""))
            except (OSError, ValueError, TypeError):
                errors.append(f"invalid manifest: {source.relative_to(media_root).as_posix()}")
                continue
            target_type = TYPE_DIRECTORIES.get(kind)
            if not target_type:
                errors.append(f"unknown manifest media kind: {source.relative_to(media_root).as_posix()}")
                continue
            target = media_root / target_type / ".sync" / source.name
            if target.exists():
                errors.append(f"target exists: {target.relative_to(media_root).as_posix()}")
            moves.append((source, target))

    if errors:
        raise RuntimeError("migration refused:\n" + "\n".join(f"- {item}" for item in errors))
    return moves


def remove_empty_legacy_directories(media_root: Path) -> None:
    protected = {
        media_root.resolve(),
        (media_root / "music").resolve(),
        (media_root / "vido").resolve(),
    }
    directories: list[Path] = []
    for category in media_root.iterdir():
        if category.is_dir() and not category.is_symlink():
            directories.extend(
                child for child in category.iterdir() if child.is_dir() and not child.is_symlink()
            )
            directories.append(category)
    directories.sort(key=lambda path: len(path.parts), reverse=True)
    for directory in directories:
        if directory.resolve() in protected:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def migrate(media_root: Path, *, apply: bool) -> int:
    media_root = media_root.expanduser().resolve()
    moves = plan_moves(media_root)
    action = "MOVE" if apply else "PLAN"
    for source, target in moves:
        print(
            f"[{action}] {source.relative_to(media_root).as_posix()}"
            f" -> {target.relative_to(media_root).as_posix()}"
        )
    if not apply:
        print(f"Planned {len(moves)} moves; rerun with --apply to execute.")
        return 0

    for source, target in moves:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    remove_empty_legacy_directories(media_root)
    print(f"Migrated {len(moves)} files.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--media-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data" / "media",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    return migrate(args.media_root, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
