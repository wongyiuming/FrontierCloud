"""Cold database inspection/preparation; never initialize node or media keys.

The host operator retains the baseline before permitting schema changes. Only
schema generation/history may change in original tables. New tables are allowed;
old values, including retired compute history, must remain byte/type equivalent.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager, nullcontext
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time as clock

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine


class PreparationError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise PreparationError(code, message)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def private_bytes(path, maximum):
    path = Path(path).absolute()
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            fail("unsafe_input", "Offline inputs require real parent directories")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in {0, os.geteuid()} or info.st_size > maximum):
            fail("unsafe_input", "Offline inputs require bounded private regular files")
        return stream.read(maximum + 1)


def typed(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, (datetime, date, time)):
        return [type(value).__name__, value.isoformat()]
    fail("unsupported_database_value", "A database value has no lossless comparison encoding")


def quoted(value):
    return "`" + value.replace("`", "``") + "`"


# Historical compute rows are deliberately not lifecycle gates.
IDLE = {
    "media_delete_operations": "1=1",
    "cluster_upload_sessions": "state NOT IN ('complete','cancelled')",
    "global_media_objects": "state<>'active'",
    "karaoke_recordings": "state NOT IN ('ready','deleted')",
    "karaoke_users": "status NOT IN ('active','banned','deleted')",
    "cluster_storage_members": "reserved_bytes<>0",
    "cluster_business_backups": "state NOT IN ('ready','failed')",
    "migration_tasks": "state NOT IN ('completed','cancelled')",
    "migration_capacity": "1=1",
    "migration_scopes": "1=1",
    "migration_drains": "reason='migration'",
    "migration_peer_reservations": "state NOT IN ('aborted','released')",
    "migration_storage_journal": "state NOT IN ('published','deleted','aborted')",
    "s3_storage_members": "reserved_bytes<>0",
    "s3_upload_sessions": "state NOT IN ('published','aborted')",
    "s3_object_bindings": "state NOT IN ('active','deleted')",
}


async def unfinished_uploads(conn, tables, identity):
    """Recognize only the S3 release transaction's retained terminal history.

    S3 uses `aborted` in both journals and retains part audit rows. Generic
    uploads keep their original idle rule; missing or conflicting proof is
    never permission to release capacity or rewrite either journal here.
    """
    predicate = IDLE['cluster_upload_sessions']
    parameters = {}
    required = {'s3_storage_members','s3_upload_sessions','s3_upload_parts',
                's3_object_bindings','global_media_objects','media_encryption'}
    if identity['role'] == 'Master' and required <= set(tables):
        predicate += """ AND NOT EXISTS (
            SELECT 1 FROM s3_upload_sessions AS u
            WHERE c.state='aborted' AND c.path_locator IS NULL
              AND u.session_id=c.upload_id AND u.member_id=c.storage_member_id
              AND u.media_id=c.media_id AND u.media_path=c.media_path
              AND u.expected_bytes=c.expected_bytes AND u.expected_bytes>0
              AND u.master_id=:master AND u.state='aborted' AND u.error_code=''
              AND u.grant_until>=0 AND u.grant_until<=:now
              AND NOT EXISTS (SELECT 1 FROM s3_upload_parts AS p
                  WHERE p.session_id=u.session_id AND (p.grant_until>:now OR p.grant_until>u.grant_until))
              AND NOT EXISTS (SELECT 1 FROM s3_storage_members AS m
                  WHERE m.member_id=u.member_id AND m.master_id<>:master)
              AND NOT EXISTS (SELECT 1 FROM s3_object_bindings AS o
                  WHERE o.media_id=u.media_id OR o.session_id=u.session_id
                    OR (o.member_id=u.member_id AND o.object_id=u.object_id))
              AND NOT EXISTS (SELECT 1 FROM global_media_objects AS g
                  WHERE g.media_id=u.media_id OR (g.storage_member_id=u.member_id AND g.object_id=u.object_id))
              AND NOT EXISTS (SELECT 1 FROM media_encryption AS e
                  WHERE e.object_kind='media' AND e.object_id=u.media_id AND e.descriptor_json IS NOT NULL)
              AND (c.content_encrypted=0 OR (c.content_encrypted=1 AND EXISTS (
                  SELECT 1 FROM media_encryption AS e WHERE e.object_kind='media'
                    AND e.object_id=u.media_id AND e.descriptor_json IS NULL)))
        )"""
        parameters = {'master':identity['node_id'],'now':int(clock.time())}
    return bool(await conn.scalar(text('SELECT COUNT(*) FROM cluster_upload_sessions AS c WHERE '+predicate),parameters))


async def fingerprint(conn, table, columns, generation, original_tables):
    # Constant memory per streamed row; multiple accumulators retain duplicates
    # without depending on database collation or loading whole tables in RAM.
    statement = "SELECT " + ",".join(map(quoted, columns)) + " FROM " + quoted(table)
    if table == "frontiercloud_schema_migrations":
        statement += " WHERE generation <= " + str(generation)
    count = total = squares = xor = 0
    modulus = 1 << 256
    async with conn.stream(text(statement.replace(":", r"\:"))) as rows:
        async for row in rows:
            if table == "sqlite_sequence" and row[columns.index("name")] not in original_tables:
                continue
            digest = int(sha([typed(value) for value in row]), 16)
            count += 1
            total = (total + digest) % modulus
            squares = (squares + digest * digest) % modulus
            xor ^= digest
    return {"rows": count, "sum": f"{total:064x}", "squares": f"{squares:064x}", "xor": f"{xor:064x}"}


@asynccontextmanager
async def read_connection(engine):
    # Compose's --wait cannot prove SQL readiness when an old override has no
    # healthcheck. Retry only transport/startup failures, never authentication.
    deadline = clock.monotonic() + 45
    while True:
        try:
            connection = await engine.connect()
            break
        except OperationalError as error:
            code = error.orig.args[0] if getattr(error.orig, 'args', ()) else None
            if engine.dialect.name != 'mysql' or code not in {1040, 2002, 2003, 2006, 2013}:
                raise
            if clock.monotonic() >= deadline:
                fail('database_not_ready', 'MySQL did not become reachable within the bounded startup wait')
            await asyncio.sleep(0.5)
    try:
        yield connection
    finally:
        await connection.close()


async def inspect_database(engine, secrets, media_keys, *, baseline=None, include_tables=True, require_idle=True):
    """Read one consistent cold snapshot with bounded row buffers."""
    from app.core import schema_migrations as migrations
    from app.services.federation import protocol
    vault = private_bytes(Path(secrets) / "node-vault.key", 44)
    if len(vault) != 44:
        fail("identity_key_invalid", "Existing node vault is required; regeneration is forbidden")
    async with read_connection(engine) as conn:
        await conn.exec_driver_sql("BEGIN" if engine.dialect.name == "sqlite" else "START TRANSACTION READ ONLY")
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        if engine.dialect.name == "sqlite":
            internal = await conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_sequence' AND type='table'"))
            if internal:
                tables.append("sqlite_sequence")
        if not {"frontiercloud_schema", "node_identity"} <= set(tables):
            fail("database_uninitialized", "An existing marked database and node identity are required")
        markers = (await conn.exec_driver_sql("SELECT singleton,generation,created_at FROM frontiercloud_schema")).all()
        if len(markers) != 1 or markers[0][0] != 1 or type(markers[0][1]) is not int:
            fail("schema_marker_invalid", "Expected exactly one existing schema marker")
        generation = markers[0][1]
        if not 1 <= generation <= migrations.CURRENT_SCHEMA_GENERATION:
            fail("schema_generation_unsupported", "Database generation is outside the supported forward-only migration chain")
        await migrations.validate_migration_history(conn)
        if "frontiercloud_schema_migrations" in tables and await conn.scalar(text(
                "SELECT COUNT(*) FROM frontiercloud_schema_migrations WHERE generation>:generation"), {"generation": generation}):
            fail("migration_history_ahead", "Migration history is ahead of the committed schema marker")
        identities = (await conn.exec_driver_sql("SELECT singleton,node_id,role,endpoint,private_key,created_at FROM node_identity")).mappings().all()
        if len(identities) != 1 or identities[0]["singleton"] != 1 or identities[0]["role"] not in {"Standalone", "Master", "Follower"}:
            fail("node_identity_invalid", "Expected exactly one valid existing node identity")
        identity = identities[0]
        try:
            public = protocol.public_key(Fernet(vault).decrypt(identity["private_key"].encode()).decode())
        except (InvalidToken, ValueError, UnicodeError):
            fail('identity_key_invalid', 'Existing node vault cannot prove the stored private identity')
        keys = {"node_vault_sha256": hashlib.sha256(vault).hexdigest()}
        encrypted = 0
        if "media_encryption" in tables:
            encrypted = int(await conn.scalar(text("SELECT COUNT(*) FROM media_encryption WHERE descriptor_json IS NOT NULL")) or 0)
        key_ids = []
        if "media_crypto_keys" in tables:
            key_ids = (await conn.exec_driver_sql("SELECT singleton,key_id FROM media_crypto_keys")).all()
        premaster_path = Path(media_keys) / "media-premaster.key"
        if key_ids or encrypted or premaster_path.exists() or premaster_path.is_symlink():
            premaster = private_bytes(premaster_path, 32)
            key_id = hmac.digest(premaster, b"frontiercloud:premaster-id:v1", "sha256").hex()
            if len(premaster) != 32 or len(key_ids) != 1 or tuple(key_ids[0]) != (1, key_id):
                fail("media_key_invalid", "Existing premaster and database key identity must agree")
            keys["media_premaster_sha256"] = hashlib.sha256(premaster).hexdigest()
        for table, predicate in IDLE.items() if require_idle else ():
            if table not in tables:
                continue
            pending = (await unfinished_uploads(conn,tables,identity) if table == 'cluster_upload_sessions'
                else await conn.scalar(text("SELECT COUNT(*) FROM " + quoted(table) + " WHERE " + predicate)))
            if pending:
                fail("unfinished_transactions", "Unreconciled state requires normal recovery before upgrading: " + table)
        original_tables = set(baseline["tables"]) if baseline else set(tables)
        if not original_tables <= set(tables):
            fail("original_table_missing", "An original database table disappeared")
        facts = {}
        for table in sorted(original_tables) if include_tables else ():
            actual = await conn.run_sync(lambda c, table=table: [v["name"] for v in inspect(c).get_columns(table)])
            columns = baseline["tables"][table]["columns"] if baseline else actual
            if table == "frontiercloud_schema":
                columns = [name for name in columns if name != "generation"]
            if not set(columns) <= set(actual):
                fail("original_column_missing", "An original database column disappeared")
            facts[table] = {"columns": columns, **await fingerprint(conn, table, columns,
                baseline["generation"] if baseline else generation, original_tables)}
        result = {"version": 1, "database": engine.dialect.name, "generation": generation,
            "node_id": identity["node_id"], "role": identity["role"], "endpoint": identity["endpoint"],
            "public_key_sha256": hashlib.sha256(public.encode()).hexdigest(), "keys": keys, "tables": facts}
    if baseline:
        if generation < baseline["generation"] or any(result[name] != baseline[name] for name in result if name != "generation"):
            fail("original_state_changed", "Original database values, identity or keys changed; the fence and recovery material are retained")
    return result


def pending_files(root):
    """Observe recovery material; neither age nor an absent SQL row permits deletion."""
    from app.services import media_publication_recovery
    root = Path(root)
    media = root / 'media'
    if media_publication_recovery.pending(media):
        fail('unfinished_publication', 'Pending media publication must recover before upgrading')
    for base, maximum in ((media, 3), (root / 'recordings', 2)):
        if base.is_symlink():
            fail('unsafe_input', 'Managed payload roots cannot be symbolic links')
        if base.exists() and not base.is_dir():
            fail('unsafe_input', 'Managed payload roots must be real directories')
        if not base.exists():
            continue
        def visit(directory, depth):
            # Match the cleanup domain, including hidden directories, without
            # retaining a payload directory's complete filename list in RAM.
            with os.scandir(directory) as entries:
                for entry in entries:
                    if base == media and entry.name.startswith('.delete-'):
                        fail('unfinished_transactions', 'Unreconciled media quarantine requires normal recovery')
                    partial = entry.name.endswith('.part') and (base != media or entry.name.startswith(('.upload-', '.cluster-upload-')))
                    if partial:
                        fail('unfinished_transactions', 'Partial media or recording input requires normal recovery')
                    if depth < maximum and entry.is_dir(follow_symlinks=False):
                        visit(entry.path, depth + 1)
        visit(base, 0)


async def execute(options):
    from app.core import db
    from app.core.config import SECRET_DIR, settings
    from app.services import runtime_gate
    identity_only = options.command == 'identity-original'
    preflight = options.command in {'preflight','identity-original'}
    verification = options.command == 'verify'
    if not preflight:
        marker = private_bytes(runtime_gate.DATA_ROOT / ".frontiercloud-native-maintenance", 4096)
        if hashlib.sha256(marker).hexdigest() != options.fence_sha256:
            fail("maintenance_owner_changed", "The hard maintenance fence differs from this host operation")
    if not identity_only:
        pending_files(runtime_gate.DATA_ROOT)
    if settings.DB_TYPE == "sqlite":
        path = Path(settings.SQLITE_PATH).absolute()
        if runtime_gate.DATA_ROOT not in path.parents or not path.stat().st_size:
            fail("database_missing", "An existing database inside DATA_ROOT is required")
        # Validate the file and every parent before opening a URI in read-only
        # mode. The normal SQLite adapter intentionally configures WAL on open.
        if path.is_symlink() or any(not stat.S_ISDIR(parent.lstat().st_mode) for parent in path.parents):
            fail("database_path_unsafe", "Database paths cannot traverse symbolic links")
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_nlink != 1:
            fail("database_path_unsafe", "The existing database must be a regular singly linked file")
        readonly = create_async_engine(db.engine.url.set(database=path.as_uri(), query={"mode": "ro", "uri": "true"}))
    else:
        readonly = db.engine
    with nullcontext() if preflight or verification else runtime_gate.acquire(create=False):
        try:
            baseline = None
            if options.command in {'prepare', 'verify'}:
                raw = private_bytes(options.baseline, 16 * 1024 * 1024)
                if hashlib.sha256(raw).hexdigest() != options.baseline_sha256:
                    fail("baseline_changed", "Retained database baseline changed")
                baseline = json.loads(raw)
            before = await inspect_database(readonly, SECRET_DIR, runtime_gate.DATA_ROOT / "media-keys", baseline=baseline,
                include_tables=not preflight,require_idle=not identity_only)
            if options.node_id and before["node_id"] != options.node_id:
                fail("node_identity_changed", "Existing node identity does not match the host operation")
            if before['role'] != 'Standalone' and not settings.TLS_ENABLED:
                fail('configuration_role_mismatch', 'Existing fixed node roles require certificate-verified HTTPS')
            if settings.DEPLOYMENT_MODE == 'only_stroge' and (before['role'] != 'Follower' or before['endpoint'] != settings.STORAGE_ENDPOINT):
                fail('configuration_role_mismatch', 'Storage configuration must retain its existing Follower endpoint')
            if preflight:
                if not identity_only:
                    pending_files(runtime_gate.DATA_ROOT)
                return {**before,'phase':'original_identity_verified' if identity_only else 'preflight_passed','readonly':True}
            if options.command == "inspect":
                return before
            if verification:
                marker = private_bytes(runtime_gate.DATA_ROOT / '.frontiercloud-native-maintenance', 4096)
                if hashlib.sha256(marker).hexdigest() != options.fence_sha256:
                    fail('maintenance_owner_changed', 'Owned maintenance fence changed during live verification')
                if before['generation'] != db.SCHEMA_GENERATION:
                    fail('schema_prepare_incomplete', 'Live database does not match the candidate generation')
                return {'phase': 'fenced_verified', 'node_id': before['node_id'], 'generation': before['generation'],
                    'baseline_sha256': options.baseline_sha256, 'original_tables': len(before['tables']),
                    'original_state_preserved': True, 'business_opened': False}
            await db.init_db()
            after = await inspect_database(readonly, SECRET_DIR, runtime_gate.DATA_ROOT / "media-keys", baseline=baseline)
            if after["generation"] != db.SCHEMA_GENERATION:
                fail("schema_prepare_incomplete", "Database preparation did not reach the candidate generation")
            return {"phase": "database_prepared", "from_generation": baseline["generation"], "generation": after["generation"],
                "node_id": after["node_id"], "baseline_sha256": options.baseline_sha256,
                "original_tables": len(after["tables"]), "original_state_preserved": True, "business_started": False}
        finally:
            await readonly.dispose()
            if readonly is not db.engine:
                await db.engine.dispose()


def error_result(error):
    if isinstance(error, PreparationError):
        return {'error': error.code, 'detail': str(error)}
    from pydantic import ValidationError
    if isinstance(error, ValidationError):
        details = error.errors(include_input=False, include_context=False, include_url=False)
        return {'error': 'configuration_invalid', 'detail': '; '.join(
            '.'.join(map(str, row['loc'])) + ': ' + row['msg'] for row in details)[:4096]}
    cause = error
    for _ in range(8):
        if isinstance(cause, DBAPIError):
            code = cause.orig.args[0] if getattr(cause.orig, 'args', ()) else None
            code = code if type(code) is int else None
            kind = {1045: 'database_authentication_failed', 1044: 'database_access_denied',
                1049: 'database_missing', 1205: 'database_lock_timeout', 1213: 'database_deadlock',
                2002: 'database_unreachable', 2003: 'database_unreachable', 2006: 'database_disconnected',
                2013: 'database_disconnected'}.get(code, 'database_driver_failed')
            return {'error': kind, 'detail': f'Database operation failed (driver_code={code})'}
        if cause.__cause__ is None:
            break
        cause = cause.__cause__
    return {'error': 'database_prepare_failed', 'detail': type(error).__name__}


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight","identity-original","inspect","prepare","verify",'probe-fenced','probe-open','probe-restored'))
    parser.add_argument("--fence-sha256")
    parser.add_argument("--node-id")
    parser.add_argument("--baseline")
    parser.add_argument("--baseline-sha256")
    options = parser.parse_args(arguments)
    fence_required=options.command in {'inspect','prepare','verify','probe-fenced'}
    if ((options.node_id and not re.fullmatch("[a-f0-9]{32}", options.node_id))
            or (fence_required or options.fence_sha256 is not None) and not re.fullmatch("[a-f0-9]{64}", options.fence_sha256 or '')):
        parser.error("invalid internal node/fence binding")
    if options.command in {'prepare', 'verify','probe-fenced','probe-open','probe-restored'} and (not options.node_id or not options.baseline or not re.fullmatch("[a-f0-9]{64}", options.baseline_sha256 or "")):
        parser.error("prepare requires the retained host baseline and its hash")
    try:
        operation = execute
        if options.command in {'probe-fenced','probe-open','probe-restored'}:
            from app.services.operator_probe import execute as operation
        print(json.dumps(asyncio.run(operation(options)), sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(error_result(error)))
        return 1


if __name__ == "__main__":
    # Delegate through the importable module so a probe importing this helper
    # shares its exact error type instead of a second __main__ class.
    from app.services.offline_upgrade import main as module_main
    raise SystemExit(module_main())
