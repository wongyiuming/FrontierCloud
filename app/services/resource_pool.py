"""Master-owned storage, compute and backup resource facts.

Nodes execute byte and compute operations, but only the Master commits business
state.  Every media object therefore has one global identity and exactly one
complete-file placement.
"""
from __future__ import annotations

import hashlib
import asyncio
import base64
import gzip
import json
import os
import ssl
import shutil
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import httpx

from app.core.db import engine
from app.services import media_objects
from app.services.federation import protocol as p
from app.services.federation import schema as s

GIB = 1024 ** 3
PHYSICAL_RESERVE_BYTES = GIB
UPLOAD_TTL_SECONDS = 30 * 60
storage_write_lock = asyncio.Lock()


async def _upsert(conn, table, values: dict, conflict_columns: tuple[str, ...],
                  update_columns: tuple[str, ...] | None = None) -> None:
    """Use the native atomic upsert for both production MySQL and test SQLite."""
    update_columns = update_columns or tuple(key for key in values if key not in conflict_columns)
    if conn.dialect.name == "sqlite":
        statement = sqlite_insert(table).values(**values)
        await conn.execute(statement.on_conflict_do_update(
            index_elements=[table.c[name] for name in conflict_columns],
            set_={name: statement.excluded[name] for name in update_columns},
        ))
        return
    statement = mysql_insert(table).values(**values)
    await conn.execute(statement.on_duplicate_key_update(**{
        name: statement.inserted[name] for name in update_columns
    }))


