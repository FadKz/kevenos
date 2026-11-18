import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_api as T
from test_event_access import request


S = T.S


class DisposableDemoAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.previous = {name: getattr(S, name, None) for name in ('DB', 'AUTH_DB', 'DATA', 'CFG', 'CONTENT', 'WORKSPACE')}
        self.addCleanup(self.restore)
        self.environment = patch.dict(os.environ, {
            'KEVENOS_HOSTED': '1',
            'KEVENOS_GATEWAY_ENABLED': '0',
            'KEVENOS_DEMO_ROLE_PASSWORDS': '1',
            'KEVENOS_STABLE_DEMO_CONTEXT': '1',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

        S.DATA = self.temp.name
        S.CFG = {
            'port': 8080, 'pairs': 30, 'stations': 8, 'rounds': 4,
            'mode': 'desk', 'rehearsal': True, 'password': 'admin',
            'guest_password': 'guest', 'view_key': 'demo-view',
            'secret': 'same-demo-session-secret',
        }
        S.CONTENT = S.bundled_content()
        path = Path(S.DATA) / 'judging.db'
        S.DB = S.open_db(path)
        S.event_workspace.initialize(S, path, S.DATA)
        S.WORKSPACE.switch('rehearsal', 'REHEARSAL')
        S.event_access.seed_guest(S)
        S.event_access.seed_demo_roles(S)

    def restore(self):
        workspace = getattr(S, 'WORKSPACE', None)
        if workspace and workspace is not self.previous['WORKSPACE']:
            workspace.close()
        for name, value in self.previous.items():
            setattr(S, name, value)

    def test_each_demo_role_signs_in_with_its_role_name(self):
        for password, expected in (
            ('admin', 'admin'), ('organizer', 'organizer'), ('scoring', 'scoring'),
            ('registration', 'registration'), ('judge', 'judge'),
            ('participant', 'participant'), ('guest', 'guest'),
        ):
            with self.subTest(password=password):
                status, body, _ = request('POST', '/api/access/login', {'password': password})
                self.assertEqual(status, 200, body)
                self.assertEqual(body['role'], expected)

    def test_demo_context_is_stable_for_fresh_instances_with_the_same_secret(self):
        import event_workspace

        first = event_workspace.initial_context(S)
        second = event_workspace.initial_context(S)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)

    def test_current_demo_access_skips_the_cross_process_seed_lock(self):
        with patch.object(
            S.event_access.database,
            'named_lock',
            side_effect=AssertionError('warm startup must not queue behind the seed lock'),
        ):
            S.event_access.seed_guest(S)
            S.event_access.seed_demo_roles(S)

    def test_demo_seed_repairs_a_participant_grant_removed_by_rehearsal_reset(self):
        credential = S.AUTH_DB.execute(
            "SELECT account_id FROM access_credentials WHERE id='demo-participant'"
        ).fetchone()
        S.AUTH_DB.execute(
            "DELETE FROM account_grants WHERE account_id=? AND role='participant'",
            (credential['account_id'],),
        )
        S.AUTH_DB.commit()

        S.event_access.seed_demo_roles(S)

        status, body, _ = request('POST', '/api/access/login', {'password': 'participant'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['role'], 'participant')


if __name__ == '__main__':
    unittest.main()
