"""An unexpected failure must say where it happened, without printing request data."""
import contextlib
import io
import unittest
from unittest.mock import patch

import test_event_access as access

S = access.S


class RequestDiagnostics(unittest.TestCase):
    def setUp(self):
        fixture = access.EventAccess('test_desk_can_run_live_but_cannot_administer_credentials_or_content')
        self.fixture = fixture
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.desk = fixture.login(fixture.create('desk')['password'])

    def failing(self, method, path, target, body=None):
        log = io.StringIO()
        error = RuntimeError('Synthetic Person 7a wrote: private detail')
        with patch.object(S, target, side_effect=error), contextlib.redirect_stderr(log):
            code, result, _ = access.request(method, path, body, self.desk)
        return code, result, log.getvalue()

    def test_a_failed_read_reports_the_failing_code_but_not_the_error_text(self):
        code, result, log = self.failing('GET', '/api/desk/scores', 'leaderboard')
        self.assertEqual((code, result), (500, {'error': 'Internal server error.'}))
        self.assertIn('Request failed: RuntimeError at ', log)
        self.assertRegex(log, r'server\.py:\d+ \w+')
        self.assertNotIn('Synthetic Person', log)
        self.assertNotIn('private detail', log)
        self.assertEqual(log.count('\n'), 1, 'one line per failure')

    def test_a_failed_write_reports_the_failing_code_too(self):
        code, result, log = self.failing('POST', '/api/desk/mode', 'judging_mode_command', {'mode': 'self'})
        self.assertEqual(code, 500, result)
        self.assertIn('Request failed: RuntimeError at ', log)
        self.assertRegex(log, r'server\.py:\d+ \w+')
        self.assertNotIn('private detail', log)

    def test_the_server_banner_names_the_product_only(self):
        _, _, headers = access.request('GET', '/api/health', None, '')
        self.assertEqual(headers['server'], ['KevenOS'])


if __name__ == '__main__':
    unittest.main()