def path_locator(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def validate_media_path(path: str) -> tuple[str, str]:
    value = PurePosixPath(path)
    parts = value.parts
    if (not path or len(path) > 1024 or "\\" in path or path.startswith("/")
            or path != "/".join(parts) or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
            or len(parts) not in (3, 4) or parts[0] not in ("music", "vido")):
        raise p.ProtocolError("媒体路径必须位于单一媒体类型的分类目录内")
    allowed = {"music": {".mp3", ".m4a", ".flac", ".wav"},
               "vido": {".mp4", ".webm", ".mkv"}}
    suffix = value.suffix.lower()
    if suffix not in allowed[parts[0]]:
        raise p.ProtocolError("媒体类型与目标目录不匹配")
    return path, "audio" if parts[0] == "music" else "video"


async def managed_local_bytes(conn=None) -> int:
    owns = conn is None
    if owns:
        conn = await engine.connect()
    try:
        value = await conn.scalar(select(func.coalesce(func.sum(s.global_media.c.size_bytes), 0)).where(
            s.global_media.c.storage_member_id == select(s.identity.c.node_id).where(s.identity.c.singleton == 1).scalar_subquery(),
            s.global_media.c.state.in_(("active", "pending_delete")),
        ))
        return int(value or 0)
    finally:
        if owns:
            await conn.close()


async def ensure_follower_business_empty(database=engine) -> None:
    """Refuse silent conversion of an established standalone business node."""
    from app.api.v1.media import MEDIA_ROOT
    roots = (MEDIA_ROOT / "music", MEDIA_ROOT / "vido", MEDIA_ROOT / "lyrics")
    if any(any(item.is_file() for item in root.rglob("*")) for root in roots if root.exists()):
        raise p.ProtocolError("Standalone 仍有媒体或歌词；请先执行显式纳管迁移，不能直接固定为 Follower")
    from sqlalchemy import text
    async with database.connect() as conn:
        for table in ("karaoke_users", "karaoke_recordings"):
            if int(await conn.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0):
                raise p.ProtocolError("Standalone 仍有 卡拉OK业务数据；请先迁移，不能直接固定为 Follower")


def physical_free(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    return int(shutil.disk_usage(root).free)


def local_filesystem_bytes(root: Path) -> int:
    return sum(item.stat().st_size for root_name in ("music", "vido")
               for item in (root / root_name).rglob("*") if item.is_file() and not item.is_symlink())


def compute_pressure() -> tuple[int, int]:
    """Return bounded host load and available memory without adding an agent dependency."""
    cores = max(1, os.cpu_count() or 1)
    try:
        cpu_percent = max(0, min(100, round(os.getloadavg()[0] * 100 / cores)))
    except (AttributeError, OSError):
        cpu_percent = 0
    memory_available = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                memory_available = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    return cpu_percent, memory_available


async def ensure_master_local(node: dict, allocation_bytes: int | None = None, *, conn=None) -> dict | None:
    if node["role"] not in ("Master", "Standalone"):
        return None
    from app.api.v1.media import MEDIA_ROOT
    now = int(time.time())
    if conn is None:
        async with engine.begin() as owned_conn:
            return await ensure_master_local(node, allocation_bytes, conn=owned_conn)
    else:
        current = (await conn.execute(select(s.storage_members).where(
            s.storage_members.c.member_id == node["node_id"]))).mappings().first()
        used = (await conn.execute(select(func.coalesce(func.sum(s.global_media.c.size_bytes), 0)).where(
            s.global_media.c.storage_member_id == node["node_id"],
            s.global_media.c.state.in_(("active", "pending_delete"))))).scalar_one()
        from sqlalchemy import text
        recording_used = int(await conn.scalar(text("""
            SELECT COALESCE(SUM(size_bytes), 0) FROM karaoke_recordings
            WHERE storage_member_id=:member_id AND state='ready'
        """), {"member_id": node["node_id"]}) or 0)
        used = max(int(used or 0), local_filesystem_bytes(MEDIA_ROOT)) + recording_used
        allocation = int(allocation_bytes if allocation_bytes is not None
                         else (current["allocated_bytes"] if current else max(used, GIB)))
        if allocation < used:
            raise p.ProtocolError("Master Local Storage Allocation 不能低于现有托管媒体用量")
        values = dict(member_id=node["node_id"], relationship_id=None, member_kind="MasterLocal",
                      transport="Local", storage_enabled=1, allocated_bytes=allocation,
                      used_bytes=used, reserved_bytes=int(current["reserved_bytes"] if current else 0),
                      physical_free_bytes=physical_free(MEDIA_ROOT), health="online", writable=1,
                      updated_at=now)
        await _upsert(conn, s.storage_members, values, ("member_id",))
        compute = dict(member_id=node["node_id"], enabled=1, worker_slots=1,
                       available_slots=1, cpu_percent=0, memory_available_bytes=0,
                       capabilities=["fallback"], updated_at=now)
        await _upsert(conn, s.compute_members, compute, ("member_id",), ("updated_at",))
        return values


async def register_follower(relation: dict, *, conn=None) -> None:
    now = int(time.time())
    if conn is None:
        async with engine.begin() as owned_conn:
            await register_follower(relation, conn=owned_conn)
        return
    else:
        current = (await conn.execute(select(s.storage_members).where(
            s.storage_members.c.member_id == relation["peer_id"]))).mappings().first()
        placed_media = int(await conn.scalar(select(func.coalesce(func.sum(
            s.global_media.c.size_bytes), 0)).where(
            s.global_media.c.storage_member_id == relation["peer_id"],
            s.global_media.c.state.in_(("active", "pending_delete")))) or 0)
        from sqlalchemy import text
        placed_recordings = int(await conn.scalar(text("""
            SELECT COALESCE(SUM(size_bytes), 0) FROM karaoke_recordings
            WHERE storage_member_id=:member_id AND state='ready'
        """), {"member_id": relation["peer_id"]}) or 0)
        authoritative_used = placed_media + placed_recordings
        summary = relation.get("summary") or {}
        storage_report = summary.get("storage") if isinstance(summary.get("storage"), dict) else {}
        values = dict(member_id=relation["peer_id"], relationship_id=relation["relationship_id"],
            member_kind="Follower", transport=relation["mode"], storage_enabled=int(bool(current and current["storage_enabled"])),
            allocated_bytes=int(current["allocated_bytes"] if current else 0),
            used_bytes=max(int(current["used_bytes"] if current else 0), authoritative_used),
            reserved_bytes=int(current["reserved_bytes"] if current else 0),
            physical_free_bytes=int(storage_report.get("physical_free_bytes") or summary.get("storage_free") or 0),
            health="online" if relation["status"] == "online" else "offline",
            writable=int(bool(current and current["writable"])), updated_at=now)
        await _upsert(conn, s.storage_members, values, ("member_id",))
        for table, defaults in (
            (s.compute_members, dict(enabled=0, worker_slots=0, available_slots=0, cpu_percent=0,
                                     memory_available_bytes=0, capabilities=[])),
            (s.backup_members, dict(enabled=0, generation=0, last_success=0, lag_seconds=0,
                                    checksum="", state="disabled")),
        ):
            await _upsert(conn, table,
                          dict(member_id=relation["peer_id"], updated_at=now, **defaults),
                          ("member_id",), ("updated_at",))
        compute_report = summary.get("compute") if isinstance(summary.get("compute"), dict) else {}
        if compute_report:
            await conn.execute(update(s.compute_members).where(s.compute_members.c.member_id == relation["peer_id"]).values(
                available_slots=max(0, int(compute_report.get("available_slots") or 0)),
                cpu_percent=max(0, min(100, int(compute_report.get("cpu_percent") or 0))),
                memory_available_bytes=max(0, int(compute_report.get("memory_available_bytes") or 0)),
                capabilities=compute_report.get("capabilities") if isinstance(compute_report.get("capabilities"), list) else [],
                updated_at=now))
        backup_report = summary.get("backup") if isinstance(summary.get("backup"), dict) else {}
        if backup_report:
            await conn.execute(update(s.backup_members).where(s.backup_members.c.member_id == relation["peer_id"]).values(
                generation=max(0, int(backup_report.get("generation") or 0)),
                last_success=max(0, int(backup_report.get("last_success") or 0)),
                lag_seconds=max(0, int(backup_report.get("lag_seconds") or 0)),
                checksum=str(backup_report.get("checksum") or "")[:64],
                state=str(backup_report.get("state") or "pending")[:24], updated_at=now))


async def configure_member(member_id: str, *, storage_enabled: bool, allocated_bytes: int,
                           compute_enabled: bool, worker_slots: int, backup_enabled: bool,
                           actor: str, store) -> None:
    if allocated_bytes < 0 or allocated_bytes > 10 * 1024 ** 4:
        raise p.ProtocolError("Storage allocation is outside the supported range")
    if storage_enabled and allocated_bytes < GIB:
        raise p.ProtocolError("Storage allocation must be at least 1 GiB")
    if not 0 <= worker_slots <= 256:
        raise p.ProtocolError("Worker slots are outside the supported range")
    now = int(time.time())
    async with store.database.begin() as conn:
        node = await store.lock(conn)
        if node["role"] != "Master":
            raise p.ProtocolError("Only Master configures resource members")
        if member_id == node["node_id"]:
            if not storage_enabled or backup_enabled:
                raise p.ProtocolError("Master Local storage is mandatory and is not a backup target")
            await ensure_master_local(node, allocated_bytes, conn=conn)
            await store.log(conn, "master-local-allocation-changed", actor,
                            member_id=member_id, allocated_bytes=allocated_bytes)
            return
        member = (await conn.execute(select(s.storage_members).where(
            s.storage_members.c.member_id == member_id).with_for_update())).mappings().first()
        if not member:
            raise p.ProtocolError("Unknown resource member")
        used = int(member["used_bytes"] or 0)
        if allocated_bytes < used:
            raise p.ProtocolError("Storage allocation cannot be lower than used capacity")
        await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == member_id).values(
            storage_enabled=int(storage_enabled), allocated_bytes=allocated_bytes,
            writable=int(storage_enabled and member["health"] == "online"), updated_at=now))
        await conn.execute(update(s.compute_members).where(s.compute_members.c.member_id == member_id).values(
            enabled=int(compute_enabled), worker_slots=worker_slots,
            available_slots=worker_slots if compute_enabled else 0, updated_at=now))
        current_backup = (await conn.execute(select(s.backup_members).where(
            s.backup_members.c.member_id == member_id).with_for_update())).mappings().first()
        if not backup_enabled:
            backup_state = "disabled"
        elif not current_backup or not bool(current_backup["enabled"]):
            backup_state = "pending"
        else:
            backup_state = str(current_backup["state"] or "pending")
        await conn.execute(update(s.backup_members).where(s.backup_members.c.member_id == member_id).values(
            enabled=int(backup_enabled), state=backup_state, updated_at=now))
        await store.log(conn, "resource-member-configured", actor, member.get("relationship_id"),
                        member_id=member_id, storage_enabled=storage_enabled,
                        allocated_bytes=allocated_bytes, compute_enabled=compute_enabled,
                        worker_slots=worker_slots, backup_enabled=backup_enabled)


