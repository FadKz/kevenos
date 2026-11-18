"""Notification tests use synthetic credentials and never contact SMTP."""
import unittest
import os
from pathlib import Path
from unittest.mock import patch
import uuid
import smtplib
import ssl
import threading
import time
from email import policy
from email.parser import BytesParser

import event_mail as M
import test_event_access as A
S = A.S


class Reply:
    def __init__(self, cookie=''):
        self.headers = {'Cookie': cookie, 'Host': 'event.example.com'}
        self.client_address = ('127.0.0.1', 1234)
        self.code = None
        self.body = None

    def send(self, code, body, *args, **kwargs):
        self.code, self.body = code, body


class Notifications(unittest.TestCase):
    login = A.EventAccess.login
    create = A.EventAccess.create
    restore = A.EventAccess.restore

    def setUp(self):
        A.EventAccess.setUp(self)
        M.migrate(S.DB)
        self.smtp_patch = patch.object(M, 'SocketSMTP', create=True)
        self.smtp = self.smtp_patch.start()
        self.addCleanup(self.smtp_patch.stop)
        socket_mount = patch.object(M.Path, 'is_socket', return_value=True)
        socket_mount.start()
        self.addCleanup(socket_mount.stop)
        self.smtp.return_value.sendmail.return_value = {}
        self.smtp.return_value.ehlo.return_value = (250, b'ready')
        self.smtp.return_value.quit.return_value = (221, b'bye')
        Path(S.DATA, 'KEVENOS_SMTP_PASSWORD').write_text('synthetic-smtp-password')
        env = patch.dict(os.environ, {'KEVENOS_SMTP_ENABLED': '1', 'KEVENOS_SECRETS_DIR': S.DATA})
        env.start()
        self.addCleanup(env.stop)

    def payload(self, **overrides):
        return {'idempotency_key': str(uuid.uuid4()), 'recipient': 'recipient@example.com',
                'subject': 'Synthetic event update', 'message': 'Synthetic private body', **overrides}

    def post(self, payload, cookie=None, check=False):
        reply = Reply(self.admin if cookie is None else cookie)
        M.handle_post(S, reply, '/api/admin/notifications' + ('/check' if check else ''), payload)
        return reply.code, reply.body

    def test_admin_authority_is_required(self):
        anonymous = Reply()
        self.assertTrue(M.handle_get(S, anonymous, '/api/admin/notifications', {}))
        self.assertEqual(anonymous.code, 401)
        guest = Reply(self.login(S.CFG['guest_password']))
        self.assertTrue(M.handle_post(S, guest, '/api/admin/notifications', {}))
        self.assertEqual(guest.code, 403)

    def test_rehearsal_cannot_send_or_connect_to_mail(self):
        from types import SimpleNamespace
        with patch.object(S, 'WORKSPACE', SimpleNamespace(mode='rehearsal'), create=True):
            info = Reply(self.admin)
            M.handle_get(S, info, M.ENDPOINT, {})
            self.assertFalse(info.body['enabled'])
            self.assertIn('rehearsal', info.body['disabled_reason'].lower())
            self.assertEqual(self.post(self.payload())[0], 409)
            self.assertEqual(self.post({}, check=True)[0], 409)
            self.smtp.assert_not_called()
            self.assertEqual(S.DB.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_delivery_is_durable_idempotent_and_does_not_store_body(self):
        payload = self.payload()
        code, result = self.post(payload)
        self.assertEqual(code, 200)
        self.assertEqual(result['message']['status'], 'sent')
        self.assertEqual(self.post(payload), (code, result))
        self.assertEqual(self.smtp.return_value.sendmail.call_count, 1)
        self.assertEqual(self.post({**payload, 'message': 'Changed body'})[0], 409)
        dump = '\n'.join(S.DB.iterdump())
        self.assertNotIn(payload['message'], dump)
        self.assertNotIn('synthetic-smtp-password', dump)
        self.assertNotIn(payload['message'], str(result))

    def test_fixed_sender_and_single_envelope_recipient(self):
        payload = self.payload(subject='Welcome — مرحبا', message='Hello\nSecond line')
        self.assertEqual(self.post(payload)[1]['message']['status'], 'sent')
        self.smtp.return_value.login.assert_called_once_with(M.SENDER, 'synthetic-smtp-password')
        self.assertEqual([call[0] for call in self.smtp.return_value.method_calls[:4]],
                         ['ehlo', 'starttls', 'ehlo', 'login'])
        context = self.smtp.return_value.starttls.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        sender, recipients, raw = self.smtp.return_value.sendmail.call_args.args
        self.assertEqual((sender, recipients), (M.SENDER, [payload['recipient']]))
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(str(parsed['From']), 'KevenOS <' + M.SENDER + '>')
        self.assertEqual(str(parsed['Subject']), payload['subject'])
        self.assertIsNone(parsed['Bcc'])
        self.assertIsNone(parsed['Cc'])

    def test_rejects_injection_multiple_recipients_and_caller_sender(self):
        bad = [{'recipient': item} for item in (
            'a@example.com,b@example.com', 'User <a@example.com>', 'a@example.com\r\nBcc: b@example.com',
            'a@example.com\n', 'é@example.com', '.first@example.com', 'a..b@example.com', 'a@-example.com')]
        bad += [{'subject': 'Hi\r\nBcc: b@example.com'}, {'subject': 'x' * 161},
                {'subject': 'nul\0'}, {'message': 'x' * 12001}, {'message': ''},
                {'message': '\ud800'}, {'sender': 'other@example.com'}, {'idempotency_key': 'not-a-uuid'}]
        for fields in bad:
            with self.subTest(fields=fields):
                self.assertEqual(self.post(self.payload(**fields))[0], 400)
        self.smtp.assert_not_called()
        self.assertEqual(S.DB.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_timeout_after_send_begins_is_uncertain_and_never_retried(self):
        self.smtp.return_value.sendmail.side_effect = TimeoutError('synthetic-smtp-password private body')
        payload = self.payload()
        _, result = self.post(payload)
        self.assertEqual(result['message']['status'], 'uncertain')
        self.assertNotIn('synthetic-smtp-password', str(result))
        self.assertEqual(self.post(payload)[1], result)
        self.assertEqual(self.smtp.return_value.sendmail.call_count, 1)

    def test_auth_failure_and_explicit_smtp_rejection_are_failed(self):
        self.smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'synthetic-smtp-password')
        _, result = self.post(self.payload())
        self.assertEqual(result['message']['status'], 'failed')
        self.assertNotIn('synthetic-smtp-password', str(result))
        self.smtp.return_value.sendmail.assert_not_called()
        self.smtp.return_value.login.side_effect = None
        self.smtp.return_value.sendmail.side_effect = smtplib.SMTPDataError(550, b'private message contents')
        _, result = self.post(self.payload())
        self.assertEqual(result['message']['status'], 'failed')
        self.assertNotIn('private message contents', str(result))

    def test_starttls_failure_prevents_authentication_and_send(self):
        for failure in (smtplib.SMTPNotSupportedError('No STARTTLS'),
                        ssl.SSLCertVerificationError('Untrusted certificate'),
                        TimeoutError('TLS timeout')):
            self.smtp.reset_mock()
            self.smtp.return_value.starttls.side_effect = failure
            self.assertEqual(self.post(self.payload())[1]['message']['status'], 'failed')
            self.smtp.return_value.login.assert_not_called()
            self.smtp.return_value.sendmail.assert_not_called()

    def test_quit_failure_cannot_undo_smtp_acceptance(self):
        self.smtp.return_value.quit.side_effect = TimeoutError('synthetic detail')
        self.assertEqual(self.post(self.payload())[1]['message']['status'], 'sent')

    def test_connection_check_never_sends_and_hides_provider_errors(self):
        self.assertEqual(self.post({}, check=True), (200, {'ok': True}))
        self.smtp.return_value.sendmail.assert_not_called()
        self.smtp.return_value.login.assert_called_once_with(M.SENDER, 'synthetic-smtp-password')
        self.assertEqual(S.DB.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)
        self.smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'synthetic-smtp-password')
        code, result = self.post({}, check=True)
        self.assertEqual(code, 503)
        self.assertNotIn('synthetic-smtp-password', str(result))
        self.assertEqual(self.post({'recipient': 'a@example.com'}, check=True)[0], 400)

    def test_rate_and_daily_limits_are_durable(self):
        for _ in range(10):
            self.assertEqual(self.post(self.payload())[0], 200)
        self.assertEqual(self.post(self.payload())[0], 429)
        self.assertEqual(self.post({}, check=True)[0], 429)
        self.assertEqual(self.smtp.call_count, 10)
        S.DB.execute('DELETE FROM notifications')
        S.DB.executemany('INSERT INTO notifications(id,fingerprint,actor_credential,recipient,subject,status,created) VALUES(?,?,?,?,?,?,?)',
                        [(str(uuid.uuid4()), 'digest', 'test', 'a@example.com', 'Synthetic', 'sent', time.time()-120) for _ in range(100)])
        S.DB.commit()
        self.assertEqual(self.post(self.payload())[0], 429)
        reply = Reply(self.admin)
        M.handle_get(S, reply, M.ENDPOINT, {})
        self.assertEqual(reply.body['remaining'], 0)
        self.assertEqual(len(reply.body['messages']), 100)
        self.assertEqual(self.smtp.call_count, 10)

    def test_check_rate_is_bounded(self):
        for _ in range(10):
            self.assertEqual(self.post({}, check=True)[0], 200)
        self.assertEqual(self.post({}, check=True)[0], 429)
        self.assertEqual(self.smtp.call_count, 10)

    def test_restart_marks_interrupted_send_uncertain(self):
        payload = self.payload()
        self.post(payload)
        S.DB.execute("UPDATE notifications SET status='sending',completed=NULL")
        S.DB.commit()
        M.migrate(S.DB)
        _, result = self.post(payload)
        self.assertEqual(result['message']['status'], 'uncertain')
        self.assertIsNotNone(result['message']['completed'])
        self.assertEqual(self.smtp.call_count, 1)

    def test_network_does_not_hold_app_lock_and_concurrent_replay_does_not_send(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_send(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release SMTP')
            return {}
        self.smtp.return_value.sendmail.side_effect = blocked_send
        payload = self.payload()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.post(payload)), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            unlocked = S.LOCK.acquire(blocking=False)
            if unlocked:
                S.LOCK.release()
            self.assertTrue(unlocked, 'SMTP IO must not hold the application lock')
            code, replay = self.post(payload)
            self.assertEqual(code, 200)
            self.assertEqual(replay['message']['status'], 'sending')
            self.assertEqual(self.smtp.return_value.sendmail.call_count, 1)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][1]['message']['status'], 'sent')

    def test_disabled_feature_and_missing_mount_fail_without_network(self):
        with patch.dict(os.environ, {'KEVENOS_SMTP_ENABLED': '0'}):
            self.assertEqual(self.post(self.payload())[0], 503)
            self.assertEqual(self.post({}, check=True)[0], 503)
        with patch.object(M.Path, 'is_socket', return_value=False):
            self.assertEqual(self.post(self.payload())[0], 503)
        Path(S.DATA, 'KEVENOS_SMTP_PASSWORD').unlink()
        self.assertEqual(self.post(self.payload())[0], 503)
        self.smtp.assert_not_called()

    def test_real_handlers_enforce_roles_csrf_and_json(self):
        desk = self.login(self.create('desk')['password'])
        guest = self.login(S.CFG['guest_password'])
        for cookie, expected in (('', 401), (guest, 403), (desk, 403)):
            for method, path, body in (
                ('GET', M.ENDPOINT, None), ('POST', M.ENDPOINT, self.payload()), ('POST', M.ENDPOINT + '/check', {})):
                self.assertEqual(A.request(method, path, body, cookie)[0], expected)
        self.assertEqual(A.request('POST', M.ENDPOINT, self.payload(), self.admin, {'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(A.request('POST', M.ENDPOINT, self.payload(), self.admin, {'Content-Type': 'text/plain'})[0], 415)
        self.smtp.assert_not_called()
        code, result, _ = A.request('POST', M.ENDPOINT, self.payload(), self.admin)
        self.assertEqual(code, 200)
        self.assertEqual(result['message']['status'], 'sent')
        actor = S.DB.execute('SELECT actor_credential FROM notifications').fetchone()[0]
        self.assertEqual(actor, 'owner-recovery')

    def test_local_legacy_organiser_can_manage_notifications(self):
        with patch.dict(os.environ, {'KEVENOS_HOSTED': '0'}):
            reply = Reply()
            reply.headers['X-Token'] = S.token_for('desk', 0)
            M.handle_get(S, reply, M.ENDPOINT, {})
            self.assertEqual(reply.code, 200)


class SMTPTransport(unittest.TestCase):
    def test_only_unix_socket_and_verified_provider_tls_are_used(self):
        with patch.object(smtplib.SMTP, 'getreply', return_value=(220, b'ready')), \
                patch.object(M.socket, 'socket') as new_socket, \
                patch.object(M.socket, 'create_connection', side_effect=AssertionError('TCP forbidden')):
            client = M.SocketSMTP()
            self.assertEqual(client._host, 'smtp.example.com')
            self.assertEqual(client.local_hostname, 'event.example.com')
            new_socket.assert_called_once_with(M.socket.AF_UNIX, M.socket.SOCK_STREAM)
            new_socket.return_value.connect.assert_called_once_with('/run/notifications/smtp.sock')
            context = ssl.create_default_context()
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            with patch.object(context, 'wrap_socket') as wrap, \
                    patch.object(client, 'ehlo_or_helo_if_needed'), \
                    patch.object(client, 'has_extn', return_value=True), \
                    patch.object(client, 'docmd', return_value=(220, b'upgrade')):
                client.starttls(context=context)
                wrap.assert_called_once_with(new_socket.return_value, server_hostname='smtp.example.com')
            with self.assertRaises(ValueError):
                client._get_socket('other.example', 587, 12)
            with self.assertRaises(ValueError):
                client._get_socket(M.SMTP_HOST, 465, 12)


if __name__ == '__main__':
    unittest.main()
