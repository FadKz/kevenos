"""Explicit Admin mail: durable duplicate protection and isolated SMTP TLS.

Call migrate once at startup before serving requests. The Unix relay carries
SMTP with mandatory STARTTLS to the fixed provider. No automatic delivery retry.
"""
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import socket
import ssl
import time
from typing import Any
import uuid

import event_access

ENDPOINT = '/api/admin/notifications'
SHARE_ENDPOINT = '/api/admin/credentials/share'
ISSUANCE_TTL = 10 * 60
SIGN_IN_URL = os.environ.get('KEVENOS_PUBLIC_URL', 'https://event.example.com').rstrip('/') + '/'
ROLE_LABELS = {'guest': 'Guest', 'participant': 'Participant', 'judge': 'Judge',
               'desk': 'Organizer and Scoring Desk', 'organizer':'Organizer','scoring':'Scoring Desk', 'admin': 'Admin', 'registration': 'Registration'}
SHARE_EXPIRED = 'This password-sharing window is no longer valid. Generate or reset the password and try again.'
SENDER = os.environ.get('KEVENOS_MAIL_SENDER', 'noreply@example.com')
SMTP_HOST = os.environ.get('KEVENOS_SMTP_HOST', 'smtp.example.com')
SMTP_SOCKET = '/run/notifications/smtp.sock'
LIMIT_PER_DAY = 100
LIMIT_PER_MINUTE = 10
PUBLIC_FIELDS = 'id,recipient,subject,status,created,completed,error'
UNAVAILABLE = 'Email is unavailable. Check notification configuration and try again.'
FAILED = 'Email was not accepted. Review the address and notification configuration before a new attempt.'
UNCERTAIN = 'Delivery could not be confirmed. Check delivery history before making a new attempt.'
REHEARSAL_DISABLED = 'Email sending and connection checks are disabled in rehearsal. Switch to live data to send real notifications.'


class SocketSMTP(smtplib.SMTP):
    """Connect only to the relay; authenticated_client requires verified TLS."""

    def __init__(self) -> None:
        super().__init__(host=SMTP_HOST, port=587, timeout=12,
                         local_hostname=os.environ.get('KEVENOS_MAIL_HELO', 'event.example.com'))

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        if host != SMTP_HOST or port != 587:
            raise ValueError('Unsupported SMTP destination.')
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            raw.settimeout(timeout)
            raw.connect(SMTP_SOCKET)
            return raw
        except BaseException:
            raw.close()
            raise


def migrate(db: Any) -> None:
    db.executescript('''
    CREATE TABLE IF NOT EXISTS notifications (
      id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, actor_credential TEXT NOT NULL,
      recipient TEXT NOT NULL, subject TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('sending','sent','failed','uncertain')),
      created REAL NOT NULL, completed REAL, error TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS notifications_created ON notifications(created);
    CREATE TABLE IF NOT EXISTS notification_checks (created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS notification_checks_created ON notification_checks(created);
    ''')
    # A previous process may have delivered before it committed its receipt.
    db.execute("UPDATE notifications SET status='uncertain',completed=?,error=? WHERE status='sending'",
               (time.time(), UNCERTAIN))
    db.execute('DELETE FROM notification_checks WHERE created<?', (time.time() - 60,))
    db.commit()


def admin(s: Any, h: Any) -> dict | None:
    ident = event_access.session(s, h.headers)
    if not ident and not s.hosted():
        legacy = s.who(h.headers)
        if legacy and legacy[0] == 'desk':
            ident = {'role': 'admin', 'credential_id': 'local-owner', 'label': 'Owner'}
    if not ident:
        h.send(401, {'error': 'Sign in to continue.'})
    elif ident['role'] != 'admin' or ident.get('actor') or ident.get('impersonation'):
        h.send(403, {'error': 'Admin access is required.'})
    else:
        return ident
    return None