async def member_configuration(member_id: str, database=engine) -> dict:
    members = await list_members(database)
    row = next((item for item in members if item["member_id"] == member_id), None)
    if not row:
        raise p.ProtocolError("Unknown resource member")
    return {"storage": {"enabled": bool(row["storage_enabled"]),
                        "allocated_bytes": int(row["allocated_bytes"])},
            "compute": {"enabled": bool(row["compute"].get("enabled")),
                        "worker_slots": int(row["compute"].get("worker_slots") or 0)},
            "backup": {"enabled": bool(row["backup"].get("enabled"))}}


async def accept_follower_configuration(configuration: dict, node: dict, database=engine) -> None:
    try:
        storage = configuration["storage"]
        compute = configuration["compute"]
        backup = configuration["backup"]
        allocated = int(storage["allocated_bytes"])
        slots = int(compute["worker_slots"])
        if (type(storage["enabled"]) is not bool or type(compute["enabled"]) is not bool
                or type(backup["enabled"]) is not bool or not 0 <= allocated <= 10 * 1024 ** 4
                or not 0 <= slots <= 256):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise p.ProtocolError("Invalid resource configuration") from exc
    from app.api.v1.media import MEDIA_ROOT
    now = int(time.time())
    async with database.begin() as conn:
        used = int((await conn.execute(select(func.coalesce(func.sum(s.global_media.c.size_bytes), 0)).where(
            s.global_media.c.storage_member_id == node["node_id"],
            s.global_media.c.state.in_(("active", "pending_delete"))))).scalar_one() or 0)
        if allocated < used:
            raise p.ProtocolError("Storage allocation cannot be lower than used capacity")
        values = dict(member_id=node["node_id"], relationship_id=None, member_kind="Follower",
            transport="Local", storage_enabled=int(storage["enabled"]), allocated_bytes=allocated,
            used_bytes=used, reserved_bytes=0, physical_free_bytes=physical_free(MEDIA_ROOT),
            health="online", writable=int(storage["enabled"]), updated_at=now)
        await _upsert(conn, s.storage_members, values, ("member_id",), tuple(
            key for key in values if key not in ("member_id", "used_bytes", "reserved_bytes")))
        await _upsert(conn, s.compute_members, dict(member_id=node["node_id"],
            enabled=int(compute["enabled"]), worker_slots=slots,
            available_slots=slots if compute["enabled"] else 0, cpu_percent=0,
            memory_available_bytes=0, capabilities=["hash", "probe", "metadata"], updated_at=now),
            ("member_id",), ("enabled", "worker_slots", "available_slots", "capabilities", "updated_at"))
        current_backup = (await conn.execute(select(s.backup_members).where(
            s.backup_members.c.member_id == node["node_id"]))).mappings().first()
        enabled = bool(backup["enabled"])
        if not enabled:
            backup_state = "disabled"
        elif not current_backup or not bool(current_backup["enabled"]):
            backup_state = "pending"
        else:
            # Applying the same desired configuration must not erase a durable
            # ready/receiving state from the actual backup data plane.
            backup_state = str(current_backup["state"] or "pending")
        await _upsert(conn, s.backup_members, dict(member_id=node["node_id"],
            enabled=int(enabled), generation=0, last_success=0, lag_seconds=0,
            checksum="", state=backup_state, updated_at=now),
            ("member_id",), ("enabled", "state", "updated_at"))


