"""Event-owned activity drafts and immutable published versions.

This is an internal domain primitive and persistence adapter. Callers must
authorize and audit actors before exposing it; it is not a public API or live
runner.
"""
from copy import deepcopy
import csv
from datetime import datetime, timezone
import io
import json
import hashlib
import math
import os
import re
import statistics
import time
from uuid import uuid4
from urllib.parse import parse_qs, urlparse
import database

KEY = 'engagement_catalog_v1'
RECEIPTS_KEY = 'engagement_receipts_v1'
DESK = '/api/desk/engagement'
ADMIN_SETTINGS = '/api/admin/engagement-settings'
LEGACY_PREVIEW = '/api/desk/engagement/legacy-preview'
ANALYTICS_CSV = '/api/desk/engagement/analytics.csv'
PLAYER = '/api/participant/engagement'
SHARED_PLAYER = '/api/engagement/player'
QUESTIONS = '/api/engagement/questions'
PAYLOAD_FIELDS = {
    'save': ({'id','title','kind','questions','audiences','description'}, {'title','kind','questions'}),
    'publish': ({'activity_id'}, {'activity_id'}),
    'archive_draft': ({'activity_id','archive'}, {'activity_id','archive'}),
    'set_quiz_enabled': ({'enabled'}, {'enabled'}),
    'open_run': ({'activity_id','version'}, {'activity_id','version'}),
    'finish_run': ({'run_id'}, {'run_id'}),
    'open_question': ({'run_id','question_id','duration','auto_reveal'}, {'run_id','question_id'}),
    'close_question': ({'run_id'}, {'run_id'}),
    'pause_question': ({'run_id'}, {'run_id'}),
    'resume_question': ({'run_id'}, {'run_id'}),
    'extend_question': ({'run_id','seconds'}, {'run_id','seconds'}),
    'reveal_question': ({'run_id'}, {'run_id'}),
    'resend_question': ({'run_id','duration','auto_reveal'}, {'run_id'}),
    'share_hall': ({'run_id','share'}, {'run_id','share'}),
    'configure_questions': ({'participants','guests'}, {'participants','guests'}),
    'moderate_question': ({'id','decision','answer'}, {'id','decision'}),
    'merge_question': ({'id','target_id'}, {'id','target_id'}),
    'copy_legacy_pack': ({'pack_id','source_digest'}, {'pack_id','source_digest'}),
}


class Conflict(ValueError):
    pass


def empty():
    return {'revision': 0, 'activities': [], 'runs': [], 'quiz_enabled':True, 'qa_settings': {'participants': False, 'guests': False}, 'qa': []}


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
    if not isinstance(payload, dict) or set(payload) - {'id', 'title', 'kind', 'questions', 'audiences', 'description'}:
        raise ValueError('Use the supported activity fields.')
    title = _text(payload.get('title'), 120, 'an activity title')
    kind = payload.get('kind')
    if not isinstance(kind, str) or kind not in {'quiz', 'poll'}:
        raise ValueError('Choose a quiz or poll.')
    prior = _activity(state, payload['id']) if 'id' in payload else None
    if prior and prior.get('archived'):
        raise Conflict('Restore this draft before editing it.')
    description = payload.get('description', prior['draft'].get('description', '') if prior else '')
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError('Enter a valid activity description.')
    description = _text(description, 500, 'an activity description') if description.strip() else ''
    audiences = payload.get('audiences', prior['draft'].get('audiences', ['participant']) if prior else ['participant'])
    if (not isinstance(audiences, list) or not audiences or
            any(not isinstance(audience, str) or audience not in ('participant', 'guest') for audience in audiences) or
            len(set(audiences)) != len(audiences)):
        raise ValueError('Choose the allowed audiences for this activity.')
    questions = payload.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 100:
        raise ValueError('Include between 1 and 100 questions.')
    previous_ids = {q['id'] for q in prior['draft']['questions']} if prior else set()
    seen_ids = set()
    clean = []
    for question in questions:
        if not isinstance(question, dict) or set(question) - {'id', 'prompt', 'type', 'options', 'correct', 'evidence', 'points', 'explanation'}:
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
        item = {'id': question_id, 'type': question_type, 'prompt': prompt, 'options': choices, 'correct': sorted(correct)}
        points = question.get('points', 1 if kind == 'quiz' else 0)
        if type(points) is not int or (kind == 'quiz' and not 1 <= points <= 100) or (kind == 'poll' and points != 0):
            raise ValueError('Choose 1 to 100 points for a quiz question; polls are unscored.')
        if kind == 'quiz':
            item['points'] = points
        explanation = question.get('explanation', '')
        if not isinstance(explanation, str) or len(explanation) > 1000:
            raise ValueError('Enter a valid explanation of at most 1000 characters.')
        if explanation.strip():
            item['explanation'] = _text(explanation, 1000, 'an explanation')
        if question.get('evidence'):
            item['evidence'] = _text(question['evidence'], 500, 'source evidence')
        elif 'evidence' in question and not isinstance(question['evidence'], str):
            raise ValueError('Enter valid source evidence.')
        clean.append(item)
    draft = {'id': prior['id'] if prior else uuid4().hex, 'title': title, 'kind': kind, 'description':description,
             'audiences': list(audiences), 'questions': clean}
    if prior:
        prior['draft'] = draft
    else:
        state['activities'].append({'id': draft['id'], 'draft': draft, 'versions': []})
    state['revision'] += 1
    return deepcopy(draft)


def publish(state, activity_id, *, expected_revision):
    _revision(state, expected_revision)
    activity = _activity(state, activity_id)
    if activity.get('archived'):
        raise Conflict('Restore this draft before publishing it.')
    version = {'version': len(activity['versions']) + 1, 'snapshot': deepcopy(activity['draft'])}
    activity['versions'].append(version)
    state['revision'] += 1
    return deepcopy(version)


def archive_draft(state, activity_id, archive, *, expected_revision):
    _revision(state, expected_revision)
    if type(archive) is not bool:
        raise ValueError('Choose whether to archive the draft.')
    activity = _activity(state, activity_id)
    if activity['versions']:
        raise Conflict('Published history cannot be archived as a draft.')
    if bool(activity.get('archived')) == archive:
        raise Conflict('The draft is already in that state.')
    activity['archived'] = archive
    state['revision'] += 1
    return {'id':activity_id, 'archived':archive}


def open_run(state, activity_id, version_number, *, expected_revision):
    _revision(state, expected_revision)
    activity = _activity(state, activity_id)
    version = next((v for v in activity['versions'] if type(version_number) is int and v['version'] == version_number), None)
    if version is None:
        raise Conflict('Choose a published activity version.')
    _require_quiz_enabled(state, version['snapshot'])
    run = {'id': uuid4().hex, 'activity_id': activity_id, 'version': version_number,
           'status': 'open', 'snapshot': deepcopy(version['snapshot'])}
    state['runs'].append(run)
    state['revision'] += 1
    return deepcopy(run)


