"""Versioned machine API: per-consumer grants, optimistic revisions and durable receipts.

Browser credentials never authorize this surface. The vault holds raw bearer
values; this process reads only a digest/grant file, reloaded on every request.
"""
from contextlib import nullcontext
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlparse
import uuid

PREFIX = '/api/v1/agent/'
MAX_BODY = 400_000
MAX_RESPONSE = 2_000_000
RATE = {}


class APIError(Exception):
    def __init__(self, code, message, status=400, retryable=False):
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
        super().__init__(message)


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS agent_receipts (
      principal TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
      command TEXT NOT NULL, context TEXT NOT NULL, workspace TEXT NOT NULL, created REAL NOT NULL,
      state TEXT NOT NULL, status INTEGER, response TEXT,
      PRIMARY KEY(principal,key));
    CREATE TABLE IF NOT EXISTS agent_confirmations (
      id TEXT PRIMARY KEY, principal TEXT NOT NULL, fingerprint TEXT NOT NULL,
      context TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS agent_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, principal TEXT NOT NULL,
      operation TEXT NOT NULL, status INTEGER NOT NULL, request_id TEXT NOT NULL,
      workspace TEXT NOT NULL, context TEXT NOT NULL);
    ''')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _catalog():
    import event_commands
    return event_commands.catalog()


def _scopes(entry):
    scope = entry['scope']
    return [scope] if isinstance(scope, str) else scope


def authorized(principal, entry):
    return all(scope in principal['scopes'] for scope in _scopes(entry))


def authenticate(headers):
    values = headers.get_all('Authorization') if hasattr(headers, 'get_all') else [headers.get('Authorization', '')]
    if len(values or []) != 1 or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{40,256}', values[0]):
        raise APIError('unauthorized', 'Use a dedicated KevenOS agent bearer.', 401)
    token_digest = hashlib.sha256(values[0][7:].encode()).hexdigest()
    filename = os.environ.get('KEVENOS_AGENT_PRINCIPALS_FILE', '')
    if not filename:
        raise APIError('agent_access_disabled', 'Agent access is not configured.', 503)
    try:
        with Path(filename).open('rb') as source:
            raw = source.read(65537)
        if len(raw) > 65536: raise ValueError()
        config = json.loads(raw)
        principals = config['principals']
        if not isinstance(principals, list) or len(principals) > 100: raise ValueError()
        seen_ids, seen_digests = set(), set()
        known_scopes = {scope for group in ('resources', 'commands') for entry in _catalog()[group] for scope in _scopes(entry)}
        found = None
        for grant in principals:
            if not isinstance(grant, dict): raise ValueError()
            ident, digest = grant['id'], grant['token_sha256']
            if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}', ident): raise ValueError()
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest): raise ValueError()
            if ident in seen_ids or digest in seen_digests: raise ValueError()
            seen_ids.add(ident); seen_digests.add(digest)
            if not isinstance(grant['label'], str) or not 1 <= len(grant['label']) <= 80: raise ValueError()
            if type(grant['expires_at']) not in (int, float) or type(grant['revoked']) is not bool: raise ValueError()
            if not isinstance(grant['scopes'], list) or not grant['scopes'] or not set(grant['scopes']) <= known_scopes: raise ValueError()
            if not isinstance(grant['workspaces'], list) or not grant['workspaces'] or not set(grant['workspaces']) <= {'live', 'rehearsal'}: raise ValueError()
            if hmac.compare_digest(digest, token_digest): found = grant
        expected = (os.environ.get('KEVENOS_AGENT_REALM', 'event'),
                    os.environ.get('KEVENOS_AGENT_PERSONA', 'operator'),
                    os.environ.get('KEVENOS_AGENT_AUDIENCE', 'kevenos'))
        if found and (found.get('realm'), found.get('persona'), found.get('audience')) == expected and not found['revoked'] and found['expires_at'] > time.time():
            return {key: found[key] for key in ('id', 'label', 'scopes', 'workspaces', 'expires_at', 'realm', 'persona', 'audience')}
    except (OSError, ValueError, TypeError, KeyError):
        raise APIError('agent_access_unavailable', 'Agent access configuration is unavailable.', 503) from None
    raise APIError('unauthorized', 'Agent credential is invalid, expired or revoked.', 401)


def _db(s):
    return s.AUTH_DB or s.DB


def revision(s):
    import event_commands
    return _digest(event_commands.snapshot(s))


def metadata(s):
    return {'context': s.WORKSPACE.context if s.WORKSPACE else 'local', 'revision': revision(s),
            'workspace': s.WORKSPACE.mode if s.WORKSPACE else 'live'}


def _workspace(s, principal):
    mode = s.WORKSPACE.mode if s.WORKSPACE else 'live'
    if mode not in principal['workspaces']:
        raise APIError('workspace_forbidden', 'This agent is not granted access to the selected workspace.', 403)
    return mode


def _rate(principal):
    now = time.monotonic()
    events = [t for t in RATE.get(principal['id'], []) if now-t < 60]
    if len(events) >= 240:
        raise APIError('rate_limited', 'Agent rate limit reached; wait one minute.', 429, True)
    events.append(now); RATE[principal['id']] = events


def _validate(schema, value):
    from jsonschema import Draft202012Validator, validators
    strict = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda checker, instance: type(instance) is int))
    errors = sorted(strict(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        # Validator messages can echo payloads or secrets. Emit only safe schema locations.
        location = '.'.join(str(p) for p in errors[0].path) or 'request'
        raise APIError('invalid_arguments', 'Invalid ' + location[:120] + '; check the published input schema.')


def _object(properties, required):
    return {'type':'object', 'properties':properties, 'required':required, 'additionalProperties':False}


def _envelope_schema(action):
    string = {'type':'string', 'minLength':1, 'maxLength':128}
    fields = {'resource':string, 'arguments':{'type':'object'}} if action == 'read' else {'command':string, 'arguments':{'type':'object'}, 'context':string, 'revision':string}
    required = list(fields)
    if action == 'execute':
        fields.update(idempotency_key=string, confirmation_id=string, confirm={'const':'EXECUTE'})
        required.append('idempotency_key')
    if action == 'receipts': fields, required = {'idempotency_key':string}, ['idempotency_key']
    return _object(fields, required)


def _entry(principal, name, group):
    entry = next((e for e in _catalog()[group] if e['name'] == name), None)
    if not entry: raise APIError('unknown_operation', 'Choose an operation from capabilities.', 404)
    if not authorized(principal, entry): raise APIError('scope_forbidden', 'Agent lacks the required operation scope.', 403)
    return entry


def _key(value):
    try:
        if str(uuid.UUID(value)) != value: raise ValueError()
    except (ValueError, AttributeError):
        raise APIError('invalid_idempotency_key', 'Use a canonical UUID for this logical change.') from None
    return value


def _audit(s, principal, operation, status, request_id):
    db = _db(s)
    with db:
        db.execute('INSERT INTO agent_audit(ts,principal,operation,status,request_id,workspace,context) VALUES(?,?,?,?,?,?,?)',
            (time.time(),principal['id'],operation[:100],status,request_id,s.WORKSPACE.mode if s.WORKSPACE else 'live',s.WORKSPACE.context if s.WORKSPACE else 'local'))
        db.execute('DELETE FROM agent_audit WHERE id <= COALESCE((SELECT id FROM agent_audit ORDER BY id DESC LIMIT 1 OFFSET 19999),0)')
        db.execute('DELETE FROM agent_confirmations WHERE expires<?', (time.time(),))


def _receipt(s, principal, key):
    return _db(s).execute('SELECT * FROM agent_receipts WHERE principal=? AND key=?', (principal['id'],key)).fetchone()


def _result(s, data, request_id):
    return {'ok':True, 'data':data, **metadata(s), 'request_id':request_id}


def operate(s, principal, action, body, request_id):
    import event_commands
    _workspace(s,principal)
    _rate(principal)
    if action == 'status':
        # Connection metadata deliberately excludes event content and PII.
        return 200, _result(s, {'principal':principal, 'workspace':s.WORKSPACE.mode if s.WORKSPACE else 'live', 'app_version':s.APP_VERSION, 'api_version':'v1'},request_id)
    if action == 'guide':
        return 200, _result(s, {'markdown':(Path(__file__).parent/'docs'/'AGENT-GUIDE.md').read_text()},request_id)
    if action == 'openapi.json':
        return 200, _result(s, openapi(principal),request_id)
    if action == 'capabilities':
        return 200, _result(s, {'api_version':'v1', **{group:[e for e in _catalog()[group] if authorized(principal,e)] for group in ('resources','commands')}},request_id)
    _validate(_envelope_schema(action),body)
    if action == 'receipts':
        receipt = _receipt(s,principal,_key(body['idempotency_key']))
        if not receipt: raise APIError('receipt_not_found','No receipt exists for this agent and key.',404)
        _entry(principal,receipt['command'],'commands')
        if receipt['workspace'] not in principal['workspaces']:
            raise APIError('workspace_forbidden','This receipt belongs to a workspace outside the current grant.',403)
        return 200,_result(s,{'key':receipt['key'],'command':receipt['command'],'state':receipt['state'], 'created':receipt['created'], 'response':json.loads(receipt['response']) if receipt['response'] else None},request_id)
    if action == 'read':
        entry = _entry(principal,body['resource'],'resources')
        _validate(entry['input_schema'],body['arguments'])
        if entry['name']=='workspace.live_preview' and not {'live','rehearsal'} <= set(principal['workspaces']):
            raise APIError('workspace_forbidden','Live preparation requires access to both Live and Rehearsal.',403)
        return 200,_result(s,event_commands.read(s,entry['name'],body['arguments'],principal),request_id)
    entry = _entry(principal,body['command'],'commands')
    _validate(entry['input_schema'],body['arguments'])
    if entry['name'] == 'workspace.switch' and body['arguments'].get('mode') not in principal['workspaces']:
        raise APIError('workspace_forbidden','This agent cannot select the requested workspace.',403)
    if entry['name']=='workspace.prepare_live' and not {'live','rehearsal'} <= set(principal['workspaces']):
        raise APIError('workspace_forbidden','Live preparation requires access to both Live and Rehearsal.',403)
    fingerprint = _digest({k:body[k] for k in ('command','arguments','context','revision')})
    if action == 'execute':
        key = _key(body['idempotency_key'])
        previous = _receipt(s,principal,key)
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise APIError('idempotency_conflict','This key already belongs to a different command or payload.',409)
            if previous['state'] in ('pending','uncertain'):
                raise APIError('outcome_uncertain','Execution may have started. Inspect the receipt and event state; do not retry with a new key.',409)
            saved = json.loads(previous['response'])
            if saved['context'] != (s.WORKSPACE.context if s.WORKSPACE else 'local'):
                raise APIError('workspace_changed','The receipt belongs to an earlier workspace context. Read current state.',409)
            saved.setdefault('workspace', previous['workspace'])
            saved['replayed'] = True
            return previous['status'], saved
    current = metadata(s)
    if body['context'] != current['context']: raise APIError('workspace_changed','Workspace changed; read current state and prepare the change again.',409)
    if body['revision'] != current['revision']: raise APIError('revision_changed','Event state changed; read current state and review the change again.',409)
    if action == 'prepare':
        confirmation = secrets.token_urlsafe(32)
        expires = time.time()+300
        with _db(s):
            _db(s).execute('INSERT INTO agent_confirmations(id,principal,fingerprint,context,expires) VALUES(?,?,?,?,?)',
                (confirmation,principal['id'],fingerprint,current['context'],expires))
        return 200,_result(s,{'command':entry['name'],'risk':entry['risk'],'requires_confirmation':entry['risk']=='confirm',
            'confirmation_id':confirmation,'expires_at':expires,'summary':entry['description'],
            'instruction':'Review these exact arguments with the owner before consequential actions. Execute with the same context and revision; high-impact commands require confirm=EXECUTE.'},request_id)
    db = _db(s)
    if entry['risk'] == 'confirm':
        confirmation = db.execute('SELECT * FROM agent_confirmations WHERE id=? AND principal=?', (body.get('confirmation_id',''),principal['id'])).fetchone()
        if not confirmation or confirmation['used'] or confirmation['expires'] < time.time() or confirmation['fingerprint'] != fingerprint or body.get('confirm') != 'EXECUTE':
            raise APIError('confirmation_required','Prepare and acknowledge this exact change before execution.',409)
    if db.execute('SELECT count(*) FROM agent_receipts').fetchone()[0] >= 100000:
        raise APIError('receipt_capacity','Receipt storage requires operator maintenance. No command executed.',503)
    # Commit reservation before any domain operation. A process crash cannot turn
    # an ambiguous mutation into a safe automatic retry.
    with db:
        db.execute('INSERT INTO agent_receipts(principal,key,fingerprint,command,context,workspace,created,state) VALUES(?,?,?,?,?,?,?,?)',
            (principal['id'],key,fingerprint,entry['name'],current['context'],s.WORKSPACE.mode if s.WORKSPACE else 'live',time.time(),'pending'))
        if entry['risk'] == 'confirm': db.execute('UPDATE agent_confirmations SET used=1 WHERE id=?',(body['confirmation_id'],))
    previous_identity = getattr(s.REQUEST_AUTH, 'identity', None)
    s.REQUEST_AUTH.identity = principal.get('_browser_identity') or {'role':'agent','credential_id':'agent:'+principal['id'],'label':principal['label']}
    try:
        data = event_commands.execute(s,entry['name'],body['arguments'],dict(principal,idempotency_key=key))
        response, status, state = _result(s,data,request_id), 200, 'completed'
        if len(_json(response).encode()) > MAX_RESPONSE: raise RuntimeError('oversize result')
    except event_commands.CommandError as error:
        status, state = error.status, 'failed'
        response = {'ok':False,'error':{'code':error.code,'message':str(error),'retryable':False,**error.details},**metadata(s),'request_id':request_id}
    except Exception:
        with db: db.execute("UPDATE agent_receipts SET state='uncertain' WHERE principal=? AND key=?",(principal['id'],key))
        raise APIError('outcome_uncertain','Execution outcome is uncertain. Inspect current state and the receipt before another attempt.',500) from None
    finally:
        s.REQUEST_AUTH.identity = previous_identity
    with db:
        db.execute('UPDATE agent_receipts SET state=?,status=?,response=? WHERE principal=? AND key=?',
            (state,status,_json(response),principal['id'],key))
    return status,response


def handle(s,h):
    path = urlparse(h.path).path
    if not path.startswith(PREFIX): return False
    h._activity_done = True
    request_id = uuid.uuid4().hex
    principal = None
    action = path[len(PREFIX):]
    try:
        principal = authenticate(h.headers)
        if h.headers.get('Origin') or h.headers.get('Cookie') or h.headers.get('X-Profile-Token'):
            raise APIError('machine_auth_only','Use a dedicated bearer client without browser credentials.',403)
        if action not in ('status','capabilities','read','prepare','execute','receipts','guide','openapi.json'):
            raise APIError('not_found','Unknown agent API path.',404)
        expected = 'GET' if action in ('status','capabilities','guide','openapi.json') else 'POST'
        if h.command != expected: raise APIError('method_not_allowed','Use '+expected+' for this endpoint.',405)
        if urlparse(h.path).query: raise APIError('invalid_arguments','This API does not accept query parameters.')
        if expected == 'POST' and h.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
            raise APIError('json_required','Use application/json.',415)
        try: body = h.body() if expected == 'POST' else {}
        except (ValueError,UnicodeError): raise APIError('invalid_json','Provide a bounded JSON object.') from None
        guard = (s.WORKSPACE.write() if action in ('prepare','execute') else s.WORKSPACE.read()) if s.WORKSPACE else nullcontext()
        with guard, s.LOCK:
            principal = authenticate(h.headers)
            status,response = operate(s,principal,action,body,request_id)
            _audit(s,principal,action if action not in ('read','execute','prepare') else body.get('command',body.get('resource',action)),status,request_id)
        if len(_json(response).encode()) > MAX_RESPONSE:
            raise APIError('response_too_large','Narrow the query or reduce its page size.',413)
        h.send(status,response)
    except Exception as error:
        import event_commands
        blocked = bool(s.WORKSPACE and s.WORKSPACE.guard.blocked)
        if blocked:error=APIError('recovery_required','Event recovery is required. Contact the event administrator.',503)
        known = isinstance(error,(APIError,event_commands.CommandError))
        status = error.status if known else 500
        response = {'ok':False,'error':{'code':error.code if known else 'internal_error',
            'message':str(error) if known else 'Agent request failed. No automatic retry is safe for a mutation.',
            'retryable':getattr(error,'retryable',False)},'request_id':request_id}
        if principal and not blocked:
            guard = s.WORKSPACE.read() if s.WORKSPACE else nullcontext()
            with guard,s.LOCK:
                response.update(metadata(s))
                _audit(s,principal,action,status,request_id)
        h.send(status,response,extra={'WWW-Authenticate':'Bearer realm="kevenos"'} if status==401 else None)
    finally:
        s.REQUEST_AUTH.identity = None
    return True


def proxy_mcp(s,h):
    if urlparse(h.path).path not in ('/mcp','/mcp/'): return False
    h._activity_done = True
    connection = None
    try:
        if h.command != 'POST': raise APIError('method_not_allowed','This stateless MCP endpoint accepts POST.',405)
        if urlparse(h.path).query or h.headers.get('Cookie') or h.headers.get('Origin') or h.headers.get('X-Profile-Token'):
            raise APIError('machine_auth_only','Use the MCP client bearer connection.',403)
        authenticate(h.headers)
        length = int(h.headers.get('Content-Length','0'))
        if not 0 < length <= MAX_BODY: raise APIError('invalid_request','MCP request body exceeds the allowed size.')
        if h.headers.get('Transfer-Encoding'): raise APIError('invalid_request','Chunked request uploads are not accepted.')
        raw = h.rfile.read(length)
        headers = {k:h.headers[k] for k in ('Authorization','Content-Type','Accept','MCP-Protocol-Version','MCP-Session-Id') if h.headers.get(k)}
        connection = http.client.HTTPConnection('127.0.0.1',int(os.environ.get('KEVENOS_MCP_PORT','8081')),timeout=120)
        connection.request('POST','/mcp',body=raw,headers=headers)
        response = connection.getresponse()
        data = response.read(4_100_001)
        if len(data)>4_100_000: raise APIError('response_too_large','Narrow the MCP request.',502)
        content_type = response.getheader('Content-Type','application/json')
        if content_type.startswith('text/event-stream'): raise APIError('unsupported_stream','Expected a stateless JSON MCP response.',502)
        extra = {k:response.getheader(k) for k in ('Mcp-Session-Id','MCP-Protocol-Version','WWW-Authenticate') if response.getheader(k)}
        h.send(response.status,data,content_type,extra)
    except APIError as error:
        h.send(error.status,{'error':error.code,'message':error.message},extra={'WWW-Authenticate':'Bearer realm="kevenos"'} if error.status==401 else None)
    except (OSError,ValueError,http.client.HTTPException):
        h.send(503,{'error':'mcp_unavailable','message':'The MCP service is unavailable; retry reads after checking API status.'})
    finally:
        if connection: connection.close()
    return True


def openapi(principal):
    """Generate the authorized HTTP contract from the same domain schemas as MCP."""
    catalog = {group:[e for e in _catalog()[group] if authorized(principal,e)] for group in ('resources','commands')}
    paths = {}
    for action in ('status','capabilities','guide','openapi.json','read','prepare','execute','receipts'):
        method = 'get' if action in ('status','capabilities','guide','openapi.json') else 'post'
        operation = {'operationId':'agent_'+action.replace('.','_'), 'security':[{'AgentBearer':[]}],
            'responses':{'200':{'description':'Success envelope with data, context, revision and request ID'},
                         '400':{'description':'Invalid schema or domain arguments'},
                         '401':{'description':'Missing, expired or revoked machine credential'},
                         '403':{'description':'Scope or workspace refused'},
                         '409':{'description':'Stale revision, missing confirmation, business barrier or uncertain outcome'}}}
        if method == 'post':
            if action in ('read','prepare','execute'):
                variants = []
                for entry in catalog['resources' if action=='read' else 'commands']:
                    schema = _envelope_schema(action)
                    schema['properties']['resource' if action=='read' else 'command'] = {'const':entry['name']}
                    schema['properties']['arguments'] = entry['input_schema']
                    if action=='execute' and entry['risk']=='confirm':
                        schema['required'] += ['confirmation_id','confirm']
                    variants.append(schema)
                schema = {'oneOf':variants} if variants else {'not':{}}
            else: schema = _envelope_schema(action)
            operation['requestBody'] = {'required':True,'content':{'application/json':{'schema':schema}}}
        paths[PREFIX+action] = {method:operation}
    return {'openapi':'3.1.0','info':{'title':'KevenOS agent API','version':'1.0.0'},
        'servers':[{'url':os.environ.get('KEVENOS_PUBLIC_URL', 'https://event.example.com')}], 'paths':paths,
        'components':{'securitySchemes':{'AgentBearer':{'type':'http','scheme':'bearer','description':'Dedicated per-consumer vault bearer; browser credentials are refused.'}}}}