async def follower_resource_summary(node: dict, database=engine) -> dict:
    from app.api.v1.media import MEDIA_ROOT
    now = int(time.time())
    async with database.begin() as conn:
        member = (await conn.execute(select(s.storage_members).where(
            s.storage_members.c.member_id == node["node_id"]))).mappings().first()
        if not member:
            return {"storage": {"enabled": False, "allocated_bytes": 0, "used_bytes": 0,
                                "reserved_bytes": 0, "physical_free_bytes": physical_free(MEDIA_ROOT)},
                    "compute": {"enabled": False, "worker_slots": 0, "available_slots": 0,
                                "cpu_percent": 0, "memory_available_bytes": 0, "capabilities": []},
                    "backup": {"enabled": False, "generation": 0, "last_success": 0,
                               "lag_seconds": 0, "checksum": "", "state": "disabled",
                               "last_size_bytes": 0, "recovery_points": 0,
                               "last_attempt": 0, "last_attempt_state": "disabled"}}
        free = physical_free(MEDIA_ROOT)
        await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == node["node_id"]).values(
            physical_free_bytes=free, updated_at=now))
        compute = (await conn.execute(select(s.compute_members).where(
            s.compute_members.c.member_id == node["node_id"]))).mappings().first()
        backup = (await conn.execute(select(s.backup_members).where(
            s.backup_members.c.member_id == node["node_id"]))).mappings().first()
        if compute:
            cpu_percent, memory_available = compute_pressure()
            await conn.execute(update(s.compute_members).where(
                s.compute_members.c.member_id == node["node_id"]
            ).values(cpu_percent=cpu_percent, memory_available_bytes=memory_available, updated_at=now))
            compute = dict(compute)
            compute.update(cpu_percent=cpu_percent, memory_available_bytes=memory_available)
        if backup:
            backup = dict(backup)
            backup["lag_seconds"] = max(0, now - int(backup.get("last_success") or 0)) if backup.get("enabled") else 0
            latest = (await conn.execute(select(s.business_backups).order_by(
                s.business_backups.c.updated_at.desc()).limit(1))).mappings().first()
            ready_points = int(await conn.scalar(select(func.count()).select_from(s.business_backups).where(
                s.business_backups.c.state == "ready")) or 0)
            latest_ready = (await conn.execute(select(s.business_backups).where(
                s.business_backups.c.state == "ready").order_by(
                s.business_backups.c.updated_at.desc()).limit(1))).mappings().first()
            backup["last_size_bytes"] = int(latest_ready["size_bytes"] if latest_ready else 0)
            backup["recovery_points"] = ready_points
            backup["last_attempt"] = int(latest["updated_at"] if latest else 0)
            backup["last_attempt_state"] = str(latest["state"] if latest else "pending")
    return {"storage": {"enabled": bool(member["storage_enabled"]),
                        "allocated_bytes": int(member["allocated_bytes"]),
                        "used_bytes": int(member["used_bytes"]),
                        "reserved_bytes": int(member["reserved_bytes"]),
                        "physical_free_bytes": free},
            "compute": dict(compute) if compute else {}, "backup": dict(backup) if backup else {}}


BACKUP_TABLES = (
    "media_visibility", "media_objects", "media_playback_stats", "media_playback_events",
    "media_lyric_links", "global_media_objects", "cluster_storage_members",
    "cluster_compute_members", "cluster_worker_jobs", "cluster_backup_members",
    "karaoke_users", "karaoke_recordings", "karaoke_registration_daily", "karaoke_audit_log",
    "admin_audit_log", "webrtc_observation_events", "webrtc_observation_summary",
    "ip_security_audit_log", "ip_security_summary", "ip_security_projection",
    "ip_auto_ban_events", "ip_permanent_whitelist", "node_audit",
)


def _json_value(value):
    if hasattr(value, "isoformat"):
        return {"$datetime": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    return value


async def build_business_backup(database=engine) -> tuple[int, Path, str]:
    """Stream a bounded-memory recovery artifact; online reads never use it."""
    from sqlalchemy import text
    from app.api.v1.media import MEDIA_ROOT
    generation = int(time.time_ns())
    descriptor, name = tempfile.mkstemp(prefix="frontier-business-backup-", suffix=".jsonl.gz")
    os.close(descriptor)
    try:
        with gzip.open(name, "wb", compresslevel=6) as output:
            def write(value: dict) -> None:
                output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                        sort_keys=True).encode("utf-8") + b"\n")

            write({"kind": "header", "version": 2, "generation": generation})
            async with database.connect() as conn:
                for table in BACKUP_TABLES:
                    result = await conn.stream(text(f"SELECT * FROM {table}"))
                    async for row in result.mappings():
                        write({"kind": "row", "table": table,
                               "value": {key: _json_value(value) for key, value in dict(row).items()}})
            lyric_root = MEDIA_ROOT / "lyrics"
            if lyric_root.exists():
                for file in sorted(lyric_root.rglob("*.lrc"), key=lambda item: item.as_posix()):
                    if file.is_file() and not file.is_symlink():
                        write({"kind": "lyric", "name": file.relative_to(lyric_root).as_posix(),
                               "payload": base64.b64encode(file.read_bytes()).decode("ascii")})
            write({"kind": "end"})
        path = Path(name)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return generation, path, digest.hexdigest()
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


async def backup_begin(master_id: str, generation: int, database=engine) -> None:
    if not p.IDENTIFIER.fullmatch(master_id) or generation <= 0:
        raise p.ProtocolError("Invalid backup generation")
    now = int(time.time())
    async with database.begin() as conn:
        await conn.execute(delete(s.business_backup_chunks).where(
            s.business_backup_chunks.c.master_id == master_id,
            s.business_backup_chunks.c.generation == generation))
        await _upsert(conn, s.business_backups, dict(master_id=master_id, generation=generation,
            checksum="", size_bytes=0, chunk_count=0, state="receiving",
            created_at=now, updated_at=now), ("master_id", "generation"))
        local_member = await conn.scalar(select(s.identity.c.node_id).where(s.identity.c.singleton == 1))
        if local_member:
            await conn.execute(update(s.backup_members).where(
                s.backup_members.c.member_id == local_member,
                s.backup_members.c.enabled == 1,
            ).values(state="receiving", updated_at=now))


