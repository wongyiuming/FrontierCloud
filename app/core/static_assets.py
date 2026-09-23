import hashlib
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
STATIC_ROOT = (BASE_DIR / "static").resolve()


@lru_cache(maxsize=32)
def static_asset_url(relative_path: str) -> str:
    path = (STATIC_ROOT / relative_path).resolve()
    if not path.is_relative_to(STATIC_ROOT) or not path.is_file():
        raise RuntimeError(f"Static asset is missing: {relative_path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return f"/static/{relative_path}?v={digest}"
