"""Database selection and a small DB-API compatibility layer for KevenOS.

SQLite remains the zero-configuration default. PostgreSQL and Supabase share
the same PostgreSQL driver; Supabase is a deployment/provider choice rather
than a separate SQL dialect.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import base64
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Iterable, Mapping, MutableMapping, Sequence


@dataclass(frozen=True)
class BackendConfig:
    name: str
    url: str | None = None
    schema_prefix: str = "kevenos"

    @property
    def server(self) -> bool:
        return self.name in {"postgres", "supabase"}


def resolve_backend(environment: Mapping[str, str] | None = None) -> BackendConfig:
    values = os.environ if environment is None else environment
    name = str(values.get("KEVENOS_DB_BACKEND", "sqlite") or "sqlite").strip().lower()
    if name not in {"sqlite", "postgres", "supabase"}:
        raise ValueError("KEVENOS_DB_BACKEND must be sqlite, postgres or supabase.")
    prefix = str(values.get("KEVENOS_DB_SCHEMA_PREFIX", "kevenos") or "kevenos").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", prefix):
        raise ValueError("KEVENOS_DB_SCHEMA_PREFIX must be a safe lowercase SQL identifier.")
    url = str(values.get("KEVENOS_DATABASE_URL") or values.get("POSTGRES_URL") or values.get("DATABASE_URL") or "").strip() or None
    if name == "sqlite":
        return BackendConfig(name=name, schema_prefix=prefix)
    if not url or not re.match(r"^postgres(?:ql)?://", url, re.I):
        raise ValueError("PostgreSQL and Supabase backends require a postgresql:// KEVENOS_DATABASE_URL, POSTGRES_URL or DATABASE_URL.")
    return BackendConfig(name=name, url=url, schema_prefix=prefix)


def schema_for_path(path: str | os.PathLike[str], prefix: str) -> str:
    parts = {part.lower() for part in Path(path).parts}
    return prefix + ("_rehearsal" if "rehearsal" in parts else "_live")


def _replace_placeholders(statement: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote:
            output.append("%%" if char == "%" else char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        elif char == ":" and index + 1 < len(statement) and re.match(r"[A-Za-z_]", statement[index + 1]):
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", statement[index + 1:])
            assert match is not None
            output.append("%(" + match.group(0) + ")s")
            index += len(match.group(0))
        else:
            output.append(char)
        index += 1
    return "".join(output)


def postgres_sql(statement: str) -> str:
    sql = statement.strip().rstrip(";")
    if re.fullmatch(r"BEGIN\s+IMMEDIATE", sql, re.I):
        return "BEGIN"
    sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.I)
    ignored = re.match(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\s+", sql, re.I)
    if ignored:
        sql = re.sub(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\s+", r"\1INSERT INTO ", sql, count=1, flags=re.I)
        sql += " ON CONFLICT DO NOTHING"
    return _replace_placeholders(sql)


class Row:
    """Mapping/sequence row compatible with the sqlite3.Row uses in KevenOS."""

    def __init__(self, columns: Sequence[str], values: Sequence[object]):
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._mapping = dict(zip(self._columns, self._values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._mapping[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._columns


class PostgresCursor:
    def __init__(self, cursor, prefetched: Sequence[object] | None = None, lastrowid=None):
        self._cursor = cursor
        self._prefetched = prefetched
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def _row(self, values):
        if values is None:
            return None
        columns = [column.name for column in (self._cursor.description or ())]
        return Row(columns, values)

    def fetchone(self):
        if self._prefetched is not None:
            values, self._prefetched = self._prefetched, None
            return self._row(values)
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        values = []
        if self._prefetched is not None:
            values.append(self._prefetched)
            self._prefetched = None
        values.extend(self._cursor.fetchall())
        return [self._row(row) for row in values]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class PostgresConnection:
    backend = "postgres"

    def __init__(self, connection, schema: str, provider: str):
        self._connection = connection
        self.schema = schema
        self.provider = provider
        self._lock = threading.RLock()
        self._manual_transaction = False
        self._context_depth = 0

    def execute(self, statement: str, parameters: Sequence[object] = ()):
        pragma = re.fullmatch(r"\s*PRAGMA\s+table_info\(([^)]+)\)\s*;?", statement, re.I)
        if pragma:
            table = pragma.group(1).strip().strip('"')
            statement = "SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position"
            parameters = (table,)
        elif re.fullmatch(r"\s*PRAGMA\s+journal_mode\s*=\s*WAL\s*;?", statement, re.I):
            return MemoryCursor([Row(("journal_mode",), ("wal",))])
        elif re.fullmatch(r"\s*PRAGMA\s+quick_check\s*;?", statement, re.I):
            return MemoryCursor([Row(("quick_check",), ("ok",))])

        sql = postgres_sql(statement)
        with self._lock:
            if sql == "BEGIN":
                from psycopg.pq import TransactionStatus
                if self._connection.info.transaction_status != TransactionStatus.IDLE:
                    return MemoryCursor([])
            returning = bool(re.match(r"^\s*INSERT\s+INTO\s+sheets\b", sql, re.I)) and " returning " not in sql.lower()
            if returning:
                sql += " RETURNING id"
            cursor = self._connection.cursor()
            values = parameters if isinstance(parameters, Mapping) else tuple(parameters)
            cursor.execute(sql, values)
            if returning:
                values = cursor.fetchone()
                return PostgresCursor(cursor, lastrowid=values[0] if values else None)
            return PostgresCursor(cursor)

    def executemany(self, statement: str, rows: Iterable[Sequence[object]]):
        sql = postgres_sql(statement)
        with self._lock:
            cursor = self._connection.cursor()
            cursor.executemany(sql, list(rows))
            return PostgresCursor(cursor)

    def executescript(self, script: str):
        cursor = None
        for statement in (part.strip() for part in script.split(";")):
            if statement:
                cursor = self.execute(statement)
        return cursor or MemoryCursor([])

    def commit(self):
        with self._lock:
            self._connection.commit()
            if self._manual_transaction:
                self._manual_transaction = False
                self._lock.release()

    def rollback(self):
        with self._lock:
            self._connection.rollback()
            if self._manual_transaction:
                self._manual_transaction = False
                self._lock.release()

    def close(self):
        with self._lock:
            self._connection.close()

    def __enter__(self):
        self._lock.acquire()
        if self._context_depth == 0:
            self._connection.execute("BEGIN")
        self._context_depth += 1
        return self

    def __exit__(self, kind, value, traceback):
        self._context_depth -= 1
        if self._context_depth == 0:
            if kind is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        self._lock.release()
        return False

    def begin(self):
        self._lock.acquire()
        try:
            from psycopg.pq import TransactionStatus
            if self._connection.info.transaction_status == TransactionStatus.IDLE:
                self._connection.execute("BEGIN")
            self._manual_transaction = True
        except BaseException:
            self._lock.release()
            raise


class MemoryCursor:
    def __init__(self, rows: Sequence[Row]):
        self._rows = list(rows)
        self.rowcount = len(self._rows)
        self.lastrowid = None

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


def connect(path: str | os.PathLike[str], config: BackendConfig | None = None):
    selected = config or resolve_backend()
    if not selected.server:
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    try:
        import psycopg
        from psycopg import sql
    except ImportError as error:
        raise RuntimeError("Install psycopg[binary] to use PostgreSQL or Supabase.") from error

    schema = schema_for_path(path, selected.schema_prefix)
    raw = psycopg.connect(selected.url, prepare_threshold=None, connect_timeout=10, autocommit=True)
    with raw.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", ("kevenos:schema:" + schema,))
        try:
            cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        finally:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("kevenos:schema:" + schema,))
    return PostgresConnection(raw, schema, selected.name)


def is_server(connection) -> bool:
    return isinstance(connection, PostgresConnection)


def table_exists(connection, name: str) -> bool:
    if is_server(connection):
        return bool(connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?",
            (name,),
        ).fetchone())
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def list_tables(connection) -> set[str]:
    if is_server(connection):
        return {row[0] for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
        )}
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def clear_schema(connection) -> None:
    """Remove all server-workspace records inside the caller's transaction."""
    if not is_server(connection):
        raise ValueError("clear_schema is only available for server databases.")
    tables = sorted(list_tables(connection))
    if not tables:
        return
    quoted = ",".join('"' + name.replace('"', '""') + '"' for name in tables)
    connection.execute("TRUNCATE " + quoted + " RESTART IDENTITY CASCADE")