def _find_run(state, run_id):
    run = next((item for item in state['runs'] if item['id'] == run_id), None)
    if not run:
        raise Conflict('Choose an activity run.')
    return run


def _run(state, run_id):
    run = _find_run(state, run_id)
    if run['status'] != 'open':
        raise Conflict('Choose an open activity run.')
    return run


def finish_run(state, run_id, *, expected_revision):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    _settle(run)
    if (run.get('delivery') or {}).get('status') in {'open', 'paused'}:
        raise Conflict('Close the current question before finishing this run.')
    run['status'] = 'completed'
    run['hall_shared'] = False
    run['completed_at'] = time.time()
    state['revision'] += 1
    return deepcopy(run)


def _delivery_status(delivery, now=None):
    now = time.time() if now is None else now
    if delivery.get('status') == 'open' and delivery.get('ends_at') is not None and now >= delivery['ends_at']:
        return 'revealed' if delivery.get('auto_reveal') else 'closed'
    return delivery.get('status')


def _require_quiz_enabled(state, snapshot):
    if snapshot.get('kind') == 'quiz' and state.get('quiz_enabled', True) is not True:
        raise Conflict('The event Admin has disabled quiz launches.')


def set_quiz_enabled(state, enabled, *, expected_revision):
    _revision(state, expected_revision)
    if type(enabled) is not bool:
        raise ValueError('Choose whether quizzes are enabled.')
    if state.get('quiz_enabled', True) is enabled:
        raise Conflict('Quizzes are already in that state.')
    if not enabled and any(run.get('status') == 'open' and run['snapshot'].get('kind') == 'quiz'
                           and _delivery_status(run.get('delivery') or {}) in {'open','paused'} for run in state['runs']):
        raise Conflict('Close the active quiz question before disabling quizzes.')
    state['quiz_enabled'] = enabled
    state['revision'] += 1
    return {'enabled':enabled}


def _settle(run, now=None):
    delivery = run.get('delivery') or {}
    status = _delivery_status(delivery, now)
    if delivery.get('status') == 'open' and status != 'open':
        delivery['status'] = status
        delivery['closed_at'] = delivery['ends_at']
        if delivery['status'] == 'revealed':
            delivery['revealed_at'] = delivery['ends_at']


def _timing(duration, auto_reveal):
    if duration is not None and (type(duration) is not int or not 1 <= duration <= 3600):
        raise ValueError('Choose a duration from 1 to 3600 seconds, or no time limit.')
    if type(auto_reveal) is not bool or (auto_reveal and duration is None):
        raise ValueError('Automatic reveal requires a timed question.')


def open_question(state, run_id, question_id, *, expected_revision, duration=15, auto_reveal=False):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    _require_quiz_enabled(state, run['snapshot'])
    _timing(duration, auto_reveal)
    if not any(item['id'] == question_id for item in run['snapshot']['questions']):
        raise Conflict('Choose a question from this activity version.')
    for item in state['runs']:
        _settle(item)
    if any(item.get('status') == 'open' and item.get('delivery', {}).get('status') in {'open','paused'}
           for item in state['runs']):
        raise Conflict('Close the current question first.')
    if question_id in run.get('delivered_question_ids', []):
        raise Conflict('This question has already been delivered.')
    now = time.time()
    if run.get('delivery'):
        run.setdefault('deliveries', []).append(run['delivery'])
    run['delivery'] = {'id': uuid4().hex, 'question_id': question_id, 'status': 'open', 'opened_at': now, 'auto_reveal':auto_reveal,
                       'ends_at': None if duration is None else now + duration}
    run.setdefault('delivered_question_ids', []).append(question_id)
    run['active_question_id'] = question_id
    state['revision'] += 1
    return deepcopy(run)


def resend_question(state, run_id, *, expected_revision, duration=15, auto_reveal=False):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    _require_quiz_enabled(state, run['snapshot'])
    _timing(duration, auto_reveal)
    for item in state['runs']:
        _settle(item)
    delivery = run.get('delivery') or {}
    if delivery.get('status') != 'closed':
        raise Conflict('Close the question before resending it; revealed answers cannot be resent.')
    if any(item.get('status') == 'open' and item.get('delivery', {}).get('status') in {'open','paused'} for item in state['runs']):
        raise Conflict('Close the current question first.')
    now = time.time()
    run.setdefault('deliveries', []).append(delivery)
    run['delivery'] = {'id': uuid4().hex, 'question_id': run['active_question_id'], 'status': 'open', 'auto_reveal':auto_reveal,
                       'opened_at': now, 'ends_at': None if duration is None else now + duration}
    state['revision'] += 1
    return deepcopy(run)


def close_question(state, run_id, *, expected_revision):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    _settle(run)
    delivery = run.get('delivery') or {}
    if delivery.get('status') not in {'open','paused'}:
        raise Conflict('There is no open question to close.')
    delivery['status'] = 'closed'
    delivery.pop('remaining_seconds', None)
    delivery['closed_at'] = time.time()
    state['revision'] += 1
    return deepcopy(run)


def pause_question(state, run_id, *, expected_revision):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    now = time.time()
    _settle(run, now)
    delivery = run.get('delivery') or {}
    if delivery.get('status') != 'open':
        raise Conflict('Only an open question can be paused.')
    delivery['remaining_seconds'] = None if delivery['ends_at'] is None else max(0, delivery['ends_at'] - now)
    delivery['paused_at'] = now
    delivery['ends_at'] = None
    delivery['status'] = 'paused'
    state['revision'] += 1
    return deepcopy(run)


def resume_question(state, run_id, *, expected_revision):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    delivery = run.get('delivery') or {}
    if delivery.get('status') != 'paused':
        raise Conflict('Only a paused question can resume.')
    if any(item['id'] != run_id and item.get('status') == 'open' and
           _delivery_status(item.get('delivery') or {}) in {'open','paused'} for item in state['runs']):
        raise Conflict('Close the other active question first.')
    remaining = delivery.pop('remaining_seconds')
    now = time.time()
    delivery['paused_seconds_total'] = delivery.get('paused_seconds_total', 0) + max(0, now - delivery['paused_at'])
    delivery['ends_at'] = None if remaining is None else now + remaining
    delivery['status'] = 'open'
    state['revision'] += 1
    return deepcopy(run)