def _tag(s: Any, purpose: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hmac.new(s.CFG['secret'].encode(), (purpose + ':' + encoded).encode(), hashlib.sha256).hexdigest()


def _revision(s: Any, credential: dict) -> str:
    # A reset creates a fresh password hash even when the chosen password is unchanged.
    fields = ('id', 'lookup', 'password_hash', 'role', 'station', 'pair', 'member', 'dataset', 'revision')
    account=event_access.auth_db(s).execute('SELECT id,revision FROM person_accounts WHERE id=?',(credential.get('account_id') or credential['id'],)).fetchone()
    return _tag(s, 'credential-share-revision', {key: credential.get(key) for key in fields}|{'account':dict(account) if account else None})


def decorate_issuance(s: Any, ident: dict, result: dict) -> dict:
    """Add a session-bound ten-minute sharing grant; call inside the issuance transaction.

    Single create/reset responses get a top-level issuance_grant. Bulk responses
    get one on each issued row. No plaintext or bearer grant is persisted.
    """
    if (ident.get('role') != 'admin' or ident.get('actor') or ident.get('impersonation')
            or not ident.get('digest') or event_access.active_dataset(s) != 'live'):
        return result
    rows = result.get('issued', []) if 'issued' in result else [result]
    for item in rows:
        target = item.get('credential', item)
        if not item.get('password') or not target.get('id'):
            continue
        stored = event_access.auth_db(s).execute('SELECT * FROM access_credentials WHERE id=? AND active=1',
                                                (target['id'],)).fetchone()
        if not stored:
            continue
        payload = {'cid': target['id'], 'rev': _revision(s, dict(stored)),
                   'issuer': _tag(s, 'credential-share-issuer', ident['digest']),
                   'dataset': event_access.active_dataset(s),
                   'expires': min(time.time() + ISSUANCE_TTL, ident.get('expires', 0))}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
        item['issuance_grant'] = encoded + '.' + _tag(s, 'credential-share-grant', encoded)
    return result


def _grant(s: Any, ident: dict, body: dict) -> dict:
    token = body.get('issuance_grant')
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError
        encoded, signature = token.split('.')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', encoded) or not hmac.compare_digest(signature, _tag(s, 'credential-share-grant', encoded)):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if (not isinstance(payload, dict) or set(payload) != {'cid', 'rev', 'issuer', 'dataset', 'expires'}
                or payload['cid'] != body.get('credential_id')
                or payload['dataset'] != event_access.active_dataset(s)
                or payload['issuer'] != _tag(s, 'credential-share-issuer', ident.get('digest'))):
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(SHARE_EXPIRED) from None


def _share_message(s: Any, ident: dict, body: dict, grant: dict) -> tuple[str, str]:
    # Recheck the issuer at the reservation boundary, after obtaining the lock.
    parent = event_access.auth_db(s).execute(
        "SELECT credential_id FROM role_sessions WHERE digest=? AND expires>?",
        (ident.get('digest'), time.time())).fetchone()
    if not parent or parent['credential_id'] != ident.get('credential_id'):
        raise ValueError(SHARE_EXPIRED)
    if parent['credential_id'] != 'owner-recovery' and not event_access.auth_db(s).execute(
            "SELECT 1 FROM access_credentials c JOIN person_accounts a ON a.id=c.account_id JOIN account_grants g ON g.account_id=a.id WHERE c.id=? AND c.active=1 AND a.active=1 AND g.role='admin'",
            (parent['credential_id'],)).fetchone():
        raise ValueError(SHARE_EXPIRED)
    stored = event_access.auth_db(s).execute('SELECT * FROM access_credentials WHERE id=? AND active=1',
                                            (grant['cid'],)).fetchone()
    credential = dict(stored) if stored else {}
    if (grant['expires'] <= time.time() or not credential
            or not hmac.compare_digest(grant['rev'], _revision(s, credential))
            or not event_access.auth_db(s).execute('SELECT 1 FROM person_accounts WHERE id=? AND active=1',(credential.get('account_id') or credential['id'],)).fetchone()
            or not event_access.event_roles.grants(s,credential.get('account_id') or credential['id'])
            or credential['role'] not in ROLE_LABELS):
        raise ValueError(SHARE_EXPIRED)
    password = body['password']
    if (not s.match_secret(event_access.lookup(s, password), credential['lookup'])
            or not s.match_secret(event_access.password_hash(password, credential['password_hash'].split(':', 1)[0]), credential['password_hash'])):
        raise ValueError(SHARE_EXPIRED)
    return credential_message(s, credential, password)


