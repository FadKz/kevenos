"""Preview timing-only agenda edits without changing event-day operation state.

Call with the workspace pinned and the application lock held. The agent API
provides authorization, confirmation, revision checks and durable receipts.
"""
from copy import deepcopy
import hashlib
import json
import time

import event_config


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _minute(value):
    sign = -1 if value.startswith('-') else 1
    hour, minute = map(int, value.lstrip('-').split(':'))
    return sign * (hour * 60 + minute)


def _time(value):
    sign = '-' if value < 0 else ''
    hour, minute = divmod(abs(value), 60)
    return f'{sign}{hour:02d}:{minute:02d}'


def _timing(item):
    start = _minute(item['time'])
    return {'start': item['time'], 'end': _time(start + item['min']),
            'duration_minutes': item['min']}


def _active(items, program, minute):
    if not items:
        return None
    if program.get('mode') == 'clock':
        index = 0
        for i, item in enumerate(items):
            if _minute(item['time']) <= minute:
                index = i
    else:
        index = next((i for i, row in enumerate(items) if row['id'] == program.get('item_id')),
                     max(0, min(len(items)-1, int(program.get('i', 0)))))
    return {'id': items[index]['id'], 'title': items[index]['title']}


def _validate_request(args):
    if not isinstance(args, dict) or set(args) - {'change', 'shift_following', 'note'}:
        raise ValueError('Provide a timing change and an explicit choice for following sections.')
    change = args.get('change')
    if not isinstance(change, dict) or set(change) - {'id', 'start_time', 'duration_minutes', 'shift_minutes'}:
        raise ValueError('Only agenda start times and durations can be changed here.')
    if not isinstance(change.get('id'), str) or len(change['id']) != 32:
        raise ValueError('Choose an existing agenda item ID.')
    if not any(key in change for key in ('start_time', 'duration_minutes', 'shift_minutes')):
        raise ValueError('Choose a start time, duration or minute shift.')
    if 'start_time' in change and 'shift_minutes' in change:
        raise ValueError('Choose either a start time or a minute shift, not both.')
    if type(args.get('shift_following')) is not bool:
        raise ValueError('Specify whether to shift following sections.')
    for key, low, high in (('duration_minutes', 0, 1440), ('shift_minutes', -1439, 1439)):
        if key in change and (type(change[key]) is not int or not low <= change[key] <= high):
            raise ValueError(f'{key} must be whole minutes within its supported range.')
    if 'start_time' in change:
        value = change['start_time']
        if not isinstance(value, str) or len(value) != 5 or value[2] != ':':
            raise ValueError('Use a 24-hour HH:MM start time.')
        try:
            hour, minute = map(int, value.split(':'))
        except (TypeError, ValueError):
            raise ValueError('Use a 24-hour HH:MM start time.') from None
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError('Use a start time between 00:00 and 23:59.')
    if 'note' in args and (not isinstance(args['note'], str) or len(args['note']) > 300):
        raise ValueError('Keep the timing-change note within 300 characters.')


def _plan(s, args):
    import event_commands as commands
    _validate_request(args)
    before = commands._agenda_items(s.CONTENT)
    after = deepcopy(before)
    change = args['change']
    index = next((i for i, row in enumerate(before) if row['id'] == change['id']), None)
    blockers, warnings = [], []
    if index is None:
        blockers.append({'code': 'agenda_item_missing', 'message': 'The agenda item no longer exists. Read the current agenda and choose its ID.'})
    else:
        selected = before[index]
        old_start = _minute(selected['time'])
        start = _minute(change['start_time']) if 'start_time' in change else old_start + change.get('shift_minutes', 0)
        duration = change.get('duration_minutes', selected['min'])
        end_delta = start + duration - old_start - selected['min']
        after[index].update(time=_time(start), min=duration)
        if args['shift_following']:
            for row in after[index+1:]:
                row['time'] = _time(_minute(row['time']) + end_delta)
        for row in after:
            row_start = _minute(row['time'])
            if row_start < 0 or row_start > 1439 or row_start + row['min'] > 1440:
                blockers.append({'code': 'day_boundary', 'message': 'Keep all agenda sections within the same day (00:00–24:00). This change crosses the day boundary.'})
                break
        if not blockers:
            try:
                commands._validate_agenda(s, after)
            except ValueError as error:
                blockers.append({'code': 'invalid_schedule', 'message': str(error)})
    raw_program = s.kv_get('program') or {}
    program = dict(raw_program, mode=raw_program.get('mode', 'manual'))
    local = time.localtime()
    now = local.tm_hour * 60 + local.tm_min
    active_before, active_after = _active(before, program, now), _active(after, program, now)
    selection_changes = active_before != active_after
    changes = [{'id': old['id'], 'title': old['title'], 'before': _timing(old), 'after': _timing(new)}
               for old, new in zip(before, after)
               if (old['time'], old['min']) != (new['time'], new['min'])]
    if program['mode'] == 'clock' and changes:
        warnings.append('The programme follows the event clock. Its current section may change immediately when these times are applied.')
    elif changes:
        warnings.append('The current manually selected section and its actual start time are preserved.')
    if changes:
        warnings.append('Agenda timing does not change the judging timers, Final state or scores.')
    elif not blockers:
        warnings.append('These timings already match the agenda; no content version will be created.')
    # Use the raw persisted program rather than program_now(): an uninitialized
    # manual program fabricates a current timestamp, while clock selection moves
    # naturally with wall time. Neither should make an otherwise exact plan stale.
    material = {'request': args, 'content': s.CONTENT,
                'configuration': event_config.config(s, read_only=True),
                'program': raw_program,
                'workspace': s.WORKSPACE.context if s.WORKSPACE else 'local'}
    report = {'can_apply': not blockers, 'changed': bool(changes), 'blockers': blockers,
              'warnings': warnings, 'changes': changes,
              'old_event_end': _timing(before[-1])['end'] if before else None,
              'new_event_end': _timing(after[-1])['end'] if after else None,
              'program': {'mode': program['mode'], 'active_item_before': active_before,
                          'active_item_after': active_after, 'selection_changes': selection_changes},
              'clock_timezone': time.strftime('%Z'), 'plan_digest': _digest(material)}
    return report, after


