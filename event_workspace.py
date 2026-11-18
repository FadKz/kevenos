"""Isolated event workspaces with durable selection and request-level switching."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

import database
import event_access
import event_config


def initial_context(s):
    configured = os.environ.get('KEVENOS_WORKSPACE_CONTEXT', '').strip()
    if configured:
        if not all(c in '0123456789abcdef' for c in configured.lower()) or len(configured) != 32:
            raise RuntimeError('KEVENOS_WORKSPACE_CONTEXT must be 32 hexadecimal characters.')
        return configured.lower()
    if os.environ.get('KEVENOS_STABLE_DEMO_CONTEXT') == '1':
        return hashlib.sha256(('kevenos-demo:'+s.CFG['secret']).encode()).hexdigest()[:32]
    return uuid.uuid4().hex


class RequestGuard:
    """Concurrent readers, writer preference, and reentrant exclusive ownership."""
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = 0
        self.waiting = 0
        self.writer = None
        self.depth = 0
        self.local = threading.local()
        self.blocked = False

    @contextmanager
    def read(self):
        ident = threading.get_ident()
        with self.condition:
            if self.blocked:
                raise RuntimeError('Workspace recovery is required before serving requests.')
            nested = getattr(self.local, 'reads', 0)
            while self.writer not in (None, ident) or (self.waiting and not nested and self.writer != ident):
                self.condition.wait()
            if self.blocked:
                raise RuntimeError('Workspace recovery is required before serving requests.')
            self.readers += 1
            self.local.reads = nested + 1
        try:
            yield
        finally:
            with self.condition:
                self.readers -= 1
                self.local.reads -= 1
                self.condition.notify_all()

    @contextmanager
    def write(self):
        ident = threading.get_ident()
        with self.condition:
            if self.blocked:
                raise RuntimeError('Workspace recovery is required before serving requests.')
            if self.writer == ident:
                self.depth += 1
            else:
                if getattr(self.local, 'reads', 0):
                    raise RuntimeError('Workspace mutation requires an exclusive request guard.')
                self.waiting += 1
                try:
                    while self.writer is not None or self.readers:
                        self.condition.wait()
                    if self.blocked:
                        raise RuntimeError('Workspace recovery is required before serving requests.')
                    self.writer = ident
                    self.depth = 1
                finally:
                    self.waiting -= 1
        try:
            yield
        finally:
            with self.condition:
                self.depth -= 1
                if not self.depth:
                    self.writer = None
                    self.condition.notify_all()


class Controller:
    def __init__(self, s, live_db_path, root_data):
        self.s = s
        self.root = Path(root_data).resolve()
        self.live_path = Path(live_db_path).resolve()
        self.live = s.DB
        self.defaults = event_config._read(self.live) or event_config._defaults(s)
        self.seed_configuration = None
        self.rehearsal = None
        self.guard = RequestGuard()
        self.read, self.write = self.guard.read, self.guard.write
        s.AUTH_DB = self.live
        import event_transition
        event_transition.recover(self)
        row = self.live.execute("SELECT v FROM kv WHERE k='workspace_control'").fetchone()
        control = json.loads(row['v']) if row else {'mode':'live', 'context':initial_context(s)}
        if control.get('mode') not in ('live','rehearsal') or not isinstance(control.get('context'),str) or not control['context']:
            raise RuntimeError('Invalid saved event workspace. Restore the control record before starting.')
        self.mode = control['mode']
        self.context = self._selection_context = control['context']
        if not row:
            self._persist(control, invalidate=False)
        self._activate(self.mode)
        self._refresh_context()

    def rehearsal_context(self, epoch):
        return hashlib.sha256(f'{self._selection_context}:{epoch}'.encode()).hexdigest()[:32]

    def _refresh_context(self):
        epoch = self.s.kv_get('rehearsal_resize_epoch') if self.mode == 'rehearsal' else None
        self.context = self.rehearsal_context(epoch) if epoch else self._selection_context

    def sync(self):
        """Refresh cached workspace selection after another process changes it."""
        row = self.live.execute("SELECT v FROM kv WHERE k='workspace_control'").fetchone()
        if not row:
            return
        control = json.loads(row['v'])
        if control.get('mode') == self.mode and control.get('context') == self._selection_context:
            self._refresh_context()
            return
        with self.write(), self.s.LOCK:
            row = self.live.execute("SELECT v FROM kv WHERE k='workspace_control'").fetchone()
            if not row:
                return
            control = json.loads(row['v'])
            self._activate(control['mode'])
            self.mode, self._selection_context = control['mode'], control['context']
            self._refresh_context()

    def _persist(self, control, invalidate=True):
        # Selection and session invalidation are one durable SQLite transaction.
        with self.s.LOCK, self.live:
            if invalidate:
                self.live.execute("DELETE FROM role_sessions WHERE role IN ('participant','judge') AND (assurance='participant_qr' OR NOT EXISTS (SELECT 1 FROM access_credentials c JOIN account_grants g ON g.account_id=c.account_id WHERE c.id=role_sessions.credential_id AND g.dataset='global'))")
            self.live.execute('UPDATE access_profiles SET revoked=1')
            self.live.execute("INSERT INTO kv(k,v) VALUES('workspace_control',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(control),))

    def _seed(self, db):
        from rehearsal_reconcile import _digest, seed_member, seed_sheet, seed_team

        s = self.s
        content = s.bundled_content()
        previous_content = s.CONTENT
        try:
            s.CONTENT = content
            configuration = event_config.initialize_db(s, db, self.seed_configuration or self.defaults)
        finally:
            s.CONTENT = previous_content
        if s.hosted(): print('Startup rehearsal-config-ready', file=sys.stderr, flush=True)
        with db:
            roster_rows = []
            member_rows = []
            for pair in range(1, configuration['team_count'] + 1):
                people = [{'id': member, 'name': seed_member(pair, member), 'coach': 0}
                          for member in range(1, event_config.member_capacity(configuration, pair) + 1)]
                roster_rows.append((pair, seed_team(pair), people[0]['name'], people[1]['name'] if len(people)>1 else ''))
                member_rows.extend((pair, person['id'], person['name'], 0) for person in people)
            db.executemany('INSERT INTO roster(pair,partner,p1,p2) VALUES(?,?,?,?)', roster_rows)
            db.executemany('INSERT INTO roster_members(pair,member,name,coach) VALUES(?,?,?,?)', member_rows)
            if s.hosted(): print('Startup rehearsal-roster-ready', file=sys.stderr, flush=True)
            sheets = []
            for station in range(1, min(configuration['judge_count'], configuration['team_count']) + 1):
                sheet = seed_sheet(station)
                sheet.update(created=time.time(), app_version=s.APP_VERSION, content_version=str(content.get('content_version', 1)))
                for field in ('ticks', 'asked', 'raised'):
                    sheet[field] = json.dumps(sheet[field])
                sheets.append(sheet)
            db.executemany("""INSERT INTO sheets(kind,pair,station,round,judge,ticks,ai,spoke,notes,feedback,asked,raised,
                          created,app_version,content_version)
                          VALUES(:kind,:pair,:station,:round,:judge,:ticks,:ai,:spoke,:notes,:feedback,:asked,:raised,
                          :created,:app_version,:content_version)""", sheets)
            sheet_fixtures = {str(row['id']): _digest(dict(row)) for row in db.execute(
                'SELECT * FROM sheets ORDER BY id DESC LIMIT ?', (len(sheets),)).fetchall()}
            if s.hosted(): print('Startup rehearsal-sheets-ready', file=sys.stderr, flush=True)
            fixtures = {'configuration': configuration, 'sheets': sheet_fixtures}
            values = {'mode':'desk','clock':dict(s._fresh_clock(),round=min(2,configuration['rounds'])),'program':{'i':0,'since':time.time(),'mode':'manual','announce':''},'public':'off','reveal':'hidden','workspace_seeded':True, 'rehearsal_fixtures': fixtures}
            db.executemany('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', [(k,json.dumps(v)) for k,v in values.items()])
        if s.hosted(): print('Startup rehearsal-seed-ready', file=sys.stderr, flush=True)

    def _practice(self):
        if self.rehearsal is None:
            folder = self.root/'rehearsal'
            folder.mkdir(parents=True, exist_ok=True)
            self.rehearsal = self.s.open_db(str(folder/'judging.db'))
            if self.s.hosted(): print('Startup rehearsal-database-ready', file=sys.stderr, flush=True)
            with database.named_lock(self.rehearsal, 'rehearsal-seed'):
                if not self.rehearsal.execute("SELECT 1 FROM kv WHERE k='workspace_seeded'").fetchone():
                    self._seed(self.rehearsal)
        return self.rehearsal

    def _activate(self, mode):
        s = self.s
        target_db = self.live if mode == 'live' else self._practice()
        target_data = str(self.root if mode == 'live' else self.root/'rehearsal')
        content = s.load_content(target_db, target_data)
        s.DB, s.DATA, s.CONTENT = target_db, target_data, content
        event_config.activate(s, self.defaults)
        s.CFG['rehearsal'] = mode == 'rehearsal'
        s.LIVE.clear()

    def switch(self, mode, confirm):
        if mode not in ('live','rehearsal') or confirm != mode.upper():
            raise ValueError('Confirm LIVE or REHEARSAL to select the event workspace.')
        saved = self.live.execute("SELECT v FROM kv WHERE k='workspace_control'").fetchone()
        control = json.loads(saved['v']) if saved else None
        if (control and self.mode == mode and control.get('mode') == mode
                and control.get('context') == self._selection_context):
            self._refresh_context()
            return self.status()
        with self.write(), self.s.LOCK, database.named_lock(self.live, 'workspace-switch'):
            saved = self.live.execute("SELECT v FROM kv WHERE k='workspace_control'").fetchone()
            control = json.loads(saved['v']) if saved else None
            if control and control.get('mode') == mode:
                self._activate(mode)
                self.mode, self._selection_context = mode, control['context']
                self._refresh_context()
                return self.status()
            old_mode = self.mode
            self._activate(mode)
            control = {'mode':mode,'context':uuid.uuid4().hex}
            try:
                self._persist(control)
            except Exception:
                self._activate(old_mode)
                raise
            self.mode, self._selection_context = mode, control['context']
            self._refresh_context()
            return self.status()

    def reset(self, confirm, configuration=None):
        if confirm != 'RESET REHEARSAL':
            raise ValueError('Type RESET REHEARSAL to replace practice data.')
        with self.write(), self.s.LOCK:
            if self.mode != 'rehearsal':
                raise ValueError('Select rehearsal before resetting practice data.')
            self.seed_configuration = event_config._validate(configuration or event_config.config(self.s))
            folder = self.root/'rehearsal'
            archived = self.root/('.rehearsal-reset-'+uuid.uuid4().hex)
            # Advance the epoch before filesystem changes: even an interrupted
            # reset cannot allow a pre-reset client to write into fresh data.
            control = {'mode':'rehearsal','context':uuid.uuid4().hex}
            with self.live:
                self.live.execute("DELETE FROM role_sessions WHERE dataset='rehearsal' AND role IN ('participant','judge')")
                self._persist(control, invalidate=False)
            self._selection_context = control['context']
            self._refresh_context()
            if database.is_server(self.rehearsal):
                with database.named_lock(self.rehearsal, 'rehearsal-reset'), self.rehearsal:
                    database.clear_schema(self.rehearsal)
                    self.rehearsal.execute("INSERT INTO kv(k,v) VALUES('schema_version',?)", (json.dumps(self.s.SCHEMA_VERSION),))
                    self._seed(self.rehearsal)
                self._activate('rehearsal')
                with self.live:
                    self.live.execute("DELETE FROM access_credentials WHERE dataset='rehearsal' AND role='participant' AND NOT EXISTS (SELECT 1 FROM account_grants g WHERE g.account_id=access_credentials.account_id AND NOT (g.role='participant' AND g.dataset='rehearsal'))")
                    affected=[r[0] for r in self.live.execute("SELECT account_id FROM account_grants WHERE dataset='rehearsal' AND role='participant'")]
                    self.live.execute("DELETE FROM account_grants WHERE dataset='rehearsal' AND role='participant'")
                    for aid in affected:
                        self.live.execute('UPDATE person_accounts SET revision=revision+1 WHERE id=?',(aid,))
                        event_access.event_roles.revoke_sessions(self.live,aid)
                self.seed_configuration = None
                self._refresh_context()
                return self.status()
            self.rehearsal.commit()
            self.rehearsal.close()
            self.rehearsal = None
            try:
                folder.rename(archived)
            except Exception:
                self.seed_configuration = None
                self._activate('rehearsal')
                self._refresh_context()
                raise
            try:
                self._activate('rehearsal')
                with self.live:
                    # Staff reauthenticate against the same password after the reset.
                    self.live.execute("DELETE FROM access_credentials WHERE dataset='rehearsal' AND role='participant' AND NOT EXISTS (SELECT 1 FROM account_grants g WHERE g.account_id=access_credentials.account_id AND NOT (g.role='participant' AND g.dataset='rehearsal'))")
                    affected=[r[0] for r in self.live.execute("SELECT account_id FROM account_grants WHERE dataset='rehearsal' AND role='participant'")]
                    self.live.execute("DELETE FROM account_grants WHERE dataset='rehearsal' AND role='participant'")
                    for aid in affected:
                        self.live.execute('UPDATE person_accounts SET revision=revision+1 WHERE id=?',(aid,))
                        event_access.event_roles.revoke_sessions(self.live,aid)
            except Exception:
                if self.rehearsal is not None: self.rehearsal.close()
                shutil.rmtree(folder, ignore_errors=True)
                archived.rename(folder)
                self.rehearsal = None
                self._activate('rehearsal')
                self._refresh_context()
                raise
            finally:
                self.seed_configuration = None
            shutil.rmtree(archived, ignore_errors=True)
            self._refresh_context()
            return self.status()

    def status(self):
        s = self.s
        with s.LOCK:
            configuration = event_config.config(s)
            roster = event_config.list_roster(s)
            registered = sum(any(person['name'].strip() for person in row['members']) for row in roster)
            participants = sum(bool(person['name'].strip()) for row in roster for person in row['members'])
            count = lambda sql,args=(): self.live.execute(sql,args).fetchone()[0]
            summary = {'registered_pairs':registered,'participants':participants,
                'issued_participants':count("SELECT count(*) FROM access_credentials WHERE role='participant' AND active=1 AND dataset=?",(self.mode,)),
                'staff_accounts':count("SELECT count(*) FROM access_credentials WHERE role IN ('admin','desk') AND active=1"),
                'judge_invites':s.DB.execute('SELECT count(*) FROM judge_invites WHERE used=0 AND expires>?',(time.time(),)).fetchone()[0],
                'judge_sessions':count("SELECT count(DISTINCT station) FROM role_sessions WHERE role='judge' AND dataset=? AND expires>?",(self.mode,time.time())),
                'round_sheets':s.DB.execute("SELECT count(*) FROM sheets WHERE kind='round' AND voided=0").fetchone()[0],
                'expected_pairs':configuration['team_count'],'expected_participants':configuration['participant_count'],
                'expected_judges':configuration['judge_count'],'team_size':configuration['team_size'],
                'round_count':configuration['rounds'],'pitch_block_minutes':configuration['pitch_block_minutes']}
            seeded = bool(s.DB.execute("SELECT 1 FROM kv WHERE k='workspace_seeded'").fetchone())
        return {'mode':self.mode,'context':self.context,'seeded':seeded,'summary':summary,'configuration':configuration}

    def close(self):
        if self.rehearsal is not None: self.rehearsal.close()
        self.live.close()


def initialize(s, live_db_path, root_data):
    s.WORKSPACE = Controller(s, live_db_path, root_data)
    return s.WORKSPACE


def _admin(s, h):
    ident = event_access.session(s, h.headers)
    if not ident and not s.hosted() and s.who(h.headers):
        role, _, _ = s.who(h.headers)
        ident = {'role':'admin' if role == 'desk' else role}
    if not ident:
        h.send(401, {'error':'Sign in to continue.'})
        return False
    if ident['role'] != 'admin':
        h.send(403, {'error':'Admin access is required.'})
        return False
    return True


def handle_get(s, h, path, q):
    if path == '/api/admin/live-transition':
        import event_transition
        return event_transition.handle(s, h)
    if path != '/api/admin/workspace': return False
    if _admin(s,h): h.send(200,s.WORKSPACE.status())
    return True


def handle_post(s, h, path, body):
    if path == '/api/admin/live-transition':
        import event_transition
        return event_transition.handle(s, h, body)
    if path != '/api/admin/workspace': return False
    if not _admin(s,h): return True
    try:
        result = s.WORKSPACE.reset(body.get('confirm')) if body.get('action') == 'reset-rehearsal' else s.WORKSPACE.switch(body.get('mode'),body.get('confirm'))
        h.send(200,result)
    except ValueError as exc:
        h.send(400,{'error':str(exc)})
    return True
