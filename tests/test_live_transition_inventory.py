"""Transition preparation must not change authority or expose private records."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('transition_inventory', Path(__file__).parents[1] / 'tools/live_transition_inventory.py')
I = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(I)


class TransitionInventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = self.root / 'bundled.json'
        self.content.write_text('{"agenda":[],"scenarios":[]}')
        for mode in ('live', 'rehearsal'):
            folder = self.root if mode == 'live' else self.root / mode
            folder.mkdir(exist_ok=True)
            with closing(sqlite3.connect(folder / 'judging.db')) as db, db:
                db.executescript('''
                CREATE TABLE kv(k TEXT PRIMARY KEY,v TEXT);
                CREATE TABLE roster(pair INTEGER,p1 TEXT,p2 TEXT);
                CREATE TABLE roster_members(pair INTEGER,member INTEGER,name TEXT);
                CREATE TABLE person_accounts(id TEXT,active INTEGER);
                CREATE TABLE access_credentials(role TEXT,dataset TEXT,active INTEGER,password_hash TEXT);
                CREATE TABLE account_grants(account_id TEXT,role TEXT,dataset TEXT);
                CREATE TABLE role_sessions(id TEXT);
                INSERT INTO roster VALUES(1,'Private Name','');
                INSERT INTO roster_members VALUES(1,1,'Private Name'),(1,3,'Private Third');
                INSERT INTO person_accounts VALUES('private-id',1);
                INSERT INTO access_credentials VALUES('judge','rehearsal',1,'private-password-hash');
                INSERT INTO account_grants VALUES('private-id','judge','rehearsal');
                ''')
                values = {'schema_version':14,'workspace_control':{'mode':'rehearsal'},
                          'event_configuration':{'participant_count':30},
                          'event_quiz_v1':{'edits':{},'members':{'private-id':{}},'answers':{'private-id':{'run1':{},'run2':{}}}},
                          'program':{'announce':'Private announcement'}}
                db.executemany('INSERT INTO kv VALUES(?,?)', [(k,json.dumps(v)) for k,v in values.items()])

    def test_read_only_and_aggregate_privacy(self):
        paths = [self.root / 'judging.db', self.root / 'rehearsal/judging.db']
        before = [p.read_bytes() for p in paths]
        report = I.inventory(self.root, self.content)
        self.assertEqual(before, [p.read_bytes() for p in paths])
        raw = json.dumps(report)
        for text in ('Private Name','Private Third','private-id','private-password-hash','Private announcement'):
            self.assertNotIn(text, raw)
        live = report['workspaces']['live']
        self.assertEqual(live['named_participants'],2)
        self.assertEqual(live['authority']['rehearsal_judges_without_live_assignment'],1)
        self.assertEqual(live['quiz']['respondents'],1)
        self.assertEqual(live['quiz']['answers'],2)

    def test_missing_database_is_not_created(self):
        missing = self.root / 'absent.db'
        with self.assertRaises(sqlite3.OperationalError):
            I.inspect_database(missing)
        self.assertFalse(missing.exists())

    def test_content_drift_and_schema_fail_closed(self):
        (self.root / 'rehearsal/content.json').write_text('{"agenda":[{"title":"Break"}],"scenarios":[]}')
        self.assertEqual(I.inventory(self.root, self.content)['different_content_sections'], ['agenda'])
        with closing(sqlite3.connect(self.root / 'judging.db')) as db, db:
            db.execute("UPDATE kv SET v='15' WHERE k='schema_version'")
        with self.assertRaises(ValueError):
            I.inventory(self.root, self.content)


if __name__ == '__main__':
    unittest.main()