async def backup_append(master_id: str, generation: int, chunk_index: int, chunk: bytes,
                        database=engine) -> None:
    if len(chunk) > 192 * 1024:
        raise p.ProtocolError("Backup chunk too large")
    if chunk_index < 0 or chunk_index > 10_000_000:
        raise p.ProtocolError("Invalid backup chunk index")
    async with database.begin() as conn:
        row = await conn.scalar(select(s.business_backups.c.state).where(
            s.business_backups.c.master_id == master_id,
            s.business_backups.c.generation == generation,
        ).with_for_update())
        if row != "receiving":
            raise p.ProtocolError("Backup generation is not receiving")
        try:
            await conn.execute(insert(s.business_backup_chunks).values(
                master_id=master_id, generation=generation, chunk_index=chunk_index,
                payload=chunk, created_at=int(time.time())))
        except IntegrityError as exc:
            raise p.ProtocolError("Duplicate backup chunk") from exc


async def backup_commit(master_id: str, generation: int, checksum: str, node: dict,
                        database=engine) -> int:
    if not re_full_hash(checksum):
        raise p.ProtocolError("Invalid backup checksum")
    now = int(time.time())
    async with database.begin() as conn:
        row = (await conn.execute(select(s.business_backups).where(
            s.business_backups.c.master_id == master_id,
            s.business_backups.c.generation == generation).with_for_update())).mappings().first()
        chunks = [dict(item) for item in (await conn.execute(select(s.business_backup_chunks).where(
            s.business_backup_chunks.c.master_id == master_id,
            s.business_backup_chunks.c.generation == generation,
        ).order_by(s.business_backup_chunks.c.chunk_index))).mappings()]
        digest, size = hashlib.sha256(), 0
        for expected_index, chunk in enumerate(chunks):
            if int(chunk["chunk_index"]) != expected_index:
                raise p.ProtocolError("Backup chunk sequence is incomplete")
            digest.update(chunk["payload"]); size += len(chunk["payload"])
        if not row or row["state"] != "receiving" or not chunks or digest.hexdigest() != checksum:
            raise p.ProtocolError("Backup checksum mismatch")
        await conn.execute(update(s.business_backups).where(
            s.business_backups.c.master_id == master_id,
            s.business_backups.c.generation == generation,
        ).values(checksum=checksum, size_bytes=size, chunk_count=len(chunks),
                 state="ready", updated_at=now))
        await conn.execute(update(s.backup_members).where(s.backup_members.c.member_id == node["node_id"]).values(
            generation=generation, last_success=now, lag_seconds=0, checksum=checksum,
            state="ready", updated_at=now))
        stale = list((await conn.execute(select(s.business_backups.c.generation).where(
            s.business_backups.c.master_id == master_id,
            s.business_backups.c.state == "ready",
        ).order_by(s.business_backups.c.generation.desc()).offset(2))).scalars())
        if stale:
            await conn.execute(delete(s.business_backup_chunks).where(
                s.business_backup_chunks.c.master_id == master_id,
                s.business_backup_chunks.c.generation.in_(stale)))
            await conn.execute(delete(s.business_backups).where(
                s.business_backups.c.master_id == master_id,
                s.business_backups.c.generation.in_(stale)))
    return size


