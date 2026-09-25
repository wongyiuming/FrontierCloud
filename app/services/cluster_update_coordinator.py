"""Master-side release fan-out executed inside the newly healthy Web container."""
from __future__ import annotations

import asyncio
import re
import sys
import time

from app.core.db import close_db, init_db
from app.services.federation.runtime import runtime
from app.services.federation.state import state
from app.services.federation.transport import transport

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
POLL_SECONDS = 4
TIMEOUT_SECONDS = 900


async def run(target: str, mode: str) -> None:
    if not SHA_RE.fullmatch(target) or mode not in {"upgrade", "rollback"}:
        raise RuntimeError("invalid cluster release target")
    await init_db()
    try:
        await state.initialize()
        if state.node.get("role") != "Master":
            raise RuntimeError("cluster release coordinator must run on Master")
        relations = [row for row in await state.list_relationships()
                     if row["direction"] == "downstream" and row["state"] == "active"]
        if not relations:
            return
        transport.open()

        async def begin(relation: dict) -> None:
            await runtime.call(relation, "/internal/v1/cluster-update/start", {
                "target_sha": target, "mode": mode,
            })

        results = await asyncio.gather(*(begin(row) for row in relations), return_exceptions=True)
        failed = [relations[index]["peer_id"] for index, value in enumerate(results) if isinstance(value, BaseException)]
        if failed:
            raise RuntimeError("release command failed for: " + ",".join(failed))

        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            async def probe(relation: dict):
                try:
                    value = await runtime.call(relation, "/internal/v1/cluster-update/status", {})
                    return relation, value.get("status") if isinstance(value, dict) else {}
                except Exception as exc:
                    return relation, {"state": "unreachable", "detail": type(exc).__name__}

            statuses = await asyncio.gather(*(probe(row) for row in relations))
            failures = []
            complete = True
            for relation, status in statuses:
                if status.get("target_sha") == target and status.get("state") == "failed":
                    failures.append(f"{relation['peer_id']}:{status.get('detail') or 'failed'}")
                if status.get("current_sha") != target or status.get("state") != "success":
                    complete = False
            if failures:
                raise RuntimeError("Follower release failed: " + " | ".join(failures))
            if complete:
                return
            await asyncio.sleep(POLL_SECONDS)
        raise TimeoutError("cluster version convergence timed out")
    finally:
        await transport.close()
        await close_db()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m app.services.cluster_update_coordinator <sha> <upgrade|rollback>")
    try:
        asyncio.run(run(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        print(f"cluster release failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
