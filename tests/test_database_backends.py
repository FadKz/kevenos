import os
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