def preview(s, args):
    return _plan(s, args)[0]


def apply(s, args, actor):
    import event_commands as commands
    request = {key: value for key, value in args.items() if key != 'plan_digest'}
    plan, items = _plan(s, request)
    if plan['plan_digest'] != args.get('plan_digest'):
        raise commands.CommandError('stale_schedule_plan', 'Preview the timing change again; the event, agenda or selected programme changed.', 409)
    if not plan['can_apply']:
        raise commands.CommandError('schedule_conflict', ' '.join(row['message'] for row in plan['blockers']), 409)
    version = s.CONTENT.get('content_version', 1)
    if plan['changed']:
        draft = deepcopy(s.CONTENT)
        # Preserve unknown per-item metadata too. Only timing and legacy IDs
        # may change; position and content remain exactly as recorded.
        for original, timing in zip(draft['agenda'], items):
            original.update(id=timing['id'], time=timing['time'], min=timing['min'])
        version = commands._save_content(s, draft, args.get('note', 'Agenda timings updated'), actor)
        # One content save and no program write: ordering and IDs are unchanged.
        # This does not claim a transaction across content files and SQLite;
        # the common API retains an uncertain receipt if a later audit fails.
    return {**plan, 'applied': plan['changed'], 'version': version,
            'schedule': commands._schedule(s, {})}