def extend_question(state, run_id, seconds, *, expected_revision):
    _revision(state, expected_revision)
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError('Choose an extension from 1 to 3600 seconds.')
    run = _run(state, run_id)
    now = time.time()
    _settle(run, now)
    delivery = run.get('delivery') or {}
    if delivery.get('status') != 'open' or delivery.get('ends_at') is None:
        raise Conflict('Only an open timed question can be extended.')
    if delivery['ends_at'] + seconds - now > 3600:
        raise ValueError('The remaining time cannot exceed 3600 seconds.')
    delivery['ends_at'] += seconds
    state['revision'] += 1
    return deepcopy(run)


def reveal_question(state, run_id, *, expected_revision):
    _revision(state, expected_revision)
    run = _run(state, run_id)
    _settle(run)
    delivery = run.get('delivery') or {}
    if delivery.get('status') != 'closed':
        raise Conflict('Close the question before revealing its answer.')
    delivery['status'] = 'revealed'
    delivery['revealed_at'] = time.time()
    state['revision'] += 1
    return deepcopy(run)


def share_hall(state, run_id, share, *, expected_revision):
    _revision(state, expected_revision)
    if type(share) is not bool:
        raise ValueError('Choose whether to share the question.')
    run = _run(state, run_id)
    if share and not run.get('delivery'):
        raise Conflict('Open a question before sharing it on the hall display.')
    if share:
        for item in state['runs']:
            item['hall_shared'] = False
    run['hall_shared'] = share
    state['revision'] += 1
    return deepcopy(run)


def configure_questions(state, *, participants, guests, expected_revision):
    _revision(state, expected_revision)
    if type(participants) is not bool or type(guests) is not bool:
        raise ValueError('Choose who can submit audience questions.')
    state['qa_settings'] = {'participants': participants, 'guests': guests}
    state['revision'] += 1
    return deepcopy(state['qa_settings'])


def ask_question(state, principal, audience, text, request_id):
    if not isinstance(principal, str) or not principal or audience not in ('participant', 'guest'):
        raise PermissionError('An individual event session is required.')
    if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
        raise ValueError('Provide a unique request ID.')
    text = _text(text, 500, 'a question')
    questions = state.get('qa', [])
    alias_id = state.get('qa_request_aliases', {}).get(request_id)
    previous = next((item for item in questions if item['request_id'] == request_id or item['id'] == alias_id), None)
    if previous:
        if previous['principal'] != principal or previous['text'] != text or previous['audience'] != audience:
            raise Conflict('Request ID was already used for a different question.')
        return {'id': previous['id'], 'received_at': previous['created_at']}
    settings = state.get('qa_settings', {})
    if not settings.get('participants') and not settings.get('guests'):
        raise Conflict('Audience questions are closed.')
    if not settings.get('participants' if audience == 'participant' else 'guests'):
        raise PermissionError('Audience questions are not enabled for this audience.')
    duplicate = next((item for item in questions if item['principal'] == principal and item['audience'] == audience
                      and item['text'] == text and item['status'] in {'pending', 'approved', 'answered', 'merged'}), None)
    if duplicate:
        aliases = state.setdefault('qa_request_aliases', {})
        if len(aliases) >= 5000:
            raise Conflict('Question retry limit reached. Ask the event team for help.')
        aliases[request_id] = duplicate['id']
        return {'id':duplicate['id'], 'received_at':duplicate['created_at']}
    if len(questions) >= 1000 or sum(item['principal'] == principal for item in questions) >= 5:
        raise Conflict('Question limit reached. Ask the event team for help.')
    item = {'id':uuid4().hex, 'request_id':request_id, 'principal':principal, 'audience':audience,
            'text':text, 'status':'pending', 'created_at':time.time()}
    state.setdefault('qa', []).append(item)
    return {'id':item['id'], 'received_at':item['created_at']}


def moderate_question(state, question_id, action, *, expected_revision, answer=None):
    _revision(state, expected_revision)
    question = next((item for item in state.get('qa', []) if item['id'] == question_id), None)
    if not question:
        raise Conflict('This question is no longer available.')
    if action == 'approve' and question['status'] == 'pending':
        question['status'] = 'approved'
    elif action == 'reject' and question['status'] in {'pending', 'approved', 'answered'}:
        question['status'] = 'rejected'
        question.pop('answer', None)
    elif action == 'answer' and question['status'] == 'approved':
        question['answer'] = _text(answer, 1000, 'an answer')
        question['status'] = 'answered'
    elif action == 'restore' and question['status'] == 'merged':
        question['status'] = 'pending'
        question.pop('merged_into', None)
    else:
        raise Conflict('This moderation action is unavailable for the question.')
    state['revision'] += 1
    return {'id':question['id'], 'status':question['status']}


def merge_question(state, question_id, target_id, *, expected_revision):
    """Link a private pending question to an already public one without transferring votes."""
    _revision(state, expected_revision)
    questions = state.get('qa', [])
    source = next((item for item in questions if item['id'] == question_id), None)
    target = next((item for item in questions if item['id'] == target_id), None)
    if (not source or not target or source is target or source['status'] != 'pending'
            or target['status'] not in {'approved', 'answered'}):
        raise Conflict('Combine a pending question with a published question.')
    source['status'], source['merged_into'] = 'merged', target['id']
    state['revision'] += 1
    return {'id':source['id'], 'status':'merged', 'target_id':target['id']}


def vote_question(state, question_id, principal, audience, active):
    if not isinstance(principal, str) or not principal or audience not in ('participant','guest'):
        raise PermissionError('An individual event session is required.')
    if type(active) is not bool:
        raise ValueError('Choose whether to support this question.')
    if not state.get('qa_settings', {}).get('participants' if audience == 'participant' else 'guests'):
        raise PermissionError('Audience questions are not enabled for this audience.')
    item = next((item for item in state.get('qa', []) if item['id'] == question_id), None)
    if not item or item['status'] not in {'approved','answered'}:
        raise Conflict('This question is not open for votes.')
    votes = item.setdefault('votes', [])
    if active and principal not in votes:
        votes.append(principal)
    elif not active and principal in votes:
        votes.remove(principal)
    return {'id':item['id'], 'votes':len(votes), 'upvoted':active}


def public_questions(state, *, principal=None):
    return [{**{key:item[key] for key in ('id','text','status','answer','created_at') if key in item},
             'votes':len(item.get('votes', [])), 'upvoted':bool(principal and principal in item.get('votes', []))}
            for item in state.get('qa', []) if item['status'] in {'approved','answered'}]