def re_full_hash(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


async def enqueue_job(job_type: str, payload: dict, idempotency_key: str,
                      media_id: str | None = None, member_id: str | None = None,
                      database=engine) -> str:
    if not re_full_hash(idempotency_key) or job_type not in {"hash", "probe", "metadata"}:
        raise p.ProtocolError("Invalid worker job")
    now, job_id = int(time.time()), uuid.uuid4().hex
    async with database.begin() as conn:
        statement = mysql_insert(s.worker_jobs).values(job_id=job_id, idempotency_key=idempotency_key,
            job_type=job_type, media_id=media_id, member_id=member_id, payload=payload, result={},
            state="queued", lease_token_hash=None, lease_expires_at=0, attempts=0,
            created_at=now, updated_at=now).prefix_with("IGNORE")
        await conn.execute(statement)
        existing = await conn.scalar(select(s.worker_jobs.c.job_id).where(
            s.worker_jobs.c.idempotency_key == idempotency_key))
    return str(existing)


async def lease_job(member_id: str, capabilities: list[str], database=engine) -> dict | None:
    allowed = [value for value in capabilities if value in {"hash", "probe", "metadata"}]
    if not allowed:
        return None
    now, lease = int(time.time()), uuid.uuid4().hex
    async with database.begin() as conn:
        row = (await conn.execute(select(s.worker_jobs).where(
            s.worker_jobs.c.job_type.in_(allowed),
            ((s.worker_jobs.c.state == "queued") | ((s.worker_jobs.c.state == "leased") &
              (s.worker_jobs.c.lease_expires_at <= now))),
            ((s.worker_jobs.c.member_id.is_(None)) | (s.worker_jobs.c.member_id == member_id)),
        ).order_by((s.worker_jobs.c.member_id == member_id).desc(), s.worker_jobs.c.created_at)
          .limit(1).with_for_update(skip_locked=True))).mappings().first()
        if not row:
            return None
        pinned = row["member_id"] == member_id
        result = dict(row.get("result") or {})
        result["_placement"] = {
            "reason": "pinned" if pinned else "capability-fifo",
            "leased_at": now,
            "capability": row["job_type"],
        }
        await conn.execute(update(s.worker_jobs).where(s.worker_jobs.c.job_id == row["job_id"]).values(
            member_id=member_id, state="leased", lease_token_hash=p.digest(lease),
            lease_expires_at=now + 120, attempts=int(row["attempts"]) + 1,
            result=result, updated_at=now))
    return {"job_id": row["job_id"], "job_type": row["job_type"], "media_id": row["media_id"],
            "payload": row["payload"], "lease": lease, "expires_at": now + 120}


async def complete_job(member_id: str, job_id: str, lease: str, result: dict,
                       database=engine) -> None:
    now = int(time.time())
    async with database.begin() as conn:
        row = (await conn.execute(select(s.worker_jobs).where(
            s.worker_jobs.c.job_id == job_id).with_for_update())).mappings().first()
        if (not row or row["state"] != "leased" or row["member_id"] != member_id
                or row["lease_expires_at"] <= now
                or not row["lease_token_hash"] or not secrets_compare(row["lease_token_hash"], p.digest(lease))):
            raise p.ProtocolError("Worker lease is stale or invalid")
        merged = dict(row.get("result") or {})
        merged.update(result)
        await conn.execute(update(s.worker_jobs).where(s.worker_jobs.c.job_id == job_id).values(
            result=merged, state="complete", lease_token_hash=None, lease_expires_at=0, updated_at=now))


def secrets_compare(left: str, right: str) -> bool:
    import secrets
    return secrets.compare_digest(left, right)


async def list_members(database=engine) -> list[dict]:
    async with database.connect() as conn:
        storage = [dict(row) for row in (await conn.execute(select(s.storage_members).order_by(
            s.storage_members.c.member_kind, s.storage_members.c.member_id))).mappings()]
        compute = {row["member_id"]: dict(row) for row in (await conn.execute(select(s.compute_members))).mappings()}
        backup = {row["member_id"]: dict(row) for row in (await conn.execute(select(s.backup_members))).mappings()}
    for row in storage:
        logical = max(0, int(row["allocated_bytes"]) - int(row["used_bytes"]) - int(row["reserved_bytes"]))
        physical = max(0, int(row["physical_free_bytes"]) - PHYSICAL_RESERVE_BYTES)
        row["available_bytes"] = min(logical, physical) if row["health"] == "online" and row["writable"] else 0
        row["online_writable_bytes"] = row["available_bytes"]
        row["offline_stored_bytes"] = int(row["used_bytes"]) if row["health"] != "online" else 0
        row["compute"] = compute.get(row["member_id"], {})
        row["backup"] = backup.get(row["member_id"], {})
    return storage


async def pool_summary(database=engine) -> dict:
    members = await list_members(database)
    return {"allocated_bytes": sum(int(x["allocated_bytes"]) for x in members if x["storage_enabled"]),
            "used_bytes": sum(int(x["used_bytes"]) for x in members),
            "reserved_bytes": sum(int(x["reserved_bytes"]) for x in members),
            "available_bytes": sum(int(x["available_bytes"]) for x in members),
            "online_writable_bytes": sum(int(x["online_writable_bytes"]) for x in members),
            "offline_stored_bytes": sum(int(x["offline_stored_bytes"]) for x in members),
            "members": members}


async def choose_member(size: int, preferred: str | None = None, database=engine) -> dict:
    candidates = [row for row in await list_members(database)
                  if row["storage_enabled"] and row["health"] == "online" and row["writable"]
                  and row["available_bytes"] >= size]
    if preferred:
        candidates = [row for row in candidates if row["member_id"] == preferred]
    if not candidates:
        raise p.ProtocolError("没有在线且容量充足的可写存储成员")
    # Preserve data locality for explicit Admin media placement; automatic
    # recordings use the member with the most currently writable bytes.
    return max(candidates, key=lambda row: (row["available_bytes"], row["member_kind"] == "MasterLocal"))


async def release_expired_uploads(database=engine) -> int:
    now = int(time.time())
    async with database.begin() as conn:
        rows = [dict(row) for row in (await conn.execute(select(s.upload_sessions).where(
            s.upload_sessions.c.state == "reserved", s.upload_sessions.c.expires_at <= now
        ).limit(500).with_for_update())).mappings()]
        for row in rows:
            await conn.execute(update(s.storage_members).where(
                s.storage_members.c.member_id == row["storage_member_id"]
            ).values(reserved_bytes=func.greatest(
                0, s.storage_members.c.reserved_bytes - int(row["expected_bytes"])), updated_at=now))
        if rows:
            await conn.execute(delete(s.upload_sessions).where(
                s.upload_sessions.c.upload_id.in_([row["upload_id"] for row in rows])))
    return len(rows)


async def reserve_upload(path: str, size: int, preferred: str | None, database=engine) -> dict:
    path, kind = validate_media_path(path)
    if size <= 0:
        raise p.ProtocolError("Upload size must be positive")
    await release_expired_uploads(database)
    member = await choose_member(size, preferred, database)
    now, upload_id = int(time.time()), uuid.uuid4().hex
    media_id = hashlib.sha256((upload_id + ":" + path).encode()).hexdigest()
    try:
        async with database.begin() as conn:
            current = (await conn.execute(select(s.storage_members).where(
                s.storage_members.c.member_id == member["member_id"]).with_for_update())).mappings().first()
            logical_available = (int(current["allocated_bytes"]) - int(current["used_bytes"])
                                 - int(current["reserved_bytes"])) if current else 0
            physical_available = max(0, int(current["physical_free_bytes"]) - PHYSICAL_RESERVE_BYTES) if current else 0
            if current and current["member_kind"] == "MasterLocal":
                from app.api.v1.media import MEDIA_ROOT
                physical_available = max(0, physical_free(MEDIA_ROOT) - PHYSICAL_RESERVE_BYTES)
            if (not current or not current["storage_enabled"] or current["health"] != "online" or not current["writable"]
                    or min(logical_available, physical_available) < size):
                raise p.ProtocolError("存储成员状态或剩余配额已变化，请刷新后重试")
            if await conn.scalar(select(s.global_media.c.media_id).where(s.global_media.c.path_locator == path_locator(path))):
                raise p.ProtocolError("全局媒体路径已存在")
            await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == member["member_id"]).values(
                reserved_bytes=s.storage_members.c.reserved_bytes + size, updated_at=now))
            await conn.execute(insert(s.upload_sessions).values(upload_id=upload_id, storage_member_id=member["member_id"],
                media_id=media_id, media_path=path, path_locator=path_locator(path), object_kind=kind,
                expected_bytes=size, state="reserved", expires_at=now + UPLOAD_TTL_SECONDS,
                created_at=now, updated_at=now))
    except IntegrityError as exc:
        raise p.ProtocolError("全局媒体路径已存在或已有上传正在进行") from exc
    return {"upload_id": upload_id, "media_id": media_id, "path": path, "type": kind, "member": member}