def credential_message(s: Any, credential: dict, password: str) -> tuple[str, str]:
    """Format the fixed access message from a newly issued or verified password."""
    roles=event_access.event_roles.grants(s,credential.get('account_id') or credential['id'])
    label = ' '.join(credential['label'].splitlines())
    names=[ROLE_LABELS[g['role']] for g in roles]
    lines = ['KevenOS event access', '', 'For: ' + label, 'Role: ' + ', '.join(names)]
    judge=next((g for g in roles if g['role']=='judge'),None)
    person=next((g for g in roles if g['role']=='participant'),None)
    if judge:lines.append('Assigned station: ' + str(judge['station']))
    if person:
        assignment=event_access.participant_assignment(s,person)
        if not assignment or not assignment['name'].strip():raise ValueError(SHARE_EXPIRED)
        lines.extend(['Team: ' + str(assignment['pair']), 'Assigned station: ' + str(assignment['station']),
                      'Round: ' + str(assignment['round']), 'Customer scenario: ' + str(assignment['scenario'])])
    sign_in = SIGN_IN_URL + ('?access=registration' if credential['role'] == 'registration' else '')
    lines.extend(['', 'Sign in: ' + sign_in, 'Enter this password to open your assigned dashboards:',
                  password, '', 'Keep this password private. If access has been reset, ask the event Admin for a new password.'])
    return 'Your KevenOS sign-in details', '\n'.join(lines)


def secret_path() -> Path:
    return Path(os.environ.get('KEVENOS_SECRETS_DIR', '/run/secrets')) / 'KEVENOS_SMTP_PASSWORD'


def configured() -> bool:
    return (os.environ.get('KEVENOS_SMTP_ENABLED') == '1' and secret_path().is_file()
            and Path(SMTP_SOCKET).is_socket())


def smtp_password() -> str:
    if not configured():
        raise ValueError(UNAVAILABLE)
    # Mounts are managed by the secrets engine, never request or env values.
    password = secret_path().read_text(encoding='utf-8').strip()
    if not password or len(password) > 4096 or '\n' in password or '\r' in password:
        raise ValueError(UNAVAILABLE)
    return password


def validate(b: dict) -> tuple[str, str, str, str]:
    if set(b) - {'idempotency_key', 'recipient', 'subject', 'message'}:
        raise ValueError('Only recipient, subject, message and idempotency_key are accepted.')
    key = b.get('idempotency_key')
    try:
        if not isinstance(key, str) or len(key) != 36:
            raise ValueError
        key = str(uuid.UUID(key))
    except (ValueError, AttributeError):
        raise ValueError('A valid request ID is required.') from None
    recipient, subject, message = (b.get(k) for k in ('recipient', 'subject', 'message'))
    if not isinstance(recipient, str) or not recipient.isascii() or len(recipient) > 254:
        raise ValueError('Enter one email address.')
    parts = recipient.split('@')
    if len(parts) != 2:
        raise ValueError('Enter one email address.')
    local, domain = parts
    atom = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    labels = domain.split('.')
    if (not 1 <= len(local) <= 64 or not re.fullmatch(atom + r'(?:\.' + atom + r')*', local)
            or len(labels) < 2 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x) for x in labels)):
        raise ValueError('Enter one email address.')
    if (not isinstance(subject, str) or not 1 <= len(subject) <= 160 or not subject.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in subject)):
        raise ValueError('Enter a subject of 1–160 characters without line breaks.')
    if (not isinstance(message, str) or not 1 <= len(message) <= 12000 or not message.strip()
            or any(ord(c) < 32 and c not in '\r\n\t' for c in message)):
        raise ValueError('Enter a message of 1–12000 characters.')
    try:
        (subject + message).encode('utf-8')
    except UnicodeError:
        raise ValueError('Use valid text for the subject and message.') from None
    return key, recipient, subject, message


def fingerprint(s: Any, recipient: str, subject: str, message: str) -> str:
    payload = json.dumps([recipient, subject, message], ensure_ascii=False, separators=(',', ':'))
    return hmac.new(s.CFG['secret'].encode(), ('notification:' + payload).encode(), hashlib.sha256).hexdigest()


def public_row(row: Any) -> dict:
    return {key: row[key] for key in PUBLIC_FIELDS.split(',')}


def remaining(s: Any, now: float) -> int:
    used = s.DB.execute('SELECT count(*) FROM notifications WHERE created>?', (now - 86400,)).fetchone()[0]
    return max(0, LIMIT_PER_DAY - used)


def rate_limited(s: Any, now: float) -> bool:
    s.DB.execute('DELETE FROM notification_checks WHERE created<=?', (now - 60,))
    sends = s.DB.execute('SELECT count(*) FROM notifications WHERE created>?', (now - 60,)).fetchone()[0]
    checks = s.DB.execute('SELECT count(*) FROM notification_checks').fetchone()[0]
    return sends + checks >= LIMIT_PER_MINUTE