def submit_answer(state, run_id, delivery_id, principal, choice):
    """Record one answer per authenticated principal and delivery.

    The caller must authenticate the principal and persist under the same lock
    as organizer commands. An identical retry returns the original receipt.
    """
    if not isinstance(principal, str) or not principal:
        raise PermissionError('An individual session is required.')
    run = _find_run(state, run_id)
    delivery = run.get('delivery') or {}
    deliveries = [*run.get('deliveries', []), delivery]
    requested = next((item for item in deliveries if item.get('id') == delivery_id), None)
    if not requested:
        raise Conflict('This question is no longer active.')
    question = next(q for q in run['snapshot']['questions'] if q['id'] == requested['question_id'])
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
    prior_ids = [item['id'] for item in deliveries if item.get('question_id') == requested['question_id']]
    previous = next((run.get('responses', {}).get(item_id, {}).get(principal) for item_id in prior_ids
                     if principal in run.get('responses', {}).get(item_id, {})), None)
    if previous:
        if previous['choice'] != choice:
            raise Conflict('Your answer has already been submitted.')
        return deepcopy(previous['receipt'])
    if run['status'] != 'open':
        raise Conflict('This activity has finished.')
    if delivery.get('id') != delivery_id:
        raise Conflict('This question is no longer active.')
    now = time.time()
    if delivery.get('status') != 'open' or (delivery['ends_at'] is not None and now >= delivery['ends_at']):
        raise Conflict('This question is closed.')
    receipt = {'id': uuid4().hex, 'delivery_id': delivery_id, 'received_at': now}
    elapsed = max(0, now - delivery['opened_at'] - delivery.get('paused_seconds_total', 0))
    run.setdefault('responses', {}).setdefault(delivery_id, {})[principal] = {'choice': choice, 'receipt': receipt, 'elapsed_seconds':elapsed}
    return deepcopy(receipt)


def player_view(run, *, principal=None):
    delivery = deepcopy(run.get('delivery'))
    if delivery:
        _settle({'delivery':delivery})
    snapshot = deepcopy(run['snapshot'])
    snapshot['questions'] = [question for question in snapshot['questions']
                             if run['status'] == 'open' and question['id'] == run.get('active_question_id')]
    for question in snapshot['questions']:
        question.pop('evidence', None)
        if (delivery or {}).get('status') != 'revealed':
            question.pop('correct', None)
            question.pop('explanation', None)
    view = {**snapshot, 'id': run['id'], 'activity_id': run['activity_id'], 'status': run['status'],
            'delivery': delivery}
    if snapshot['questions']:
        ordered = run['snapshot']['questions']
        position = next(index + 1 for index, question in enumerate(ordered)
                        if question['id'] == run['active_question_id'])
        view['progress'] = {'position':position, 'total':len(ordered)}
    delivery = delivery or {}
    if principal and delivery.get('status') == 'revealed' and snapshot['kind'] == 'quiz' and snapshot['questions']:
        ids = [item['id'] for item in [*run.get('deliveries', []), delivery] if item.get('question_id') == run['active_question_id']]
        response = next((run.get('responses', {}).get(item_id, {}).get(principal) for item_id in ids
                         if principal in run.get('responses', {}).get(item_id, {})), None)
        if response:
            question = snapshot['questions'][0]
            selected = response['choice'] if isinstance(response['choice'], list) else [response['choice']]
            points = question.get('points', 1)
            view['result'] = {'earned': points if selected == question['correct'] else 0, 'available': points}
    return view


def player_score(state, principal, audience):
    """Score only explicitly revealed quiz questions available to this audience."""
    earned = available = 0
    now = time.time()
    for run in state['runs']:
        snapshot = run['snapshot']
        if snapshot['kind'] != 'quiz' or audience not in snapshot.get('audiences', ['participant']):
            continue
        deliveries = [*run.get('deliveries', []), *([run['delivery']] if run.get('delivery') else [])]
        released = {item['question_id'] for item in deliveries if _delivery_status(item, now) == 'revealed'}
        for question in snapshot['questions']:
            if question['id'] not in released:
                continue
            points = question.get('points', 1)
            available += points
            response = next((run.get('responses', {}).get(item['id'], {}).get(principal) for item in deliveries
                             if item.get('question_id') == question['id'] and
                             principal in run.get('responses', {}).get(item['id'], {})), None)
            if response:
                selected = response['choice'] if isinstance(response['choice'], list) else [response['choice']]
                earned += points if selected == question['correct'] else 0
    return {'earned':earned, 'available':available} if available else None


def desk_view(state):
    """Project aggregate results without returning individual responses."""
    result = deepcopy({key:value for key,value in state.items() if key not in {'runs','qa','qa_request_aliases'}})
    result['runs'] = [deepcopy({key:value for key,value in run.items() if key != 'responses'})
                      for run in state.get('runs', [])]
    for run in result['runs']:
        _settle(run)
    result['qa'] = [{**{key:item[key] for key in ('id','text','status','answer','created_at','audience','merged_into') if key in item},
                     'votes':len(item.get('votes', []))}
                    for item in state.get('qa', [])]
    for source, run in zip(state.get('runs', []), result['runs']):
        responses = source.get('responses', {})
        delivery = run.get('delivery') or {}
        deliveries = [*run.get('deliveries', []), *([delivery] if delivery else [])]
        question_analytics = []
        for question in run['snapshot']['questions']:
            attempts = [item for item in deliveries if item.get('question_id') == question['id']]
            if not attempts:
                continue
            people = {}
            for attempt in attempts:
                for principal, response in responses.get(attempt['id'], {}).items():
                    people.setdefault(principal, (response, attempt))
            total = len(people)
            distribution = correct_count = median_seconds = None
            if total >= 5:
                if question['type'] in {'single', 'multi', 'true_false', 'rating'}:
                    count = 5 if question['type'] == 'rating' else len(question['options'])
                    distribution = [0] * count
                    for response, _ in people.values():
                        selected = response['choice'] if isinstance(response['choice'], list) else [response['choice']]
                        for choice in selected:
                            distribution[choice - 1 if question['type'] == 'rating' else choice] += 1
                elapsed = [response.get('elapsed_seconds', max(0, response['receipt']['received_at'] - attempt['opened_at']))
                           for response, attempt in people.values()]
                median_seconds = round(statistics.median(elapsed), 1)
                if run['snapshot']['kind'] == 'quiz' and any(item['status'] == 'revealed' for item in attempts):
                    correct_count = sum((response['choice'] if isinstance(response['choice'], list) else [response['choice']]) == question['correct']
                                        for response, _ in people.values())
            question_analytics.append({'question_id':question['id'], 'respondents':total, 'denominator':total,
                'distribution':distribution, 'correct_count':correct_count, 'median_seconds':median_seconds,
                'viewed':None, 'suppressed_below':5})
        run['question_analytics'] = question_analytics
        run['analytics'] = next((item for item in question_analytics if item['question_id'] == run.get('active_question_id')),
            {'respondents':0, 'denominator':0, 'distribution':None, 'correct_count':None, 'median_seconds':None,
             'viewed':None, 'suppressed_below':5})
    return result


