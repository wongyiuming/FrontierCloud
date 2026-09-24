"""Additive tables, isolated from local media/deletion transactions."""
from sqlalchemy import (BigInteger, Column, Integer, JSON, LargeBinary, MetaData,
                        String, Table, Text, UniqueConstraint, Index)
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable, CreateIndex

metadata = MetaData()

identity = Table("node_identity", metadata,
    Column("singleton", Integer, primary_key=True),
    Column("node_id", String(32), nullable=False),
    Column("role", String(16), nullable=False),
    Column("endpoint", String(512), nullable=False),
    Column("private_key", Text, nullable=False),
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

# Resource-pool facts. The Master-owned global catalog is the only catalog.
storage_members = Table("cluster_storage_members", metadata,
    Column("member_id", String(32), primary_key=True),
    Column("relationship_id", String(32)),
    Column("member_kind", String(16), nullable=False),
    Column("transport", String(8), nullable=False),
    Column("storage_enabled", Integer, nullable=False),
    Column("allocated_bytes", BigInteger, nullable=False),
    Column("used_bytes", BigInteger, nullable=False),
    Column("reserved_bytes", BigInteger, nullable=False),
    Column("physical_free_bytes", BigInteger, nullable=False),
    Column("health", String(16), nullable=False),
    Column("writable", Integer, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    UniqueConstraint("relationship_id", name="uq_storage_member_relationship"))
Index("idx_storage_member_health", storage_members.c.storage_enabled,
      storage_members.c.health, storage_members.c.writable)

global_media = Table("global_media_objects", metadata,
    Column("media_id", String(64), primary_key=True),
    Column("storage_member_id", String(32), nullable=False),
    Column("object_id", String(64), nullable=False),
    Column("media_path", String(1024), nullable=False),
    Column("path_locator", String(64), nullable=False),
    Column("object_kind", String(16), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("etag", String(128), nullable=False),
    Column("state", String(24), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    UniqueConstraint("path_locator", name="uq_global_media_path"),
    UniqueConstraint("storage_member_id", "object_id", name="uq_global_media_placement"))
Index("idx_global_media_member_state", global_media.c.storage_member_id, global_media.c.state)
Index("idx_global_media_path", global_media.c.media_path, mysql_length=191)

upload_sessions = Table("cluster_upload_sessions", metadata,
    Column("upload_id", String(32), primary_key=True),
    Column("storage_member_id", String(32), nullable=False),
    Column("media_id", String(64), nullable=False),
    Column("media_path", String(1024), nullable=False),
    Column("path_locator", String(64)),
    Column("object_kind", String(16), nullable=False),
    Column("expected_bytes", BigInteger, nullable=False),
    Column("state", String(24), nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    UniqueConstraint("path_locator", name="uq_cluster_upload_path"))
Index("idx_cluster_upload_expiry", upload_sessions.c.state, upload_sessions.c.expires_at)

compute_members = Table("cluster_compute_members", metadata,
    Column("member_id", String(32), primary_key=True),
    Column("enabled", Integer, nullable=False),
    Column("worker_slots", Integer, nullable=False),
    Column("available_slots", Integer, nullable=False),
    Column("cpu_percent", Integer, nullable=False),
    Column("memory_available_bytes", BigInteger, nullable=False),
    Column("capabilities", JSON, nullable=False),
    Column("updated_at", BigInteger, nullable=False))

worker_jobs = Table("cluster_worker_jobs", metadata,
    Column("job_id", String(32), primary_key=True),
    Column("idempotency_key", String(64), nullable=False),
    Column("job_type", String(48), nullable=False),
    Column("media_id", String(64)),
    Column("member_id", String(32)),
    Column("payload", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("state", String(24), nullable=False),
    Column("lease_token_hash", String(64)),
    Column("lease_expires_at", BigInteger, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    UniqueConstraint("idempotency_key", name="uq_cluster_worker_idempotency"))
Index("idx_cluster_worker_schedule", worker_jobs.c.state, worker_jobs.c.lease_expires_at)

backup_members = Table("cluster_backup_members", metadata,
    Column("member_id", String(32), primary_key=True),
    Column("enabled", Integer, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("last_success", BigInteger, nullable=False),
    Column("lag_seconds", BigInteger, nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("state", String(24), nullable=False),
    Column("updated_at", BigInteger, nullable=False))

business_backups = Table("cluster_business_backups", metadata,
    Column("master_id", String(32), primary_key=True),
    Column("generation", BigInteger, primary_key=True),
    Column("checksum", String(64), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("chunk_count", Integer, nullable=False),
    Column("state", String(24), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False))
Index("idx_business_backup_latest", business_backups.c.master_id,
      business_backups.c.state, business_backups.c.generation)

business_backup_chunks = Table("cluster_business_backup_chunks", metadata,
    Column("master_id", String(32), primary_key=True),
    Column("generation", BigInteger, primary_key=True),
    Column("chunk_index", Integer, primary_key=True),
    Column("payload", LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"), nullable=False),
    Column("created_at", BigInteger, nullable=False))


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