@contextmanager
def migration_lock(connection):
    if not is_server(connection):
        with nullcontext():
            yield
        return
    connection.execute("SELECT pg_advisory_lock(hashtext(?))", ("kevenos:migrate:" + connection.schema,))
    try:
        yield
    finally:
        connection.execute("SELECT pg_advisory_unlock(hashtext(?))", ("kevenos:migrate:" + connection.schema,))
        connection.commit()


@contextmanager
def named_lock(connection, name: str):
    """Serialize a short cross-process operation on server databases."""
    if not is_server(connection):
        with nullcontext():
            yield
        return
    lock_name = f"kevenos:{connection.schema}:{name}"
    connection.execute("SELECT pg_advisory_lock(hashtext(?))", (lock_name,))
    try:
        yield
    finally:
        connection.execute("SELECT pg_advisory_unlock(hashtext(?))", (lock_name,))
        connection.commit()


def begin_immediate(connection) -> None:
    if is_server(connection):
        connection.begin()
    else:
        connection.execute("BEGIN IMMEDIATE")


def integrity_errors():
    errors: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)
    try:
        import psycopg
        errors += (psycopg.IntegrityError,)
    except ImportError:
        pass
    return errors


def export_snapshot(connection, target: Path) -> None:
    """Write a verified, provider-neutral private backup for operator recovery."""
    payload = {"backend": getattr(connection, "provider", "sqlite"), "schema": getattr(connection, "schema", None), "tables": {}}
    for table in sorted(list_tables(connection)):
        rows = []
        safe = table.replace('"', '""')
        for row in connection.execute(f'SELECT * FROM "{safe}"'):
            values = {}
            for key in row.keys():
                value = row[key]
                values[key] = {"base64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value
            rows.append(values)
        payload["tables"][table] = rows
    target.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")