def operator_view(state):
    """Published activities and operational counts, without drafts or private data."""
    def snapshot(source):
        return {**{k:deepcopy(source[k]) for k in ('id','title','kind','description','audiences') if k in source},
                'questions':[{k:deepcopy(q[k]) for k in ('id','type','prompt','options','points') if k in q}
                             for q in source.get('questions', [])]}

    activities = [{'id':item['id'], 'versions':[{'version':v['version'], 'snapshot':snapshot(v['snapshot'])}
                                               for v in item.get('versions', [])]}
                  for item in state.get('activities', []) if item.get('versions')]
    runs = []
    for source in state.get('runs', []):
        run = deepcopy({k:source[k] for k in ('id','activity_id','version','status','delivery',
                                               'deliveries','active_question_id','hall_shared') if k in source})
        run['snapshot'] = snapshot(source['snapshot'])
        _settle(run)
        delivery = run.get('delivery') or {}
        attempts = [item for item in [*run.get('deliveries', []), delivery]
                    if item.get('question_id') == run.get('active_question_id')]
        respondents = {principal for attempt in attempts
                       for principal in source.get('responses', {}).get(attempt.get('id'), {})}
        run['respondents'] = len(respondents)
        runs.append(run)
    return {'revision':state['revision'], 'quiz_enabled':state.get('quiz_enabled', True), 'now':time.time(),
            'activities':activities, 'runs':runs}


def hall_view(state):
    """Return only explicitly shared, currently released information."""
    run = next((item for item in state['runs'] if item.get('hall_shared') and item.get('status') == 'open'), None)
    if not run or not run.get('delivery'):
        return None
    player = player_view(run)
    if not player['questions']:
        return None
    question = player['questions'][0]
    view = {'title': player['title'], 'kind': player['kind'], 'prompt': question['prompt'],
            'status': player['delivery']['status'], 'ends_at': player['delivery']['ends_at']}
    if player['delivery']['status'] == 'revealed':
        if question.get('correct'):
            view['options'] = question['options']
            view['correct'] = question['correct']
        stats = next(item for item in desk_view(state)['runs'] if item['id'] == run['id'])['analytics']
        if stats['distribution'] is not None:
            view['distribution'] = stats['distribution']
            view['respondents'] = stats['respondents']
            view['options'] = question['options'] if question['type'] != 'rating' else ['1', '2', '3', '4', '5']
    return view


def hall_quiz(service):
    """Adapt the privacy-filtered hall projection to the existing room renderer."""
    if os.environ.get('KEVENOS_ENGAGEMENT_V2') != '1':
        return None
    view = hall_view(_read(service.DB, KEY, empty()))
    if not view:
        return None
    options = [{'key': chr(ord('A') + index), 'text': text} for index, text in enumerate(view.get('options', []))]
    result = {'kind': 'question', 'prompt': view['prompt'], 'status': view['status'],
              'end': view['ends_at'], 'options': options,
              'results_pending': view['status'] == 'closed'}
    if 'correct' in view:
        result['correct'] = [options[index]['key'] for index in view['correct']]
    if 'distribution' in view:
        result['bars'] = [{**option, 'count': view['distribution'][index]} for index, option in enumerate(options)]
    return result


def _apply(state, action, payload, expected_revision, db=None):
    if action == 'save':
        return save_draft(state, payload, expected_revision=expected_revision)
    if action == 'publish':
        return publish(state, payload.get('activity_id'), expected_revision=expected_revision)
    if action == 'archive_draft':
        return archive_draft(state, payload.get('activity_id'), payload.get('archive'), expected_revision=expected_revision)
    if action == 'set_quiz_enabled':
        return set_quiz_enabled(state, payload.get('enabled'), expected_revision=expected_revision)
    if action == 'open_run':
        return open_run(state, payload.get('activity_id'), payload.get('version'), expected_revision=expected_revision)
    if action == 'finish_run':
        return finish_run(state, payload.get('run_id'), expected_revision=expected_revision)
    if action == 'open_question':
        return open_question(state, payload.get('run_id'), payload.get('question_id'), expected_revision=expected_revision,
                             duration=payload.get('duration', 15), auto_reveal=payload.get('auto_reveal', False))
    if action == 'close_question':
        return close_question(state, payload.get('run_id'), expected_revision=expected_revision)
    if action == 'pause_question':
        return pause_question(state, payload.get('run_id'), expected_revision=expected_revision)
    if action == 'resume_question':
        return resume_question(state, payload.get('run_id'), expected_revision=expected_revision)
    if action == 'extend_question':
        return extend_question(state, payload.get('run_id'), payload.get('seconds'), expected_revision=expected_revision)
    if action == 'resend_question':
        return resend_question(state, payload.get('run_id'), expected_revision=expected_revision, duration=payload.get('duration', 15),
                               auto_reveal=payload.get('auto_reveal', False))
    if action == 'share_hall':
        return share_hall(state, payload.get('run_id'), payload.get('share'), expected_revision=expected_revision)
    if action == 'configure_questions':
        return configure_questions(state, participants=payload.get('participants'), guests=payload.get('guests'), expected_revision=expected_revision)
    if action == 'moderate_question':
        return moderate_question(state, payload.get('id'), payload.get('decision'), answer=payload.get('answer'), expected_revision=expected_revision)
    if action == 'merge_question':
        return merge_question(state, payload.get('id'), payload.get('target_id'), expected_revision=expected_revision)
    if action == 'copy_legacy_pack':
        return copy_legacy_pack(state, db, payload, expected_revision=expected_revision)
    return reveal_question(state, payload.get('run_id'), expected_revision=expected_revision)


def _read(db, key, default):
    row = db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
    return json.loads(row['v']) if row else default


def _write(db, key, value):
    db.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
               (key, json.dumps(value)))


def _validate_command(action, payload):
    if not isinstance(action, str) or action not in PAYLOAD_FIELDS or not isinstance(payload, dict):
        raise ValueError('Choose a supported catalog operation.')
    allowed, required = PAYLOAD_FIELDS[action]
    if set(payload) - allowed or required - set(payload):
        raise ValueError('Use the supported fields for this operation.')


