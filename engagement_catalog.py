"""Event-owned activity drafts and immutable published versions.

This is an internal domain primitive and persistence adapter. Callers must
authorize and audit actors before exposing it; it is not a public API or live
runner.
"""
from copy import deepcopy
import json
import hashlib
import os
import re
import time
from uuid import uuid4
import database

KEY = 'engagement_catalog_v1'
RECEIPTS_KEY = 'engagement_receipts_v1'
DESK = '/api/desk/engagement'
PLAYER = '/api/participant/engagement'


class Conflict(ValueError):
    pass


def empty():
    return {'revision': 0, 'activities': [], 'runs': []}


def _revision(state, expected):
    if type(expected) is not int or expected != state['revision']:
        raise Conflict('Activity catalog changed. Reload and try again.')


def _text(value, maximum, field):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'Enter {field} with at most {maximum} characters.')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError(f'Enter valid text for {field}.') from None
    return value.strip()


def _activity(state, activity_id):
    result = next((a for a in state['activities'] if a['id'] == activity_id), None)
    if result is None:
        raise Conflict('Activity does not exist in this workspace.')
    return result


def save_draft(state, payload, *, expected_revision):
    _revision(state, expected_revision)
    if not isinstance(payload, dict) or set(payload) - {'id', 'title', 'kind', 'questions'}:
        raise ValueError('Use the supported activity fields.')
    title = _text(payload.get('title'), 120, 'an activity title')
    kind = payload.get('kind')
    if not isinstance(kind, str) or kind not in {'quiz', 'poll'}:
        raise ValueError('Choose a quiz or poll.')
    questions = payload.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 100:
        raise ValueError('Include between 1 and 100 questions.')
    prior = _activity(state, payload['id']) if 'id' in payload else None
    previous_ids = {q['id'] for q in prior['draft']['questions']} if prior else set()
    seen_ids = set()
    clean = []
    for question in questions:
        if not isinstance(question, dict) or set(question) - {'id', 'prompt', 'type', 'options', 'correct'}:
            raise ValueError('Use the supported question fields.')
        question_id = question.get('id')
        if question_id is not None and (not isinstance(question_id, str) or question_id not in previous_ids or question_id in seen_ids):
            raise ValueError('Keep existing question IDs and omit IDs for new questions.')
        question_id = question_id or uuid4().hex
        seen_ids.add(question_id)
        question_type = question.get('type')
        if not isinstance(question_type, str) or question_type not in {'single', 'multi', 'true_false', 'rating', 'text'}:
            raise ValueError('Choose a supported question type.')
        prompt = _text(question.get('prompt'), 500, 'a question')
        if question_type == 'true_false':
            if question.get('options', ['True', 'False']) != ['True', 'False']:
                raise ValueError('True or false options cannot be changed.')
            choices = ['True', 'False']
        elif question_type in {'single', 'multi'}:
            options = question.get('options')
            if not isinstance(options, list) or not 2 <= len(options) <= 8:
                raise ValueError('Include between 2 and 8 answer options.')
            choices = [_text(option, 150, 'an answer option') for option in options]
        else:
            if question.get('options', []) != [] or kind == 'quiz':
                raise ValueError('Rating and text questions are unscored.')
            choices = []
        correct = question.get('correct', [])
        if not isinstance(correct, list) or any(type(i) is not int or i < 0 or i >= len(choices) for i in correct):
            raise ValueError('Choose a valid correct answer.')
        if len(set(correct)) != len(correct) or (kind == 'quiz' and
                (not correct or (question_type != 'multi' and len(correct) != 1))) or (kind == 'poll' and correct):
            raise ValueError('Choose the correct answers for a quiz; polls are unscored.')
        clean.append({'id': question_id, 'type': question_type, 'prompt': prompt, 'options': choices, 'correct': sorted(correct)})
    draft = {'id': prior['id'] if prior else uuid4().hex, 'title': title, 'kind': kind, 'questions': clean}
    if prior:
        prior['draft'] = draft
    else:
        state['activities'].append({'id': draft['id'], 'draft': draft, 'versions': []})
    state['revision'] += 1
    return deepcopy(draft)


