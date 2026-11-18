"""Admin-only browser adapter to the common, confirmed scheduling command API."""
import hashlib
import time
import uuid
import agent_api
import event_config

PATH = '/api/admin/agenda'


def handle(s, h, path, body):
    if path != PATH:
        return False
    try:
        with s.LOCK:
            if not event_config._admin(s, h):
                return True
            ident = s.event_access.session(s, h.headers)
            if ident and ident.get('impersonation'):
                raise agent_api.APIError('agenda_forbidden', 'Return to your own Admin profile to edit the agenda.', 403)
            if not isinstance(body, dict):
                raise agent_api.APIError('invalid_request', 'Provide an agenda request.')
            action = body.get('action')
            envelope = {key: value for key, value in body.items() if key != 'action'}
            allowed = (action == 'read' and envelope.get('resource') in ('schedule.items', 'schedule.edit_preview')) or (action in ('prepare', 'execute') and envelope.get('command') == 'schedule.edit') or action == 'receipts'
            if not allowed:
                raise agent_api.APIError('agenda_operation_forbidden', 'Choose an agenda operation.', 403)
            # Bind receipts and confirmations to this exact authenticated session/profile.
            binding = (ident or {}).get('digest') or str(s.who(h.headers))
            principal = {'id': 'agenda-' + hashlib.sha256(binding.encode()).hexdigest()[:32],
                         'label': 'Admin agenda', 'realm': 'event', 'persona': 'operator',
                         'audience': 'kevenos', 'scopes': ['event:read', 'content:read', 'content:write'],
                         'workspaces': ['live', 'rehearsal'], 'expires_at': time.time() + 60,
                         '_browser_identity': ident}
            status, result = agent_api.operate(s, principal, action, envelope, uuid.uuid4().hex)
            if not result.get('ok'):
                error = result['error']
                result = {'error': error['message'], 'code': error['code']}
        h.send(status, result)
    except agent_api.APIError as error:
        h.send(error.status, {'code': error.code, 'error': str(error)})
    return True