def _legacy_delivery_open(db, *, include_audience=True):
    now = time.time()
    try:
        quiz = _read(db, 'event_quiz_v1', {})
        audience = _read(db, 'audience_questions_v1', {}) if include_audience else {}
        if not isinstance(quiz, dict) or not isinstance(audience, dict):
            raise ValueError
        runs, packs = quiz.get('runs', []), audience.get('packs', [])
        if not isinstance(runs, list) or not isinstance(packs, list):
            raise ValueError
        for item, active_status, deadline_key, clock in [
                *((item, 'open', 'end', now) for item in runs),
                *((item, 'live', 'closesAt', now * 1000) for item in packs)]:
            if not isinstance(item, dict) or not isinstance(item.get('status'), str):
                raise ValueError
            if item['status'] != active_status:
                continue
            deadline = item.get(deadline_key)
            if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
                raise ValueError
            if deadline is None or deadline > clock:
                return True
        return False
    except (ValueError, TypeError, OverflowError) as error:
        raise Conflict('Legacy delivery state requires inspection before sending a question.') from error


def legacy_preview(db):
    """Inventory historical data without interpreting or changing scores."""
    quiz = _read(db, 'event_quiz_v1', {})
    audience = _read(db, 'audience_questions_v1', {})
    archive = _read(db, 'event_quiz_reset_archive_v1', [])
    if not isinstance(quiz, dict) or not isinstance(audience, dict) or not isinstance(archive, list):
        raise ValueError('Legacy records require inspection before inventory.')
    answers = quiz.get('answers', {})
    runs = quiz.get('runs', [])
    if not isinstance(answers, dict) or not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        raise ValueError('Legacy quiz records require inspection before inventory.')
    if any(not isinstance(entries, dict) for entries in answers.values()):
        raise ValueError('Legacy quiz answer records require inspection before inventory.')
    archive_runs = archive_answers = 0
    for entry in archive:
        if not isinstance(entry, dict) or not isinstance(entry.get('state'), dict):
            raise ValueError('Legacy quiz reset archives require inspection before inventory.')
        previous = entry['state']
        previous_runs, previous_answers = previous.get('runs'), previous.get('answers')
        if (not isinstance(previous_runs, list) or any(not isinstance(run, dict) for run in previous_runs)
                or not isinstance(previous_answers, dict)
                or any(not isinstance(items, dict) for items in previous_answers.values())):
            raise ValueError('Legacy quiz reset archives require inspection before inventory.')
        archive_runs += len(previous_runs)
        archive_answers += sum(len(items) for items in previous_answers.values())
    cases = {'voided':sum(run.get('status') == 'void' for run in runs),
             'unscored':sum(run.get('points') == 0 for run in runs),
             'excluded_audiences':sum(bool(run.get('excluded')) for run in runs),
             'predictions':sum(bool(run.get('prediction')) for run in runs),
             'warmup_or_post':sum(run.get('kind') in {'warmup', 'post'} for run in runs),
             'catchup_or_waiting':sum(run.get('kind') == 'waiting' or bool(run.get('catchup')) for run in runs),
             'open':sum(run.get('status') == 'open' for run in runs)}
    packs = audience.get('packs', [])
    if not isinstance(packs, list) or any(not isinstance(item, dict) for item in packs):
        raise ValueError('Legacy audience records require inspection before inventory.')
    if any(item.get('status') == 'draft' and (not isinstance(item.get('id'), str) or
           not isinstance(item.get('questions', []), list)) for item in packs):
        raise ValueError('Legacy audience drafts require inspection before inventory.')
    drafts = [{'id':item['id'], 'title':item.get('title',''), 'question_count':len(item.get('questions', [])),
               'digest':_legacy_digest(item)} for item in packs if item.get('status') == 'draft']
    records = db.execute('SELECT count(*) AS n FROM kv WHERE k LIKE ?', ('audience_answers_v1:%',)).fetchone()['n']
    return {'quiz':{'revision':quiz.get('rev', 0), 'runs':len(runs),
                    'answering_accounts':len(answers), 'answer_entries':sum(len(entries) for entries in answers.values()),
                    'archives':len(archive), 'archive_runs':archive_runs,
                    'archive_answer_entries':archive_answers, 'cases':cases},
            'audience':{'revision':audience.get('rev', 0), 'packs':len(packs),
                        'draft_packs':sum(item.get('status') == 'draft' for item in packs),
                        'answer_records':records, 'drafts':drafts},
            'historical_results':'retained_in_legacy', 'changes_made':False}