def publish(state, activity_id, *, expected_revision):
    _revision(state, expected_revision)
    activity = _activity(state, activity_id)
    version = {'version': len(activity['versions']) + 1, 'snapshot': deepcopy(activity['draft'])}
    activity['versions'].append(version)
    state['revision'] += 1
    return deepcopy(version)


def open_run(state, activity_id, version_number, *, expected_revision):
    _revision(state, expected_revision)
    activity = _activity(state, activity_id)
    version = next((v for v in activity['versions'] if type(version_number) is int and v['version'] == version_number), None)
    if version is None:
        raise Conflict('Choose a published activity version.')
    run = {'id': uuid4().hex, 'activity_id': activity_id, 'version': version_number,
           'status': 'open', 'snapshot': deepcopy(version['snapshot'])}
    state['runs'].append(run)
    state['revision'] += 1
    return deepcopy(run)


def _run(state, run_id):
    run = next((item for item in state['runs'] if item['id'] == run_id), None)
    if not run or run['status'] != 'open':
        raise Conflict('Choose an open activity run.')
    return run


def open_question(state, run_id, question_id, *, expected_revision, duration=15):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    if duration is not None and (type(duration) is not int or not 1 <= duration <= 3600):
        raise ValueError('Choose a duration from 1 to 3600 seconds, or no time limit.')
    if not any(item['id'] == question_id for item in run['snapshot']['questions']):
        raise Conflict('Choose a question from this activity version.')
    if any(item.get('status') == 'open' and item.get('delivery', {}).get('status') == 'open'
           for item in state['runs']):
        raise Conflict('Close the current question first.')
    if question_id in run.get('delivered_question_ids', []):
        raise Conflict('This question has already been delivered.')
    now = time.time()
    run['delivery'] = {'id': uuid4().hex, 'status': 'open', 'opened_at': now,
                       'ends_at': None if duration is None else now + duration}
    run.setdefault('delivered_question_ids', []).append(question_id)
    run['active_question_id'] = question_id
    state['revision'] += 1
    return deepcopy(run)


def close_question(state, run_id, *, expected_revision):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    delivery = run.get('delivery') or {}
    if delivery.get('status') != 'open':
        raise Conflict('There is no open question to close.')
    delivery['status'] = 'closed'
    delivery['closed_at'] = time.time()
    state['revision'] += 1
    return deepcopy(run)


def reveal_question(state, run_id, *, expected_revision):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    delivery = run.get('delivery') or {}
    if delivery.get('status') != 'closed':
        raise Conflict('Close the question before revealing its answer.')
    delivery['status'] = 'revealed'
    delivery['revealed_at'] = time.time()
    state['revision'] += 1
    return deepcopy(run)


def submit_answer(state, run_id, delivery_id, principal, choice):
    """Record one answer per authenticated principal and delivery.

    The caller must authenticate the principal and persist under the same lock
    as organizer commands. An identical retry returns the original receipt.
    """
    if not isinstance(principal, str) or not principal:
        raise PermissionError('An individual session is required.')
    run = _run(state, run_id)
    delivery = run.get('delivery') or {}
    if delivery.get('id') != delivery_id:
        raise Conflict('This question is no longer active.')
    question = next(q for q in run['snapshot']['questions'] if q['id'] == run['active_question_id'])
    question_type = question['type']
    if question_type in {'single', 'true_false'}:
        if type(choice) is not int or not 0 <= choice < len(question['options']):
            raise ValueError('Choose a valid answer.')
    elif question_type == 'multi':
        if (not isinstance(choice, list) or not choice or
                any(type(item) is not int or not 0 <= item < len(question['options']) for item in choice) or
                len(set(choice)) != len(choice)):
            raise ValueError('Choose valid, distinct answers.')
        choice = sorted(choice)
    elif question_type == 'rating':
        if type(choice) is not int or not 1 <= choice <= 5:
            raise ValueError('Choose a rating from 1 to 5.')
    elif question_type == 'text':
        choice = _text(choice, 1000, 'an answer')
    else:
        raise ValueError('Unsupported question type.')
    answers = run.get('responses', {}).get(delivery_id, {})
    previous = answers.get(principal)
    if previous:
        if previous['choice'] != choice:
            raise Conflict('Your answer has already been submitted.')
        return deepcopy(previous['receipt'])
    now = time.time()
    if delivery.get('status') != 'open' or (delivery['ends_at'] is not None and now >= delivery['ends_at']):
        raise Conflict('This question is closed.')
    receipt = {'id': uuid4().hex, 'delivery_id': delivery_id, 'received_at': now}
    run.setdefault('responses', {}).setdefault(delivery_id, {})[principal] = {'choice': choice, 'receipt': receipt}
    return deepcopy(receipt)


