"""Import a read-only media staging directory through normal Admin transactions."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path

from dev_control import Client, ROOT


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    if os.geteuid() != 0:
        raise RuntimeError("root is required")
    source = ROOT / "import"
    manifest_path = ROOT / "import-manifest.jsonl"
    done = {}
    if manifest_path.exists():
        done = {row["source"]: row for row in map(json.loads, manifest_path.read_text().splitlines()) if row.get("result") == "uploaded"}
    master = Client("master")
    files = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in {".mp3", ".mp4", ".lrc"})
    # Prefer the canonical all/ lyric collection, retaining only one file per name.
    lyrics = {}
    for path in sorted((p for p in files if p.suffix.lower() == ".lrc"), key=lambda p: ("all" not in p.parts, str(p))):
        lyrics.setdefault(path.name, path)
    files = list(lyrics.values()) + [p for p in files if p.suffix.lower() != ".lrc"]
    with manifest_path.open("a", encoding="utf-8", buffering=1) as manifest:
        for index, path in enumerate(files):
            relative = path.relative_to(source).as_posix()
            if relative in done:
                continue
            row = {"source": relative, "bytes": path.stat().st_size, "sha256": digest(path)}
            try:
                if path.suffix.lower() == ".lrc":
                    with path.open("rb") as stream:
                        response = master.client.post(master.endpoint + "/api/v1/media/admin/upload/lyric",
                            headers={"X-CSRF-Token": master.csrf}, files={"file": (path.name, stream, "text/plain")})
                    response.raise_for_status()
                    row.update(path="lyrics/" + path.name, response=response.json())
                else:
                    category = "music" if path.suffix.lower() == ".mp3" else "vido"
                    parent = path.relative_to(source).parts[1:-1]
                    # Normalize all imports to category/artist/album/file depth.
                    artist = parent[0] if parent else "Imported"
                    album = parent[1] if len(parent) > 1 else "Singles"
                    target_dir = f"{category}/{artist}/{album}"
                    site_type = ("primary", "direct", "relay")[index % 3]
                    reservation = master.api("/api/v1/media/admin/upload/session", {
                        "site_type": site_type, "target_dir": target_dir,
                        "filename": path.name, "size_bytes": path.stat().st_size,
                    })
                    headers = {"Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                               "Content-Length": str(path.stat().st_size)}
                    direct = reservation["transport"] == "Direct"
                    headers.update({"Origin": master.endpoint} if direct else {"X-CSRF-Token": master.csrf})
                    with path.open("rb") as stream:
                        response = master.client.put(reservation["upload_url"] if direct else master.endpoint + reservation["upload_url"],
                                                     headers=headers, content=stream, timeout=600)
                    response.raise_for_status()
                    if direct:
                        master.api(f"/api/v1/media/admin/upload/session/{reservation['upload_id']}/finalize", {})
                    row.update(path=target_dir + "/" + path.name, media_id=reservation["media_id"], site_type=site_type)
                row["result"] = "uploaded"
            except Exception as exc:
                row.update(result="failed", error=str(exc)[:1000])
            manifest.write(json.dumps(row, ensure_ascii=False) + "\n")
            if row["result"] == "failed":
                print(json.dumps({"index": index, "result": "failed", "error": row["error"]}), flush=True)
            elif index % 25 == 0:
                print(json.dumps({"index": index, "total": len(files), "result": "uploaded"}), flush=True)


if __name__ == "__main__":
    main()
