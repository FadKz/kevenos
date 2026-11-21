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



if __name__ == "__main__":
    unittest.main()
