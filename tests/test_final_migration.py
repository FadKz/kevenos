"""Additive migration and read-only rollback compatibility checks."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import threading
import unittest

import server
import event_config

ROOT=Path(__file__).resolve().parents[2]
guard=None
if (ROOT/'rollback-guard.py').exists():
    spec=importlib.util.spec_from_file_location('rollback_guard',ROOT/'rollback-guard.py')
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


@unittest.skipUnless(guard and (ROOT/'base/server.py').exists(), 'Release-capsule qualification requires immutable 4.2.1 base and rollback guard')
class FinalMigration(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'judging.db'
        self.db=server.open_db(str(self.path));self.addCleanup(self.db.close)
        self.s=SimpleNamespace(DB=self.db,LOCK=threading.RLock(),CFG={'pairs':30,'stations':8,'rounds':4},CONTENT=server.bundled_content())
        event_config.activate(self.s)

    def test_guard_is_read_only_and_defaults_run_in_legacy_421(self):
        with self.db:
            self.db.execute("INSERT INTO roster(pair,partner,p1,p2,coach1) VALUES(1,'Example','Alex','Sam',1)")
            self.db.execute("INSERT INTO sheets(kind,pair,station,round,ticks,ai,asked,raised,app_version) VALUES('round',1,1,1,'[1,1,0,0,0,0,0,0,0,0]',0,'[]','[]','4.2.1')")
        before='\n'.join(self.db.iterdump())
        self.assertEqual(guard.check_database(self.path),[])
        self.assertEqual('\n'.join(self.db.iterdump()),before)
        script='''import json,sys,sqlite3
sys.path.insert(0,sys.argv[1]);import server as s
s.CFG={'pairs':30,'stations':8,'rounds':4};s.CONTENT=s.bundled_content()
s.DB=s.open_db(sys.argv[2]);row=s.leaderboard()[0]
assert s.APP_VERSION=='4.2.1'
assert row['pair']==1 and row['total']==22
assert s.roster_row(1)['p2']=='Sam'
s.DB.close()
print('legacy-default-read-ok')'''
        proc=subprocess.run([sys.executable,'-B','-c',script,str(ROOT/'base'),str(self.path)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(proc.stdout.strip(),'legacy-default-read-ok')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sheets').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM team_assignments').fetchone()[0],30)

    def test_guard_blocks_each_new_lifecycle_and_capacity_condition(self):
        cases=[
            ("INSERT INTO kv(k,v) VALUES('final',?)",(json.dumps({'pairs':[1],'run_id':'legacy-example'}),),'Final lifecycle'),
            ("UPDATE kv SET v=? WHERE k='event_configuration'",(json.dumps({'participant_count':9,'team_size':3,'judge_count':2}),),'non-default event'),
            ("INSERT INTO roster_members(pair,member,name) VALUES(1,3,'Example')",(),'additional member'),
            ("UPDATE team_assignments SET scenario='S4' WHERE pair=1",(),'stable assignments'),
            ("INSERT INTO sheets(kind,pair,station,final_run) VALUES('final',1,1,'example')",(),'Final receipts'),
        ]
        for sql,args,reason in cases:
            with self.subTest(reason=reason):
                self.db.execute('SAVEPOINT guardtest')
                self.db.execute(sql,args);self.db.execute('RELEASE guardtest')
                self.assertTrue(any(reason in r for r in guard.check_database(self.path)))
                # Isolate each reason in a fresh clean database.
                self.db.close();self.path.unlink()
                self.db=server.open_db(str(self.path));self.s.DB=self.db
                event_config.activate(self.s)
        self.db.close()
