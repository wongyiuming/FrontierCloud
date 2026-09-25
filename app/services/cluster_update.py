"""Master-side propagation and convergence wait for an already updated commit."""
from __future__ import annotations

import asyncio
import sys
import time

from app.services.federation.runtime import runtime
from app.services.federation.state import state

POLL_SECONDS = 2
CONVERGENCE_TIMEOUT = 300


async def propagate(version: str) -> None:
    await state.initialize()
    if state.node["role"] != "Master":
        raise RuntimeError("Cluster propagation is only valid on the Master")
    followers = [row for row in await state.list_relationships()
                 if row["direction"] == "downstream" and row["state"] == "active"]
    if not followers:
        return

    for relation in followers:
        await runtime.call(relation, "/internal/v1/cluster-update", {"version": version})

    pending = {row["relationship_id"]: row for row in followers}
    deadline = time.monotonic() + CONVERGENCE_TIMEOUT
    failures: list[str] = []
    while pending and time.monotonic() < deadline:
        for identifier, relation in list(pending.items()):
            try:
                result = await runtime.call(
                    relation, "/internal/v1/cluster-update/status", {"version": version}
                )
            except Exception:
                continue
            if result.get("version") != version:
                continue
            if result.get("status") == "success":
                pending.pop(identifier, None)
            elif result.get("status") == "failed":
                failures.append(f"{relation['peer_id']}: {result.get('detail') or 'update failed'}")
                pending.pop(identifier, None)
        if pending:
            await asyncio.sleep(POLL_SECONDS)

    await runtime.stop()
    if failures:
        raise RuntimeError("Follower update failure: " + "; ".join(failures))
    if pending:
        peers = ", ".join(row["peer_id"] for row in pending.values())
        raise RuntimeError("Follower convergence timeout: " + peers)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.services.cluster_update <commit-sha>", file=sys.stderr)
        return 2
    try:
        asyncio.run(propagate(sys.argv[1]))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
