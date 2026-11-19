"""Cross-connection engagement command serialization; disposable database only."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import engagement_catalog as catalog
import server


@unittest.skipUnless(os.environ.get('KEVENOS_TEST_DATABASE_URL'), 'Disposable PostgreSQL URL required')
class PostgresConcurrency(unittest.TestCase):
    def test_answer_and_close_are_serialized_across_connections(self):
        environment = {'KEVENOS_DB_BACKEND': 'postgres',
                       'KEVENOS_DATABASE_URL': os.environ['KEVENOS_TEST_DATABASE_URL'],
                       'KEVENOS_DB_SCHEMA_PREFIX': 'engtest_' + uuid4().hex[:12]}
        with patch.dict(os.environ, environment):
            connections = [server.open_db('/tmp/engagement-test/judging.db') for _ in range(2)]
        for db in connections:
            self.addCleanup(db.close)
        services = [SimpleNamespace(DB=db, LOCK=threading.RLock()) for db in connections]
        operator = {'role': 'organizer', 'credential_id': 'operator'}
        draft = catalog.execute(services[0], 'save', {'title': 'Quiz', 'kind': 'quiz', 'questions': [
            {'type': 'single', 'prompt': 'Question?', 'options': ['A', 'B'], 'correct': [1]},
        ]}, expected_revision=0, request_id='1' * 32, identity=operator)
        catalog.execute(services[0], 'publish', {'activity_id': draft['id']}, expected_revision=1,
                        request_id='2' * 32, identity=operator)
        run = catalog.execute(services[0], 'open_run', {'activity_id': draft['id'], 'version': 1},
                              expected_revision=2, request_id='3' * 32, identity=operator)
        run = catalog.execute(services[0], 'open_question', {'run_id': run['id'],
                              'question_id': run['snapshot']['questions'][0]['id']}, expected_revision=3,
                              request_id='4' * 32, identity=operator)
        barrier = threading.Barrier(2)
        def respond():
            barrier.wait(timeout=10)
            try:
                catalog.answer(services[0], run['id'], run['delivery']['id'], 1,
                               identity={'role': 'participant', 'credential_id': 'participant-1'})
                return 'accepted'
            except catalog.Conflict:
                return 'closed'
        def close():
            barrier.wait(timeout=10)
            catalog.execute(services[1], 'close_question', {'run_id': run['id']},
                            expected_revision=4, request_id='5' * 32, identity=operator)
            return 'closed'
        with ThreadPoolExecutor(max_workers=2) as pool:
            answer_future, close_future = pool.submit(respond), pool.submit(close)
            outcome = answer_future.result()
            self.assertEqual(close_future.result(), 'closed')
        state = catalog._read(connections[0], catalog.KEY, catalog.empty())
        saved = state['runs'][0]
        self.assertEqual(saved['delivery']['status'], 'closed')
        self.assertEqual(bool(saved.get('responses')), outcome == 'accepted')

    def test_same_request_retries_once_across_two_connections(self):
        environment = {'KEVENOS_DB_BACKEND': 'postgres',
                       'KEVENOS_DATABASE_URL': os.environ['KEVENOS_TEST_DATABASE_URL'],
                       'KEVENOS_DB_SCHEMA_PREFIX': 'engtest_' + uuid4().hex[:12]}
        with patch.dict(os.environ, environment):
            connections = [server.open_db('/tmp/engagement-test/judging.db') for _ in range(2)]
        for db in connections:
            self.addCleanup(db.close)
        services = [SimpleNamespace(DB=db, LOCK=threading.RLock()) for db in connections]
        barrier = threading.Barrier(2)
        identity = {'role': 'organizer', 'credential_id': 'synthetic-operator'}
        payload = {'title': 'Parallel poll', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Choose', 'options': ['A', 'B']}
        ]}
        def run(index):
            barrier.wait(timeout=10)
            return catalog.execute(services[index], 'save', payload, expected_revision=0,
                                   request_id='f' * 32, identity=identity)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, range(2)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(connections[0].execute("SELECT count(*) FROM audit WHERE action='engagement-save'").fetchone()[0], 1)

    def test_two_connections_cannot_both_write_same_revision(self):
        environment = {'KEVENOS_DB_BACKEND': 'postgres',
                       'KEVENOS_DATABASE_URL': os.environ['KEVENOS_TEST_DATABASE_URL'],
                       'KEVENOS_DB_SCHEMA_PREFIX': 'engtest_' + uuid4().hex[:12]}
        with patch.dict(os.environ, environment):
            connections = [server.open_db('/tmp/engagement-test/judging.db') for _ in range(2)]
        for db in connections:
            self.addCleanup(db.close)
        services = [SimpleNamespace(DB=db, LOCK=threading.RLock()) for db in connections]
        barrier = threading.Barrier(2)
        identity = {'role': 'organizer', 'credential_id': 'synthetic-operator'}
        payload = {'title': 'Parallel poll', 'kind': 'poll', 'questions': [
            {'type': 'single', 'prompt': 'Choose', 'options': ['A', 'B']}
        ]}
        def run(index):
            barrier.wait(timeout=10)
            try:
                catalog.execute(services[index], 'save', payload, expected_revision=0,
                                request_id=str(index + 1) * 32, identity=identity)
                return 'saved'
            except catalog.Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, range(2)))
        self.assertCountEqual(outcomes, ['saved', 'conflict'])
        self.assertEqual(connections[0].execute("SELECT count(*) FROM audit WHERE action='engagement-save'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
