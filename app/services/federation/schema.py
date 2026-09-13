"""Additive tables, isolated from local media/deletion transactions."""
from sqlalchemy import (BigInteger, Column, Integer, JSON, MetaData, String,
                        Table, Text, UniqueConstraint, Index)
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable, CreateIndex

metadata = MetaData()

identity = Table("node_identity", metadata,
    Column("singleton", Integer, primary_key=True),
    Column("node_id", String(32), nullable=False),
    Column("role", String(16), nullable=False),
    Column("endpoint", String(512), nullable=False),
    Column("private_key", Text, nullable=False),
    Column("catalog_version", BigInteger, nullable=False),
    Column("created_at", BigInteger, nullable=False))

relationships = Table("node_relationships", metadata,
    Column("relationship_id", String(32), primary_key=True),
    Column("peer_id", String(32), nullable=False),
    Column("peer_endpoint", String(512), nullable=False),
    Column("peer_key", String(64), nullable=False),
    Column("credential", Text, nullable=False),
    Column("direction", String(16), nullable=False),
    Column("mode", String(8), nullable=False),
    Column("state", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("last_heartbeat", BigInteger, nullable=False),
    Column("rtt_ms", Integer, nullable=False),
    Column("failures", Integer, nullable=False),
    Column("recoveries", Integer, nullable=False),
    Column("cursor", BigInteger, nullable=False),
    Column("peer_version", String(64), nullable=False),
    Column("protocol", Integer, nullable=False),
    Column("summary", JSON, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("peer_id", name="uq_node_relationship_peer"))

pairs = Table("node_pair_packages", metadata,
    Column("nonce", String(32), primary_key=True),
    Column("token_hash", String(64), nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("state", String(16), nullable=False),
    Column("relationship_id", String(32)),
    Column("master_id", String(32)))

requests = Table("node_request_nonces", metadata,
    Column("relationship_id", String(32), primary_key=True),
    Column("nonce", String(32), primary_key=True),
    Column("expires_at", BigInteger, nullable=False))
Index("idx_node_nonce_expiry", requests.c.expires_at)

audit = Table("node_audit", metadata,
    Column("audit_id", String(32), primary_key=True),
    Column("action", String(48), nullable=False),
    Column("relationship_id", String(32)),
    Column("actor", String(128), nullable=False),
    Column("detail", JSON, nullable=False),
    Column("created_at", BigInteger, nullable=False))
Index("idx_node_audit_time", audit.c.created_at)

exports = Table("node_media_exports", metadata,
    Column("object_id", String(64), primary_key=True),
    Column("path", String(1024), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("deleted", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("payload", JSON, nullable=False))
Index("idx_node_export_version", exports.c.version)

catalog = Table("node_media_catalog", metadata,
    Column("resource_id", String(64), primary_key=True),
    Column("owner_id", String(32), nullable=False),
    Column("object_id", String(64), nullable=False),
    Column("relationship_id", String(32), nullable=False),
    Column("path", String(1024), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("deleted", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("owner_id", "object_id", name="uq_node_catalog_identity"))
Index("idx_node_catalog_relationship", catalog.c.relationship_id)
Index("idx_node_catalog_path", catalog.c.path, mysql_length=191)

stats = Table("node_playback_stats", metadata,
    Column("resource_id", String(64), primary_key=True),
    Column("play_score", BigInteger, nullable=False),
    Column("preference", Integer, nullable=False),
    Column("updated_at", BigInteger, nullable=False))
events = Table("node_playback_events", metadata,
    Column("session_id", String(36), primary_key=True),
    Column("resource_id", String(64), primary_key=True),
    Column("expires_at", BigInteger, nullable=False))
Index("idx_node_playback_expiry", events.c.expires_at)


def migration_statements():
    # MySQL DDL commits individually under the existing schema migration lock.
    for table in metadata.sorted_tables:
        table.dialect_options["mysql"].update(engine="InnoDB", charset="utf8mb4", collate="utf8mb4_bin")
        statement = str(CreateTable(table, if_not_exists=True).compile(dialect=mysql.dialect()))
        position = statement.rfind(")")
        indexes = ""
        for index in sorted(table.indexes, key=lambda item: item.name):
            statement_index = str(CreateIndex(index).compile(dialect=mysql.dialect()))
            columns = statement_index[statement_index.index("("):]
            indexes += f",\n INDEX {index.name} {columns}"
        yield statement[:position] + indexes + "\n" + statement[position:]