def player_view(run, *, principal=None):
    snapshot = deepcopy(run['snapshot'])
    snapshot['questions'] = [question for question in snapshot['questions']
                             if run['status'] == 'open' and question['id'] == run.get('active_question_id')]
    for question in snapshot['questions']:
        if run.get('delivery', {}).get('status') != 'revealed':
            question.pop('correct', None)
    view = {**snapshot, 'id': run['id'], 'activity_id': run['activity_id'], 'status': run['status'],
            'delivery': deepcopy(run.get('delivery'))}
    delivery = run.get('delivery') or {}
    if principal and delivery.get('status') == 'revealed' and snapshot['kind'] == 'quiz' and snapshot['questions']:
        response = run.get('responses', {}).get(delivery['id'], {}).get(principal)
        if response:
            question = snapshot['questions'][0]
            selected = response['choice'] if isinstance(response['choice'], list) else [response['choice']]
            view['result'] = {'earned': int(selected == question['correct']), 'available': 1}
    return view


def _apply(state, action, payload, expected_revision):
    if action == 'save':
        return save_draft(state, payload, expected_revision=expected_revision)
    if action == 'publish':
        return publish(state, payload.get('activity_id'), expected_revision=expected_revision)
    if action == 'open_run':
        return open_run(state, payload.get('activity_id'), payload.get('version'), expected_revision=expected_revision)
    if action == 'open_question':
        return open_question(state, payload.get('run_id'), payload.get('question_id'), expected_revision=expected_revision,
                             duration=payload.get('duration', 15))
    if action == 'close_question':
        return close_question(state, payload.get('run_id'), expected_revision=expected_revision)
    return reveal_question(state, payload.get('run_id'), expected_revision=expected_revision)


def _read(db, key, default):
    row = db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
    return json.loads(row['v']) if row else default


def _write(db, key, value):
    db.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
               (key, json.dumps(value)))


def persist(service, action, payload, *, expected_revision):
    """Apply one catalog operation atomically in the selected event workspace.

    Authorization, audit and idempotency remain the responsibility of the
    future command adapter. SQLite is supported for a single app instance.
    """
    if not isinstance(action, str) or action not in {'save', 'publish', 'open_run', 'open_question', 'close_question', 'reveal_question'} or not isinstance(payload, dict):
        raise ValueError('Choose a supported catalog operation.')
    db = service.DB
    with service.LOCK, database.named_lock(db, 'engagement-catalog'), db:
        state = _read(db, KEY, empty())
        result = _apply(state, action, payload, expected_revision)
        _write(db, KEY, state)
        return result


