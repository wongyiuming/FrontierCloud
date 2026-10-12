"""SQLite execution boundary for existing, parameter-bound application SQL.

Only the small MySQL DML vocabulary used by FrontierCloud is normalized. Quoted
strings, identifiers and comments are preserved; bound values never enter this
function. Schema DDL has its own static SQLite implementation. MySQL execution
does not use this adapter.
"""
from __future__ import annotations

import re
import sqlite3
import ipaddress
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


_PROTECTED = re.compile(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`|--[^\n]*|/\*.*?\*/", re.S)
_PATH_COLUMN = r"(?<![a-z_0-9])(?:[a-z_][a-z_0-9]*\.)?(?:media_path|relative_path|lyric_path)"
_LIKE_ATOM = r"(?:\?(?:[0-9]+)?|:[a-z_][a-z_0-9]*|__fc_literal_[0-9]+__)"
_LIKE_VALUE = rf"(?:{_LIKE_ATOM}(?:\s*\|\|\s*{_LIKE_ATOM})*|\(\s*{_LIKE_ATOM}(?:\s*\|\|\s*{_LIKE_ATOM})*\s*\))"
_PATH_LIKE = re.compile(rf"(?P<column>{_PATH_COLUMN})\s+(?P<negation>NOT\s+)?LIKE\s+(?P<value>{_LIKE_VALUE})(?:\s+ESCAPE\s+(?P<escape>{_LIKE_ATOM}))?", re.I)


def _path_glob(pattern, escape=None):
    """Translate LIKE wildcards; SQLite itself performs case-sensitive matching."""
    if pattern is None:
        return None
    pattern = str(pattern)
    if escape is not None and len(str(escape)) != 1:
        raise ValueError("ESCAPE expression must be a single character")
    escape = str(escape) if escape is not None else None
    literal = {"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]"}
    translated, index = [], 0
    while index < len(pattern):
        character = pattern[index]
        if escape is not None and character == escape:
            index += 1
            if index == len(pattern):
                # SQLite LIKE with a trailing escape never matches.
                return "[]"
            character = pattern[index]
            translated.append(literal.get(character, character))
        elif character == "%":
            translated.append("*")
        elif character == "_":
            translated.append("?")
        else:
            translated.append(literal.get(character, character))
        index += 1
    return "".join(translated)


def normalize_statement(statement: str) -> str:
    protected: list[str] = []

    def protect(match):
        protected.append(match.group())
        return f"__fc_literal_{len(protected) - 1}__"

    sql = _PROTECTED.sub(protect, statement)
    # Core startswith emits LIKE (... || '%') ESCAPE '/'. Raw managed path
    # predicates use bound LIKE + ESCAPE '!'. Preserve exact filesystem identity
    # without changing LIKE on usernames/search or using a deprecated PRAGMA.
    sql = _PATH_LIKE.sub(lambda match: (
        f"{match['column']} {'NOT ' if match['negation'] else ''}GLOB "
        f"fc_path_glob({match['value']}, {match['escape'] or 'NULL'})"
    ), sql)
    if re.search(rf"{_PATH_COLUMN}\s+(?:NOT\s+)?LIKE\b", sql, re.I):
        raise ValueError("Unsupported managed-path LIKE expression")
    ignored_insert = bool(re.match(r"\s*INSERT\s+IGNORE\s+INTO\b", sql, re.I))
    sql = re.sub(r"\bINSERT\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.I)
    if re.search(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", sql, re.I):
        sql = re.sub(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", "ON CONFLICT DO UPDATE SET", sql, flags=re.I)
        sql = re.sub(r"\bVALUES\s*\(\s*([a-z_][a-z_0-9]*)\s*\)", r"excluded.\1", sql, flags=re.I)
    sql = re.sub(r"\s+FOR\s+(?:UPDATE|SHARE)\s*(?:SKIP\s+LOCKED\s*)?$", "", sql, flags=re.I)
    sql = re.sub(r"\bBINARY\s+", "", sql, flags=re.I)
    sql = re.sub(r"\bLEFT\s*\(", "fc_left(", sql, flags=re.I)
    sql = re.sub(r"\bCHAR_LENGTH\s*\(", "length(", sql, flags=re.I)
    sql = re.sub(r"\bIF\s*\(", "fc_if(", sql, flags=re.I)
    # SQLite loses declared column types through MIN/MAX. These are the two
    # typed datetime aggregate shapes used by WebRTC and IP-security services.
    sql = re.sub(r"\b(MIN|MAX)\((first_seen|last_seen)\)\s+AS\s+\2\b", lambda match: f'{match[1]}({match[2]}) AS "{match[2]} [TIMESTAMP]"', sql, flags=re.I)
    sql = re.sub(r"\bSELECT\s+MIN\(created_at\)\s+FROM\b", 'SELECT MIN(created_at) AS "minimum_created_at [TIMESTAMP]" FROM', sql, flags=re.I)
    bounded_delete = re.fullmatch(r"\s*DELETE\s+FROM\s+([a-z_][a-z_0-9]*)\s+WHERE\s+(.+?)\s+LIMIT\s+(\d+)\s*", sql, re.I | re.S)
    if bounded_delete:
        table, predicate, limit = bounded_delete.groups()
        sql = f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {predicate} LIMIT {limit})"
    if ignored_insert:
        # Unlike INSERT OR IGNORE, preserve CHECK/NOT NULL errors. Only a unique
        # identity collision is an ignored registration or counted playback event.
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return re.sub(r"__fc_literal_(\d+)__", lambda match: protected[int(match[1])], sql)


def _timestamp(value: bytes) -> datetime:
    return datetime.fromisoformat(value.decode("ascii"))


def _inet6_aton(value):
    # MySQL's binary network-byte ordering, including decimal padded IPv4.
    # Zone IDs/network masks/short IPv4 forms are explicitly invalid there.
    if not isinstance(value, str) or value != value.strip() or "%" in value or "/" in value:
        return None
    if "." in value:
        tail = value.rsplit(":", 1)[-1]
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", tail) or any(len(part) > 16 for part in tail.split(".")):
            return None
        normalized = ".".join(str(int(part)) for part in tail.split("."))
        value = value[:-len(tail)] + normalized
    try:
        return ipaddress.ip_address(value).packed
    except ValueError:
        return None


class _SQLiteEngine(AsyncEngine):
    @asynccontextmanager
    async def begin(self):
        # Explicit business write transactions acquire the SQLite writer lock
        # before their first read. Ordinary connect() readers stay read-only
        # snapshots and cannot block a nested independent lookup or WAL writer.
        async with self.connect() as connection:
            await connection.execution_options(fc_write_transaction=True)
            async with connection.begin():
                yield connection


def create_sqlite_engine(url) -> AsyncEngine:
    sqlite3.register_converter("TIMESTAMP", _timestamp)
    sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
    base = create_async_engine(
        url, pool_pre_ping=True, pool_size=5, max_overflow=0,
        connect_args={"timeout": 30, "detect_types": sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES},
    )
    database = _SQLiteEngine(base.sync_engine)

    @event.listens_for(database.sync_engine, "connect")
    def configure_connection(connection, _record):
        # SQLAlchemy owns transaction boundaries. Only explicit write scopes
        # reserve a writer; WAL readers can continue during uploads/backup reads.
        connection.isolation_level = None
        cursor = connection.cursor()
        for pragma in ("PRAGMA busy_timeout=30000", "PRAGMA foreign_keys=ON", "PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL"):
            cursor.execute(pragma)
        cursor.close()
        connection.create_function("greatest", -1, lambda *values: None if None in values else max(values))
        connection.create_function("least", -1, lambda *values: None if None in values else min(values))
        connection.create_function("fc_left", 2, lambda value, count: None if value is None or count is None else str(value)[:max(0, int(count))])
        connection.create_function("fc_if", 3, lambda condition, yes, no: yes if condition else no)
        connection.create_function("INET6_ATON", 1, _inet6_aton, deterministic=True)
        connection.create_function("fc_path_glob", 2, _path_glob, deterministic=True)

    @event.listens_for(database.sync_engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE" if connection.get_execution_options().get("fc_write_transaction") else "BEGIN")

    @event.listens_for(database.sync_engine, "before_cursor_execute", retval=True)
    def adapt_statement(_connection, _cursor, statement, parameters, _context, _executemany):
        return normalize_statement(statement), parameters

    return database
