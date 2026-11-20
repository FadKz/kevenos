"""Optional pg_dump recovery test; runs only against an explicitly named disposable container."""
import os
import re
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import engagement_catalog as catalog
import server


@unittest.skipUnless(os.environ.get('KEVENOS_TEST_DOCKER_POSTGRES') and os.environ.get('KEVENOS_TEST_DATABASE_URL'),
                     'Explicit disposable PostgreSQL container and URL required')
class BackupRecovery(unittest.TestCase):
    def test_pg_dump_restores_activity_answer_moderation_and_replay(self):
        container = os.environ['KEVENOS_TEST_DOCKER_POSTGRES']
        if container != 'kevenos-engagement-test-db':
            self.skipTest('Only the dedicated synthetic test container is supported.')
        suffix = uuid4().hex[:12]
        source, restored = 'engsrc_' + suffix, 'engrestore_' + suffix
        self.assertRegex(source, r'^engsrc_[a-f0-9]{12}$')
        self.assertRegex(restored, r'^engrestore_[a-f0-9]{12}$')
        def docker(*args, **kwargs):
            return subprocess.run(['docker','exec',container,*args], check=True, capture_output=True, **kwargs)
        def url_for(database):
            parts = urlsplit(os.environ['KEVENOS_TEST_DATABASE_URL'])
            return urlunsplit((parts.scheme, parts.netloc, '/' + database, parts.query, parts.fragment))
        def connection(database):
            env = {'KEVENOS_DB_BACKEND':'postgres', 'KEVENOS_DATABASE_URL':url_for(database),
                   'KEVENOS_DB_SCHEMA_PREFIX':'engbackup_' + suffix}
            with patch.dict(os.environ, env):
                return server.open_db('/tmp/engagement-backup/judging.db')
        try:
            docker('createdb','-U','postgres',source)
            docker('createdb','-U','postgres',restored)
            db = connection(source)
            try:
                service = SimpleNamespace(DB=db, LOCK=threading.RLock())
                identity = {'role':'organizer','credential_id':'operator'}
                payload = {'title':'Backup poll','kind':'poll','questions':[{'type':'single','prompt':'Choose','options':['A','B']}]}
                draft = catalog.execute(service, 'save', payload, expected_revision=0, request_id='1'*32, identity=identity)
                catalog.execute(service, 'publish', {'activity_id':draft['id']}, expected_revision=1, request_id='2'*32, identity=identity)
                run = catalog.execute(service, 'open_run', {'activity_id':draft['id'],'version':1}, expected_revision=2, request_id='3'*32, identity=identity)
                run = catalog.execute(service, 'open_question', {'run_id':run['id'],'question_id':run['snapshot']['questions'][0]['id'],'duration':None},
                                      expected_revision=3, request_id='4'*32, identity=identity)
                answer = catalog.answer(service, run['id'], run['delivery']['id'], 1, identity={'role':'participant','credential_id':'p1'})
                catalog.execute(service, 'configure_questions', {'participants':True,'guests':False}, expected_revision=4, request_id='5'*32, identity=identity)
                with service.LOCK, catalog.database.named_lock(db, 'engagement-catalog'), db:
                    state = catalog._read(db, catalog.KEY, catalog.empty())
                    question = catalog.ask_question(state, 'participant:p1', 'participant', 'Synthetic question?', '6'*32)
                    catalog._write(db, catalog.KEY, state)
                dump = docker('pg_dump','-U','postgres','-Fc','-d',source).stdout
                catalog.execute(service, 'save', {**payload, 'title':'After backup'}, expected_revision=5, request_id='7'*32, identity=identity)
            finally:
                db.close()
            subprocess.run(['docker','exec','-i',container,'pg_restore','-U','postgres','-d',restored],
                           input=dump, check=True, capture_output=True)
            db = connection(restored)
            try:
                service = SimpleNamespace(DB=db, LOCK=threading.RLock())
                state = catalog._read(db, catalog.KEY, catalog.empty())
                self.assertEqual([item['draft']['title'] for item in state['activities']], ['Backup poll'])
                self.assertEqual(state['qa'][0]['id'], question['id'])
                self.assertEqual(catalog.answer(service, run['id'], run['delivery']['id'], 1,
                                 identity={'role':'participant','credential_id':'p1'}), answer)
                self.assertEqual(catalog.execute(service, 'save', payload, expected_revision=0, request_id='1'*32, identity=identity), draft)
            finally:
                db.close()
        finally:
            for name in (source, restored):
                if re.fullmatch(r'eng(?:src|restore)_[a-f0-9]{12}', name):
                    subprocess.run(['docker','exec',container,'dropdb','-U','postgres','--if-exists',name],
                                   capture_output=True, check=False)