async def upload_session(upload_id: str, database=engine) -> dict:
    if not p.IDENTIFIER.fullmatch(upload_id):
        raise p.ProtocolError("Invalid upload session")
    async with database.connect() as conn:
        row = (await conn.execute(select(s.upload_sessions, s.storage_members.c.relationship_id,
            s.storage_members.c.transport, s.storage_members.c.member_kind,
            s.storage_members.c.health).join(s.storage_members,
            s.upload_sessions.c.storage_member_id == s.storage_members.c.member_id).where(
            s.upload_sessions.c.upload_id == upload_id))).mappings().first()
    if not row:
        raise p.ProtocolError("Upload session not found")
    return dict(row)


async def finalize_upload(upload_id: str, *, object_id: str, actual_size: int, etag: str,
                          database=engine) -> dict:
    now = int(time.time())
    async with database.begin() as conn:
        row = (await conn.execute(select(s.upload_sessions).where(
            s.upload_sessions.c.upload_id == upload_id).with_for_update())).mappings().first()
        if row and row["state"] == "complete":
            existing = (await conn.execute(select(s.global_media).where(
                s.global_media.c.media_id == row["media_id"]))).mappings().first()
            if existing:
                return dict(existing)
        if not row or row["state"] != "reserved" or row["expires_at"] <= now:
            raise p.ProtocolError("Upload reservation is missing or expired")
        if actual_size != int(row["expected_bytes"]):
            raise p.ProtocolError("Uploaded file size does not match reservation")
        values = dict(media_id=row["media_id"], storage_member_id=row["storage_member_id"], object_id=object_id,
            media_path=row["media_path"], path_locator=path_locator(row["media_path"]), object_kind=row["object_kind"],
            size_bytes=actual_size, etag=etag[:128], state="active", created_at=now, updated_at=now)
        await conn.execute(insert(s.global_media).values(**values))
        await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == row["storage_member_id"]).values(
            reserved_bytes=s.storage_members.c.reserved_bytes - row["expected_bytes"],
            used_bytes=s.storage_members.c.used_bytes + actual_size, updated_at=now))
        await conn.execute(update(s.upload_sessions).where(s.upload_sessions.c.upload_id == upload_id).values(
            state="complete", path_locator=None, updated_at=now))
    return values


async def fail_upload(upload_id: str, database=engine) -> None:
    now = int(time.time())
    async with database.begin() as conn:
        row = (await conn.execute(select(s.upload_sessions).where(
            s.upload_sessions.c.upload_id == upload_id).with_for_update())).mappings().first()
        if not row or row["state"] != "reserved":
            return
        await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == row["storage_member_id"]).values(
            reserved_bytes=func.greatest(0, s.storage_members.c.reserved_bytes - row["expected_bytes"]), updated_at=now))
        await conn.execute(delete(s.upload_sessions).where(s.upload_sessions.c.upload_id == upload_id))


async def media(identifier: str, database=engine) -> dict:
    if not p.OBJECT_ID.fullmatch(identifier):
        raise p.ProtocolError("Invalid media identity")
    async with database.connect() as conn:
        row = (await conn.execute(select(s.global_media).where(
            s.global_media.c.media_id == identifier, s.global_media.c.state.in_(("active", "pending_delete"))))).mappings().first()
    if not row:
        raise p.ProtocolError("Media object not found")
    return dict(row)


async def list_media(database=engine) -> list[dict]:
    async with database.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(s.global_media).where(
            s.global_media.c.state == "active").order_by(s.global_media.c.media_path))).mappings()]


async def mark_pending_delete(media_ids: list[str], database=engine) -> list[dict]:
    async with database.begin() as conn:
        rows = [dict(row) for row in (await conn.execute(select(s.global_media).where(
            s.global_media.c.media_id.in_(media_ids)).with_for_update())).mappings()]
        if rows:
            await conn.execute(update(s.global_media).where(s.global_media.c.media_id.in_(media_ids)).values(
                state="pending_delete", updated_at=int(time.time())))
        return rows


async def complete_delete(media_id: str, database=engine) -> None:
    now = int(time.time())
    async with database.begin() as conn:
        row = (await conn.execute(select(s.global_media).where(
            s.global_media.c.media_id == media_id).with_for_update())).mappings().first()
        if not row:
            return
        await conn.execute(update(s.storage_members).where(
            s.storage_members.c.member_id == row["storage_member_id"]).values(
            used_bytes=func.greatest(0, s.storage_members.c.used_bytes - row["size_bytes"]), updated_at=now))
        await conn.execute(delete(s.global_media).where(s.global_media.c.media_id == media_id))


async def has_valid_files(member_id: str, database=engine) -> bool:
    async with database.connect() as conn:
        return bool(await conn.scalar(select(func.count()).select_from(s.global_media).where(
            s.global_media.c.storage_member_id == member_id,
            s.global_media.c.state.in_(("active", "pending_delete")))))


