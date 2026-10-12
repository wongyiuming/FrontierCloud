"""Durable browser-encryption facts; the database never stores a media key."""
from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()

encryption = Table(
    "media_encryption", metadata,
    Column("object_kind", String(16), primary_key=True),
    Column("object_id", String(64), primary_key=True),
    Column("file_id", String(32), nullable=False),
    Column("descriptor_json", Text, nullable=True),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("file_id", name="uq_media_encryption_file_id"),
)

keys = Table(
    "media_crypto_keys", metadata,
    Column("singleton", Integer, primary_key=True),
    Column("key_id", String(64), nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