def _edit_plan(s, args):
    import event_commands as commands
    from jsonschema import Draft202012Validator
    # Also validate direct callers: this planner is an authority shared by clients.
    if not Draft202012Validator(commands.EDIT_PREVIEW).is_valid(args):
        raise commands.CommandError('invalid_agenda_edit', 'Choose a valid agenda operation and bounded fields.')
    before = commands._agenda_items(s.CONTENT)
    after = deepcopy(before)
    edit = args['edit']
    raw_program = s.kv_get('program') or {}
    program = dict(raw_program, mode=raw_program.get('mode', 'manual'))
    material = {'request': args, 'content': s.CONTENT,
                'configuration': event_config.config(s, read_only=True),
                'program': raw_program,
                'workspace': s.WORKSPACE.context if s.WORKSPACE else 'local'}
    digest = _digest(material)
    local = time.localtime()
    now = local.tm_hour * 60 + local.tm_min
    active_before = _active(before, program, now)
    blockers, warnings = [], []
    def index(key):
        if key not in edit:
            return len(after)
        found = next((i for i, row in enumerate(after) if row['id'] == edit[key]), None)
        if found is None:
            raise ValueError('The selected section no longer exists. Reload the agenda.')
        return found
    try:
        action = edit['action']
        if not before:
            raise ValueError('Restore an agenda with a starting time before using this editor.')
        if action == 'insert':
            if len(after) >= 100:
                raise ValueError('Keep the agenda within 100 sections.')
            at = index('before_id')
            start = _minute(after[at]['time']) if at < len(after) else _minute(after[-1]['time']) + after[-1]['min']
            item = dict(line='', who='', do=[], scene='info')
            item.update(edit['item'])
            if item['min'] < 1:
                raise ValueError('A new section needs at least one minute.')
            item.update(id=digest[:32], time=_time(start))
            for row in after[at:]:
                row['time'] = _time(_minute(row['time']) + item['min'])
            after.insert(at, item)
        elif action == 'update':
            at = index('id')
            old_end = _minute(after[at]['time']) + after[at]['min']
            after[at].update(edit['changes'])
            delta = _minute(after[at]['time']) + after[at]['min'] - old_end
            for row in after[at+1:]:
                row['time'] = _time(_minute(row['time']) + delta)
        elif action == 'remove':
            at = index('id')
            if len(after) == 1:
                raise ValueError('Keep at least one agenda section.')
            if program['mode'] == 'manual' and after[at]['id'] == active_before['id']:
                raise ValueError('This section is currently on display. Select another section in Event Operations before removing it.')
            removed = after.pop(at)
            for row in after[at:]:
                row['time'] = _time(_minute(row['time']) - removed['min'])
        elif action == 'move':
            at = index('id')
            index('before_id') # Check a stale anchor before modifying the list.
            if edit.get('before_id') != edit['id']:
                gaps = [_minute(b['time']) - _minute(a['time']) - a['min'] for a, b in zip(before, before[1:])]
                row = after.pop(at)
                after.insert(index('before_id'), row)
                start = _minute(before[0]['time'])
                for i, row in enumerate(after):
                    row['time'] = _time(start)
                    start += row['min'] + (gaps[i] if i < len(gaps) else 0)
                warnings.append('Reordering retains the day start, section durations and gaps between timetable positions.')
        for row in after:
            start = _minute(row['time'])
            if start < 0 or start > 1439 or start + row['min'] > 1440:
                raise ValueError('Keep all sections within the same day (00:00–24:00).')
        commands._validate_agenda(s, after)
    except ValueError as error:
        blockers.append({'code': 'schedule_conflict', 'message': str(error)})
    active_after = _active(after, program, now)
    if program['mode'] == 'manual' and active_before:
        active_after = next(({'id': row['id'], 'title': row['title']} for row in after if row['id'] == active_before['id']), None)
    if program['mode'] == 'clock':
        # Crossing a clock boundary can change the live-display effect between
        # preview and execution even when no persisted event state changed.
        digest = _digest({'state': digest,
                          'active_before': (active_before or {}).get('id'),
                          'active_after': (active_after or {}).get('id')})
    old_by_id, new_by_id = ({row['id']: row for row in rows} for rows in (before, after))
    changes = []
    old_positions = {row['id']: i for i, row in enumerate(before)}
    new_positions = {row['id']: i for i, row in enumerate(after)}
    for row in after + [row for row in before if row['id'] not in new_by_id]:
        old, new = old_by_id.get(row['id']), new_by_id.get(row['id'])
        if old != new or old_positions.get(row['id']) != new_positions.get(row['id']):
            changes.append({'id': row['id'], 'title': row['title'],
                            'kind': 'added' if old is None else 'removed' if new is None else 'updated',
                            'before': _timing(old) if old else None,
                            'after': _timing(new) if new else None,
                            'details': {key: {'before': (old or {}).get(key), 'after': (new or {}).get(key)}
                                        for key in ('title', 'line', 'who', 'do', 'scene')
                                        if (old or {}).get(key) != (new or {}).get(key)
                                        and (old and new or (old or {}).get(key) or (new or {}).get(key))}})
    changed = before != after
    if active_before and active_before['id'] in old_by_id and active_before['id'] in new_by_id:
        old_current, new_current = old_by_id[active_before['id']], new_by_id[active_before['id']]
        if any(old_current.get(key) != new_current.get(key) for key in ('title', 'line', 'who', 'do', 'scene')):
            warnings.append('You are editing the current displayed section: its text/scene changes immediately. Changing the title or scene can also affect quiz availability.')
    if changed:
        warnings.append('Judging timers, Final state and scores are unchanged.')
        warnings.append('The clock selects the current section from the new times immediately.' if program['mode'] == 'clock'
                        else 'The current manual section and its actual start are preserved. Event Operations chooses when to show a new break.')
    else:
        warnings.append('No agenda changes to apply.')
    return {'can_apply': not blockers, 'changed': changed, 'blockers': blockers, 'warnings': warnings,
            'changes': changes, 'items': after,
            'old_event_end': _timing(before[-1])['end'] if before else None,
            'new_event_end': _timing(after[-1])['end'] if after else None,
            'program': {'mode': program['mode'], 'active_item_before': active_before,
                        'active_item_after': active_after,
                        'selection_changes': (active_before or {}).get('id') != (active_after or {}).get('id')},
            'clock_timezone': time.strftime('%Z'), 'plan_digest': digest}, after


def edit_preview(s, args):
    return _edit_plan(s, args)[0]


def edit_apply(s, args, actor):
    import event_commands as commands
    request = {key: value for key, value in args.items() if key != 'plan_digest'}
    plan, items = _edit_plan(s, request)
    if plan['plan_digest'] != args.get('plan_digest'):
        raise commands.CommandError('stale_schedule_plan', 'The agenda or event changed. Preview again before applying.', 409)
    if not plan['can_apply']:
        raise commands.CommandError('schedule_conflict', ' '.join(row['message'] for row in plan['blockers']), 409)
    version = s.CONTENT.get('content_version', 1)
    if plan['changed']:
        # Preserve metadata outside the editable projection, also when reordering.
        originals = {item['id']: raw for item, raw in zip(commands._agenda_items(s.CONTENT), s.CONTENT['agenda'])}
        rows = [dict(deepcopy(originals.get(item['id'], {})), **item) for item in items]
        result = commands._save_agenda(s, rows, args.get('note', 'Agenda updated'), actor)
        version = result['version']
    return {**plan, 'applied': plan['changed'], 'version': version, 'schedule': commands._schedule(s, {})}
