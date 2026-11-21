import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class DatabaseBackendConfigurationTests(unittest.TestCase):
    def resolve(self, **values):
        from database import resolve_backend

        environment = {}
        environment.update(values)
        return resolve_backend(environment)

    def test_sqlite_is_the_default_and_needs_no_server_url(self):
        config = self.resolve()
        self.assertEqual(config.name, "sqlite")
        self.assertIsNone(config.url)

    def test_postgres_and_supabase_use_an_explicit_postgres_url(self):
        for name in ("postgres", "supabase"):
            with self.subTest(name=name):
                config = self.resolve(
                    KEVENOS_DB_BACKEND=name,
                    KEVENOS_DATABASE_URL="postgresql://kevenos:secret@db.example/kevenos",
                    KEVENOS_DB_SCHEMA_PREFIX="demo_1",
                )
                self.assertEqual(config.name, name)
                self.assertEqual(config.schema_prefix, "demo_1")
                self.assertTrue(config.server)

    def test_marketplace_and_conventional_postgres_url_names_are_supported(self):
        for key in ("POSTGRES_URL", "DATABASE_URL"):
            with self.subTest(key=key):
                config = self.resolve(KEVENOS_DB_BACKEND="supabase", **{key: "postgresql://marketplace/db"})
                self.assertEqual(config.url, "postgresql://marketplace/db")

    def test_server_backends_fail_closed_without_a_postgres_url(self):
        from database import resolve_backend

        for values in (
            {"KEVENOS_DB_BACKEND": "postgres"},
            {"KEVENOS_DB_BACKEND": "supabase", "KEVENOS_DATABASE_URL": "sqlite:///tmp/demo.db"},
            {"KEVENOS_DB_BACKEND": "unknown"},
            {"KEVENOS_DB_BACKEND": "postgres", "KEVENOS_DB_SCHEMA_PREFIX": "not-safe!", "KEVENOS_DATABASE_URL": "postgres://db/app"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_backend(values)

    def test_live_and_rehearsal_use_separate_server_schemas(self):
        from database import schema_for_path

        root = Path("/tmp/kevenos/data")
        self.assertEqual(schema_for_path(root / "judging.db", "kevenos"), "kevenos_live")
        self.assertEqual(schema_for_path(root / "rehearsal" / "judging.db", "kevenos"), "kevenos_rehearsal")

    def test_supabase_marketplace_metadata_is_not_passed_to_psycopg(self):
        from database import postgres_client_url

        self.assertEqual(
            postgres_client_url(
                "postgresql://user:secret@pooler.example/app?sslmode=require&supa=base-pooler.x&options=discarded",
                "kevenos_live",
            ),
            "postgresql://user:secret@pooler.example/app?sslmode=require&options=-csearch_path%3Dkevenos_live",
        )


class PostgresDialectTests(unittest.TestCase):
    def test_translates_the_sqlite_constructs_used_by_kevenos(self):
        from database import postgres_sql

        self.assertEqual(postgres_sql("SELECT * FROM kv WHERE k=?"), "SELECT * FROM kv WHERE k=%s")
        self.assertIn("BIGSERIAL PRIMARY KEY", postgres_sql("CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB)"))
        self.assertIn("BYTEA", postgres_sql("CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB)"))
        self.assertEqual(
            postgres_sql("INSERT OR IGNORE INTO x(a,b) VALUES(?,?)"),
            "INSERT INTO x(a,b) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        )
        self.assertEqual(postgres_sql("BEGIN IMMEDIATE"), "BEGIN")

    def test_does_not_replace_question_marks_inside_sql_literals(self):
        from database import postgres_sql

        self.assertEqual(postgres_sql("SELECT '?' AS literal, v FROM kv WHERE k=?"), "SELECT '?' AS literal, v FROM kv WHERE k=%s")

    def test_translates_named_parameters_and_escapes_literal_like_patterns(self):
        from database import postgres_sql

        self.assertEqual(
            postgres_sql("INSERT INTO x(a,b) VALUES(:first,:second)"),
            "INSERT INTO x(a,b) VALUES(%(first)s,%(second)s)",
        )
        self.assertEqual(
            postgres_sql("SELECT v FROM kv WHERE k LIKE 'sclock:%'"),
            "SELECT v FROM kv WHERE k LIKE 'sclock:%%'",
        )

    def test_stores_timestamps_with_full_precision(self):
        from database import postgres_sql

        created = postgres_sql("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, expires REAL NOT NULL)")
        self.assertIn("ts DOUBLE PRECISION,", created)
        self.assertIn("expires DOUBLE PRECISION NOT NULL", created)
        self.assertNotIn("REAL", created)
        self.assertIn("DOUBLE PRECISION", postgres_sql("ALTER TABLE x ADD COLUMN last_used real"))
        # Only column types change: data and predicates keep the word.
        self.assertEqual(postgres_sql("SELECT v FROM kv WHERE k='real'"), "SELECT v FROM kv WHERE k='real'")
        self.assertEqual(postgres_sql("INSERT INTO x(note) VALUES('a real note')"), "INSERT INTO x(note) VALUES('a real note')")
        self.assertIn("ts DOUBLE PRECISION", postgres_sql("CREATE TEMP TABLE scratch (ts REAL)"))
        self.assertIn("ts DOUBLE PRECISION", postgres_sql("CREATE TEMPORARY TABLE IF NOT EXISTS scratch (ts REAL)"))
        # Text and quoted names inside a table definition are left alone.
        quoted = postgres_sql("CREATE TABLE x (kind TEXT DEFAULT 'real', \"real\" TEXT, ts REAL)")
        self.assertIn("DEFAULT 'real'", quoted)
        self.assertIn('"real" TEXT', quoted)
        self.assertIn("ts DOUBLE PRECISION", quoted)


class RawConnection:
    """Stands in for the psycopg connection behind PostgresConnection."""

    def __init__(self):
        from psycopg.pq import TransactionStatus
        self._status = TransactionStatus
        self.state = TransactionStatus.IDLE
        self.statements = []
        self.closed = False
        self.fail_on = {}

    @property
    def info(self):
        return SimpleNamespace(transaction_status=self.state)

    def execute(self, statement):
        self.statements.append(statement)
        if statement in self.fail_on:
            raise self.fail_on[statement]
        if statement == "BEGIN":
            self.state = self._status.INTRANS

    def commit(self):
        self.statements.append("COMMIT")
        self.state = self._status.IDLE

    def rollback(self):
        self.statements.append("ROLLBACK")
        if "ROLLBACK" in self.fail_on:
            raise self.fail_on["ROLLBACK"]
        self.state = self._status.IDLE

    def cursor(self):
        return RawCursor(self)


class RawCursor:
    def __init__(self, raw):
        self.raw = raw
        self.description = None
        self.rowcount = 0
        self._rows = []

    def execute(self, statement, parameters=()):
        self.raw.statements.append(statement)
        self.description, self._rows = None, []
        for fragment, outcome in getattr(self.raw, "responses", ()):
            if fragment in statement:
                if isinstance(outcome, BaseException):
                    raise outcome
                self.description = [SimpleNamespace(name=name) for name in outcome[0]]
                self._rows = list(outcome[1])
                break

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class PostgresConnectionLockTests(unittest.TestCase):
    """Once a unit of work ends, every other request thread must be able to use the connection."""

    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg is required for the PostgreSQL connection wrapper")
        import database
        self.database = database
        self.raw = RawConnection()
        self.connection = database.PostgresConnection(self.raw, "kevenos_live", "postgres")

    def free_for_other_threads(self, timeout=2.0):
        outcome = []

        def probe():
            acquired = self.connection._lock.acquire(timeout=timeout)
            if acquired:
                self.connection._lock.release()
            outcome.append(acquired)

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        return outcome == [True]

    def test_begin_immediate_inside_a_transaction_block_releases_the_connection(self):
        with self.connection:
            self.database.begin_immediate(self.connection)
            self.assertFalse(self.free_for_other_threads(timeout=0.05), "the block must hold the connection")
        self.assertTrue(self.free_for_other_threads(), "connection stayed locked after the block ended")
        self.assertFalse(self.connection._manual_transaction)
        self.assertEqual(self.raw.statements, ["BEGIN", "COMMIT"])

    def test_failed_block_with_begin_immediate_rolls_back_and_releases(self):
        with self.assertRaises(RuntimeError):
            with self.connection:
                self.database.begin_immediate(self.connection)
                raise RuntimeError("synthetic failure")
        self.assertTrue(self.free_for_other_threads(), "connection stayed locked after the failed block")
        self.assertFalse(self.connection._manual_transaction)
        self.assertEqual(self.raw.statements, ["BEGIN", "ROLLBACK"])

    def test_manual_transaction_releases_on_commit_and_on_rollback(self):
        for finish, expected in ((self.connection.commit, "COMMIT"), (self.connection.rollback, "ROLLBACK")):
            self.raw.statements.clear()
            self.database.begin_immediate(self.connection)
            self.assertFalse(self.free_for_other_threads(timeout=0.05), "an open transaction must hold the connection")
            finish()
            self.assertTrue(self.free_for_other_threads(), "connection stayed locked after " + expected)
            self.assertEqual(self.raw.statements, ["BEGIN", expected])

    def test_a_block_that_cannot_begin_leaves_the_connection_free(self):
        self.raw.fail_on["BEGIN"] = ConnectionError("synthetic: server went away")
        with self.assertRaises(ConnectionError):
            with self.connection:
                self.fail("the block must not run")
        self.assertEqual(self.connection._context_depth, 0)
        self.assertTrue(self.free_for_other_threads(), "connection stayed locked after a failed BEGIN")

    def test_a_block_whose_rollback_fails_leaves_the_connection_free(self):
        self.raw.fail_on["ROLLBACK"] = ConnectionError("synthetic: server went away")
        with self.assertRaises(ConnectionError):
            with self.connection:
                raise RuntimeError("synthetic failure")
        self.assertEqual(self.connection._context_depth, 0)
        self.assertTrue(self.free_for_other_threads(), "connection stayed locked after a failed rollback")

    def test_repeated_begin_in_one_manual_transaction_still_releases(self):
        self.database.begin_immediate(self.connection)
        self.database.begin_immediate(self.connection)
        self.connection.commit()
        self.assertTrue(self.free_for_other_threads(), "connection stayed locked after a repeated begin")
        self.assertEqual(self.raw.statements, ["BEGIN", "COMMIT"])


class TimestampConversionTests(unittest.TestCase):
    """Columns created by releases up to 1.2.0 are widened without endangering a running event."""

    def setUp(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest("psycopg is required for the PostgreSQL connection wrapper")
        import database
        self.database, self.psycopg = database, psycopg
        self.raw = RawConnection()
        self.connection = database.PostgresConnection(self.raw, "kevenos_live", "postgres")

    def columns(self, *pairs):
        return ("pg_attribute", (("relname", "attname"), list(pairs)))

    def altered(self):
        return [statement for statement in self.raw.statements if statement.startswith("ALTER TABLE")]

    def test_nothing_to_convert_issues_no_table_change(self):
        self.raw.responses = [self.columns()]
        self.assertEqual(self.database.widen_real_columns(self.connection), 0)
        self.assertEqual(self.altered(), [])

    def test_each_table_is_converted_in_its_own_short_transaction(self):
        self.raw.responses = [self.columns(("audit", "ts"), ("role_sessions", "expires"), ("sheets", "created"), ("audit", "seen"))]
        self.assertEqual(self.database.widen_real_columns(self.connection), 4)
        self.assertEqual(self.altered(), [
            'ALTER TABLE "audit" ALTER COLUMN "ts" TYPE DOUBLE PRECISION, ALTER COLUMN "seen" TYPE DOUBLE PRECISION',
            'ALTER TABLE "role_sessions" ALTER COLUMN "expires" TYPE DOUBLE PRECISION',
            'ALTER TABLE "sheets" ALTER COLUMN "created" TYPE DOUBLE PRECISION',
        ])
        log = self.raw.statements
        for statement in self.altered():
            position = log.index(statement)
            begun = position - log[:position][::-1].index("BEGIN") - 1
            inside = log[begun + 1:position]
            self.assertNotIn("COMMIT", inside, "each change runs in a transaction of its own")
            bounded = next(index for index, entry in enumerate(inside) if "lock_timeout" in entry)
            serialized = next(index for index, entry in enumerate(inside) if "pg_advisory_xact_lock" in entry)
            self.assertLess(bounded, serialized, "every wait in the transaction is bounded, including the advisory lock")
            self.assertEqual(log[position + 1], "COMMIT", "the table lock is released before the next table")

    def test_a_busy_table_is_postponed_and_start_up_continues(self):
        busy = self.psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
        self.raw.responses = [('ALTER TABLE "audit"', busy),
                              self.columns(("audit", "ts"), ("sheets", "created"))]
        with patch("sys.stderr"):
            converted = self.database.widen_real_columns(self.connection)
        self.assertEqual(converted, 1)
        self.assertIn('ALTER TABLE "sheets" ALTER COLUMN "created" TYPE DOUBLE PRECISION', self.raw.statements)
        self.assertIn("ROLLBACK", self.raw.statements)
        self.assertEqual(self.connection._context_depth, 0)
        self.assertFalse(self.connection._manual_transaction)
        acquired = self.connection._lock.acquire(blocking=False)
        self.assertTrue(acquired)
        self.connection._lock.release()

    def test_a_lost_connection_is_reported_not_swallowed(self):
        lost = self.psycopg.OperationalError("synthetic: server closed the connection")

        class Responses(list):
            def __iter__(inner):
                return iter([('ALTER TABLE "audit"', lost), self.columns(("audit", "ts"), ("sheets", "created"))])
        self.raw.responses = Responses()
        original = self.raw.rollback

        def closing_rollback():
            self.raw.closed = True
            original()
        self.raw.rollback = closing_rollback
        with patch("sys.stderr"), self.assertRaises(self.psycopg.OperationalError):
            self.database.widen_real_columns(self.connection)
        self.assertEqual(self.altered(), ['ALTER TABLE "audit" ALTER COLUMN "ts" TYPE DOUBLE PRECISION'])
        acquired = self.connection._lock.acquire(blocking=False)
        self.assertTrue(acquired)
        self.connection._lock.release()

    def test_busy_tables_cannot_delay_start_up_beyond_a_fixed_budget(self):
        busy = self.psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
        tables = [("t%02d" % index, "ts") for index in range(12)]
        self.raw.responses = [("ALTER TABLE", busy), self.columns(*tables)]
        clock = iter(range(0, 10_000, 2))          # every look at the clock is two seconds later
        with patch("sys.stderr"), patch.object(self.database.time, "monotonic", lambda: next(clock)):
            converted = self.database.widen_real_columns(self.connection, lock_timeout_ms=2000)
        self.assertEqual(converted, 0)
        self.assertLessEqual(len(self.altered()), 4, "start-up stops trying once the time budget is spent")

    def test_sqlite_is_untouched(self):
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE audit (ts REAL)")
        self.assertEqual(self.database.widen_real_columns(connection), 0)


if __name__ == "__main__":
    unittest.main()
