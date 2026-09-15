"""Real MySQL/Redis checks using only uniquely named fixture tables and keys."""
from __future__ import annotations

import asyncio
import inspect
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request

from app.api.v1 import media
from app.core import db
from app.core.config import settings
from app.core.redis import redis_client
from app.services import media_manager, media_objects, network_observation as observation, playback
from app.services.federation import routing, schema as s
from app.services.federation.catalog import Catalog
from app.services.federation.state import State


async def main():
    prefix = "fc_sql_test_" + uuid.uuid4().hex[:12] + "_"
    local_tables = ["media_objects", "media_playback_stats", "media_playback_events", "media_lyric_links",
                    "media_visibility", "media_delete_operations",
                    "webrtc_observation_events", "webrtc_observation_summary"]
    node_tables = [table.name for table in s.metadata.sorted_tables]
    names = local_tables + node_tables
    database = create_async_engine(settings.MYSQL_URL, pool_size=5, max_overflow=15)
    statements, created, reservation_keys = [], [], []
    inject_summary_failure = False

    @event.listens_for(database.sync_engine, "before_cursor_execute", retval=True)
    def isolate(_conn, _cursor, statement, parameters, _context, executemany):
        if inject_summary_failure and "INSERT INTO webrtc_observation_summary" in statement:
            raise RuntimeError("injected summary failure")
        statements.append((statement, executemany))
        for name in names:
            statement = re.sub(r"\b" + re.escape(name) + r"\b", prefix + name, statement)
        # MySQL CHECK constraint names are schema-wide, unlike index names.
        statement = re.sub(r"(CONSTRAINT\s+)(\w+)", lambda match: match[1] + prefix + match[2], statement)
        return statement, parameters

    async def count(name):
        async with database.connect() as conn:
            return await conn.scalar(text("SELECT COUNT(*) FROM " + name))

    try:
        async with database.connect() as conn:
            source = inspect.getsource(db.init_db)
            for name in local_tables:
                statement = re.search(r"CREATE TABLE IF NOT EXISTS " + name + r"\s*\(.*?\"\"\"", source, re.S).group(0)[:-3]
                await conn.execute(text(statement.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)))
                created.append(name)
                await conn.commit()
            for statement in s.migration_statements():
                await conn.execute(text(statement))
                created.append(re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", statement).group(1))
                await conn.commit()

        store = State(database, Fernet.generate_key())
        await store.initialize()
        await store.promote("Master", "https://master.example.com", "fixture")
        owner, relation_id = uuid.uuid4().hex, uuid.uuid4().hex
        peer = dict(node_id=owner, endpoint="https://slave.example.com", public_key="a" * 43,
                    app_version="fixture")
        await store.prepare(relation_id, peer, "fixture-credential", "fixture")
        await store.activate(relation_id, "fixture")
        await store.heartbeat(relation_id, True)
        identifiers = [f"{number:064x}" for number in range(1, 9)]
        async with database.begin() as conn:
            await conn.execute(insert(s.catalog), [dict(resource_id=identifier, owner_id=owner,
                object_id=f"{100 + number:064x}", relationship_id=relation_id,
                path=f"music/owner/song-{number}.mp3", version=number + 1, deleted=0,
                payload=dict(play_score=7, preference=2, has_lyrics=False, type="audio"))
                for number, identifier in enumerate(identifiers)])
        remote_catalog = Catalog(store)

        async def remote(identifier, session=None, delta=None):
            return await routing.mutate_stats(identifier, delta=delta, session=session,
                                              played=30, duration=60, path=None)

        with patch.object(routing, "state", store), patch.object(routing, "catalog", remote_catalog):
            # Identity changes must not be the lock for ordinary resource accounting.
            async with database.begin() as locked:
                await store.lock(locked)
                result = await asyncio.wait_for(remote(identifiers[1], str(uuid.uuid4())), 5)
                assert result["counted"]

            session = str(uuid.uuid4())
            results = await asyncio.gather(*(remote(identifier, session) for identifier in identifiers for _ in range(3)))
            assert sum(result["counted"] for result in results) == len(identifiers)
            assert await count("node_playback_events") == len(identifiers) + 1

            # Lock one media row; another media on the SAME relationship progresses.
            pending = None
            try:
                async with database.begin() as locked:
                    await locked.execute(select(s.stats).where(s.stats.c.resource_id == identifiers[0]).with_for_update())
                    pending = asyncio.create_task(remote(identifiers[0], str(uuid.uuid4())))
                    result = await asyncio.wait_for(remote(identifiers[2], str(uuid.uuid4())), 5)
                    assert result["counted"] and not pending.done()
                await asyncio.wait_for(pending, 5)
            finally:
                if pending and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

            await asyncio.gather(*(remote(identifiers[0], delta=1) for _ in range(12)))
            assert (await remote(identifiers[0], delta=1))["preference"] == 7
            async with database.begin() as conn:
                await conn.execute(update(s.events).where(s.events.c.session_id == session).values(expires_at=int(time.time()) - 1))
            results = await asyncio.gather(*(remote(identifier, session) for identifier in identifiers))
            assert all(result["counted"] for result in results)
            # Revocation blocks future updates without touching another source.
            await store.revoke(relation_id, "fixture")
            try:
                await remote(identifiers[0], str(uuid.uuid4()))
            except ValueError:
                pass
            else:
                raise AssertionError("revoked relationship accepted playback")

        with tempfile.TemporaryDirectory(prefix="fc_sql_media_") as directory:
            root = Path(directory)
            paths = [f"music/fixture/song-{number}.mp3" for number in range(105)]
            for path in paths:
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"ID3 fixture")
            with patch.object(media_objects, "engine", database), patch.object(playback, "engine", database):
                # Concurrent initial registration must converge under RR snapshots.
                mappings = await asyncio.gather(*(media_objects.ensure_objects([(paths[0], "audio")]) for _ in range(8)))
                assert len({mapping[paths[0]] for mapping in mappings}) == 1
                session = str(uuid.uuid4())
                results = await asyncio.gather(*(playback.record_playback(root, path, session, 30, 60)
                    for path in paths[:2] for _ in range(6)))
                assert sum(result["counted"] for result in results) == 2
                assert await count("media_playback_events") == 2
                async with database.begin() as conn:
                    await conn.execute(text("UPDATE media_playback_events SET expires_at=:expired"),
                                       {"expired": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)})
                results = await asyncio.gather(*(playback.record_playback(root, path, session, 30, 60) for path in paths[:2]))
                assert all(result["counted"] for result in results)

                # Prefix deletion keeps SQL wildcard and case neighbors. The
                # real MySQL collation must not weaken the binary path boundary.
                selected_dir = root / "music" / "A_%!"
                sibling_dir = root / "music" / "aX%!!"
                selected_dir.mkdir(parents=True)
                sibling_dir.mkdir(parents=True)
                selected_path = "music/A_%!/selected.mp3"
                sibling_path = "music/aX%!!/sibling.mp3"
                (root / selected_path).write_bytes(b"ID3 selected")
                (root / sibling_path).write_bytes(b"ID3 sibling")
                await media_objects.ensure_objects([(selected_path, "audio"), (sibling_path, "audio")])
                with patch.object(media_manager, "MEDIA_ROOT", root), patch.object(media_manager, "engine", database), \
                     patch.object(media_manager, "invalidate_media_catalog", new=AsyncMock()):
                    deleted = await media_manager.MediaManager.delete(["music/A_%!"])
                assert deleted == 1 and not selected_dir.exists() and sibling_dir.is_dir()
                async with database.connect() as conn:
                    assert await conn.scalar(text("SELECT COUNT(*) FROM media_objects WHERE media_path=:path"), {"path": selected_path}) == 0
                    assert await conn.scalar(text("SELECT COUNT(*) FROM media_objects WHERE media_path=:path"), {"path": sibling_path}) == 1

                exported = Catalog(store)
                with patch.object(media, "MEDIA_ROOT", root), patch.object(media, "_hidden_set", new=AsyncMock(return_value=set())):
                    before = len(statements)
                    await exported.scan(force=True)
                    writes = [sql for sql, _ in statements[before:] if sql.lstrip().startswith("INSERT INTO node_media_exports")]
                    assert len(writes) == 2, f"105 exports required {len(writes)} writes instead of two bounded batches"
                    before = len(statements)
                    await exported.scan(force=True)
                    assert not any(sql.lstrip().startswith("INSERT INTO node_media_exports") for sql, _ in statements[before:])
                    for path in paths[:3]:
                        (root / path).unlink()
                    await exported.scan(force=True)
                    async with database.connect() as conn:
                        assert await conn.scalar(select(s.exports.c.deleted).where(s.exports.c.path == paths[0])) == 1

        request = Request({"type": "http", "client": ("203.0.113.250", 32000), "headers": []})
        recent = datetime(2026, 9, 14, 7, 10)
        earlier = recent - timedelta(minutes=1)
        with patch.object(observation, "engine", database), \
             patch.object(observation.redis_client, "set", new=AsyncMock(return_value=True)):
            with patch.object(observation, "_utcnow", return_value=recent):
                await observation.record_observation(request, ["198.51.100.250"], None)
            with patch.object(observation, "_utcnow", return_value=earlier):
                await observation.record_observation(request, ["198.51.100.250"], "timeout")
            async with database.connect() as conn:
                row = (await conn.execute(text("SELECT * FROM webrtc_observation_summary"))).mappings().one()
                assert row["first_seen"] == earlier and row["last_seen"] == recent
                assert row["last_outcome"] == "ok" and row["observation_count"] == 2
            before = await count("webrtc_observation_events")
            inject_summary_failure = True
            with patch.object(observation.redis_client, "eval", new=AsyncMock()):
                try:
                    await observation.record_observation(request, ["198.51.100.251"], None)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("summary fault did not reject transaction")
            inject_summary_failure = False
            assert await count("webrtc_observation_events") == before

        key = prefix + "cooldown"
        reservation_keys.append(key)
        await redis_client.set(key, "new-reservation", ex=30)
        assert await redis_client.eval(observation.RELEASE_RESERVATION, 1, key, "old-reservation") == 0
        assert await redis_client.get(key) in ("new-reservation", b"new-reservation")
        assert await redis_client.eval(observation.RELEASE_RESERVATION, 1, key, "new-reservation") == 1
        print("mysql-concurrency-smoke-ok: independent resources, idempotence, expiry, revocation, registration, catalog batches, observation chronology/rollback; Redis reservation CAS")
    finally:
        inject_summary_failure = False
        if reservation_keys:
            await redis_client.delete(*reservation_keys)
        async with database.connect() as conn:
            for name in reversed(created):
                await conn.execute(text("DROP TABLE IF EXISTS " + name))
                await conn.commit()
        await database.dispose()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