def execute(service, action, payload, *, expected_revision, request_id, identity):
    """Authorize and atomically persist an audited, replay-safe command.

    identity must originate from the server's authenticated session, never
    directly from a request body. This is not yet an HTTP or MCP endpoint.
    """
    if not isinstance(identity, dict) or identity.get('dashboard', identity.get('role')) not in {'admin', 'organizer', 'desk'} or not identity.get('credential_id') or (identity.get('impersonation') or {}).get('mode') == 'preview':
        raise PermissionError('Organizer access is required.')
    if not isinstance(action, str) or action not in {'save', 'publish', 'open_run', 'open_question', 'close_question', 'reveal_question'} or not isinstance(payload, dict):
        raise ValueError('Choose a supported catalog operation.')
    if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
        raise ValueError('Provide a unique request ID.')
    actor = identity.get('actor') or identity
    fingerprint = hashlib.sha256(json.dumps({'action': action, 'payload': payload,
        'revision': expected_revision, 'actor': actor.get('credential_id', ''),
        'effective': identity['credential_id']}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    db = service.DB
    with service.LOCK, database.named_lock(db, 'engagement-catalog'), db:
        receipts = _read(db, RECEIPTS_KEY, {})
        if request_id in receipts:
            receipt = receipts[request_id]
            if receipt['fingerprint'] != fingerprint:
                raise Conflict('Request ID was already used for a different operation.')
            return deepcopy(receipt['result'])
        if len(receipts) >= 5000:
            raise Conflict('Activity operation history is full; contact the administrator.')
        state = _read(db, KEY, empty())
        result = _apply(state, action, payload, expected_revision)
        receipts[request_id] = {'fingerprint': fingerprint, 'result': result}
        _write(db, KEY, state)
        _write(db, RECEIPTS_KEY, receipts)
        profile = identity.get('impersonation') or {}
        db.execute('''INSERT INTO audit(ts,actor,action,detail,actor_credential,actor_label,
                      effective_credential,effective_role,profile_id,impersonation_mode)
                      VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (time.time(), identity.get('dashboard') or identity.get('role', ''), 'engagement-' + action,
                    request_id, actor.get('credential_id',''), actor.get('label',''),
                    identity['credential_id'], identity.get('role',''), profile.get('id',''), profile.get('mode','')))
        return deepcopy(result)


def answer(service, run_id, delivery_id, choice, *, identity):
    """Submit against the selected workspace using a server-authenticated participant."""
    if (not isinstance(identity, dict) or identity.get('role') != 'participant'
            or not identity.get('credential_id') or (identity.get('impersonation') or {}).get('mode') == 'preview'):
        raise PermissionError('An individual participant session is required.')
    db = service.DB
    with service.LOCK, database.named_lock(db, 'engagement-catalog'), db:
        state = _read(db, KEY, empty())
        receipt = submit_answer(state, run_id, delivery_id, identity['credential_id'], choice)
        _write(db, KEY, state)
        return receipt


def handle(service, handler, path, body=None):
    """Experimental, explicitly enabled Organizer and participant adapters."""
    if path not in {DESK, PLAYER} or os.environ.get('KEVENOS_ENGAGEMENT_V2') != '1':
        return False
    identity = service.event_access.session(service, handler.headers)
    if not identity:
        handler.send(401, {'error': 'Sign in to continue.'})
        return True
    role = identity.get('dashboard', identity.get('role'))
    if path == PLAYER:
        if identity.get('role') != 'participant':
            handler.send(403, {'error': 'Participant access is required.'})
            return True
        try:
            assignment = service.event_access.participant_assignment(service, identity)
            if not assignment or not assignment['name'].strip():
                raise Conflict('Your participant assignment is unavailable.')
            if body is not None:
                if not isinstance(body, dict) or set(body) != {'run_id', 'delivery_id', 'choice'}:
                    raise ValueError('Use the supported answer fields.')
                receipt = answer(service, body['run_id'], body['delivery_id'], body['choice'], identity=identity)
                handler.send(200, {'receipt': receipt})
                return True
            with service.LOCK:
                state = _read(service.DB, KEY, empty())
            candidates = [run for run in state['runs'] if run['status'] == 'open' and run.get('delivery')]
            run = max(candidates, key=lambda item: item['delivery']['opened_at']) if candidates else None
            answered = bool(run and identity['credential_id'] in run.get('responses', {}).get(run['delivery']['id'], {}))
            handler.send(200, {'question': player_view(run, principal=identity['credential_id']) if run else None,
                               'answered': answered, 'now': time.time()})
        except PermissionError as error:
            handler.send(403, {'error': str(error)})
        except Conflict as error:
            handler.send(409, {'error': str(error)})
        except ValueError as error:
            handler.send(400, {'error': str(error)})
        return True
    if role not in {'admin', 'organizer', 'desk'}:
        handler.send(403, {'error': 'Organizer access is required.'})
        return True
    if body is not None and (identity.get('impersonation') or {}).get('mode') == 'preview':
        handler.send(403, {'error': 'This profile is a read-only preview.'})
        return True
    try:
        if body is not None:
            if not isinstance(body, dict) or set(body) != {'action', 'revision', 'request_id', 'payload'}:
                raise ValueError('Use the supported command fields.')
            execute(service, body['action'], body['payload'], expected_revision=body['revision'],
                    request_id=body['request_id'], identity=identity)
        with service.LOCK:
            state = _read(service.DB, KEY, empty())
        handler.send(200, state)
    except PermissionError as error:
        handler.send(403, {'error': str(error)})
    except Conflict as error:
        handler.send(409, {'error': str(error)})
    except ValueError as error:
        handler.send(400, {'error': str(error)})
    return True
