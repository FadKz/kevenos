"""Participant Web Push subscriptions, scoped to the active event workspace."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

KEY = 'participant_push_v1'
PATH = '/api/participant/push'
STAFF_PATH = '/api/staff/push'
HISTORY = 'participant_push_history_v1'
TEST_ARM = 'participant_push_test_arm_v1'
MESSAGES_KEY = 'participant_desk_messages_v1'
TEST_KINDS = {'question', 'audience', 'announcement', 'agenda', 'help'}
MESSAGES = {
    'announcement': 'The Event Desk has an update.',
    'audience': 'A new Audience question is open in KevenOS.',
    'test': 'Your KevenOS notification test worked.',
    'question': 'A quiz question is open. Open KevenOS to answer.',
    'results': 'Results are available in KevenOS.',
    'agenda': 'The event agenda has changed.',
    'help': 'The Event Desk replied to your help request.',
}
PENDING = {'queued', 'sending'}


def public_key(secret):
    encoded = secret.read_text(encoding='utf-8').strip()
    private = serialization.load_der_private_key(
        base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)), password=None)
    if not isinstance(private, ec.EllipticCurvePrivateKey) or not isinstance(private.curve, ec.SECP256R1):
        raise ValueError('Invalid notification signing key.')
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(public).decode().rstrip('=')


def validate_subscription(value):
    if not isinstance(value, dict) or not isinstance(value.get('keys'), dict):
        raise ValueError('Invalid notification subscription.')
    endpoint = value.get('endpoint')
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError('Invalid notification endpoint.')
    parts = urlsplit(endpoint)
    host = parts.hostname or ''
    if parts.scheme != 'https' or not (host.endswith('.push.apple.com') or host == 'fcm.googleapis.com') or parts.port not in (None, 443) or parts.username or parts.password or parts.fragment:
        raise ValueError('This notification service is not supported.')
    keys = value['keys']
    for key, length in (('p256dh', 65), ('auth', 16)):
        encoded = keys.get(key)
        if not isinstance(encoded, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', encoded):
            raise ValueError('Invalid notification key.')
        try:
            decoded = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        except ValueError:
            raise ValueError('Invalid notification key.') from None
        if len(decoded) != length or (key == 'p256dh' and decoded[0] != 4):
            raise ValueError('Invalid notification key.')
        if key == 'p256dh':
            try:
                ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded)
            except ValueError:
                raise ValueError('Invalid notification key.') from None
    return {'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']}}


def handle(s, h, path, body=None):
    if path == STAFF_PATH:
        with s.LOCK:
            ident = s.event_access.session(s, h.headers)
            role = (ident.get('dashboard') or ident['role']) if ident else None
            if role not in {'admin', 'organizer', 'desk', 'scoring', 'registration'} or ident.get('impersonation'):
                h.send(403 if ident else 401, {'error': 'Staff sign-in required.'})
            elif body is None:
                result = report(s)
                if role in {'admin', 'organizer', 'desk'} and result['workspace'] == 'rehearsal':
                    result.update(test_controls(s))
                h.send(200, result)
            elif role not in {'admin', 'organizer', 'desk'} or s.event_access.active_dataset(s) != 'rehearsal':
                h.send(403, {'error':'Device selection is available to Rehearsal organizers and admins.'})
            elif not isinstance(body, dict):
                h.send(400, {'error':'Provide a device action.'})
            elif body.get('action') == 'disarm':
                s.kv_set(TEST_ARM, {})
                h.send(200, {'ok':True})
            elif body.get('action') == 'arm' and body.get('confirm') == 'TEST DEVICE':
                try:
                    arm_test_device(s, body.get('device'), body.get('kind'))
                    s.audit(role,'push-test-arm',body['kind'])
                    h.send(200, {'ok':True})
                except ValueError as error:
                    h.send(400, {'error':str(error)})
            else:
                h.send(400, {'error':'Confirm the selected test device.'})
        return True
    if path != PATH:
        return False
    with s.LOCK:
        ident = s.event_access.session(s, h.headers)
        if not ident or ident['role'] != 'participant' or ident.get('impersonation'):
            h.send(403 if ident else 401, {'error': 'Sign in as a participant to manage notifications.'})
            return True
        if not s.event_access.participant_assignment(s, ident):
            h.send(409, {'error': 'Your participant assignment is unavailable.'})
            return True
        secret = Path(os.environ.get('KEVENOS_SECRETS_DIR', '/run/secrets')) / 'KEVENOS_WEB_PUSH_VAPID_PRIVATE_KEY'
        try:
            vapid_public_key = public_key(secret)
        except (OSError, ValueError):
            vapid_public_key = None
        available = bool(vapid_public_key)
        rehearsal = s.event_access.active_dataset(s) == 'rehearsal'
        if body is None:
            h.send(200, {'available': available, 'public_key': vapid_public_key,
                         'test_available': available and rehearsal})
            return True
        if not available and body.get('action') != 'unsubscribe':
            h.send(503, {'error': 'Notifications are not available yet.'})
            return True
        try:
            subscription = validate_subscription(body.get('subscription'))
            action = body.get('action')
            if action not in ('subscribe', 'unsubscribe', 'status', 'test'):
                raise ValueError('Choose a notification action.')
            digest = hashlib.sha256(subscription['endpoint'].encode()).hexdigest()
            rows = s.kv_get(KEY) or {}
            if action == 'test':
                if not rehearsal:
                    h.send(409, {'error': 'Device tests are available in Rehearsal.'})
                    return True
                current = active_devices(s, digest).get(digest)
                if (not current or current[0]['credential_id'] != ident['credential_id']
                        or current[0]['subscription'] != subscription):
                    h.send(403, {'error': 'Enable alerts on this device before testing.'})
                    return True
                history = s.kv_get(HISTORY) or []
                now = time.time()
                if any(record['kind'] == 'test' and record['created'] > now - 60 and
                       (digest in record['targets'] or record.get('test_owner') == current[1])
                       for record in history):
                    h.send(429, {'error': 'Wait one minute between notification tests.'})
                    return True
                enqueue(s, 'test', ttl=100, test_device=digest, delay=15)
                h.send(200, {'ok': True, 'delay_seconds': 15})
                return True
            if action == 'status':
                current = active_devices(s, digest).get(digest)
                h.send(200, {'subscribed': bool(current and current[0]['credential_id'] == ident['credential_id'])})
                return True
            if action == 'subscribe':
                if len(rows) >= 500 and digest not in rows:
                    raise ValueError('Notification registrations are full.')
                from event_operations import subject
                owner, _ = subject(s, ident)
                row = {'credential_id': ident['credential_id'], 'subscription': subscription,
                       'owner': owner, 'binding': uuid.uuid4().hex}
                if rows.get(digest, {}).get('owner') != owner or rows[digest]['subscription'] != subscription or rows[digest]['credential_id'] != ident['credential_id']:
                    rows[digest] = row
            elif rows.get(digest, {}).get('credential_id') == ident['credential_id']:
                rows.pop(digest)
            s.kv_set(KEY, rows)
            h.send(200, {'ok': True})
        except ValueError as error:
            h.send(400, {'error': str(error)})
    return True


def active_devices(s, only=None):
    """Resolve current participant grants and assignments, including legacy subscriptions."""
    from event_operations import stamp
    dataset = s.event_access.active_dataset(s)
    grants = {row['id']: row for row in s.event_access.auth_db(s).execute(
        "SELECT c.id,c.account_id,g.pair,g.member FROM access_credentials c "
        "JOIN person_accounts a ON a.id=c.account_id AND a.active=1 "
        "JOIN account_grants g ON g.account_id=a.id "
        "WHERE g.role='participant' AND c.active=1 AND g.dataset=?", (dataset,))}
    devices = {}
    subscriptions = s.kv_get(KEY) or {}
    candidates = subscriptions.items() if only is None else ((only, subscriptions[only]),) if only in subscriptions else ()
    for digest, row in candidates:
        grant = grants.get(row['credential_id'])
        if not grant:
            continue
        assignment = s.event_access.participant_assignment(s, dict(grant, role='participant'))
        if not assignment or not assignment['name'].strip():
            continue
        owner = stamp([grant['account_id'], dataset, grant['pair'], grant['member'], assignment['name']])
        if row.get('owner', owner) == owner:
            devices[digest] = (row, owner, grant['pair'])
    return devices


def provider_for(record):
    host = urlsplit(record['subscription']['endpoint']).hostname
    return 'Google' if host == 'fcm.googleapis.com' else 'product vendor'


def arm_test_device(s, digest, kind):
    if s.event_access.active_dataset(s) != 'rehearsal':
        raise ValueError('Device selection is available only in Rehearsal.')
    devices = active_devices(s)
    if not isinstance(digest, str) or digest not in devices or not isinstance(kind, str) or kind not in TEST_KINDS:
        raise ValueError('Choose an enrolled device and alert type.')
    record, owner, _ = devices[digest]
    s.kv_set(TEST_ARM, {'device':digest, 'kind':kind, 'binding':record.get('binding', owner),
                       'expires':time.time()+600})
    return {'armed':True, 'kind':kind, 'expires_in':600}


def test_controls(s):
    arm = s.kv_get(TEST_ARM) or {}
    devices = active_devices(s)
    if arm.get('expires', 0) <= time.time() or arm.get('device') not in devices:
        arm = {}
    return {'test_devices':[{'id':key,'label':f'Team {pair} · {provider_for(row)} · {key[:6]}'}
                            for key,(row,_,pair) in devices.items()],
            'test_arm':{key:arm[key] for key in ('device','kind','expires')} if arm else None}


def enqueue(s, kind, *, owner=None, recipients=None, ttl=300, stored=None, test_device=None, delay=0):
    """Record the alert and its device outcomes in the active workspace."""
    with s.LOCK:
        devices = active_devices(s)
        now = time.time()
        rows = s.kv_get(HISTORY) or []
        for record in rows:
            if record['expires'] <= now:
                for target in record['targets'].values():
                    if target['status'] in PENDING:
                        target['status'] = 'expired' if target['status'] == 'queued' else 'unconfirmed'
        # Keep active jobs and a bounded history of completed events.
        recent = [r for r in rows if not any(t['status'] in PENDING for t in r['targets'].values())][-100:]
        outstanding = [r for r in rows if any(t['status'] in PENDING for t in r['targets'].values())]
        targets = {}
        rehearsal = s.event_access.active_dataset(s) != 'live'
        arm = (s.kv_get(TEST_ARM) or {}) if rehearsal and kind in TEST_KINDS else {}
        selected = None
        for digest, (row, assigned_owner, pair) in devices.items():
            if test_device is not None and digest != test_device:
                continue
            if owner is not None and row.get('owner') != owner:
                continue
            if recipients is not None and not recipients(row['credential_id'], pair):
                continue
            binding = row.get('binding', assigned_owner)
            armed = (arm.get('kind') == kind and arm.get('device') == digest and
                     arm.get('binding') == binding and arm.get('expires', 0) > now)
            if armed:
                selected = digest
            targets[digest] = {'binding': binding,
                               'status': 'rehearsal' if rehearsal and kind != 'test' and not armed else 'queued',
                               'attempts': 0, 'next_at': now + delay}
        if len(outstanding) >= 100:
            for target in targets.values():
                if target['status'] == 'queued':
                    target['status'] = 'queue_full'
        rows = [*recent, *outstanding, {'id': uuid.uuid4().hex, 'kind': kind, 'created': now,
                                       'expires': now + ttl, 'targets': targets}]
        if test_device is not None:
            rows[-1]['test_owner'] = devices[test_device][1]
            endpoint = devices[test_device][0]['subscription']['endpoint']
            rows[-1]['provider'] = 'Google' if urlsplit(endpoint).hostname == 'fcm.googleapis.com' else 'product vendor'
        writes = [(HISTORY, rows)]
        if stored:
            writes.append(stored)
        if selected:
            rows[-1].update(rehearsal_device=selected, provider=provider_for(devices[selected][0]))
            writes.append((TEST_ARM, {}))
        with s.DB:
            s.DB.executemany('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                             [(key, json.dumps(value)) for key,value in writes])


def report(s):
    from collections import Counter
    rows = sorted(s.kv_get(HISTORY) or [], key=lambda row: row['created'], reverse=True)
    return {'devices': len(active_devices(s)), 'workspace': s.event_access.active_dataset(s),
            'events': [{'kind': row['kind'], 'created': row['created'],
                        'counts': dict(Counter(t['status'] for t in row['targets'].values())),
                        **({'provider': row['provider']} if 'provider' in row else {}),
                        'rehearsal_test': bool(row.get('rehearsal_device'))}
                       for row in rows[:30]]}


def announce(s, text, important=False, at=None):
    if text.strip():
        messages = s.kv_get(MESSAGES_KEY) or []
        record = {'id':uuid.uuid4().hex, 'text':text, 'important':important, 'updated_at':at or time.time()}
        enqueue(s, 'announcement', stored=(MESSAGES_KEY, (messages + [record])[-30:]))


def route_allowed(mode, record, digest):
    if mode == 'rehearsal':
        return record['kind'] == 'test' or record.get('rehearsal_device') == digest
    return record['kind'] != 'test' and not record.get('rehearsal_device')


class PushConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def relay_post(path, url, data, headers, timeout):
    """Keep encryption and signing on the web container, forward ciphertext over UDS."""
    import requests

    parts = urlsplit(url)
    target = parts.path or '/'
    if parts.query:
        target += '?' + parts.query
    connection = PushConnection(path, timeout)
    try:
        connection.request('POST', target, body=data,
                           headers={**headers, 'X-Push-Provider': parts.hostname})
        received = connection.getresponse()
        response = requests.Response()
        response.status_code = received.status
        response.headers.update(received.getheaders())
        response._content = received.read(8192)
        return response
    finally:
        connection.close()


def send(subscription, kind, ttl):
    import requests
    from pywebpush import webpush, WebPushException

    class NoRedirects(requests.Session):
        def post(self, url, **kwargs):
            relay = os.environ.get('KEVENOS_PUSH_SOCKET')
            if relay:
                return relay_post(relay, url, kwargs['data'], kwargs['headers'], kwargs['timeout'])
            return super().post(url, **kwargs, allow_redirects=False)

    try:
        subscription = validate_subscription(subscription)
        secret = Path(os.environ.get('KEVENOS_SECRETS_DIR', '/run/secrets')) / 'KEVENOS_WEB_PUSH_VAPID_PRIVATE_KEY'
        with NoRedirects() as session:
            result = webpush(subscription, data=json.dumps({'title': 'KevenOS', 'body': MESSAGES[kind],
                             'kind': kind}), vapid_private_key=secret.read_text(encoding='utf-8').strip(),
                             vapid_claims={'sub': os.environ.get('KEVENOS_VAPID_SUBJECT', 'mailto:noreply@example.com')}, ttl=ttl,
                             timeout=5, requests_session=session)
        return result.status_code
    except WebPushException as error:
        return error.response.status_code if error.response is not None else 503
    except requests.RequestException:
        return 503
    except (OSError, http.client.HTTPException):
        return 503
    except ValueError:
        return 400


def deliver_one(s, record_id, digest):
    """Claim a delivery, release the DB lock during network I/O, then save the receipt."""
    with s.WORKSPACE.read():
        with s.LOCK:
            rows = s.kv_get(HISTORY) or []
            record = next((r for r in rows if r['id'] == record_id), None)
            if not record or not route_allowed(s.WORKSPACE.mode, record, digest):
                return
            target = record['targets'][digest]
            now = time.time()
            if target['status'] != 'queued' or target['next_at'] > now:
                return
            current = active_devices(s, digest).get(digest)
            if record['expires'] <= now:
                target['status'] = 'expired'
            elif not current or target['binding'] != current[0].get('binding', current[1]):
                target['status'] = 'unsubscribed'
            else:
                target['status'] = 'sending'
                target['attempts'] += 1
            s.kv_set(HISTORY, rows)
            if target['status'] != 'sending':
                return
            subscription = current[0]['subscription']
            ttl = max(1, int(record['expires'] - now))
        code = send(subscription, record['kind'], ttl)
        with s.LOCK:
            rows = s.kv_get(HISTORY) or []
            record = next((r for r in rows if r['id'] == record_id), None)
            if not record:
                return
            target = record['targets'][digest]
            if 200 <= code < 300:
                target['status'] = 'accepted'
            elif code in (404, 410):
                target['status'] = 'invalid'
                devices = s.kv_get(KEY) or {}
                if devices.get(digest) == current[0]:
                    devices.pop(digest)
                    s.kv_set(KEY, devices)
            elif (code == 429 or code >= 500) and target['attempts'] < 3:
                target.update(status='queued', next_at=time.time() + 2 ** target['attempts'])
            else:
                target['status'] = 'failed'
            s.kv_set(HISTORY, rows)


def jobs(s, recover=False):
    with s.WORKSPACE.read(), s.LOCK:
        rows = s.kv_get(HISTORY) or []
        now = time.time()
        ready = []
        changed = False
        for record in rows:
            for digest, target in record['targets'].items():
                if not route_allowed(s.WORKSPACE.mode, record, digest):
                    continue
                if target['status'] == 'sending' and recover:
                    target['status'] = 'unconfirmed'
                    changed = True
                if target['status'] == 'queued':
                    if record['expires'] <= now:
                        target['status'] = 'expired'
                        changed = True
                    elif target['next_at'] <= now:
                        ready.append((record['id'], digest))
        if changed:
            s.kv_set(HISTORY, rows)
        return ready[:32]


def start(s):
    def work():
        from event_quiz import KEY as QUIZ_KEY, expire, load, now
        recovered = set()
        with ThreadPoolExecutor(max_workers=8) as pool:
            while True:
                try:
                    with s.WORKSPACE.read(), s.LOCK:
                        if s.kv_get(QUIZ_KEY):
                            quiz = load(s)
                            before = [(r['status'], r['released']) for r in quiz['runs']]
                            expire(quiz, now(s))
                            if before != [(r['status'], r['released']) for r in quiz['runs']]:
                                quiz['rev'] += 1
                                s.kv_set(QUIZ_KEY, quiz)
                    with s.WORKSPACE.read():
                        mode = s.WORKSPACE.mode
                        pending = jobs(s, mode not in recovered)
                        recovered.add(mode)
                    futures = [pool.submit(deliver_one, s, *job) for job in pending]
                    for future in futures:
                        future.result()
                except Exception as error:
                    # Provider URLs and credentials never belong in log output.
                    print('Participant notification worker:', type(error).__name__, flush=True)
                time.sleep(1)
    threading.Thread(target=work, daemon=True, name='participant-notifications').start()


def quiz_transition_snapshot(state):
    """Detach only prior fields consumed by quiz_changed, before any mutation.

    Caller must hold the event lock/fenced transaction from load through save.
    Answers, member records and reset archives are not notification transitions.
    """
    fields = ('id', 'status', 'resends', 'kind', 'released')
    return {'runs': [deepcopy({key: run[key] for key in fields if key in run})
                     for run in state.get('runs', [])],
            'release': deepcopy(state.get('release'))}


def quiz_changed(s, before, after):
    """Use persisted quiz transitions so polling and repeats do not send twice."""
    if not after.get('enabled'):
        return False
    import event_quiz
    previously = {r['id']: r for r in before.get('runs', [])}
    opened = [r for r in after['runs'] if r['status'] == 'open' and
              (r['id'] not in previously or previously[r['id']]['status'] != 'open'
               or r.get('resends') != previously[r['id']].get('resends'))]
    def ready(state):
        post = [r for r in state.get('runs', []) if r['kind'] == 'post' and r['status'] != 'void']
        post_closed = bool(post) and all(r['status'] == 'closed' and r['released'] for r in post)
        return {r['id'] for r in state.get('runs', []) if event_quiz.result_ready(r, post_closed)}
    released = ready(after) - ready(before)
    published = bool(after.get('release') and not before.get('release'))
    stored = (event_quiz.KEY, after)
    if opened:
        def can_answer(credential, pair):
            joined = after['members'].get(credential, {}).get('joined')
            answers = after['answers'].get(credential, {})
            return any(r['id'] not in answers and event_quiz.eligible(r, pair) and
                       (joined or r.get('manual_delivery') or r['kind'] in {'pulse', 'vote'}) for r in opened)
        ttl = max(1, min(300, int(max(r['end'] if r['end'] is not None else event_quiz.now(s) + 300 for r in opened) - event_quiz.now(s))))
        enqueue(s, 'question', recipients=can_answer, ttl=ttl, stored=stored)
        stored = None
    if released or published:
        enqueue(s, 'results', recipients=None if published else
                lambda credential, pair: bool(released.intersection(after['answers'].get(credential, {}))),
                stored=stored)
    return bool(opened or released or published)