def _legacy_digest(pack):
    return hashlib.sha256(json.dumps(pack, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def copy_legacy_pack(state, db, payload, *, expected_revision):
    """Copy one frozen legacy draft; never mutate old packs or historical results."""
    _revision(state, expected_revision)
    pack_id, digest = payload.get('pack_id'), payload.get('source_digest')
    if not isinstance(pack_id, str) or not re.fullmatch(r'[0-9a-f]{32}', pack_id) or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Choose a valid legacy draft and preview.')
    if os.environ.get('KEVENOS_ENGAGEMENT_V2') != '1':
        raise Conflict('Enable the new runner before copying a legacy draft.')
    if pack_id in state.get('legacy_imports', {}):
        raise Conflict('This legacy draft was already copied.')
    source = _read(db, 'audience_questions_v1', {})
    pack = next((item for item in source.get('packs', []) if item.get('id') == pack_id), None)
    if not pack or pack.get('status') != 'draft' or _legacy_digest(pack) != digest:
        raise Conflict('The legacy draft changed. Review the inventory again.')
    questions = pack.get('questions')
    if not isinstance(questions, list) or not questions or any(
            not isinstance(question, dict) or not isinstance(question.get('id'), str) or
            not re.fullmatch(r'[0-9a-f]{32}', question['id']) for question in questions):
        raise ValueError('This legacy draft has invalid question identifiers.')
    if len({question['id'] for question in questions}) != len(questions):
        raise ValueError('This legacy draft has duplicate question identifiers.')
    converted = []
    for question in questions:
        kind = question.get('type')
        if kind == 'choice':
            options = question.get('options')
            if not isinstance(options, list) or any(not isinstance(option, dict) for option in options):
                raise ValueError('This legacy draft has invalid answer options.')
            converted.append({'type':'single', 'prompt':question.get('prompt'),
                              'options':[option.get('text') for option in options]})
        elif kind in {'rating','text'}:
            converted.append({'type':kind, 'prompt':question.get('prompt')})
        else:
            raise ValueError('This legacy draft contains an unsupported question type.')
    draft = save_draft(state, {'title':pack.get('title'), 'description':pack.get('description',''),
                               'kind':'poll', 'questions':converted}, expected_revision=expected_revision)
    state.setdefault('legacy_imports', {})[pack_id] = {'activity_id':draft['id'], 'source_digest':digest,
        'question_map':{old['id']:new['id'] for old, new in zip(pack['questions'], draft['questions'])}}
    return draft


def analytics_csv(state, workspace, run_id=None):
    """Export aggregate response metrics; never include individual responses."""
    def safe(value):
        text = str(value)
        return "'" + text if text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')) else text
    projected = desk_view(state)
    runs = [run for run in projected['runs'] if run_id is None or run['id'] == run_id]
    if run_id is not None and not runs:
        raise Conflict('The requested run does not exist.')
    if sum(len(run['question_analytics']) for run in runs) > 10000:
        raise ValueError('Export one run at a time for this event.')
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['generated_utc','workspace','activity','version','run_id','question','points_per_correct_answer','respondents',
                     'denominator','views','option_counts','correct_count','median_seconds','suppressed_below'])
    generated = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for run in runs:
        questions = {item['id']:item for item in run['snapshot']['questions']}
        for metric in run['question_analytics']:
            question = questions[metric['question_id']]
            labels = ['1','2','3','4','5'] if question['type'] == 'rating' else question['options']
            counts = metric['distribution']
            breakdown = '; '.join(f'{label}: {count}' for label, count in zip(labels, counts)) if counts is not None else 'suppressed or not applicable'
            writer.writerow([generated, safe(workspace), safe(run['snapshot']['title']), run['version'], run['id'],
                             safe(question['prompt']), question.get('points', 1) if run['snapshot']['kind'] == 'quiz' else '',
                             metric['respondents'], metric['denominator'], 'not tracked',
                             safe(breakdown), '' if metric['correct_count'] is None else metric['correct_count'],
                             '' if metric['median_seconds'] is None else metric['median_seconds'], metric['suppressed_below']])
    return ('\ufeff' + buffer.getvalue()).encode('utf-8')


def persist(service, action, payload, *, expected_revision):
    """Apply one catalog operation atomically in the selected event workspace.

    Authorization, audit and idempotency remain the responsibility of the
    future command adapter. SQLite is supported for a single app instance.
    """
    _validate_command(action, payload)
    db = service.DB
    with service.LOCK, database.named_lock(db, 'engagement-catalog'), db:
        state = _read(db, KEY, empty())
        result = _apply(state, action, payload, expected_revision, db)
        _write(db, KEY, state)
        return result


OPERATOR_ACTIONS = frozenset({'open_run', 'finish_run', 'open_question', 'close_question',
    'pause_question', 'resume_question', 'extend_question', 'resend_question',
    'reveal_question', 'share_hall'})


def execute(service, action, payload, *, expected_revision, request_id, identity):
    """Authorize and atomically persist an audited, replay-safe command.

    identity must originate from the server's authenticated session, never
    directly from a request body. This is not yet an HTTP or MCP endpoint.
    """
    if not isinstance(identity, dict) or identity.get('dashboard', identity.get('role')) not in {'admin', 'organizer', 'desk', 'operator'} or not identity.get('credential_id') or (identity.get('impersonation') or {}).get('mode') == 'preview':
        raise PermissionError('Organizer access is required.')
    if identity.get('dashboard', identity.get('role')) == 'operator' and action not in OPERATOR_ACTIONS:
        raise PermissionError('This action requires Organizer access.')
    if action == 'set_quiz_enabled' and identity.get('dashboard', identity.get('role')) != 'admin':
        raise PermissionError('Admin access is required to change quiz availability.')
    _validate_command(action, payload)
    if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
        raise ValueError('Provide a unique request ID.')
    actor = identity.get('actor') or identity
    fingerprint = hashlib.sha256(json.dumps({'action': action, 'payload': payload,
        'revision': expected_revision, 'actor': actor.get('credential_id', ''),
        'effective': identity['credential_id']}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    db = service.DB
    with service.LOCK, database.named_lock(db, 'engagement-catalog'), db:
        receipt_key = f'{RECEIPTS_KEY}:{request_id}'
        receipt = _read(db, receipt_key, None)
        if receipt is None:
            receipt = _read(db, RECEIPTS_KEY, {}).get(request_id)
        if receipt is not None:
            if receipt['fingerprint'] != fingerprint:
                raise Conflict('Request ID was already used for a different operation.')
            return deepcopy(receipt['result'])
        state = _read(db, KEY, empty())
        if (action == 'set_quiz_enabled' and payload.get('enabled') is False
                and os.environ.get('KEVENOS_ENGAGEMENT_V2') == '1'
                and _legacy_delivery_open(db, include_audience=False)):
            raise Conflict('Close the existing legacy quiz question before disabling quizzes.')
        if action in {'open_question','resend_question'} and os.environ.get('KEVENOS_ENGAGEMENT_V2') == '1' and _legacy_delivery_open(db):
            raise Conflict('Close the existing quiz or audience question before starting an Engagement Hub question.')
        result = _apply(state, action, payload, expected_revision, db)
        _write(db, KEY, state)
        _write(db, receipt_key, {'fingerprint': fingerprint, 'result': result})
        profile = identity.get('impersonation') or {}
        db.execute('''INSERT INTO audit(ts,actor,action,detail,actor_credential,actor_label,
                      effective_credential,effective_role,profile_id,impersonation_mode)
                      VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (time.time(), identity.get('dashboard') or identity.get('role', ''), 'engagement-' + action,
                    request_id, actor.get('credential_id',''), actor.get('label',''),
                    identity['credential_id'], identity.get('role',''), profile.get('id',''), profile.get('mode','')))
        return deepcopy(result)


def answer(service, run_id, delivery_id, choice, *, identity):
    """Submit using a server-authenticated participant or individual guest session."""
    if (not isinstance(identity, dict) or identity.get('role') not in {'participant','guest'}
            or (identity.get('impersonation') or {}).get('mode') == 'preview'):
        raise PermissionError('An individual attendee session is required.')
    audience = identity['role']
    principal = identity.get('credential_id') if audience == 'participant' else ('guest:' + identity['digest'] if identity.get('digest') else None)
    if not principal:
        raise PermissionError('An individual attendee session is required.')
    db = service.DB
    with service.LOCK, database.named_lock(db, 'engagement-catalog'), db:
        state = _read(db, KEY, empty())
        run = _find_run(state, run_id)
        if audience not in run['snapshot'].get('audiences', ['participant']):
            raise PermissionError('This activity is not open to your audience.')
        receipt = submit_answer(state, run_id, delivery_id, principal, choice)
        _write(db, KEY, state)
        return receipt


def handle(service, handler, path, body=None):
    """Experimental, explicitly enabled Organizer and participant adapters."""
    if path not in {DESK, ADMIN_SETTINGS, LEGACY_PREVIEW, ANALYTICS_CSV, PLAYER, SHARED_PLAYER, QUESTIONS} or os.environ.get('KEVENOS_ENGAGEMENT_V2') != '1':
        return False
    identity = service.event_access.session(service, handler.headers)
    if not identity:
        handler.send(401, {'error': 'Sign in to continue.'})
        return True
    role = identity.get('dashboard', identity.get('role'))
    if path == ADMIN_SETTINGS:
        if role != 'admin' or identity.get('impersonation'):
            handler.send(403, {'error':'Admin access is required to change quiz availability.'})
            return True
        try:
            if body is not None:
                if not isinstance(body, dict) or set(body) != {'action','revision','request_id','payload'} or body.get('action') != 'set_quiz_enabled':
                    raise ValueError('Use the supported setting fields.')
                execute(service, body['action'], body['payload'], expected_revision=body['revision'],
                        request_id=body['request_id'], identity=identity)
            with service.LOCK:
                state = _read(service.DB, KEY, empty())
            handler.send(200, {'revision':state['revision'], 'quiz_enabled':state.get('quiz_enabled', True)})
        except PermissionError as error:
            handler.send(403, {'error':str(error)})
        except Conflict as error:
            handler.send(409, {'error':str(error)})
        except ValueError as error:
            handler.send(400, {'error':str(error)})
        return True
    if path == QUESTIONS:
        audience = identity.get('role')
        if audience not in {'participant', 'guest'}:
            handler.send(403, {'error':'Participant or guest access is required.'})
            return True
        try:
            if audience == 'participant':
                assignment = service.event_access.participant_assignment(service, identity)
                if not assignment or not assignment['name'].strip():
                    raise Conflict('Your participant assignment is unavailable.')
                principal = 'participant:' + identity['credential_id']
            else:
                digest = identity.get('digest')
                if not digest:
                    raise PermissionError('An individual guest session is required.')
                principal = 'guest:' + digest
            if body is not None:
                if (identity.get('impersonation') or {}).get('mode') == 'preview':
                    raise PermissionError('This profile is a read-only preview.')
                if not isinstance(body, dict) or set(body) not in ({'text','request_id'}, {'question_id','upvote'}):
                    raise ValueError('Use the supported question fields.')
                db = service.DB
                with service.LOCK, database.named_lock(db, 'engagement-catalog'), db:
                    state = _read(db, KEY, empty())
                    receipt = (ask_question(state, principal, audience, body['text'], body['request_id'])
                               if 'text' in body else vote_question(state, body['question_id'], principal, audience, body['upvote']))
                    _write(db, KEY, state)
                handler.send(200, {'receipt':receipt})
                return True
            with service.LOCK:
                state = _read(service.DB, KEY, empty())
            visible_ids = {item['id'] for item in state.get('qa', []) if item['status'] in {'approved','answered'}}
            own = [{**{key:item[key] for key in ('id','text','status','answer','created_at') if key in item},
                    **({'merged_into':item['merged_into']} if item.get('merged_into') in visible_ids else {})}
                   for item in state.get('qa', []) if item['principal'] == principal]
            enabled = state.get('qa_settings', {}).get('participants' if audience == 'participant' else 'guests', False)
            handler.send(200, {'enabled':enabled, 'questions':public_questions(state, principal=principal), 'own':own})
        except PermissionError as error:
            handler.send(403, {'error':str(error)})
        except Conflict as error:
            handler.send(409, {'error':str(error)})
        except ValueError as error:
            handler.send(400, {'error':str(error)})
        return True
    if path in {PLAYER, SHARED_PLAYER}:
        audience = identity.get('role')
        if audience not in ({'participant'} if path == PLAYER else {'participant','guest'}):
            handler.send(403, {'error': 'Attendee access is required.'})
            return True
        try:
            if audience == 'participant':
                assignment = service.event_access.participant_assignment(service, identity)
                if not assignment or not assignment['name'].strip():
                    raise Conflict('Your participant assignment is unavailable.')
                principal = identity['credential_id']
            else:
                if not identity.get('digest'):
                    raise PermissionError('An individual guest session is required.')
                principal = 'guest:' + identity['digest']
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
            if run and audience not in run['snapshot'].get('audiences', ['participant']):
                run = None
            own = next((run.get('responses', {}).get(item['id'], {}).get(principal)
                for item in [*run.get('deliveries', []), run['delivery']]
                if item.get('question_id') == run['active_question_id'] and
                principal in run.get('responses', {}).get(item['id'], {})), None) if run else None
            handler.send(200, {'question': player_view(run, principal=principal) if run else None,
                               'answered': bool(own), 'selection': own['choice'] if own else None,
                               'score': player_score(state, principal, audience), 'now': time.time()})
        except PermissionError as error:
            handler.send(403, {'error': str(error)})
        except Conflict as error:
            handler.send(409, {'error': str(error)})
        except ValueError as error:
            handler.send(400, {'error': str(error)})
        return True
    if role == 'operator' and path != DESK:
        handler.send(403, {'error':'This action requires Organizer access.'})
        return True
    if role not in {'admin', 'organizer', 'desk', 'operator'}:
        handler.send(403, {'error': 'Organizer access is required.'})
        return True
    if path == DESK and isinstance(body, dict) and body.get('action') == 'set_quiz_enabled':
        handler.send(403, {'error':'Use Admin event settings to change quiz availability.'})
        return True
    if path == ANALYTICS_CSV:
        if body is not None:
            handler.send(405, {'error':'Download analytics with GET.'})
            return True
        try:
            query = parse_qs(urlparse(handler.path).query, keep_blank_values=True, strict_parsing=True)
            if set(query) - {'run_id'} or any(len(value) != 1 for value in query.values()):
                raise ValueError('Use a single run filter.')
            run_id = query.get('run_id', [None])[0]
            if run_id is not None and not re.fullmatch(r'[0-9a-f]{32}', run_id):
                raise ValueError('Choose a valid run.')
            with service.LOCK:
                state = _read(service.DB, KEY, empty())
            data = analytics_csv(state, service.WORKSPACE.mode if service.WORKSPACE else 'local', run_id)
            handler.send(200, data, 'text/csv', {'Content-Disposition':'attachment; filename=engagement-analytics.csv'})
        except Conflict as error:
            handler.send(409, {'error':str(error)})
        except ValueError as error:
            handler.send(400, {'error':str(error)})
        return True
    if path == LEGACY_PREVIEW:
        if body is not None:
            handler.send(405, {'error':'Read the compatibility inventory with GET.'})
        else:
            try:
                with service.LOCK:
                    result = legacy_preview(service.DB)
                handler.send(200, result)
            except ValueError as error:
                handler.send(409, {'error':str(error)})
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
        handler.send(200, operator_view(state) if role == 'operator' else desk_view(state))
    except PermissionError as error:
        handler.send(403, {'error': str(error)})
    except Conflict as error:
        handler.send(409, {'error': str(error)})
    except ValueError as error:
        handler.send(400, {'error': str(error)})
    return True
