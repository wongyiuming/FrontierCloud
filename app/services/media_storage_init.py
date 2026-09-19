from __future__ import annotations

import os
from pathlib import Path


APP_UID = 10001
APP_GID = 10001
DATA_ROOT = Path("/app/data")
MEDIA_DIRECTORIES = ("media", "media/music", "media/vido", "media/lyrics")


def _set_owner(path: Path, uid: int, gid: int) -> None:
    if path.is_symlink():
        return
    info = path.stat(follow_symlinks=False)
    if info.st_uid != uid or info.st_gid != gid:
        os.chown(path, uid, gid, follow_symlinks=False)


def initialize_media_storage(
    data_root: Path = DATA_ROOT,
    uid: int = APP_UID,
    gid: int = APP_GID,
) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    for relative_path in MEDIA_DIRECTORIES:
        (data_root / relative_path).mkdir(parents=True, exist_ok=True)

    for current, directory_names, file_names in os.walk(data_root, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names[:] = [
            name for name in directory_names
            if not (current_path / name).is_symlink()
        ]
        _set_owner(current_path, uid, gid)
        for file_name in file_names:
            _set_owner(current_path / file_name, uid, gid)


def main() -> None:
    initialize_media_storage()
    print("media storage initialized for uid=10001 gid=10001")


if __name__ == "__main__":
    main()
