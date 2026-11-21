"""Credential sharing with synthetic credentials and mocked SMTP only."""
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import time

import event_access as A
import event_mail as M
import server as S
from test_event_mail import Reply


class CredentialSharing(unittest.TestCase):
    def setUp(self):
        previous = {key: getattr(S, key, None) for key in ('CFG', 'DB', 'DATA', 'CONTENT', 'AUTH_DB', 'WORKSPACE')}
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        S.DATA = tmp.name
        S.CFG = S.load_config(str(Path(S.DATA) / 'config.json'))
        S.AUTH_DB = S.WORKSPACE = None
        S.DB = S.open_db(str(Path(S.DATA) / 'sharing.db'))
        S.CONTENT = S.bundled_content()

        def restore():
            S.DB.close()
            for key, value in previous.items():
                setattr(S, key, value)
        self.addCleanup(restore)
        self.env = patch.dict(os.environ, {'KEVENOS_HOSTED': '1', 'KEVENOS_PUBLIC_URL': 'https://event.example.com'})
        self.env.start(); self.addCleanup(self.env.stop)
        with S.LOCK, S.DB:
            admin = A.create_credential(S, 'admin', 'Synthetic administrator')
            self.token, _ = A.mint_session(S, 'admin', admin['credential']['id'], admin['credential']['label'])
        self.cookie = 'pp_access=' + self.token
        self.ident = A.session(S, {'Cookie': self.cookie})
        secrets_patch = patch.object(M, 'smtp_password', return_value='synthetic-smtp-password')
        self.secret = secrets_patch.start(); self.addCleanup(secrets_patch.stop)
        smtp_patch = patch.object(M, 'SocketSMTP')
        self.smtp = smtp_patch.start(); self.addCleanup(smtp_patch.stop)
        self.smtp.return_value.ehlo.return_value = (250, b'ready')
        self.smtp.return_value.quit.return_value = (221, b'bye')
        self.smtp.return_value.sendmail.return_value = {}

    def issue(self, role='desk', **fields):
        with S.LOCK, S.DB:
            result = A.create_credential(S, role, 'Synthetic ' + role, **fields)
            return M.decorate_issuance(S, self.ident, result)

    def payload(self, issued, **changes):
        return {'credential_id': issued['credential']['id'], 'issuance_grant': issued['issuance_grant'],
                'password': issued['password'], 'recipient': 'recipient@example.com',
                'idempotency_key': str(uuid.uuid4()), **changes}

    def post(self, body, cookie=None):
        reply = Reply(self.cookie if cookie is None else cookie)
        self.assertTrue(M.handle_post(S, reply, '/api/admin/credentials/share', body))
        return reply.code, reply.body

    def test_sign_in_details_are_not_emailed_without_a_usable_event_address(self):
        for value in (None, '', 'event.example.com', 'https://event.example.com/?x=1', 'https://user:secret@event.example.com',
                      'https://event.example.com#', 'https://event.example.com?', 'https://event.example.com/#'):
            issued = self.issue()
            with patch.dict(os.environ):
                os.environ.pop('KEVENOS_PUBLIC_URL', None)
                if value is not None:
                    os.environ['KEVENOS_PUBLIC_URL'] = value
                code, response = self.post(self.payload(issued))
            self.assertEqual(code, 409, '%r: %r' % (value, response))
            self.assertIn('KEVENOS_PUBLIC_URL', response['error'])
        self.assertEqual(self.smtp.return_value.sendmail.call_count, 0)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_sign_in_details_use_the_configured_event_address(self):
        issued = self.issue()
        with patch.dict(os.environ, {'KEVENOS_PUBLIC_URL': 'https://tickets.example.org/'}):
            code, response = self.post(self.payload(issued))
        self.assertEqual(code, 200, response)
        raw = self.smtp.return_value.sendmail.call_args.args[2]
        body = BytesParser(policy=policy.default).parsebytes(raw).get_content()
        self.assertIn('Sign in: https://tickets.example.org/\n', body.replace('\r\n', '\n'))
        self.assertNotIn('event.example.com', body)

    def test_issued_password_is_sent_once_using_fixed_template_without_storage(self):
        issued = self.issue()
        request = self.payload(issued)
        code, response = self.post(request)
        self.assertEqual(code, 200, response)
        self.assertEqual(response['message']['status'], 'sent')
        self.assertEqual(self.post(request), (code, response))
        self.assertEqual(self.smtp.return_value.sendmail.call_count, 1)
        sender, recipients, raw = self.smtp.return_value.sendmail.call_args.args
        self.assertEqual(sender, M.SENDER)
        self.assertEqual(recipients, [request['recipient']])
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        body = mail.get_content()
        self.assertIn(issued['password'], body)
        self.assertIn('Organizer', body)
        self.assertIn('https://event.example.com/', body)
        dump = '\n'.join(S.DB.iterdump())
        self.assertNotIn(issued['password'], dump)
        self.assertNotIn(issued['issuance_grant'], dump)
        self.assertNotIn(issued['password'], json.dumps(response))

    def test_replay_after_grant_expiry_returns_receipt_but_new_attempt_does_not_send(self):
        request = self.payload(self.issue())
        expected = self.post(request)
        with patch.object(M.time, 'time', return_value=time.time() + 601):
            self.assertEqual(self.post(request), expected)
            self.assertEqual(self.post({**request, 'idempotency_key': str(uuid.uuid4())})[0], 409)
        self.assertEqual(self.smtp.return_value.sendmail.call_count, 1)

    def test_grant_wrong_issuer_tampering_target_and_password_are_rejected(self):
        issued = self.issue(); request = self.payload(issued)
        with S.LOCK, S.DB:
            other, _ = A.mint_session(S, 'admin', self.ident['credential_id'], 'Same admin another session')
        self.assertEqual(self.post(request, cookie='pp_access=' + other)[0], 409)
        for changed in ({'issuance_grant': issued['issuance_grant'][:-1] + ('a' if issued['issuance_grant'][-1] != 'a' else 'b')},
                        {'credential_id': self.ident['credential_id']}, {'password': 'WrongPassword9!'}):
            self.assertEqual(self.post({**request, **changed})[0], 409)
        self.secret.assert_not_called(); self.smtp.assert_not_called()

    def test_reset_same_password_and_revoke_invalidate_grant(self):
        issued = self.issue(); old = self.payload(issued)
        with S.LOCK, S.DB:
            cid = issued['credential']['id']
            S.DB.execute('DELETE FROM access_credentials WHERE id=?', (cid,))
            current = A.create_credential(S, 'desk', 'Synthetic desk', password=issued['password'], cid=cid)
            current = M.decorate_issuance(S, self.ident, current)
        self.assertEqual(self.post(old)[0], 409)
        with S.DB:
            S.DB.execute('UPDATE access_credentials SET active=0 WHERE id=?', (cid,))
        self.assertEqual(self.post(self.payload(current))[0], 409)
        self.secret.assert_not_called(); self.smtp.assert_not_called()

    def test_rehearsal_has_no_grants_secret_reads_connections_or_reservations(self):
        issued = self.issue(); request = self.payload(issued)
        with patch.object(S, 'WORKSPACE', SimpleNamespace(mode='rehearsal')):
            self.assertEqual(self.post(request)[0], 409)
            with S.LOCK, S.DB:
                another = A.create_credential(S, 'desk', 'Synthetic other organizer')
                self.assertNotIn('issuance_grant', M.decorate_issuance(S, self.ident, another))
        self.secret.assert_not_called(); self.smtp.assert_not_called()
        self.assertEqual(S.DB.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_rejects_non_admin_impersonation_extra_fields_and_bad_recipient(self):
        issued = self.issue(); request = self.payload(issued)
        self.assertEqual(self.post(request, cookie='')[0], 401)
        with S.LOCK, S.DB:
            token, _ = A.mint_session(S, 'desk', issued['credential']['id'], 'Synthetic organizer')
        self.assertEqual(self.post(request, cookie='pp_access=' + token)[0], 403)
        with patch.object(A, 'session', return_value=dict(self.ident, actor={'role': 'admin'}, impersonation={'mode': 'support'})):
            self.assertEqual(self.post(request)[0], 403)
        for changed in ({'sender': 'other@example.com'}, {'message': 'Custom body'}, {'role': 'admin'},
                        {'recipient': 'a@example.com,b@example.com'}, {'recipient': 'a@example.com\r\nBcc:x@example.com'}):
            self.assertEqual(self.post({**request, **changed})[0], 400)
        self.secret.assert_not_called(); self.smtp.assert_not_called()

    def test_conflicting_replay_and_uncertain_delivery_never_resend(self):
        request = self.payload(self.issue())
        self.smtp.return_value.sendmail.side_effect = TimeoutError('Secret SMTP or body data must not escape')
        code, response = self.post(request)
        self.assertEqual((code, response['message']['status']), (200, 'uncertain'))
        self.assertEqual(self.post(request), (code, response))
        self.assertEqual(self.post({**request, 'recipient': 'second@example.com'})[0], 409)
        M.migrate(S.DB)
        self.assertEqual(self.post(request)[1]['message']['status'], 'uncertain')
        self.assertEqual(self.smtp.return_value.sendmail.call_count, 1)
        self.assertNotIn('Secret SMTP', json.dumps(response))

    def test_bulk_grants_are_per_credential_and_participant_template_is_assigned(self):
        with S.LOCK, S.DB:
            S.DB.execute('INSERT INTO roster(pair,partner,p1,p2) VALUES(1,?,?,?)', ('Example company', 'Alex Example', 'Sam Example'))
            first = A.create_credential(S, 'participant', 'Alex', pair=1, member=1)
            second = A.create_credential(S, 'participant', 'Sam', pair=1, member=2)
            bulk = {'issued': [{**item['credential'], 'password': item['password']} for item in (first, second)], 'skipped': 0}
            M.decorate_issuance(S, self.ident, bulk)
        self.assertNotEqual(bulk['issued'][0]['issuance_grant'], bulk['issued'][1]['issuance_grant'])
        item = bulk['issued'][0]
        issued = {'credential': item, 'password': item['password'], 'issuance_grant': item['issuance_grant']}
        self.assertEqual(self.post(self.payload(issued))[0], 200)
        mail = BytesParser(policy=policy.default).parsebytes(self.smtp.return_value.sendmail.call_args.args[2])
        for text in ('Participant', 'Alex Example', 'Team: 1', 'Assigned station: 1', 'Round: 1', 'Customer scenario: S1'):
            self.assertIn(text, mail.get_content())
        self.assertNotIn('Sam Example', mail.get_content())

    def test_staff_email_uses_bound_station_and_revision_change_invalidates_grant(self):
        issued = self.issue('judge', station=3)
        self.assertEqual(self.post(self.payload(issued))[0], 200)
        mail = BytesParser(policy=policy.default).parsebytes(self.smtp.return_value.sendmail.call_args.args[2])
        self.assertIn('Role: Judge', mail.get_content())
        self.assertIn('Assigned station: 3', mail.get_content())
        with S.DB:
            S.DB.execute('UPDATE access_credentials SET station=4,revision=revision+1 WHERE id=?', (issued['credential']['id'],))
        self.assertEqual(self.post(self.payload(issued))[0], 409)
        self.assertEqual(self.smtp.return_value.sendmail.call_count, 1)

    def test_revoked_issuer_and_expired_issuer_cannot_send(self):
        request = self.payload(self.issue())
        with patch.object(M.time, 'time', return_value=self.ident['expires'] + 1):
            self.assertEqual(self.post(request)[0], 401)
        with S.DB:
            S.DB.execute('UPDATE access_credentials SET active=0 WHERE id=?', (self.ident['credential_id'],))
        self.assertEqual(self.post(request)[0], 401)
        self.secret.assert_not_called(); self.smtp.assert_not_called()

    def test_issuer_revoked_between_authorization_and_reservation_cannot_send(self):
        request = self.payload(self.issue())
        authorize = M.admin

        def revoke_after_check(s, h):
            ident = authorize(s, h)
            with s.LOCK, A.auth_db(s):
                A.auth_db(s).execute('DELETE FROM role_sessions WHERE digest=?', (ident['digest'],))
            return ident

        with patch.object(M, 'admin', side_effect=revoke_after_check):
            self.assertEqual(self.post(request)[0], 409)
        self.secret.assert_not_called(); self.smtp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