def close_smtp(client: Any) -> None:
    if client is not None:
        try:
            client.quit()
        except Exception:
            pass  # QUIT failure does not undo an already acknowledged delivery.
        finally:
            client.close()


def authenticated_client(password: str) -> SocketSMTP:
    client = SocketSMTP()
    try:
        code, _ = client.ehlo()
        if code != 250:
            raise smtplib.SMTPHeloError(code, b'Unavailable')
        # _host is the fixed provider hostname, so stdlib STARTTLS uses it for
        # SNI and certificate hostname validation even over the Unix relay.
        client.starttls(context=ssl.create_default_context())
        code, _ = client.ehlo()
        if code != 250:
            raise smtplib.SMTPHeloError(code, b'Unavailable')
        client.login(SENDER, password)
        return client
    except BaseException:
        client.close()
        raise


def deliver(key: str, recipient: str, subject: str, message: str, password: str) -> tuple[str, str]:
    client = None
    sending = False
    try:
        mail = EmailMessage(policy=SMTP)
        mail['From'] = 'KevenOS <' + SENDER + '>'
        mail['To'] = recipient
        mail['Subject'] = subject
        mail['Date'] = formatdate(localtime=False)
        mail['Message-ID'] = '<' + key + '@' + os.environ.get('KEVENOS_MAIL_ID_DOMAIN', 'example.com') + '>'
        mail.set_content(message, cte='quoted-printable')
        encoded = mail.as_bytes()
        client = authenticated_client(password)
        sending = True
        refused = client.sendmail(SENDER, [recipient], encoded)
        return ('failed', FAILED) if refused else ('sent', '')
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
            smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError):
        return 'failed', FAILED
    except Exception:
        # SMTP exceptions can echo AUTH or message data; never expose their text.
        return ('uncertain', UNCERTAIN) if sending else ('failed', FAILED)
    finally:
        try:
            close_smtp(client)
        except Exception:
            pass


def handle_get(s: Any, h: Any, path: str, q: dict) -> bool:
    if path != ENDPOINT:
        return False
    if not admin(s, h):
        return True
    with s.LOCK:
        messages = [dict(row) for row in s.DB.execute('SELECT ' + PUBLIC_FIELDS + ' FROM notifications ORDER BY created DESC,id DESC LIMIT 100')]
        left = remaining(s, time.time())
    rehearsal = event_access.active_dataset(s) == 'rehearsal'
    h.send(200, {'sender': SENDER, 'enabled': not rehearsal and configured(),
                 'disabled_reason': REHEARSAL_DISABLED if rehearsal else '', 'limit_per_day': LIMIT_PER_DAY,
                 'remaining': left, 'messages': messages, 'smtp_host': SMTP_HOST})
    return True


def check(s: Any, h: Any) -> None:
    try:
        password = smtp_password()
    except (OSError, ValueError, UnicodeError):
        h.send(503, {'error': UNAVAILABLE})
        return
    with s.LOCK, s.DB:
        s.DB.execute('BEGIN IMMEDIATE')
        limited = rate_limited(s, time.time())
        if not limited:
            s.DB.execute('INSERT INTO notification_checks(created) VALUES(?)', (time.time(),))
    if limited:
        h.send(429, {'error': 'Too many notification requests. Wait a minute.'})
        return
    client = None
    try:
        client = authenticated_client(password)
        code, _ = client.quit()
        if code != 221:
            raise smtplib.SMTPException('Unavailable')
    except Exception:
        h.send(503, {'error': UNAVAILABLE})
    else:
        h.send(200, {'ok': True})
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def handle_post(s: Any, h: Any, path: str, b: dict) -> bool:
    if path not in (ENDPOINT, ENDPOINT + '/check', SHARE_ENDPOINT):
        return False
    ident = admin(s, h)
    if not ident:
        return True
    if event_access.active_dataset(s) == 'rehearsal':
        h.send(409, {'error': REHEARSAL_DISABLED})
        return True
    if not event_access.same_origin(h):
        h.send(403, {'error': 'Use this site to submit changes.'})
        return True
    if path.endswith('/check'):
        if b:
            h.send(400, {'error': 'The connection check does not accept message details.'})
        else:
            check(s, h)
        return True
    if path == SHARE_ENDPOINT:
        return share(s, h, ident, b)
    try:
        key, recipient, subject, message = validate(b)
    except ValueError as exc:
        h.send(400, {'error': str(exc)})
        return True
    return submit(s, h, ident, key, recipient, subject, message, fingerprint(s, recipient, subject, message))


