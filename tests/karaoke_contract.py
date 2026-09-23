from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Query

from app.api.v1.karaoke_contract import KaraokeContext


CONTRACT = ROOT / "contracts" / "karaoke-openapi.json"


def rendered() -> str:
    application = FastAPI(title="FrontierCloud Karaoke Contract", version="1")

    @application.get("/api/v1/karaoke/context", response_model=KaraokeContext)
    async def context(media: str = Query(..., min_length=80, max_length=512, pattern=r"^[A-Za-z0-9_-]+={0,2}$")):
        raise NotImplementedError

    return json.dumps(application.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    expected = rendered()
    if args.write:
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(expected, encoding="utf-8")
        return 0
    if not CONTRACT.exists() or CONTRACT.read_text(encoding="utf-8") != expected:
        print("Karaoke OpenAPI snapshot is stale; run python -m tests.karaoke_contract --write")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
