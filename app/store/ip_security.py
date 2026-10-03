"""Private SQL variants for durable security state; no dialect leaks on the wire."""
from datetime import datetime
from sqlalchemy import text


def is_sqlite(connection):
    return getattr(getattr(connection, "dialect", None), "name", "mysql") == "sqlite"


def locked_query(connection, statement):
    # SQLite callers hold BEGIN IMMEDIATE before authoritative reads.
    return text(statement.replace(" FOR UPDATE", "") if is_sqlite(connection) else statement)


async def lock_ip(connection, ip):
    conflict = ("ON CONFLICT(ip_address) DO UPDATE SET ip_address=excluded.ip_address"
                if is_sqlite(connection) else "ON DUPLICATE KEY UPDATE ip_address=VALUES(ip_address)")
    await connection.execute(text("INSERT INTO ip_security_locks (ip_address) VALUES (:ip) " + conflict), {"ip": ip})


async def increment_attack(connection, ip, now):
    conflict = ("""ON CONFLICT(ip_address) DO UPDATE SET attack_count=attack_count+1,
        last_attack_at=MAX(COALESCE(last_attack_at, excluded.last_attack_at), excluded.last_attack_at)"""
        if is_sqlite(connection) else """ON DUPLICATE KEY UPDATE attack_count=attack_count+1,
        last_attack_at=GREATEST(COALESCE(last_attack_at, VALUES(last_attack_at)), VALUES(last_attack_at))""")
    await connection.execute(text("""INSERT INTO ip_security_summary
        (ip_address, attack_count, last_attack_at) VALUES (:ip, 1, :now) """ + conflict), {"ip": ip, "now": now})


async def put_whitelist(connection, values):
    conflict = ("ON CONFLICT(ip_address) DO UPDATE SET note=excluded.note"
                if is_sqlite(connection) else "ON DUPLICATE KEY UPDATE note=:note")
    await connection.execute(text("""INSERT INTO ip_permanent_whitelist
        (ip_address, created_at, created_by_session_hash, note)
        VALUES (:ip, :now, :session, :note) """ + conflict), values)


def timestamp(value):
    # Textual SQL has no SQLAlchemy DATETIME result processor.
    return datetime.fromisoformat(value) if isinstance(value, str) else value
