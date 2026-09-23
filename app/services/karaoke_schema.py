"""MySQL schema for Karaoke identities and user-owned recordings."""
from sqlalchemy import BigInteger, Column, Index, Integer, JSON, MetaData, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

metadata = MetaData()

users = Table(
    "karaoke_users", metadata,
    Column("user_id", String(32), primary_key=True),
    Column("username", String(64), nullable=False),
    Column("username_key", String(128), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("quota_bytes", BigInteger, nullable=False),
    Column("used_bytes", BigInteger, nullable=False),
    Column("storage_relationship_id", String(32)),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    UniqueConstraint("username_key", name="uq_karaoke_user_name"),
)
Index("idx_karaoke_users_status", users.c.status, users.c.created_at)

recordings = Table(
    "karaoke_recordings", metadata,
    Column("recording_id", String(32), primary_key=True),
    Column("user_id", String(32), nullable=False),
    Column("storage_relationship_id", String(32), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("content_type", String(96), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", String(64)),
    Column("state", String(16), nullable=False),
    Column("title", String(255), nullable=False),
    Column("lyrics", JSON, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)
Index("idx_karaoke_recording_user_time", recordings.c.user_id, recordings.c.created_at)
Index("idx_karaoke_recording_storage", recordings.c.storage_relationship_id, recordings.c.state)

audit = Table(
    "karaoke_audit_log", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("user_id", String(32)),
    Column("action", String(48), nullable=False),
    Column("result", String(24), nullable=False),
    Column("client_ip", String(45), nullable=False),
    Column("webrtc_addresses", JSON, nullable=False),
    Column("detail", JSON, nullable=False),
    Column("request_id", String(128)),
    Column("trace_id", String(32)),
    Column("created_at", BigInteger, nullable=False),
)
Index("idx_karaoke_audit_ip_time", audit.c.client_ip, audit.c.created_at)
Index("idx_karaoke_audit_user_time", audit.c.user_id, audit.c.created_at)

registration_daily = Table(
    "karaoke_registration_daily", metadata,
    Column("client_ip", String(45), primary_key=True),
    Column("day_key", String(8), primary_key=True),
    Column("failure_count", Integer, nullable=False),
    Column("success_count", Integer, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)


def migration_statements():
    for table in metadata.sorted_tables:
        table.dialect_options["mysql"].update(engine="InnoDB", charset="utf8mb4", collate="utf8mb4_bin")
        statement = str(CreateTable(table, if_not_exists=True).compile(dialect=mysql.dialect()))
        position = statement.rfind(")")
        indexes = ""
        for index in sorted(table.indexes, key=lambda item: item.name):
            compiled = str(CreateIndex(index).compile(dialect=mysql.dialect()))
            indexes += f",\n INDEX {index.name} {compiled[compiled.index('('):]}"
        yield statement[:position] + indexes + "\n" + statement[position:]
