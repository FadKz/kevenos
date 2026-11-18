"""Registration operations and single-use participant sign-in links."""
import base64
from copy import deepcopy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import database

PATH = '/api/registration'
REDEEM = '/api/access/registration-link'
LINK_TTL = 300


class Conflict(ValueError):
    pass


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS registration_links (
        digest TEXT PRIMARY KEY, credential_id TEXT NOT NULL UNIQUE,
        revision INTEGER NOT NULL, dataset TEXT NOT NULL,
        context TEXT NOT NULL, expires REAL NOT NULL)""")


def active_credentials(s):
    rows=s.event_access.auth_db(s).execute("""SELECT c.*,g.pair AS grant_pair,g.member AS grant_member,g.subject
        FROM account_grants g JOIN person_accounts a ON a.id=g.account_id AND a.active=1
        JOIN access_credentials c ON c.account_id=a.id AND c.active=1
        WHERE g.role='participant' AND g.dataset=?""",(s.event_access.active_dataset(s),))
    return {(r['grant_pair'],r['grant_member']):dict(r,pair=r['grant_pair'],member=r['grant_member']) for r in rows}


def snapshot(s):
    config = s.event_config.config(s)
    attendees = s.kv_get('attendees') or {'participants': {}, 'directory': {}}
    issued = active_credentials(s)
    teams = []
    for pair in range(1, config['team_count'] + 1):
        row = s.roster_row(pair)
        team = {key: row[key] for key in ('pair', 'partner', 'station', 'round', 'scenario')}
        team['members'] = []
        for person in row['members']:
            member = person['id']
            meta = attendees['participants'].get(f'{pair}:{member}', {})
            if meta.get('name') != person['name']:
                meta = {}
            credential = issued.get((pair, member))
            team['members'].append(person | {'checked_in': bool(meta.get('checked_in')),
                'checked_in_at': meta.get('checked_in_at'), 'has_password': bool(credential),
                'access_revision': credential['revision'] if credential else None})
        teams.append(team)
    credentials = sorted((row['id'], row['revision']) for row in issued.values())
    revision = hashlib.sha256(json.dumps([teams, credentials], sort_keys=True).encode()).hexdigest()
    return {'teams': teams, 'revision': revision, 'rehearsal': s.event_access.active_dataset(s) == 'rehearsal',
            'roster_locked': s.event_config._running(s) or s.event_config._final_started(s)}


def slot(s, body, prefix='', occupied=True):
    pair, member = body.get(prefix+'pair'), body.get(prefix+'member')
    if type(pair) is not int or type(member) is not int:
        raise ValueError('Choose a team and a member slot.')
    row = s.roster_row(pair)
    if not row or not 1 <= member <= len(row['members']):
        raise ValueError('Choose a member slot within the event capacity.')
    person = row['members'][member-1]
    if bool(person['name']) != occupied:
        raise Conflict('This slot is empty.' if occupied else 'That slot is already occupied. Choose an empty slot.')
    return row, person


def name_field(value, label, required=True):
    if not isinstance(value, str) or len(value.strip()) > 80 or (required and not value.strip()):
        raise ValueError(f'Enter a valid {label} of up to 80 characters.')
    return value.strip()


def update_roster(s, body, action, actor):
    import event_commands
    if s.event_config._running(s) or s.event_config._final_started(s):
        raise Conflict('Judging is underway. Ask the Scoring Desk to handle team changes.')
    row, person = slot(s, body, occupied=action != 'register')
    pairs = {row['pair']: row}
    directory = deepcopy(event_commands._attendees(s))
    previous = directory['participants'].get(f'{row["pair"]}:{person["id"]}', {})
    if previous.get('name') != person['name']:
        previous = {}
    other = None
    if action == 'register':
        person['name'] = name_field(body.get('name'), 'participant name')
        if any(member['name'].casefold() == person['name'].casefold() for member in row['members'] if member is not person):
            raise Conflict('Someone with that name is already on this team.')
        if not any(member['name'] for member in row['members'] if member is not person):
            row['partner'] = name_field(body.get('company', ''), 'company name', required=False) or row['partner']
        if type(body.get('checked_in', False)) is not bool:
            raise ValueError('Choose a valid check-in state.')
        if body.get('checked_in'):
            previous = {'name': person['name'], 'checked_in': True, 'checked_in_at': s.time.time()}
    elif action == 'correct':
        person['name'] = name_field(body.get('name'), 'participant name')
        if any(member['name'].casefold() == person['name'].casefold() for member in row['members'] if member is not person):
            raise Conflict('Someone with that name is already on this team.')
    else:
        target, replacement = slot(s, body, 'to_', occupied=action == 'swap')
        if (row['pair'], person['id']) == (target['pair'], replacement['id']):
            raise Conflict('Choose a different participant or slot.')
        if target['pair'] == row['pair']:
            replacement = row['members'][replacement['id']-1]
            target = row
        else:
            pairs[target['pair']] = target
        if any(member['name'].casefold() == person['name'].casefold() for member in target['members'] if member is not person and member is not replacement):
            raise Conflict('Someone with that name is already on the destination team.')
        if action == 'swap':
            if any(member['name'].casefold() == replacement['name'].casefold() for member in row['members'] if member is not person and member is not replacement):
                raise Conflict('Someone with that name is already on the original team.')
            if replacement['name'].casefold() == person['name'].casefold():
                raise Conflict('These participants have the same name. Ask Admin to verify their identities before swapping.')
            swapped = directory['participants'].get(f'{target["pair"]}:{replacement["id"]}', {})
            if swapped.get('name') == replacement['name']:
                other = (row['pair'], person['id'], swapped)
        if not any(member['name'] for member in target['members']):
            target['partner'] = name_field(body.get('company', ''), 'company name', required=False) or row['partner']
        original = {'name': replacement['name'], 'coach': replacement['coach']} if action == 'swap' else {'name': '', 'coach': 0}
        replacement.update(name=person['name'], coach=person['coach'])
        person.update(original)
        row, person = target, replacement
    for pair in pairs:
        clock = s.clock_public(pairs[pair]['station'])
        started = clock['round'] > pairs[pair]['round'] or (clock['round'] == pairs[pair]['round'] and clock['started_any'])
        scored = s.DB.execute("SELECT 1 FROM sheets WHERE pair=? AND kind IN ('round','final') AND voided=0", (pair,)).fetchone()
        if started or scored:
            raise Conflict('This assignment has begun or has a score. Ask the Scoring Desk to handle the change.')
    untouched = [item for item in s.event_config.list_roster(s) if item['pair'] not in pairs]
    s.event_config.save_roster(s, [*untouched, *pairs.values()])
    if previous or other:
        current = event_commands._attendees(s)
        if previous:
            current['participants'][f'{row["pair"]}:{person["id"]}'] = previous | {'name': person['name']}
        if other:
            other_pair, other_member, other_meta = other
            current['participants'][f'{other_pair}:{other_member}'] = other_meta
        s.kv_set('attendees', current)
    s.audit(actor, 'registration-'+action, f'Team {row["pair"]} / member {person["id"]}')


def credential_action(s, h, body, action, actor):
    row, person = slot(s, body)
    pair, member = row['pair'], person['id']
    access = s.event_access
    db = access.auth_db(s)
    current = active_credentials(s).get((pair, member))
    if current and action in ('reset','revoke') :
        roles=[dict(g) for g in db.execute('SELECT role FROM account_grants WHERE account_id=?',(current['account_id'] or current['id'],))]
        if any(g['role']!='participant' for g in roles):
            raise Conflict('This participant also has staff access. Ask Admin to manage their password.')
    if action == 'issue' and current:
        raise Conflict('A password already exists. Use a sign-in QR or explicitly reset it.')
    if action in ('reset', 'revoke') and not current:
        raise Conflict('There is no active password for this participant.')
    with db:
        result = {}
        if action == 'revoke':
            db.execute('UPDATE access_credentials SET active=0,password_box=NULL WHERE id=?', (current['id'],))
            db.execute('DELETE FROM role_sessions WHERE credential_id=?', (current['id'],))
        elif action == 'reset':
            db.execute('DELETE FROM role_sessions WHERE credential_id=?', (current['id'],))
            db.execute('DELETE FROM access_credentials WHERE id=?', (current['id'],))
            result = access.create_credential(s, 'participant', person['name'], pair, member,
                                              cid=current['id'], revision=current['revision']+1)
            db.execute('UPDATE access_credentials SET created=? WHERE id=?', (current['created'], current['id']))
        elif not current:
            result = access.create_credential(s, 'participant', person['name'], pair, member)
        if action == 'qr':
            if getattr(s, 'WORKSPACE', None) is None:
                raise Conflict('QR sign-in requires an event workspace.')
            code = secrets.token_urlsafe(32)
            credential = current or result['credential']
            now = s.time.time()
            db.execute('DELETE FROM registration_links WHERE expires<=? OR credential_id=?', (now, credential['id']))
            db.execute('INSERT INTO registration_links VALUES(?,?,?,?,?,?)',
                       (s.credential_digest(code), credential['id'], credential['revision'],
                        access.active_dataset(s), s.WORKSPACE.context, now + LINK_TTL))
            from qrcode import make
            from qrcode.image.svg import SvgPathFillImage
            host = h.headers.get('Host', '')
            local = re.fullmatch(r'(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?', host)
            origin = 'http://' + host if local else os.environ.get('KEVENOS_PUBLIC_URL', 'https://event.example.com').rstrip('/')
            link = origin + '/join#' + code
            svg = make(link, image_factory=SvgPathFillImage).to_string()
            result.update(link=link, qr_image='data:image/svg+xml;base64,' + base64.b64encode(svg).decode(),
                          expires_at=now + LINK_TTL)
        elif current:
            db.execute('DELETE FROM registration_links WHERE credential_id=?', (current['id'],))
    s.audit(actor, 'registration-'+action, f'Team {pair} / member {member}')
    return result | {'ok': True, 'name': person['name']}


def redeem(s, h, body):
    access = s.event_access
    db = access.auth_db(s)
    code = body.get('code')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code) or not s.WORKSPACE:
        return h.send(403, {'error': 'This sign-in link is invalid or expired. Ask Registration for a fresh QR.'})
    previous = access.root_session(s, h.headers)
    if previous and (previous['role'] not in ('guest', 'participant') or any(g['role'] not in ('guest','participant') for g in previous.get('grants',[]))):
        return h.send(409, {'error': 'This browser is signed in as event staff. Open the QR link in a private browser tab to use it without signing out.'})
    ip, _ = access.client_location(s, h)
    if s.throttled(ip):
        return h.send(429, {'error': 'Too many sign-in attempts. Try again in a minute.'})
    with db:
        key = s.credential_digest(code)
        row = db.execute("""SELECT c.* FROM registration_links l
            JOIN access_credentials c ON c.id=l.credential_id AND c.revision=l.revision
            WHERE l.digest=? AND l.dataset=? AND l.context=? AND l.expires>?
              AND c.active=1""",
            (key, access.active_dataset(s), s.WORKSPACE.context, s.time.time())).fetchone()
        if row:
            assignment=next((g for g in access.event_roles.grants(s,row['account_id']) if g['role']=='participant'),None)
            row=dict(row, pair=assignment['pair'],member=assignment['member'],label=assignment['subject']) if assignment else None
        if row:
            roster = s.roster_row(row['pair'])
            if not roster or not 1 <= row['member'] <= len(roster['members']) or roster['members'][row['member']-1]['name'] != row['label']:
                row = None
        if row and previous and previous['role'] == 'participant' and previous['credential_id'] != row['id']:
            return h.send(409, {'error': 'This browser is signed in as a different participant. Open the QR link in a private browser tab.'})
        if row and db.execute('DELETE FROM registration_links WHERE digest=?', (key,)).rowcount != 1:
            row = None
        cookie = None
        if row and (not previous or previous['role'] == 'guest'):
            token, ttl = access.mint_session(s, 'participant', row['id'], row['label'], assurance='participant_qr')
            cookie = access.access_cookie(s, token, ttl)
            if previous:
                db.execute('DELETE FROM role_sessions WHERE digest=?', (previous['digest'],))
            db.execute('UPDATE access_credentials SET last_used=? WHERE id=?', (s.time.time(), row['id']))
    if not row:
        s.throttled(ip, True)
        return h.send(403, {'error': 'This sign-in link has expired or been used. Ask Registration for a fresh QR.'})
    h._activity_identity = {'role': 'participant', 'credential_id': row['id'], 'label': row['label']}
    h._activity_action = 'registration-qr-signin'
    return h.send(200, {'next': '/participant', 'session_changed': cookie is not None},
                  extra={'Set-Cookie': cookie} if cookie else None)


def handle(s, h, path, body=None):
    if path not in (PATH, REDEEM):
        return False
    with s.LOCK:
        if path == REDEEM:
            if body is None:
                h.send(405, {'error': 'Open your QR sign-in link in a browser.'})
            else:
                redeem(s, h, body)
            return True
        ident = s.event_access.session(s, h.headers)
        if not ident or ident['role'] not in ('registration', 'admin'):
            h.send(403 if ident else 401, {'error': 'Sign in with a Registration or Admin password.'})
            return True
        if body is None:
            h.send(200, snapshot(s))
            return True
        try:
            if body.get('revision') != snapshot(s)['revision']:
                raise Conflict('Registration changed. Refresh and review the participant before retrying.')
            action = body.get('action')
            if action in ('register', 'correct', 'move', 'swap'):
                update_roster(s, body, action, ident['role'])
                result = {'ok': True}
            elif action == 'checkin':
                import event_commands
                row, person = slot(s, body)
                if type(body.get('checked_in')) is not bool:
                    raise ValueError('Choose a valid check-in state.')
                event_commands._update_participant(s, {'pair': row['pair'], 'member': person['id'],
                    'checked_in': body['checked_in']}, ident['role'], ident)
                result = {'ok': True}
            elif action in ('issue', 'reset', 'revoke', 'qr'):
                result = credential_action(s, h, body, action, ident['role'])
            else:
                raise ValueError('Choose a registration action.')
            result['state'] = snapshot(s)
        except Conflict as error:
            h.send(409, {'error': str(error)})
        except ValueError as error:
            h.send(400, {'error': str(error)})
        except database.integrity_errors():
            h.send(409, {'error': 'Participant access changed. Refresh before retrying.'})
        else:
            h.send(200, result)
    return True
