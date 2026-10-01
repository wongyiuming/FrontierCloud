"""Connection and transaction policy; dialect selection stays in the store layer."""
from pathlib import Path
from contextlib import asynccontextmanager

from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings


def create_database_engine(configuration: Settings) -> AsyncEngine:
    if configuration.DB_TYPE == "mysql":
        return create_async_engine(
            configuration.MYSQL_URL, pool_pre_ping=True, pool_recycle=1800,
            pool_size=5, max_overflow=10,
        )

    path = Path(configuration.SQLITE_PATH).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(path)),
        connect_args={"timeout": 5}, pool_size=4, max_overflow=0,
    )

    @event.listens_for(database.sync_engine, "connect")
    def configure_connection(connection, _record):
        # SQLAlchemy owns BEGIN, including transactional DDL and savepoints.
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    @event.listens_for(database.sync_engine, "begin")
    def begin_transaction(connection):
        statement = "BEGIN IMMEDIATE" if connection.get_execution_options().get("sqlite_write") else "BEGIN"
        connection.exec_driver_sql(statement)

    return database


@asynccontextmanager
async def write_transaction(database):
    """Reserve a SQLite writer before reading data that will be mutated."""
    if getattr(getattr(database, "dialect", None), "name", "mysql") != "sqlite":
        async with database.begin() as connection:
            yield connection
        return
    async with database.connect() as connection:
        connection = await connection.execution_options(sqlite_write=True)
        async with connection.begin():
            yield connection