def share(s: Any, h: Any, ident: dict, body: dict) -> bool:
    try:
        if set(body) != {'credential_id', 'issuance_grant', 'password', 'recipient', 'idempotency_key'}:
            raise ValueError('Only credential_id, issuance_grant, password, recipient and idempotency_key are accepted.')
        password = body.get('password')
        if not isinstance(password, str) or not 8 <= len(password) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in password):
            raise ValueError('This password cannot be emailed. Generate a new password to share.')
        key, recipient, _, _ = validate({'idempotency_key': body.get('idempotency_key'),
                                       'recipient': body.get('recipient'), 'subject': 'Credential sharing', 'message': password})
    except (TypeError, ValueError) as exc:
        h.send(400, {'error': str(exc)})
        return True
    try:
        grant = _grant(s, ident, body)
    except ValueError as exc:
        h.send(409, {'error': str(exc)})
        return True
    digest = _tag(s, 'credential-share-request', body)
    return submit(s, h, ident, key, recipient, '', '', digest,
                  prepare=lambda: _share_message(s, ident, body, grant), bind_actor=True)


def submit(s: Any, h: Any, ident: dict, key: str, recipient: str, subject: str,
           message: str, digest: str, prepare=None, bind_actor: bool = False) -> bool:
    """Browser adapter for the shared reservation and delivery operation."""
    h.send(*submit_delivery(s, ident, key, recipient, subject, message, digest, prepare, bind_actor))
    return True


def submit_delivery(s: Any, ident: dict, key: str, recipient: str, subject: str,
                    message: str, digest: str, prepare=None, bind_actor: bool = False,
                    issue=None) -> tuple[int, dict]:
    """Reserve once before SMTP. Issuance and reservation share a live DB transaction."""
    if event_access.active_dataset(s) == 'rehearsal':
        return 409, {'error': REHEARSAL_DISABLED}
    response = None
    actor = ident.get('credential_id') or 'owner-recovery'
    with s.LOCK, s.DB:
        s.DB.execute('BEGIN IMMEDIATE')
        previous = s.DB.execute('SELECT * FROM notifications WHERE id=?', (key,)).fetchone()
        if previous:
            if (not hmac.compare_digest(previous['fingerprint'], digest)
                    or (bind_actor and previous['actor_credential'] != actor)):
                response = 409, {'error': 'This request ID was already used for a different message.'}
            else:
                response = 200, {'message': public_row(previous)}
        if response is None and prepare is not None:
            try:
                subject, message = prepare()
            except ValueError as exc:
                response = 409, {'error': str(exc)}
        if response is None:
            try:
                password = smtp_password()
            except (OSError, ValueError, UnicodeError):
                response = 503, {'error': UNAVAILABLE}
        now = time.time()
        if response is None and remaining(s, now) == 0:
            response = 429, {'error': 'The 100-message daily notification limit has been reached.'}
        if response is None and rate_limited(s, now):
            response = 429, {'error': 'Too many notification requests. Wait a minute.'}
        if response is None and issue is not None:
            s.DB.execute('SAVEPOINT notification_issuance')
            try:
                subject, message = issue()
            except (ValueError, sqlite3.IntegrityError) as exc:
                s.DB.execute('ROLLBACK TO notification_issuance')
                response = 409, {'error': (str(exc) if isinstance(exc, ValueError) else
                                          'An active access record already exists. Use reset for that record.')}
            finally:
                s.DB.execute('RELEASE notification_issuance')
        if response is None:
            s.DB.execute('INSERT INTO notifications(id,fingerprint,actor_credential,recipient,subject,status,created) VALUES(?,?,?,?,?,?,?)',
                         (key, digest, actor, recipient, subject, 'sending', now))
    if response is not None:
        return response
    # The reservation is committed before SMTP.
    status, error = deliver(key, recipient, subject, message, password)
    with s.LOCK, s.DB:
        s.DB.execute('UPDATE notifications SET status=?,completed=?,error=? WHERE id=?', (status, time.time(), error, key))
        result = dict(s.DB.execute('SELECT ' + PUBLIC_FIELDS + ' FROM notifications WHERE id=?', (key,)).fetchone())
    return 200, {'message': result}
