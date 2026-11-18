"""Compatibility guard and one-way migration preserve credentials and event data."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
import database_guard
import test_multi_role as fixture
S=fixture.S

# Keep the migration fixture without duplicating its cases in unittest discovery.
class AccountMigration(unittest.TestCase):
    setUp=fixture.MultiRole.setUp
    request=fixture.MultiRole.request
    login=fixture.MultiRole.login
    issue=fixture.MultiRole.issue

    def test_legacy_migration_preserves_passwords_and_adds_equivalent_access(self):
        admin=self.issue('admin');desk=self.issue('desk');judge=self.issue('judge',station=3)
        db=S.AUTH_DB
        fields='id,lookup,password_hash,password_box,revision,active'
        before=[tuple(r) for r in db.execute('SELECT '+fields+' FROM access_credentials ORDER BY id')]
        with db:
            db.execute('DELETE FROM account_grants');db.execute('DELETE FROM person_accounts')
            db.execute("UPDATE access_credentials SET account_id=''")
            S.event_access.event_roles.migrate(db)
        after=[tuple(r) for r in db.execute('SELECT '+fields+' FROM access_credentials ORDER BY id')]
        self.assertEqual(before,after)
        self.assertEqual({g['role'] for g in S.event_access.event_roles.grants(S,admin['credential']['id'])},{'admin','organizer','scoring','registration'})
        self.assertEqual({g['role'] for g in S.event_access.event_roles.grants(S,desk['credential']['id'])},{'organizer','scoring'})
        self.assertEqual(S.event_access.event_roles.grants(S,judge['credential']['id'])[0]['station'],3)
        with db:S.event_access.event_roles.migrate(db)
        self.assertEqual(db.execute('SELECT count(*) FROM person_accounts').fetchone()[0],len(before))

    def test_guard_rejects_older_binary_against_migrated_database(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);source=root/'old.py';source.write_text('SCHEMA_VERSION = 13\n')
            with sqlite3.connect(root/'judging.db') as db:
                db.execute('CREATE TABLE kv(k TEXT PRIMARY KEY,v TEXT)')
                db.execute('INSERT INTO kv VALUES(?,?)',('schema_version','14'))
            with self.assertRaisesRegex(RuntimeError,'newer application'):database_guard.check(source,root)
            source.write_text('SCHEMA_VERSION = 14\n')
            database_guard.check(source,root)

    def test_future_schema_is_refused_without_downgrade(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'future.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE kv(k TEXT PRIMARY KEY,v TEXT)')
                db.execute('INSERT INTO kv VALUES(?,?)',('schema_version','999'))
            with self.assertRaisesRegex(RuntimeError,'newer'):S.open_db(str(path))
            with sqlite3.connect(path) as db:self.assertEqual(db.execute("SELECT v FROM kv WHERE k='schema_version'").fetchone()[0],'999')
