"""Event-owned activity drafts and immutable published versions.

This is a domain primitive only: callers must persist changes atomically and
authorize the actor before invoking it. It is not a public API or live runner.
"""
from copy import deepcopy
from uuid import uuid4


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
    if kind not in {'quiz', 'poll'}:
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
        if question.get('type') != 'single':
            raise ValueError('Only single-choice questions are supported in this draft.')
        prompt = _text(question.get('prompt'), 500, 'a question')
        options = question.get('options')
        if not isinstance(options, list) or not 2 <= len(options) <= 8:
            raise ValueError('Include between 2 and 8 answer options.')
        choices = [_text(option, 150, 'an answer option') for option in options]
        correct = question.get('correct', [])
        if not isinstance(correct, list) or any(type(i) is not int or i < 0 or i >= len(choices) for i in correct):
            raise ValueError('Choose a valid correct answer.')
        if (kind == 'quiz' and len(correct) != 1) or (kind == 'poll' and correct):
            raise ValueError('A quiz needs one correct answer; polls are unscored.')
        clean.append({'id': question_id, 'type': 'single', 'prompt': prompt, 'options': choices, 'correct': list(correct)})
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
    version = next((v for v in activity['versions'] if v['version'] == version_number), None)
    if version is None:
        raise Conflict('Choose a published activity version.')
    run = {'id': uuid4().hex, 'activity_id': activity_id, 'version': version_number,
           'status': 'open', 'snapshot': deepcopy(version['snapshot'])}
    state['runs'].append(run)
    state['revision'] += 1
    return deepcopy(run)


def player_view(run):
    snapshot = deepcopy(run['snapshot'])
    for question in snapshot['questions']:
        question.pop('correct', None)
    return {**snapshot, 'id': run['id'], 'activity_id': run['activity_id'], 'status': run['status']}