async def retry_pending_deletes(store, limit: int = 20) -> int:
    if store.node["role"] != "Master":
        return 0
    from app.api.v1.media import MEDIA_ROOT
    async with store.database.connect() as conn:
        rows = [dict(row) for row in (await conn.execute(select(s.global_media).where(
            s.global_media.c.state == "pending_delete").limit(limit))).mappings()]
    completed = 0
    for row in rows:
        try:
            if row["storage_member_id"] == store.node["node_id"]:
                target = (MEDIA_ROOT / row["media_path"]).resolve()
                if MEDIA_ROOT not in target.parents:
                    continue
                target.unlink(missing_ok=True)
            else:
                async with store.database.connect() as conn:
                    member = (await conn.execute(select(s.storage_members).where(
                        s.storage_members.c.member_id == row["storage_member_id"]))).mappings().first()
                if not member or member["health"] != "online" or not member["relationship_id"]:
                    continue
                relation = await store.relationship(member["relationship_id"])
                token = p.storage_token(store.unseal(relation["credential"]), relation["relationship_id"],
                    store.node["node_id"], row["storage_member_id"], row["media_id"], row["object_id"],
                    "delete", row["media_path"], int(row["size_bytes"]), int(time.time()))
                async with httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False,
                                             timeout=httpx.Timeout(20, connect=8)) as client:
                    response = await client.post(relation["peer_endpoint"] +
                        f"/internal/v1/storage/{row['object_id']}/delete",
                        headers={"X-Storage-Capability": token})
                if response.status_code != 200:
                    continue
            async with store.database.begin() as conn:
                from sqlalchemy import text
                await conn.execute(text("DELETE FROM media_lyric_links WHERE media_id=:id"), {"id": row["media_id"]})
                await conn.execute(text("DELETE FROM media_playback_events WHERE media_id=:id"),
                                   {"id": row["media_id"]})
                await conn.execute(text("DELETE FROM media_playback_stats WHERE media_id=:id"),
                                   {"id": row["media_id"]})
                if row["storage_member_id"] == store.node["node_id"]:
                    await conn.execute(text("DELETE FROM media_objects WHERE media_id=:id"),
                                       {"id": row["object_id"]})
            await complete_delete(row["media_id"], store.database)
            completed += 1
        except Exception:
            continue
    return completed


async def adopt_master_local_media(node: dict, database=engine) -> None:
    """Idempotently register the Master's existing local media in the global catalog."""
    if node["role"] != "Master":
        return
    from app.api.v1.media import MEDIA_ROOT
    existing_bytes = local_filesystem_bytes(MEDIA_ROOT)
    async with database.begin() as conn:
        configured = await conn.scalar(select(s.storage_members.c.allocated_bytes).where(
            s.storage_members.c.member_id == node["node_id"]))
        await ensure_master_local(node, max(int(configured or 0), GIB, existing_bytes), conn=conn)
    discovered = []
    for root_name, object_kind, extensions in (
        ("music", "audio", {".mp3", ".m4a", ".flac", ".wav"}),
        ("vido", "video", {".mp4", ".webm", ".mkv"}),
    ):
        for target in (MEDIA_ROOT / root_name).rglob("*"):
            if not target.is_file() or target.is_symlink() or target.suffix.lower() not in extensions:
                continue
            relative = target.relative_to(MEDIA_ROOT)
            if len(relative.parts) not in (3, 4) or any(part.startswith(".") for part in relative.parts):
                continue
            discovered.append((relative.as_posix(), object_kind))
    await media_objects.ensure_objects(discovered, database)
    async with database.begin() as conn:
        from sqlalchemy import text
        local_rows = [dict(row) for row in (await conn.execute(text(
            "SELECT media_id, object_kind, media_path FROM media_objects "
            "WHERE object_kind IN ('audio','video')"))).mappings()]
        for item in local_rows:
            target = (MEDIA_ROOT / item["media_path"]).resolve()
            if not target.is_file() or MEDIA_ROOT not in target.parents:
                continue
            info = target.stat()
            values = dict(media_id=item["media_id"], storage_member_id=node["node_id"], object_id=item["media_id"],
                media_path=item["media_path"], path_locator=path_locator(item["media_path"]),
                object_kind=item["object_kind"], size_bytes=info.st_size,
                etag=f'"{int(info.st_mtime):x}-{info.st_size:x}"', state="active",
                created_at=int(info.st_ctime), updated_at=int(info.st_mtime))
            statement = mysql_insert(s.global_media).values(**values)
            await conn.execute(statement.on_duplicate_key_update(
                storage_member_id=statement.inserted.storage_member_id, object_id=statement.inserted.object_id,
                size_bytes=statement.inserted.size_bytes, etag=statement.inserted.etag,
                object_kind=statement.inserted.object_kind, state="active", updated_at=statement.inserted.updated_at))
    # Recompute used counters after adoption.
    async with database.begin() as conn:
        rows = (await conn.execute(select(s.global_media.c.storage_member_id,
            func.coalesce(func.sum(s.global_media.c.size_bytes), 0).label("used")).where(
            s.global_media.c.state.in_(("active", "pending_delete"))).group_by(s.global_media.c.storage_member_id))).mappings()
        totals = {row["storage_member_id"]: int(row["used"]) for row in rows}
        members = (await conn.execute(select(s.storage_members.c.member_id))).scalars()
        for member_id in members:
            await conn.execute(update(s.storage_members).where(s.storage_members.c.member_id == member_id).values(
                used_bytes=totals.get(member_id, 0), updated_at=int(time.time())))
