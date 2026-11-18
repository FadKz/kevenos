"""Public liveness must remain available without database work."""
import unittest
from unittest.mock import Mock, patch
import test_event_access as access


class PublicFastPaths(unittest.TestCase):
    def test_readiness_detects_database_outage(self):
        db = Mock()
        db.execute.side_effect = RuntimeError('database unavailable')
        with patch.object(access.S, 'WORKSPACE', None), patch.object(access.S, 'DB', db):
            status, body, headers = access.request('GET', '/api/health')
        self.assertEqual((status, body), (503, {'ok': False}))
        self.assertEqual(headers['cache-control'], ['no-store'])

    def test_readiness_checks_database(self):
        db = Mock()
        with patch.object(access.S, 'WORKSPACE', None), patch.object(access.S, 'DB', db):
            status, body, _ = access.request('GET', '/api/health')
        self.assertEqual((status, body), (200, {'ok': True}))
        db.execute.assert_called_once_with('SELECT 1')

    def test_readiness_fails_when_live_database_or_recovery_guard_fails(self):
        db = Mock()
        workspace = Mock()
        workspace.guard.blocked = False
        workspace.live.execute.side_effect = RuntimeError('live database unavailable')
        with patch.object(access.S, 'WORKSPACE', workspace), patch.object(access.S, 'DB', db):
            self.assertEqual(access.request('GET', '/api/health')[0], 503)
            workspace.guard.blocked = True
            self.assertEqual(access.request('GET', '/api/health')[0], 503)

    def test_liveness_does_not_sync_or_query_sessions(self):
        workspace = Mock()
        workspace.sync.side_effect = AssertionError('liveness accessed workspace')
        with patch.object(access.S, 'WORKSPACE', workspace), \
             patch.object(access.S.event_access, 'session', side_effect=AssertionError('liveness accessed session')):
            status, body, headers = access.request('GET', '/healthz')
        self.assertEqual((status, body), (200, {'ok': True}))
        self.assertEqual(headers['cache-control'], ['no-store'])
        self.assertNotIn('set-cookie', headers)

    def test_public_asset_is_cacheable_without_workspace_or_session(self):
        workspace = Mock()
        workspace.sync.side_effect = AssertionError('asset accessed workspace')
        with patch.object(access.S, 'WORKSPACE', workspace), \
             patch.object(access.S.event_access, 'session', side_effect=AssertionError('asset accessed session')):
            status, body, headers = access.request('GET', '/static/app.css')
        self.assertEqual(status, 200)
        self.assertTrue(body)
        self.assertIn('public', headers['cache-control'][0])
        self.assertNotIn('set-cookie', headers)
        self.assertNotIn('x-event-context', headers)

    def test_unlisted_asset_is_not_exposed(self):
        status, _, _ = access.request('GET', '/static/../server.py')
        self.assertNotEqual(status, 200)

    def test_assets_are_available_during_initialization(self):
        with patch.object(access.S, 'STARTING', True), \
             patch.object(access.S, 'READY') as ready:
            status, body, _ = access.request('GET', '/static/app.css')
        self.assertEqual(status, 200)
        self.assertTrue(body)
        ready.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
