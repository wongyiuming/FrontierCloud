"""Persistent brand logo storage with versioned built-in fallbacks."""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BRAND_DIR = ROOT / "static" / "brand"
CUSTOM_BRAND_DIR = ROOT / "data" / "brand"
MAX_LOGO_BYTES = 8 * 1024 * 1024

BRANDS = {
    "entertainment": {
        "label": "前沿娱乐",
        "default": "frontier-entertainment.webp",
        "download": "frontier-entertainment-logo",
    },
    "media": {
        "label": "前沿媒体",
        "default": "frontier-media.webp",
        "download": "frontier-media-logo",
    },
    "music": {
        "label": "前沿音乐",
        "default": "frontier-music.webp",
        "download": "frontier-music-logo",
    },
}

_IMAGE_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class BrandLogo:
    kind: str
    label: str
    path: Path
    media_type: str
    suffix: str
    source: str

    @property
    def custom(self) -> bool:
        return self.source == "custom"


def _brand(kind: str) -> dict[str, str]:
    try:
        return BRANDS[kind]
    except KeyError as exc:
        raise ValueError("未知 Logo 类型") from exc


def _custom_candidates(kind: str) -> list[Path]:
    _brand(kind)
    return [CUSTOM_BRAND_DIR / f"{kind}{suffix}" for suffix in _IMAGE_TYPES]


def _valid_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def effective_logo(kind: str) -> BrandLogo:
    meta = _brand(kind)
    for path in _custom_candidates(kind):
        if _valid_file(path):
            return BrandLogo(
                kind=kind,
                label=meta["label"],
                path=path,
                media_type=_IMAGE_TYPES[path.suffix],
                suffix=path.suffix,
                source="custom",
            )
    default = DEFAULT_BRAND_DIR / meta["default"]
    if not _valid_file(default):
        raise FileNotFoundError(f"内置 Logo 缺失: {meta['default']}")
    return BrandLogo(
        kind=kind,
        label=meta["label"],
        path=default,
        media_type="image/webp",
        suffix=".webp",
        source="default",
    )


@lru_cache(maxsize=64)
def _version_for_file(
    path_value: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
) -> str:
    del size, mtime_ns, ctime_ns
    digest = hashlib.sha256()
    with Path(path_value).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def logo_version(logo: BrandLogo) -> str:
    stat = logo.path.stat()
    return _version_for_file(
        str(logo.path),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def public_logo_url(kind: str) -> str:
    logo = effective_logo(kind)
    return f"/api/v1/media/brand/logo/{kind}?v={logo_version(logo)}"


def inspect_upload(data: bytes) -> tuple[str, str]:
    if not data:
        raise ValueError("Logo 文件为空")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("Logo 文件不能超过 8 MiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ValueError("仅支持真实 PNG 或 WebP 图片")


def store_custom(kind: str, data: bytes) -> BrandLogo:
    _brand(kind)
    suffix, _media_type = inspect_upload(data)
    CUSTOM_BRAND_DIR.mkdir(parents=True, exist_ok=True)
    destination = CUSTOM_BRAND_DIR / f"{kind}{suffix}"
    temporary = CUSTOM_BRAND_DIR / (
        f".{kind}-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    )
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        for candidate in _custom_candidates(kind):
            if candidate != destination:
                candidate.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    _version_for_file.cache_clear()
    return effective_logo(kind)


def delete_custom(kind: str) -> bool:
    changed = False
    for path in _custom_candidates(kind):
        if path.exists():
            if path.is_symlink():
                raise ValueError("拒绝删除符号链接 Logo")
            path.unlink()
            changed = True
    if changed:
        _version_for_file.cache_clear()
    return changed


def describe(kind: str) -> dict:
    logo = effective_logo(kind)
    version = logo_version(logo)
    return {
        "kind": kind,
        "label": logo.label,
        "source": logo.source,
        "custom": logo.custom,
        "format": logo.suffix.lstrip("."),
        "size_bytes": logo.path.stat().st_size,
        "version": version,
        "url": f"/api/v1/media/brand/logo/{kind}?v={version}",
    }


def download_filename(kind: str, suffix: str) -> str:
    meta = _brand(kind)
    return f"{meta['download']}{suffix}"
