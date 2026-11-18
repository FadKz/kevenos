"""Public liveness must remain available without database work."""
import unittest
from unittest.mock import Mock, patch
import test_event_access as access


class PublicFastPaths(unittest.TestCase):
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
