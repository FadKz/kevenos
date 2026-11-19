"""Request-neutral, scoped operations for KevenOS event agents.

The API owns authorization, schema validation, revision checks and receipts.
Call these operations with the active workspace pinned and the domain lock held.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import time
import uuid

import audience_questions
import event_access
import event_config
import event_mail
import event_quiz
import engagement_catalog
import final_event


class CommandError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def obj(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {},
            'required': list(required), 'additionalProperties': False}


def string(maximum, minimum=0, **keywords):
    return {'type': 'string', 'minLength': minimum, 'maxLength': maximum, **keywords}


def integer(low, high):
    return {'type': 'integer', 'minimum': low, 'maximum': high}


def choices(*values):
    return {'enum': list(values), 'type': 'string'}


def array(items, maximum, minimum=0, **keywords):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum, **keywords}


def actions(**fields):
    return {'oneOf': [obj({'action': choices(action), **properties}, ('action', *required))
                      for action, (properties, required) in fields.items()]}


BOOL = {'type': 'boolean'}
TEAM = integer(1, 1000)
MEMBER = integer(1, 10)
STATION = integer(1, 32)
VERSION = integer(1, 2**31 - 1)
REASON = string(500, 8)
LIMIT = integer(1, 100)
OFFSET = integer(0, 100000)
ADDRESS = string(254, 3)
NOTE = string(300)
CREDENTIAL_ID = string(24, 24, pattern='^[0-9a-f]{24}$')
AGENDA_ID = string(32, 32, pattern='^[0-9a-f]{32}$')
SCENES = ('info', 'prep', 'pitch', 'scores', 'final', 'awards', 'message')
AGENDA_FIELDS = {
    'time': string(5, 5, pattern='^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'),
    'title': string(160, 1), 'line': string(600), 'min': integer(0, 1440),
    'who': string(160), 'do': array(string(600, 1), 20), 'scene': choices(*SCENES),
}
AGENDA_ROW = obj({'id': AGENDA_ID, **AGENDA_FIELDS}, AGENDA_FIELDS)
ROSTER_MEMBER = obj({'id': MEMBER, 'name': string(80), 'coach': BOOL}, ('id', 'name', 'coach'))
ROSTER_TEAM = obj({'pair': TEAM, 'partner': string(80), 'members': array(ROSTER_MEMBER, 10)},
                  ('pair', 'partner', 'members'))
CONFIG_FIELDS = {name: integer(*bounds) for name, bounds in event_config.LIMITS.items()}
CONFIG = obj(CONFIG_FIELDS, CONFIG_FIELDS)
PAGE = {'limit': LIMIT, 'offset': OFFSET}
SECTIONS = ('event', 'steps', 'rubric', 'bonus_tick', 'bonus_note', 'questions', 'objections',
            'scenarios', 'demo', 'tiebreak', 'final_rule', 'bells', 'judge_rules', 'mock')
# Content structure has a typed schema per section. Question references can be extended.
TEXT = string(5000)
REQUIRED_TEXT = string(5000, 1)
QUESTION_ID = string(10, 2, pattern='^Q[1-9][0-9]{0,8}$')
TICK_ID = string(3, 2, pattern='^[DMSC][0-9]$')


def fields(*names, **schemas):
    properties = {**dict.fromkeys(names, TEXT), **schemas}
    return obj(properties, properties)


SCENARIO_SCHEMA = fields(
    'id', 'type', 'name', 'line', 'persona', 'lean_in', stations=array(STATION, 32, uniqueItems=True),
    card=array(TEXT, 30), reveals={'type': 'object', 'propertyNames': QUESTION_ID,
                                  'additionalProperties': TEXT, 'maxProperties': 100},
    objections=array(array(TEXT, 3, 3), 30), model=fields('discover', 'maw', 'solve', 'close'),
    bonus=fields('ai', 'package'),
)
TICK_SCHEMA = fields(id=TICK_ID, text=REQUIRED_TEXT, points=integer(10, 10),
                     counts=REQUIRED_TEXT, short=REQUIRED_TEXT)
MOCK_KEY = fields(**{key: fields('why', earned=BOOL)
                     for key in ('D1','D2','D3','M1','M2','S1','S2','S3','C1','C2')})
SECTION_SCHEMAS = {
    'event': fields('name', 'headline', 'date', 'time', 'venue', 'layout', 'goal', 'who'),
    'steps': array(fields('n', 'name', 'line', minutes=integer(1, 10)), 4, 4),
    'rubric': array(fields('name', points=integer(20, 30), ticks=array(TICK_SCHEMA, 3, 2)), 4, 4),
    'bonus_tick': fields('counts', text=REQUIRED_TEXT, points=integer(2, 2)),
    'bonus_note': TEXT,
    'questions': array(fields('group', items=array(fields('text', id=QUESTION_ID, core=BOOL), 100, 1)), 20, 1),
    'objections': array(fields('kind', 'worry', 'mac', 'service'), 50),
    'scenarios': array(SCENARIO_SCHEMA, 4, 4),
    'demo': SCENARIO_SCHEMA,
    'tiebreak': TEXT,
    'final_rule': TEXT,
    'bells': array(fields('at', 'what'), 20),
    'judge_rules': array(TEXT, 50),
    'mock': fields('intro', 'feedback', lines=array(fields('step', 'who', 'line', 'tick'), 100), key=MOCK_KEY),
}
# Keep the registry adjacent to handlers so the schemas and dispatch evolve together.
RESOURCES = {}
COMMANDS = {}


def resource(name, description, scope, schema=None):
    def register(handler):
        RESOURCES[name] = ({'name': name, 'description': description, 'scope': scope,
                            'input_schema': schema or obj()}, handler)
        return handler
    return register


def command(name, description, scope, schema=None, risk='confirm'):
    def register(handler):
        # Every command returns a read-back projection. Require its read scope
        # as well, so a write-only grant never discloses previously stored data.
        granted = [scope] if isinstance(scope, str) else list(scope)
        required = list(granted)
        paired = {'workspace:write': 'event:read', 'notifications:send': 'notifications:read'}
        for item in granted:
            read_scope = paired.get(item, item[:-6] + ':read' if item.endswith(':write') else None)
            if read_scope and read_scope not in required:
                required.append(read_scope)
        if name == 'display.reveal' and 'event:read' not in required:
            required.append('event:read')
        COMMANDS[name] = ({'name': name, 'description': description, 'scope': required,
                           'input_schema': {'type': 'object', **(schema or obj())}, 'risk': risk}, handler)
        return handler
    return register


def catalog():
    return deepcopy({'resources': [item[0] for item in RESOURCES.values()],
                     'commands': [item[0] for item in COMMANDS.values()]})


def read(s, name, args, principal):
    try:
        handler = RESOURCES[name][1]
    except KeyError:
        raise CommandError('unknown_resource', 'The requested resource does not exist.', 404) from None
    return handler(s, args)


def execute(s, name, args, principal):
    try:
        handler = COMMANDS[name][1]
    except KeyError:
        raise CommandError('unknown_command', 'The requested command does not exist.', 404) from None
    try:
        return handler(s, args, 'agent:' + principal['id'], principal)
    except ValueError as error:
        raise CommandError('invalid_arguments', str(error)) from None


def snapshot(s):
    """Read canonical state for CAS without time-dependent counters or auth telemetry."""
    def rows(db, sql, args=()):
        return [list(row) for row in db.execute(sql, args)]

    dataset = event_access.active_dataset(s)
    return {
        'content': s.CONTENT,
        'quiz_revision': event_quiz.load(s)['rev'],
        'engagement_revision': (s.kv_get(engagement_catalog.KEY) or engagement_catalog.empty())['revision'],
        'audience_revision': audience_questions.load(s)['rev'],
        'state': rows(s.DB, """
            SELECT k,v FROM kv WHERE k IN (
              'event_configuration','clock','mode','final','calib','public','program','reveal','attendees','event_operations_v1'
            ) OR k LIKE 'sclock:%' ORDER BY k
        """),
        'roster': rows(s.DB, 'SELECT * FROM roster ORDER BY pair'),
        'members': rows(s.DB, 'SELECT * FROM roster_members ORDER BY pair,member'),
        'assignments': rows(s.DB, 'SELECT * FROM team_assignments ORDER BY pair'),
        'sheets': rows(s.DB, 'SELECT * FROM sheets ORDER BY id'),
        'credentials': rows(event_access.auth_db(s), """
            SELECT id,role,label,pair,member,station,dataset,revision,active
              FROM access_credentials WHERE dataset IN (?, 'global') ORDER BY id
        """, (dataset,)),
    }


def _domain_result(result, status):
    if status >= 400:
        details = {key: result[key] for key in ('missing', 'review_token') if key in result}
        raise CommandError(result.get('code', 'state_conflict' if status == 409 else 'invalid_arguments'),
                           result['error'], status, details)
    return result


def _conflict(message):
    raise CommandError('state_conflict', message, 409)


def _missing(message):
    raise CommandError('not_found', message, 404)


def _page(values, args, key):
    start, limit = args.get('offset', 0), args.get('limit', 50)
    return {key: values[start:start + limit], 'total': len(values),
            'next_offset': start + limit if start + limit < len(values) else None}


def _workspace(s):
    if not s.WORKSPACE:
        raise CommandError('workspace_unavailable', 'This event has no workspace controller.', 409)
    return s.WORKSPACE


@resource('event.status', 'Read the current event stage, clocks and judge submission progress.', 'event:read')
def _status(s, args):
    final = final_event.get(s)
    in_final = final_event.engaged(final)
    clock = s.final_clock_public(final) if in_final else s.clock_public()
    stage_pair = final_event.current_pair(final)
    kind = 'final' if in_final else 'round'
    submitted = {(sheet['pair'], sheet['station'], sheet['round']) for sheet in
                 (final_event.sheets(s, final) if in_final else s.active_sheets(kind))
                 if sheet['id'] not in final.get('legacy_ballot_ids', [])}
    stations = []
    preliminary = s.preliminary_review(s.preliminary_receipts())
    for station in range(1, s.CFG['stations'] + 1):
        station_clock = clock if in_final else s.clock_public(station)
        round_no = 0 if in_final else station_clock['round']
        if in_final:
            pair = stage_pair if station in final['panel'] else None
        else:
            pair = event_config.pair_for(s, round_no, station, read_only=True)
        stations.append({'station': station, 'pair': pair, 'clock': station_clock,
                         'submitted': (pair, station, round_no) in submitted})
    return {'event': {key: s.CONTENT['event'].get(key) for key in ('name', 'date', 'venue')},
            'workspace': event_access.active_dataset(s), 'mode': s.mode(), 'clock': clock,
            'stations': stations, 'final_status': final.get('status'),
            'preliminary_outstanding': [{'round': row['round'], 'station': row['station'], 'pair': row['pair']}
                                        for row in preliminary if row['registered'] and not row['accepted'] and not row['exception']],
            'calibration_active': bool((s.kv_get('calib') or {}).get('active')),
            'display': s.public_mode(), 'reveal': s.kv_get('reveal', 'hidden'),
            'versions': {key: s.version_info()[key] for key in ('app','schema','content')}}


@resource('event.configuration', 'Read event capacity, judge count, rounds and schedule warnings.', 'event:read')
def _configuration(s, args):
    return event_config.status(s)


@resource('event.workspace', 'Read the selected dataset, capacity and registration counts.', 'event:read')
def _workspace_status(s, args):
    result = _workspace(s).status()
    return {key: value for key, value in result.items() if key != 'context'}


@command('event.configure', 'Resize teams, participant capacity and judges before scoring begins.', 'event:write', CONFIG)
def _configure(s, args, actor, principal):
    result = event_config.update(s, args)
    s.audit(actor, 'event-configuration', json.dumps(args, sort_keys=True))
    return result


@resource('rehearsal.resize_preview', 'Preview a rehearsal-only resize and identify protected data or conflicts.',
          'event:read', CONFIG)
def _rehearsal_resize_preview(s, args):
    import rehearsal_reconcile

    try:
        return rehearsal_reconcile.preview(s, args)
    except rehearsal_reconcile.ResizeConflict as error:
        raise CommandError(error.code, str(error), 409) from None


@command('rehearsal.resize_apply', 'Apply the exact previewed rehearsal resize, preserving protected records.',
         'event:write', obj({**CONFIG_FIELDS, 'plan_digest': string(64, 64, pattern='^[0-9a-f]{64}$')},
                            (*CONFIG_FIELDS, 'plan_digest')))
def _rehearsal_resize_apply(s, args, actor, principal):
    import rehearsal_reconcile

    try:
        result = rehearsal_reconcile.apply(s, args, args['plan_digest'])
    except rehearsal_reconcile.ResizeConflict as error:
        raise CommandError(error.code, str(error), 409) from None
    if result['applied'] and not result['replayed']:
        s.audit(actor, 'rehearsal-resize', args['plan_digest'])
    return result


@resource('workspace.live_preview', 'Preview the Staff-preserving Live transition, counts, blockers and exact plan digest.',
          ['event:read', 'access:read', 'workspace:write'],
          obj({'guest_policy': choices('revoke', 'retain')}))
def _live_preview(s, args):
    import event_transition
    try:
        return event_transition.preview(s, args.get('guest_policy', 'revoke'))
    except event_transition.Conflict as error:
        raise CommandError('transition_conflict', str(error), 409) from None


@command('workspace.prepare_live', 'Activate the reviewed Live plan: preserve Staff, retire Participant access and reset Live event data. Sends no notifications.',
         ['workspace:write', 'access:write', 'event:write'],
         obj({'plan_digest':string(64,64,pattern='^[0-9a-f]{64}$'),
              'guest_policy':choices('revoke','retain'), 'confirm':choices('PREPARE LIVE')},
             ('plan_digest','guest_policy','confirm')))
def _prepare_live(s, args, actor, principal):
    import event_transition
    if not {'live','rehearsal'} <= set(principal.get('workspaces', [])):
        raise CommandError('workspace_forbidden', 'Live preparation requires access to both Live and Rehearsal.', 403)
    try:
        return event_transition.apply(s, args['plan_digest'], args['confirm'],
            principal['idempotency_key'], actor, args['guest_policy'])
    except event_transition.Conflict as error:
        raise CommandError('transition_conflict', str(error), 409) from None


@command('workspace.switch', 'Select the live or rehearsal event dataset.', 'workspace:write',
         obj({'mode': choices('live', 'rehearsal')}, ('mode',)))
def _switch(s, args, actor, principal):
    mode = args['mode']
    _workspace(s).switch(mode, mode.upper())
    s.audit(actor, 'workspace-switch', mode)
    return _workspace_status(s, {})


@command('workspace.reset_rehearsal', 'Replace rehearsal with seeded practice data and rotate its workspace context.',
         'workspace:write', obj({'configuration': CONFIG}))
def _reset(s, args, actor, principal):
    _workspace(s).reset('RESET REHEARSAL', configuration=args.get('configuration'))
    s.audit(actor, 'workspace-reset', 'rehearsal')
    return _workspace_status(s, {})


def _agenda_items(content):
    items = []
    for index, item in enumerate(content.get('agenda', [])):
        normalized = {key: item[key] for key in AGENDA_FIELDS if key in item}
        normalized.setdefault('scene', 'info')
        # Old content lacks IDs. A deterministic ID can be carried forward by
        # the first agent edit and is stable across process restarts.
        legacy_id = sha256(json.dumps([index, normalized], sort_keys=True).encode()).hexdigest()[:32]
        items.append({'id': item.get('id') or legacy_id, **normalized})
    return items


def _validate_agenda(s, items):
    ids = set()
    previous_start, previous_end = -1, 0
    pitch_count = 0
    for item in items:
        if item['id'] in ids:
            raise ValueError('Agenda item IDs must be unique.')
        ids.add(item['id'])
        if not item['title'].strip():
            raise ValueError('Every agenda item needs a title.')
        hours, minutes = map(int, item['time'].split(':'))
        start = hours * 60 + minutes
        end = start + item['min']
        if start < previous_start or start < previous_end or end > 1440:
            raise ValueError('Agenda items must run in time order without overlap within one day.')
        previous_start, previous_end = start, end
        if item['scene'] == 'pitch':
            pitch_count += 1
            if item['min'] < event_config.config(s, read_only=True)['pitch_block_minutes']:
                raise ValueError('Allow at least the configured number of minutes for preliminary pitch rounds.')
    if pitch_count > 1:
        raise ValueError('Use a single pitch block for preliminary rounds.')


def _save_agenda(s, items, note, actor, active_item_id=None, content=None):
    previous = _agenda_items(s.CONTENT)
    program = s.program_now()
    program.setdefault('mode', 'manual')
    selected_id = active_item_id or (previous[program['i']]['id'] if previous else None)
    index_by_id = {item['id']: index for index, item in enumerate(items)}
    if active_item_id and active_item_id not in index_by_id:
        raise ValueError('Choose an active item in the updated agenda.')
    if program['mode'] == 'manual' and selected_id and selected_id not in index_by_id:
        _conflict('Choose which agenda item should be active when removing the current item.')
    if active_item_id and program['mode'] == 'clock':
        _conflict('Switch the program to manual mode before selecting an active item.')
    draft = deepcopy(s.CONTENT) if content is None else content
    draft['agenda'] = items
    if previous and program['mode'] == 'manual' and selected_id and index_by_id[selected_id] != program['i']:
        # Pin the existing identity before publishing reordered content. If a
        # filesystem/audit failure interrupts publication, program_now resolves
        # this ID in either old or new content instead of displaying another row.
        program['item_id'] = previous[program['i']]['id']
        s.kv_set('program', program)
    version = _save_content(s, draft, note, actor)
    if program['mode'] == 'manual' and selected_id:
        if active_item_id and (not previous or active_item_id != previous[program['i']]['id']):
            program.update(since=time.time(), scene='auto')
        program['i'] = index_by_id[selected_id]
        s.kv_set('program', program)
    if previous != items:
        from event_push import enqueue
        enqueue(s, 'agenda')
    return {'version': version, **_schedule(s, {})}


def _save_content(s, content, note, actor):
    errors = s.validate_content(content)
    if errors:
        raise ValueError(' '.join(errors[:4]))
    base = s.bundled_content()
    def scoring_shape(value):
        return (
            [(group['points'], [tick['points'] for tick in group['ticks']]) for group in value['rubric']],
            [(step['n'], step['minutes']) for step in value['steps']],
            value['bonus_tick']['points'],
        )

    if scoring_shape(content) != scoring_shape(base):
        raise ValueError('Keep original rubric weights, bonus points, step numbers and step durations.')
    return s.save_content(content, note, actor)


@resource('schedule.items', 'Read the ordered agenda with stable IDs, active item and schedule warnings.', 'content:read')
def _schedule(s, args):
    items = _agenda_items(s.CONTENT)
    return {'items': items, 'active_item_id': items[s.program_now()['i']]['id'] if items else None,
            'clock_timezone': time.strftime('%Z'), 'program_mode': s.program_now().get('mode', 'manual'), 'warnings': event_config.schedule_warnings(s)}


@command('schedule.replace', 'Replace or reorder the agenda by ID. Supply new times for reordered blocks.',
         'content:write', obj({'items': array(AGENDA_ROW, 100, 1), 'active_item_id': AGENDA_ID, 'note': NOTE}, ('items',)))
def _replace_schedule(s, args, actor, principal):
    known = {item['id'] for item in _agenda_items(s.CONTENT)}
    items = []
    for item in args['items']:
        if 'id' in item and item['id'] not in known:
            raise ValueError('Use an existing agenda ID or omit the ID for a new item.')
        items.append(dict(item, id=item.get('id') or uuid.uuid4().hex))
    _validate_agenda(s, items)
    return _save_agenda(s, items, args.get('note', 'Agenda updated'), actor, args.get('active_item_id'))


@command('schedule.update_item', 'Edit an existing agenda item while preserving its ID and position.',
         'content:write', obj({'id': AGENDA_ID, 'changes': {**obj(AGENDA_FIELDS), 'minProperties': 1}, 'note': NOTE},
                              ('id', 'changes')))
def _update_agenda_item(s, args, actor, principal):
    items = _agenda_items(s.CONTENT)
    for item in items:
        if item['id'] == args['id']:
            item.update(args['changes'])
            break
    else:
        _missing('The agenda item was not found.')
    _validate_agenda(s, items)
    return _save_agenda(s, items, args.get('note', 'Agenda item updated'), actor)



TIMING_CHANGE = {
    **obj({'id': AGENDA_ID, 'start_time': AGENDA_FIELDS['time'],
           'duration_minutes': {**integer(0, 1440), 'description': 'New TOTAL duration. Extend by 5 minutes: read current min and add 5 here; keep start_time unchanged.'}, 'shift_minutes': {**integer(-1439, 1439), 'description': 'Move the START by this signed amount without changing duration. Do not use for extending or shortening a section.'}}, ('id',)),
    'anyOf': [{'required': [field]} for field in ('start_time', 'duration_minutes', 'shift_minutes')],
    'not': {'required': ['start_time', 'shift_minutes']},
}
TIMING_FIELDS = {'change': TIMING_CHANGE, 'shift_following': BOOL, 'note': NOTE}
TIMING_PREVIEW = obj(TIMING_FIELDS, ('change', 'shift_following'))
TIMING_APPLY = obj({**TIMING_FIELDS, 'plan_digest': string(64, 64, pattern='^[a-f0-9]{64}$')},
                   ('change', 'shift_following', 'plan_digest'))


@resource('schedule.retime_preview',
          'Preview a section start/duration or minute shift, optionally moving all following sections by its end-time change.',
          'content:read', TIMING_PREVIEW)
def _retime_preview(s, args):
    import event_schedule
    try:
        return event_schedule.preview(s, args)
    except ValueError as error:
        raise CommandError('invalid_timing', str(error)) from None


@command('schedule.retime',
         'Apply an exact agenda timing preview. Change only section times/durations; preserve judging timers and current manual selection.',
         ['event:write', 'content:read'], TIMING_APPLY)
def _retime_schedule(s, args, actor, principal):
    import event_schedule
    return event_schedule.apply(s, args, actor)



# Structural changes share one preview/apply contract across Admin, API and MCP.
EDIT_OPERATION = {'oneOf': [
    obj({'action': {'const': 'insert'}, 'before_id': AGENDA_ID,
         'item': obj({k: v for k, v in AGENDA_FIELDS.items() if k != 'time'}, ('title', 'min'))}, ('action', 'item')),
    obj({'action': {'const': 'update'}, 'id': AGENDA_ID,
         'changes': {**obj(AGENDA_FIELDS), 'minProperties': 1}}, ('action', 'id', 'changes')),
    obj({'action': {'const': 'remove'}, 'id': AGENDA_ID}, ('action', 'id')),
    obj({'action': {'const': 'move'}, 'id': AGENDA_ID, 'before_id': AGENDA_ID}, ('action', 'id')),
]}
# Keep fields discoverable for clients/models that struggle with union-only
# schemas. The original oneOf branches still enforce each action's exact shape.
EDIT_OPERATION.update(obj({
    'action': choices('insert', 'update', 'remove', 'move'),
    'id': AGENDA_ID,
    'before_id': {**AGENDA_ID, 'description': 'Insert/move before this existing item ID. Omit to append. To insert after a section, use its following item ID.'},
    'item': EDIT_OPERATION['oneOf'][0]['properties']['item'],
    'changes': EDIT_OPERATION['oneOf'][1]['properties']['changes'],
}, ('action',)))
EDIT_FIELDS = {'edit': EDIT_OPERATION, 'note': NOTE}
EDIT_PREVIEW = obj(EDIT_FIELDS, ('edit',))
EDIT_APPLY = obj({**EDIT_FIELDS, 'plan_digest': string(64, 64, pattern='^[a-f0-9]{64}$')}, ('edit', 'plan_digest'))


@resource('schedule.edit_preview',
          'Preview inserting a break/section, editing, removing or moving a section. Automatically shift later times; show all changes and finish time. Omit before_id to append.',
          'content:read', EDIT_PREVIEW)
def _edit_schedule_preview(s, args):
    import event_schedule
    return event_schedule.edit_preview(s, args)


@command('schedule.edit',
         'Apply an exact structural agenda preview. Preserve manual current section, actual start, scores and judging timers. Requires the preview plan_digest.',
         'content:write', EDIT_APPLY)
def _edit_schedule(s, args, actor, principal):
    import event_schedule
    return event_schedule.edit_apply(s, args, actor)


def _content_version(s, version):
    try:
        return s.content_version(version)
    except FileNotFoundError:
        _missing('The content version was not found.')


@resource('content.section', 'Read an editable content section at the current or a historical version.',
          'content:read', obj({'section': choices(*SECTIONS), 'version': VERSION}, ('section',)))
def _section(s, args):
    version = args.get('version', s.CONTENT.get('content_version', 1))
    return {'section': args['section'], 'version': version,
            'value': deepcopy(_content_version(s, version)[args['section']])}


@resource('content.history', 'Read the content version history and edit notes.', 'content:read', obj(PAGE))
def _history(s, args):
    items = s.history_list()
    if not any(item['version'] == 1 for item in items):
        items.append({'version': 1, 'saved': None, 'note': 'Bundled baseline'})
    return _page(items, args, 'history')


@command('content.update_section', 'Replace one typed content section while preserving scoring invariants.',
         'content:write', {'oneOf': [obj({'section': choices(section), 'value': schema, 'note': NOTE},
                                         ('section', 'value')) for section, schema in SECTION_SCHEMAS.items()]})
def _edit_section(s, args, actor, principal):
    content = deepcopy(s.CONTENT)
    content[args['section']] = args['value']
    version = _save_content(s, content, args.get('note', 'Updated ' + args['section']), actor)
    return {'version': version, 'section': args['section']}


@command('content.restore', 'Restore historical event content as a new version, including the agenda.',
         'content:write', obj({'version': VERSION, 'active_item_id': AGENDA_ID, 'note': NOTE}, ('version',)))
def _restore_content(s, args, actor, principal):
    version = args['version']
    draft = deepcopy(_content_version(s, version))
    items = _agenda_items(draft)
    # Historical schedules can predate a resize. Validate their timings before
    # activating them and retain the selected item when it still exists.
    _validate_agenda(s, items)
    return _save_agenda(s, items, args.get('note', f'Restored content version {version}'), actor,
                        args.get('active_item_id'), draft)


def _team(row):
    return {'pair': row['pair'], 'partner': row['partner'], 'members': [
        {**member, 'coach': bool(member['coach'])} for member in row['members']
    ]}


@resource('roster.teams', 'Read registered teams and stable member slots.', 'roster:read',
          obj({**PAGE, 'pair': TEAM}))
def _roster(s, args):
    teams = [_team(row) for row in event_config.list_roster(s) if not args.get('pair') or row['pair'] == args['pair']]
    return _page(teams, args, 'teams')


def _save_roster(s, rows, actor, allow_empty=False):
    count = event_config.save_roster(s, rows, allow_empty=allow_empty)
    s.audit(actor, 'roster', f'{count} teams')
    return {'rows': count}


@command('roster.replace', 'Replace all registered teams and reconcile participant access for changed slots.',
         'roster:write', obj({'rows': array(ROSTER_TEAM, 1000, 1)}, ('rows',)))
def _replace_roster(s, args, actor, principal):
    return _save_roster(s, args['rows'], actor)


@command('roster.update_team', 'Replace one team and its member slots, preserving other teams.',
         'roster:write', ROSTER_TEAM)
def _update_team(s, args, actor, principal):
    rows = [_team(row) for row in event_config.list_roster(s) if row['pair'] != args['pair']]
    return _save_roster(s, sorted([*rows, args], key=lambda row: row['pair']), actor)


@command('roster.remove_team', 'Remove a team that has no accepted scoring sheets and revoke its participant access.',
         'roster:write', obj({'pair': TEAM}, ('pair',)))
def _remove_team(s, args, actor, principal):
    roster = event_config.list_roster(s)
    rows = [_team(row) for row in roster if row['pair'] != args['pair']]
    if len(rows) == len(roster):
        _missing('The team was not found.')
    return _save_roster(s, rows, actor, allow_empty=True)


def _attendees(s):
    return s.kv_get('attendees') or {'participants': {}, 'directory': {}}


def _participant_key(pair, member):
    return f'{pair}:{member}'


def reconcile_attendees(s, rows):
    """Drop slot metadata whenever the registered person disappears or changes."""
    directory = _attendees(s)
    if not directory['participants']:
        return
    registered = {_participant_key(row['pair'], member['id']): member['name']
                  for row in rows for member in row['members'] if member['name'].strip()}
    retained = {key: item for key, item in directory['participants'].items()
                if registered.get(key) == item['name']}
    if retained != directory['participants']:
        directory['participants'] = retained
        s.DB.execute("UPDATE kv SET v=? WHERE k='attendees'", (json.dumps(directory),))


def _checked_in(item, flag):
    if item.get('checked_in', False) != flag:
        item.update(checked_in=flag, checked_in_at=time.time() if flag else None)


def _contact_email(value):
    if not value:
        return ''
    key = str(uuid.UUID(int=0))
    _, recipient, _, _ = event_mail.validate({'idempotency_key': key, 'recipient': value,
                                              'subject': 'Contact validation', 'message': 'Contact validation'})
    return recipient


DIRECTORY_ID = string(36, 36, pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
CATEGORIES = ('guest', 'staff', 'organizer')
CONTACT_FIELDS = {'email': string(254), 'checked_in': BOOL}
DIRECTORY_FIELDS = {'category': choices(*CATEGORIES), 'name': string(80, 1), 'company': string(80), **CONTACT_FIELDS}


@resource('attendees.checkins', 'Read participant, guest and event-crew check-in records and contact email.',
          'attendees:read', obj({**PAGE, 'category': choices('participant', *CATEGORIES)}))
def _checkins(s, args):
    data = _attendees(s)
    category = args.get('category')
    items = []
    if category in (None, 'participant'):
        for row in event_config.list_roster(s):
            for member in row['members']:
                if not member['name'].strip():
                    continue
                key = _participant_key(row['pair'], member['id'])
                metadata = data['participants'].get(key, {})
                if metadata.get('name') != member['name']:
                    metadata = {}
                items.append({'category': 'participant', 'pair': row['pair'], 'member': member['id'],
                              'name': member['name'], 'company': row['partner'],
                              'email': metadata.get('email', ''), 'checked_in': metadata.get('checked_in', False),
                              'checked_in_at': metadata.get('checked_in_at')})
    items.extend(item for item in data['directory'].values() if category in (None, item['category']))
    return _page(items, args, 'attendees')


@command('attendees.update_participant', 'Set check-in or contact email for a named participant slot.',
         'attendees:write', {**obj({'pair': TEAM, 'member': MEMBER, **CONTACT_FIELDS}, ('pair', 'member')),
                             'minProperties': 3}, risk='routine')
def _update_participant(s, args, actor, principal):
    pair, member = args['pair'], args['member']
    row = event_config.roster_row(s, pair)
    if not row or member > len(row['members']) or not row['members'][member - 1]['name'].strip():
        _missing('The named participant slot was not found.')
    name = row['members'][member - 1]['name']
    data = _attendees(s)
    key = _participant_key(pair, member)
    item = data['participants'].get(key, {})
    if item.get('name') != name:
        item = {'name': name, 'email': '', 'checked_in': False, 'checked_in_at': None}
    if 'email' in args:
        item['email'] = _contact_email(args['email'])
    if 'checked_in' in args:
        _checked_in(item, args['checked_in'])
    data['participants'][key] = item
    s.kv_set('attendees', data)
    s.audit(actor, 'attendee-update', f'participant {pair}/{member}')
    return {'category': 'participant', 'pair': pair, 'member': member, **item}


@command('attendees.create', 'Register a guest, staff member or organizer in the check-in directory.',
         'attendees:write', obj(DIRECTORY_FIELDS, ('category', 'name')), risk='routine')
def _create_attendee(s, args, actor, principal):
    data = _attendees(s)
    if len(data['directory']) >= 1000:
        _conflict('The attendee directory is full.')
    uid = str(uuid.uuid4())
    item = {'id': uid, 'name': '', 'company': '', 'email': '', 'checked_in': False, 'checked_in_at': None}
    return _save_directory_attendee(s, args, actor, data, item)


def _save_directory_attendee(s, args, actor, data, item):
    item = dict(item)
    for field in ('name', 'company', 'category'):
        if field in args:
            item[field] = args[field].strip()
    if not item['name']:
        raise ValueError('Enter an attendee name.')
    if 'email' in args:
        item['email'] = _contact_email(args['email'])
    if 'checked_in' in args:
        _checked_in(item, args['checked_in'])
    data['directory'][item['id']] = item
    s.kv_set('attendees', data)
    s.audit(actor, 'attendee-update', item['id'])
    return item


@command('attendees.update', 'Update the check-in, category or contact details of a directory attendee.',
         'attendees:write', {**obj({'id': DIRECTORY_ID, **DIRECTORY_FIELDS}, ('id',)), 'minProperties': 2}, risk='routine')
def _update_attendee(s, args, actor, principal):
    data = _attendees(s)
    item = data['directory'].get(args['id'])
    if item is None:
        _missing('The attendee was not found.')
    return _save_directory_attendee(s, args, actor, data, item)


@command('attendees.remove', 'Remove a guest or crew record and its contact information.',
         'attendees:write', obj({'id': DIRECTORY_ID}, ('id',)))
def _remove_attendee(s, args, actor, principal):
    data = _attendees(s)
    if data['directory'].pop(args['id'], None) is None:
        _missing('The attendee was not found.')
    s.kv_set('attendees', data)
    s.audit(actor, 'attendee-remove', args['id'])
    return {'id': args['id'], 'removed': True}


@resource('assignments.list', 'Read the assigned preliminary station, round and original scenario for each team.',
          'event:read', obj({**PAGE, 'pair': TEAM}))
def _assignments(s, args):
    count = event_config.config(s, read_only=True)['team_count']
    pairs = (args['pair'],) if args.get('pair') else range(1, count + 1)
    rows = [event_config.assignment_for(s, pair, read_only=True) for pair in pairs if pair <= count]
    return _page(rows, args, 'assignments')


@command('assignments.swap', 'Swap the preliminary station and round of two teams before scoring. Preserve their original scenarios.',
         'event:write', obj({'pairs': array(TEAM, 2, 2, uniqueItems=True)}, ('pairs',)))
def _swap_assignments(s, args, actor, principal):
    reason = event_config.blocked_reason(s)
    if reason:
        _conflict(reason)
    pairs = args['pairs']
    rows = [event_config.assignment_for(s, pair) for pair in pairs]
    if any(row is None for row in rows):
        raise ValueError('Choose two teams within the configured capacity.')
    with s.DB:
        event_config._invalidate_prepared_final(s)
        for pair, row in zip(pairs, reversed(rows)):
            s.DB.execute('UPDATE team_assignments SET station=?,round_no=? WHERE pair=?',
                         (row['station'], row['round'], pair))
        s.DB.execute("DELETE FROM kv WHERE k='clock' OR k LIKE 'sclock:%'")
    s.LIVE.clear()
    s.audit(actor, 'assignments-swap', f'{pairs[0]} and {pairs[1]}')
    return {'assignments': [event_config.assignment_for(s, pair) for pair in pairs]}


@command('event.mode', 'Select the shared Desk clock or independent station clocks.', 'event:write',
         obj({'mode': choices('self', 'desk')}, ('mode',)))
def _mode(s, args, actor, principal):
    return _domain_result(*s.judging_mode_command(args, actor))


CLOCK_ACTIONS = ('start', 'pause', 'reset', 'feedback', 'finish', 'next', 'goto', 'seek')
ROUND_CLOCK_FIELDS = {'station': STATION, 'expected_round': TEAM}
ROUND_REVIEW_FIELDS = {'override_reason': REASON, 'review_token': string(64, 64, pattern='^[a-f0-9]{64}$')}


@command('event.clock', 'Run or adjust clocks. To advance, collect preliminary receipts or retry with the current review token and a recorded Desk exception.',
         'event:write', actions(**{
             action: (ROUND_CLOCK_FIELDS, ()) for action in CLOCK_ACTIONS if action not in ('next', 'goto', 'seek')
         }, next=({**ROUND_CLOCK_FIELDS, **ROUND_REVIEW_FIELDS}, ()),
             goto=({**ROUND_CLOCK_FIELDS, **ROUND_REVIEW_FIELDS, 'round': TEAM}, ('round',)),
             seek=({**ROUND_CLOCK_FIELDS, 'elapsed': {'type': 'number', 'minimum': 0, 'maximum': 900}}, ('elapsed',))))
def _clock(s, args, actor, principal):
    action, station = args['action'], args.get('station')
    final = final_event.get(s)
    if final_event.engaged(final):
        if station is not None:
            raise ValueError('The Final has a shared clock. Omit the station.')
        if args.get('expected_round') is not None or args.get('review_token') is not None:
            _conflict('The Final is on stage. Refresh the current event before using its clock.')
        if action == 'start' and s.final_clock_public(final)['running']:
            return {'clocks': [s.final_clock_public(final)]}
        return {'clocks': [_domain_result(*s.final_clock_action(final, action, args))]}
    if args.get('override_reason') and 'scoring:write' not in principal.get('scopes', []):
        raise CommandError('scope_forbidden', 'A preliminary scoring exception requires scoring:write.', 403)
    return _domain_result(*s.preliminary_clock_command(args, actor))


@resource('program.state', 'Read current agenda selection, scene, announcement and score visibility. display=off hides only leaderboard scores; announcements and agenda remain visible.', 'event:read')
def _program(s, args):
    value = s.program_now()
    items = _agenda_items(s.CONTENT)
    return {'mode': value['mode'], 'active_item_id': items[value['i']]['id'] if items else None,
            'index': value['i'], 'scene': value.get('scene', 'auto'),
            'announcement': value.get('announce', ''),
            'announcement_updated_at': value.get('announce_updated_at'),
            'room_announcement': value.get('room_announce', value.get('announce', '')),
            'room': s.current_room(final_event.get(s), s.kv_get('reveal', 'hidden'))[0],
            'display': s.public_mode(), 'reveal': s.kv_get('reveal', 'hidden')}


@command('program.select', 'Select the active agenda item or move to an adjacent item in manual mode.',
         'event:write', actions(set=({'id': AGENDA_ID}, ('id',)), next=({}, ()), previous=({}, ())))
def _program_select(s, args, actor, principal):
    pr = s.program_now()
    items = _agenda_items(s.CONTENT)
    action = args['action']
    if action == 'set':
        index = next((i for i, item in enumerate(items) if item['id'] == args['id']), None)
        if index is None:
            _missing('The agenda item was not found.')
    else:
        index = pr['i'] + (1 if action == 'next' else -1)
        if not 0 <= index < len(items):
            _conflict('There is no adjacent agenda item in that direction.')
    s.select_program(index)
    s.audit(actor, 'program-select', items[index]['id'])
    return _program(s, {})


@command('program.mode', 'Switch between a manual program and the host clock schedule.', 'event:write',
         obj({'mode': choices('manual', 'clock')}, ('mode',)))
def _program_mode(s, args, actor, principal):
    s.kv_set('program', dict(s.program_now(), mode=args['mode']))
    s.audit(actor, 'program-mode', args['mode'])
    return _program(s, {})


@command('program.scene', 'Set the audience screen scene or follow the current program automatically.', 'event:write',
         obj({'scene': choices('auto', *SCENES)}, ('scene',)))
def _program_scene(s, args, actor, principal):
    s.program_scene(args['scene'])
    s.audit(actor, 'program-scene', args['scene'])
    return _program(s, {})


@command('program.return_to_agenda', 'Clear room shares and scene overrides and resume the current programme without sending anything to phones.', 'event:write')
def _return_to_agenda(s, args, actor, principal):
    s.program_scene('auto')
    s.audit(actor, 'program-scene', 'auto')
    return _program(s, {})


@command('program.announce', 'Post or clear an announcement. Use channel=room for projector only or channel=phones for participant phones. The legacy default is both. Empty text clears only the selected destination.', 'event:write',
         obj({'text': string(200), 'important': BOOL, 'channel': choices('room','phones','both')}, ('text',)))
def _announce(s, args, actor, principal):
    text = args['text'].strip()
    channel = args.get('channel', 'both')
    s.program_announcement(text, channel, args.get('important', False))
    s.audit(actor, 'program-announce', channel + (':shown' if text else ':cleared'))
    return _program(s, {})


@command('display.mode', 'Show or hide preliminary leaderboard scores. off does not blank the screen: agenda, scenes and announcements remain visible. live exposes scores and needs an explicit score-release request.', 'event:write',
         obj({'mode': choices('live', 'off')}, ('mode',)))
def _display(s, args, actor, principal):
    s.kv_set('public', args['mode'])
    s.audit(actor, 'public', args['mode'])
    return _program(s, {})


@command('display.reveal', 'Select hidden scores, the preliminary top three, or already published winners.',
         'scoring:write', obj({'mode': choices('hidden', 'top3', 'winners')}, ('mode',)))
def _reveal(s, args, actor, principal):
    mode = args['mode']
    if mode == 'winners' and final_event.get(s).get('status') != 'completed':
        _conflict('Publish the Final with final.publish before showing winners.')
    s.kv_set('reveal', mode)
    s.audit(actor, 'reveal', mode)
    return _program(s, {})


@resource('final.state', 'Read the Final run, judge receipts, remaining sheets, tie decisions and publication state.',
          'scoring:read')
def _final_state(s, args):
    final = final_event.get(s)
    state = final_event.state(s, final)
    return {'state': state, 'rankings': final_event.rows(s, final),
            'decision': {key: final['decision'][key] for key in ('order', 'reason', 'at') if key in final['decision']}
            if final.get('decision') else None}


def _run_final(s, action, args, actor):
    _domain_result(*final_event.action(s, {'action': action, **args}, actor))
    s.audit(actor, 'agent-final-' + action)
    return _final_state(s, {})


@command('final.setup', 'Select exactly three eligible finalists and the judging panel before the Final starts.', 'scoring:write',
         obj({'pairs': array(TEAM, 3, 3, uniqueItems=True),
              'panel': array(STATION, 32, 1, uniqueItems=True)}, ('pairs', 'panel')))
def _final_setup(s, args, actor, principal):
    return _run_final(s, 'set', args, actor)


@command('final.start', 'Start the prepared Final after preliminary receipts are accounted for. A Desk exception requires the current review token.', 'scoring:write', obj(ROUND_REVIEW_FIELDS))
def _final_start(s, args, actor, principal):
    return _run_final(s, 'start', args, actor)


@command('final.advance', 'Select a finalist after pausing and receiving the required sheets or recording an exception.',
         'scoring:write', actions(next=({'override_reason': REASON}, ()),
                                  goto=({'index': integer(0, 11), 'override_reason': REASON}, ('index',))))
def _final_advance(s, args, actor, principal):
    return _run_final(s, args['action'], args, actor)


@command('final.review', 'Enter results review after all finalists have scored, with any recorded sheet exceptions.',
         'scoring:write', obj({'override_reason': REASON}))
def _final_review(s, args, actor, principal):
    return _run_final(s, 'review', args, actor)


@command('final.resolve_tie', 'Record panel-approved ordering for a remaining tie after total, Solve and Discover.',
         'scoring:write', obj({'order': array(TEAM, 12, 1, uniqueItems=True), 'reason': REASON}, ('order', 'reason')))
def _final_resolve(s, args, actor, principal):
    return _run_final(s, 'resolve', args, actor)


@command('final.publish', 'Freeze reviewed Final scores and publish the winners to the audience.', 'scoring:write')
def _final_publish(s, args, actor, principal):
    return _run_final(s, 'publish', {'confirm': 'PUBLISH'}, actor)


@command('final.reopen', 'Reopen published Final scores for correction and hide the results.', 'scoring:write',
         obj({'reason': REASON}, ('reason',)))
def _final_reopen(s, args, actor, principal):
    return _run_final(s, 'reopen', args, actor)


QUIZ_PAGE = {'limit': integer(1, 50), 'offset': OFFSET}
QUIZ_QID = string(10, 1, pattern='^[A-Za-z0-9]+$')
QUIZ_RUN_ID = string(32, 3, pattern='^[A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,3}$')
QUIZ_OPTION_KEY = choices(*'ABCDETF')
QUIZ_CHANGES = obj({
    'prompt': string(240, 1),
    'options': array(obj({'key': QUIZ_OPTION_KEY, 'text': string(150, 1)}, ('key', 'text')), 5, 2),
    'correct': {'anyOf': [QUIZ_OPTION_KEY, {'type': 'null'}]},
    'points': integer(0, 10), 'seconds': integer(10, 120),
    'explanation': string(500), 'cue': string(280), 'time': AGENDA_FIELDS['time'],
    'phase': choices(*event_quiz.PHASES[:-1]), 'priority': choices('core', 'optional'),
})
QUIZ_RETIME = {'qid': QUIZ_QID,'time': AGENDA_FIELDS['time'],'phase': choices(*event_quiz.PHASES[:-1]),'shift_following': BOOL}
QUIZ_RETIME_REQUIRED = ('qid','time','shift_following')
QUIZ_DURATION = {'anyOf': [integer(10, 120), {'type':'null'}]}
QUIZ_CONTROL = actions(
    settings=({'enabled': BOOL}, ('enabled',)),
    edit=({'qid': QUIZ_QID, 'changes': QUIZ_CHANGES}, ('qid', 'changes')),
    send=({'qid': QUIZ_QID, 'seconds': QUIZ_DURATION, 'room': BOOL}, ('qid',)),
    resend=({'id': QUIZ_RUN_ID, 'seconds': QUIZ_DURATION, 'room': BOOL}, ('id',)),
    delay=({'qid': QUIZ_QID, 'seconds': integer(15, 300)}, ('qid',)),
    skip=({'qid': QUIZ_QID}, ('qid',)),
    restore=({'id': QUIZ_RUN_ID}, ('id',)),
    invite=({}, ()),
    warmup=({'minutes': integer(1, 20)}, ()),
    post=({'minutes': integer(1, 20)}, ()),
    catchup=({'minutes': integer(1, 10), 'confirm_missing': BOOL}, ()),
    close_warmup=({}, ()),
    close_waiting=({}, ()),
    close_post=({}, ()),
    extend=({'id': QUIZ_RUN_ID, 'seconds': integer(5, 120)}, ('id',)),
    close=({'id': QUIZ_RUN_ID}, ('id',)),
    void=({'id': QUIZ_RUN_ID,'reason': REASON}, ('id',)),
    reset_question=({'id': QUIZ_RUN_ID,'confirm': choices('RESET QUESTION'),'reason': REASON}, ('id','confirm','reason')),
    reset=({'confirm': choices('RESET QUIZ'),'reason': REASON}, ('confirm','reason')),
    restore_reset=({'archive_id': AGENDA_ID,'confirm': choices('RESTORE QUIZ'),'reason': REASON}, ('archive_id','confirm','reason')),
    retime=(QUIZ_RETIME, QUIZ_RETIME_REQUIRED),
    show=({'id': QUIZ_RUN_ID, 'results': BOOL}, ('id',)),
    hide=({}, ()),
    standings=({}, ()),
    spotlight=({}, ()),
    predictions=({}, ()),
    people_choice=({}, ()),
    prize=({'prize': string(180)}, ('prize',)),
    champion=({}, ()),
    show_champion=({}, ()),
)


def _quiz_view(s):
    state = event_quiz.load(s)
    event_quiz.expire(state, event_quiz.now(s))
    return event_quiz.desk_state(s, state)


@resource('quiz.state', 'Read current quiz controls, agenda cues and recent runs. Set include_bank to read all draft questions.',
          'scoring:read', obj({**QUIZ_PAGE, 'include_bank': BOOL}))
def _quiz_state(s, args):
    view = _quiz_view(s)
    used = {run['qid'] for run in view['runs'] if run['status']!='void'}
    queue = [question for question in view['bank'] if question['phase']==view['phase'] and
             question['id'] not in used and not question.get('schedules') and
             question['type']!='reveal' and question['id']!='P00']
    result = {key:view[key] for key in ('enabled','phase','title','now','hall','hall_visible','release','prize',
              'operator','phase_baselines','reset_archives','restorable_reset_id')}
    result.update(_page(list(reversed(view['runs'])), {'limit':12, **args}, 'runs'),
                  open_run_ids=[run['id'] for run in view['runs'] if run['status']=='open'],
                  queue=sorted(queue, key=lambda question:question['time']))
    if args.get('include_bank'):
        result['bank'] = view['bank']
    return result


@resource('quiz.timeline_preview', 'Preview moving queued quiz cues without changing the event programme or sending questions.',
          'scoring:read', obj(QUIZ_RETIME, QUIZ_RETIME_REQUIRED))
def _quiz_timeline_preview(s,args):
    state = event_quiz.load(s)
    try:
        return event_quiz.retime_preview(state,{**args,'rev':state['rev']})
    except event_quiz.Conflict as error:
        _conflict(str(error))
    except ValueError as error:
        raise CommandError('invalid_arguments',str(error)) from None


@resource('quiz.analytics', 'Read aggregate participation, answer distributions, learning and recent Desk activity. Filter by run ID, status or question text.',
          'scoring:read', obj({**QUIZ_PAGE, 'run_id': QUIZ_RUN_ID, 'query': string(240, 1),
                               'status': choices('open', 'closed', 'skipped', 'void')}))
def _quiz_analytics(s, args):
    view = _quiz_view(s)
    analytics = view['analytics']
    runs = list(reversed(view['runs']))
    if 'run_id' in args:
        runs = [run for run in runs if run['id']==args['run_id']]
        if not runs:
            _missing('The quiz run does not exist in the selected workspace.')
    if 'status' in args:
        runs = [run for run in runs if run['status']==args['status']]
    if 'query' in args:
        query = args['query'].strip().casefold()
        runs = [run for run in runs if query in (run['qid']+' '+run['prompt']).casefold()]
    paged = _page(runs, {'limit':12, **args}, 'questions')
    paged['questions'] = [{key:run[key] for key in ('id','qid','prompt','kind','status','points','responses','bars')} |
                          analytics['questions'][run['id']] for run in paged['questions']]
    return {**{key:view[key] for key in ('enabled','phase','learning','activity')},
            **{key:analytics[key] for key in ('summary','teams','companies','confidence','competencies')},
            **paged}


@command('quiz.control', 'Edit quiz cues, send or resend to phones, or explicitly share on the room screen. Individual sends default to 60 seconds; pass seconds=null for manual close or 10–120 for a timer. Set room=true on send or resend for timed presenter results. Spotlight shows standings for 15 seconds. Use show with results=true for a manual reveal. Reset and recovery require confirmation.',
         'scoring:write', QUIZ_CONTROL)
def _quiz_control(s, args, actor, principal):
    state = event_quiz.load(s)
    event_quiz.expire(state, event_quiz.now(s))
    before = {run['id'] for run in state['runs']}
    try:
        archive_id = event_quiz.apply_desk_action(s, state, {**args,'rev':state['rev']}, actor)
    except event_quiz.Conflict as error:
        _conflict(str(error))
    result = {'action':args['action'],'enabled':state['enabled'],'qid':args.get('qid'),'run_id':args.get('id'),
              'new_run_ids':[run['id'] for run in state['runs'] if run['id'] not in before and args['action']!='restore_reset']}
    if archive_id:
        result['archive_id'] = archive_id
    return result


ENGAGEMENT_ID = string(32, 32, pattern='^[0-9a-f]{32}$')
ENGAGEMENT_REV = integer(0, 2**31 - 1)
ENGAGEMENT_DURATION = {'type':['integer','null'], 'minimum':1, 'maximum':3600}
ENGAGEMENT_QUESTION = obj({'id':ENGAGEMENT_ID, 'type':choices('single','multi','true_false','rating','text'),
                           'prompt':string(500,1), 'options':array(string(150,1),8,2),
                           'correct':array(integer(0,7),8,0,uniqueItems=True)}, ('type','prompt'))
ENGAGEMENT_DRAFT = obj({'id':ENGAGEMENT_ID, 'title':string(120,1), 'kind':choices('quiz','poll'),
                        'audiences':array(choices('participant','guest'),2,1,uniqueItems=True),
                        'questions':array(ENGAGEMENT_QUESTION,100,1)}, ('title','kind','questions'))
ENGAGEMENT_CONTROL = actions(
    save=({'revision':ENGAGEMENT_REV,'payload':ENGAGEMENT_DRAFT}, ('revision','payload')),
    publish=({'revision':ENGAGEMENT_REV,'payload':obj({'activity_id':ENGAGEMENT_ID},('activity_id',))}, ('revision','payload')),
    open_run=({'revision':ENGAGEMENT_REV,'payload':obj({'activity_id':ENGAGEMENT_ID,'version':VERSION},('activity_id','version'))}, ('revision','payload')),
    open_question=({'revision':ENGAGEMENT_REV,'payload':obj({'run_id':ENGAGEMENT_ID,'question_id':ENGAGEMENT_ID,'duration':ENGAGEMENT_DURATION},('run_id','question_id'))}, ('revision','payload')),
    close_question=({'revision':ENGAGEMENT_REV,'payload':obj({'run_id':ENGAGEMENT_ID},('run_id',))}, ('revision','payload')),
    reveal_question=({'revision':ENGAGEMENT_REV,'payload':obj({'run_id':ENGAGEMENT_ID},('run_id',))}, ('revision','payload')),
    resend_question=({'revision':ENGAGEMENT_REV,'payload':obj({'run_id':ENGAGEMENT_ID,'duration':ENGAGEMENT_DURATION},('run_id',))}, ('revision','payload')),
    share_hall=({'revision':ENGAGEMENT_REV,'payload':obj({'run_id':ENGAGEMENT_ID,'share':BOOL},('run_id','share'))}, ('revision','payload')),
    configure_questions=({'revision':ENGAGEMENT_REV,'payload':obj({'participants':BOOL,'guests':BOOL},('participants','guests'))}, ('revision','payload')),
    moderate_question=({'revision':ENGAGEMENT_REV,'payload':obj({'id':ENGAGEMENT_ID,'decision':choices('approve','reject','answer'),
                           'answer':string(1000,1)},('id','decision'))}, ('revision','payload')),
)


def _engagement_enabled():
    if os.environ.get('KEVENOS_ENGAGEMENT_V2') != '1':
        raise CommandError('feature_disabled','Engagement Hub is not enabled in this deployment.',409)


@resource('engagement.state', 'Read a page of engagement activities, runs, or moderated audience questions. Individual poll answers are excluded.',
          'engagement:read', obj({'collection':choices('activities','runs','questions'), **PAGE}))
def _engagement_state(s, args):
    _engagement_enabled()
    state = engagement_catalog.desk_view(s.kv_get(engagement_catalog.KEY) or engagement_catalog.empty())
    collection = args.get('collection','activities')
    values = state.get('qa', []) if collection == 'questions' else state[collection]
    return {'revision':state['revision'], 'collection':collection, 'question_settings':state.get('qa_settings', {}),
            **_page(values, {'limit':20, **args}, collection)}


@command('engagement.control', 'Create and publish activities or control a run. Confirm the exact action, workspace, version and audience before execution.',
         'engagement:write', ENGAGEMENT_CONTROL)
def _engagement_control(s, args, actor, principal):
    _engagement_enabled()
    identity = {'role':'organizer', 'credential_id':'agent:' + principal['id'], 'label':principal['label']}
    try:
        engagement_catalog.execute(s, args['action'], args['payload'], expected_revision=args['revision'],
                                   request_id=uuid.UUID(principal['idempotency_key']).hex, identity=identity)
    except engagement_catalog.Conflict as error:
        _conflict(str(error))
    return {'action':args['action'], 'revision':s.kv_get(engagement_catalog.KEY)['revision']}


AUDIENCE_ID = string(32,32,pattern=audience_questions.ID_PATTERN)
AUDIENCE_OPTION = obj({'key':string(32,1,pattern=audience_questions.OPTION_PATTERN),
                       'text':string(120,1)}, ('key','text'))
AUDIENCE_QUESTION = {**obj({
    'id':AUDIENCE_ID,'type':choices('choice','rating','text'),'prompt':string(500,1),
    'options':array(AUDIENCE_OPTION,8,2)}, ('type','prompt')), 'oneOf': [
    obj({'id':AUDIENCE_ID,'type':choices('choice'),'prompt':string(500,1),
         'options':array(AUDIENCE_OPTION,8,2)}, ('type','prompt','options')),
    obj({'id':AUDIENCE_ID,'type':choices('rating','text'),'prompt':string(500,1)}, ('type','prompt')),
]}
AUDIENCE_PACK = obj({'id':AUDIENCE_ID,'title':string(120,1),'description':string(500),
                     'questions':array(AUDIENCE_QUESTION,audience_questions.MAX_QUESTIONS,1)},
                    ('title','questions'))
AUDIENCE_CONTROL = actions(
    save=({'pack':AUDIENCE_PACK}, ('pack',)),
    send=({'packId':AUDIENCE_ID,'minutes':integer(1,audience_questions.MAX_MINUTES)}, ('packId',)),
    resend=({'packId':AUDIENCE_ID,'minutes':integer(1,audience_questions.MAX_MINUTES)}, ('packId',)),
    show=({'packId':AUDIENCE_ID,'questionId':AUDIENCE_ID,'results':BOOL}, ('packId','questionId')),
    hide=({}, ()),
    **{action:({'packId':AUDIENCE_ID}, ('packId',)) for action in ('close','cancel','release')})
# Keep fields directly discoverable for clients that simplify discriminated unions.
# The original oneOf branches still enforce action-specific required/forbidden fields.
AUDIENCE_CONTROL.update(obj({
    'action':choices('save','send','resend','close','cancel','release','show','hide'),
    'pack':AUDIENCE_PACK,'packId':AUDIENCE_ID,'questionId':AUDIENCE_ID,'results':BOOL,
    'minutes':integer(1,audience_questions.MAX_MINUTES)}, ('action',)))
AUDIENCE_READ = obj({'packId':AUDIENCE_ID,'limit':integer(1,30),'offset':integer(0,audience_questions.MAX_PACKS)})


@resource('audience.state', 'Read unscored Audience question packs and response distributions without attendee identities. Pass packId for detail.',
          'event:read', AUDIENCE_READ)
def _audience_state(s,args):
    state = audience_questions.load(s)
    if 'packId' in args:
        try:
            packs = [audience_questions.find_pack(state,args['packId'])]
        except audience_questions.Conflict as error:
            _missing(str(error))
        return audience_questions.desk_view(s,state,packs=packs,include_text=False)
    offset, limit = args.get('offset',0), args.get('limit',10)
    packs = state['packs'][offset:offset+limit]
    return {**audience_questions.desk_view(s,state,packs=packs,include_text=False),
            'total':len(state['packs']),'offset':offset,'limit':limit}


@command('audience.control', 'Save or edit a draft Audience pack, send it to participants, resend it, close it, cancel it or share aggregate results after confirmation. Send and resend go only to phones. Use action=show with packId and questionId to show one question in the room, results=true for released aggregates, or action=hide to remove it. No quiz points are used. First save with action=save and pack={title,questions}; each question has type, prompt, and options=[{key,text}] for choice only. Read the returned pack.id, then prepare action=send with packId. Omit minutes to leave it open until manually closed. There is no send_pack action. Use agent_capabilities(command=audience.control) for the exact schema.',
         'event:write', AUDIENCE_CONTROL)
def _audience_control(s,args,actor,principal):
    state = audience_questions.load(s)
    try:
        pack = audience_questions.control(s,state,{**args,'expectedRev':state['rev']},actor)
    except audience_questions.Conflict as error:
        _conflict(str(error))
    return {'action':args['action'],'pack':pack,'rev':state['rev'],'hall':state.get('hall')}


@resource('scoring.leaderboard', 'Read ranked preliminary scores and scoring review flags.', 'scoring:read', obj(PAGE))
def _leaderboard(s, args):
    rows = _page(s.leaderboard(), args, 'leaderboard')
    return {**rows, 'flags': s.flags()[:100], 'reveal': s.kv_get('reveal', 'hidden')}


@resource('scoring.sheets', 'Read individual preliminary, calibration or Final judging sheets.', 'scoring:read',
          obj({**PAGE, 'kind': choices('round', 'calib', 'final'), 'include_voided': BOOL, 'pair': TEAM, 'id': VERSION}))
def _sheets(s, args):
    filters, values = [], []
    if not args.get('include_voided'):
        filters.append('voided=0')
    for key in ('id', 'kind', 'pair'):
        if key in args:
            filters.append(key + '=?')
            values.append(args[key])
    where = ' WHERE ' + ' AND '.join(filters) if filters else ''
    count = s.DB.execute('SELECT count(*) FROM sheets' + where, values).fetchone()[0]
    start, limit = args.get('offset', 0), args.get('limit', 20)
    rows = [s.sheet_dict(row) for row in s.DB.execute('SELECT * FROM sheets' + where + ' ORDER BY id LIMIT ? OFFSET ?',
                                                     (*values, limit, start))]
    return {'sheets': rows, 'total': count, 'next_offset': start + limit if start + limit < count else None}


@command('scoring.correct_sheet', 'Correct rubric ticks, AI bonus and the applicable 1–5 rating on an accepted scoring sheet.',
         'scoring:write', obj({'id': VERSION, 'ticks': array(BOOL, 10, 10), 'ai': BOOL,
                               'wrap_rating': integer(1,5), 'final_rating': integer(1,5), 'reason': REASON},
                              ('id', 'ticks', 'ai', 'reason')))
def _correct_sheet(s, args, actor, principal):
    return _edit_sheet(s, args, actor, False)


@command('scoring.void_sheet', 'Void an accepted scoring sheet and record the reason.', 'scoring:write',
         obj({'id': VERSION, 'reason': REASON}, ('id', 'reason')))
def _void_sheet(s, args, actor, principal):
    return _edit_sheet(s, args, actor, True)


def _edit_sheet(s, args, actor, void):
    sheet_id = args['id']
    reason = args['reason'].strip()
    if not 8 <= len(reason) <= 500:
        raise ValueError('Record a clear reason of 8–500 characters.')
    sheet = s.DB.execute('SELECT * FROM sheets WHERE id=? AND voided=0', (sheet_id,)).fetchone()
    if sheet is None:
        _missing('The accepted scoring sheet was not found.')
    final = final_event.get(s)
    current_final = sheet['kind'] == 'final' and sheet['final_run'] == final.get('run_id')
    if current_final and final.get('status') == 'completed':
        _conflict('Published Final scores are locked. Reopen results with a reason first.')
    if current_final and sheet_id in final.get('legacy_ballot_ids', []) and not void:
        _conflict('A legacy Final sheet needs a new judge submission using its original scenario.')
    with s.DB:
        if void:
            s.DB.execute('UPDATE sheets SET voided=1,edited_by=? WHERE id=?', (actor, sheet_id))
        else:
            wrap_rating=args.get('wrap_rating',sheet['wrap_rating'])
            final_rating=args.get('final_rating',sheet['final_rating'])
            if sheet['kind']=='round' and not 1<=wrap_rating<=5:
                raise ValueError('Provide the corrected 1–5 overall impression rating.')
            if sheet['kind']=='final' and not 1<=final_rating<=5:
                raise ValueError('Provide the corrected 1–5 Final rating.')
            s.DB.execute('UPDATE sheets SET ticks=?,ai=?,wrap_rating=?,final_rating=?,edited_by=? WHERE id=?',
                         (json.dumps([int(tick) for tick in args['ticks']]), int(args['ai']),wrap_rating,final_rating,actor,sheet_id))
        if current_final:
            final_event.invalidate_decision(s, final)
    s.audit(actor, 'sheet-' + ('void' if void else 'ticks'), f'sheet {sheet_id} {reason}')
    return {'id': sheet_id, 'voided': void}


@resource('calibration.status', 'Read the calibration key and station differences.', 'scoring:read')
def _calibration(s, args):
    key = s.CONTENT['mock']['key']
    ids = s.tick_ids(s.CONTENT)
    ticks = [bool(key[tick]['earned']) for tick in ids]
    rows = []
    for sheet in s.active_sheets('calib'):
        differences = [tick for tick, earned, actual in zip(ids, ticks, sheet['ticks']) if earned != bool(actual)]
        rows.append({'station': sheet['station'], 'judge': sheet['judge'], 'ticks': sheet['ticks'],
                     'diff': differences, 'off': len(differences)})
    return {'active': bool((s.kv_get('calib') or {}).get('active')), 'rows': rows, 'ids': ids, 'key': ticks}


@command('calibration.set', 'Enable or disable the judge calibration exercise before the Final.', 'scoring:write',
         obj({'active': BOOL}, ('active',)))
def _set_calibration(s, args, actor, principal):
    if final_event.engaged(final_event.get(s)):
        _conflict('The Final is in progress or completed; calibration cannot replace it.')
    s.kv_set('calib', args)
    s.audit(actor, 'calib', args['active'])
    return {'active': args['active']}


@resource('notifications.rehearsal_push', 'Read enrolled Rehearsal test devices and aggregate push receipts.',
          'notifications:read', obj({}))
def _rehearsal_push(s, args):
    import event_push
    if event_access.active_dataset(s) != 'rehearsal':
        _conflict('Rehearsal must be active to inspect test devices.')
    return {**event_push.report(s), **event_push.test_controls(s)}


@command('notifications.arm_rehearsal_push', 'Arm one enrolled Rehearsal device to receive the next matching real event alert. Expires in ten minutes.',
         'notifications:send', obj({'device':string(64,64,pattern='^[0-9a-f]{64}$'),
                                    'kind':choices('question','audience','announcement')}, ('device','kind')))
def _arm_rehearsal_push(s, args, actor, principal):
    import event_push
    try:
        result = event_push.arm_test_device(s, args['device'], args['kind'])
    except ValueError as error:
        _conflict(str(error))
    s.audit(actor,'push-test-arm',args['kind'])
    return result


@resource('notifications.deliveries', 'Read delivery availability, quota and delivery receipts without message bodies.',
          'notifications:read', obj(PAGE))
def _deliveries(s, args):
    start, limit = args.get('offset', 0), args.get('limit', 50)
    count = s.DB.execute('SELECT count(*) FROM notifications').fetchone()[0]
    rows = [event_mail.public_row(row) for row in s.DB.execute(
        'SELECT ' + event_mail.PUBLIC_FIELDS + ' FROM notifications ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
        (limit, start))]
    rehearsal = event_access.active_dataset(s) == 'rehearsal'
    return {'sender': event_mail.SENDER, 'enabled': not rehearsal and event_mail.configured(),
            'disabled_reason': event_mail.REHEARSAL_DISABLED if rehearsal else '',
            'remaining': event_mail.remaining(s, time.time()), 'limit_per_day': event_mail.LIMIT_PER_DAY,
            'messages': rows, 'total': count, 'next_offset': start + limit if start + limit < count else None}


def _mail_result(status, body):
    if status >= 400:
        error = {400: 'invalid_arguments', 409: 'state_conflict', 429: 'rate_limited',
                 503: 'delivery_unavailable'}.get(status, 'delivery_failed')
        raise CommandError(error, body['error'], status)
    return body


@command('notifications.send', 'Send one live event notification to an explicitly approved recipient.',
         'notifications:send', obj({'recipient': ADDRESS, 'subject': string(160, 1), 'message': string(12000, 1)},
                                   ('recipient', 'subject', 'message')))
def _send_mail(s, args, actor, principal):
    key, recipient, subject, message = event_mail.validate({**args, 'idempotency_key': principal['idempotency_key']})
    digest = event_mail.fingerprint(s, recipient, subject, message)
    status, result = event_mail.submit_delivery(s, {'credential_id': actor}, key, recipient, subject,
                                                message, digest, bind_actor=True)
    response = _mail_result(status, result)
    s.audit(actor, 'notification-send', key)
    return response


MANAGED_ROLES = ('participant', 'judge', 'desk')
MAIL_SCOPES = ['access:write', 'notifications:send']


@resource('credentials.list', 'Read event access records and revision metadata without passwords or bearer grants.',
          'access:read', obj({**PAGE, 'role': choices(*MANAGED_ROLES, 'admin', 'guest'), 'active': BOOL}))
def _credentials(s, args):
    # last_used is activity telemetry and may change between reads without a
    # meaningful credential revision. Agents only need issuance state.
    fields = 'id,label,role,pair,member,station,revision,active,created,dataset'
    filters, params = ["dataset IN (?, 'global')"], [event_access.active_dataset(s)]
    for key in ('role', 'active'):
        if key in args:
            filters.append(key + '=?')
            params.append(args[key])
    rows = [dict(row) for row in event_access.auth_db(s).execute(
        'SELECT ' + fields + ' FROM access_credentials WHERE ' + ' AND '.join(filters) + ' ORDER BY created DESC,id',
        params)]
    return _page(rows, args, 'credentials')


def _managed_credential(s, credential_id):
    row = event_access.auth_db(s).execute('SELECT ' + event_access.CREDENTIAL_FIELDS +
                                          ' FROM access_credentials WHERE id=?', (credential_id,)).fetchone()
    if not row or row['role'] not in MANAGED_ROLES or row['dataset'] != event_access.credential_dataset(s, row['role']):
        _missing('The managed event access record was not found.')
    account=event_access.auth_db(s).execute('SELECT account_id FROM access_credentials WHERE id=?',(row['id'],)).fetchone()
    actual={g[0] for g in event_access.auth_db(s).execute('SELECT role FROM account_grants WHERE account_id=?',(account['account_id'],))}
    expected={'organizer','scoring'} if row['role']=='desk' else {row['role']}
    if not actual <= expected:_conflict('This person has additional roles. Manage their password in Event Administration.')
    if row['role'] == 'desk' and event_access.active_dataset(s) != 'live':
        _conflict('Organizer access can only be changed in the live workspace.')
    return dict(row)


@command('credentials.revoke', 'Revoke participant, judge or organizer access and invalidate its active sessions.',
         'access:write', obj({'id': CREDENTIAL_ID}, ('id',)))
def _revoke_credential(s, args, actor, principal):
    credential = _managed_credential(s, args['id'])
    db = event_access.auth_db(s)
    with db:
        db.execute('UPDATE access_credentials SET active=0,password_box=NULL WHERE id=?', (credential['id'],))
        db.execute('DELETE FROM role_sessions WHERE credential_id=?', (credential['id'],))
    s.audit(actor, 'credential-revoke', credential['id'])
    return {'id': credential['id'], 'active': False}


def _credential_delivery(s, operation, args, actor, principal, issue):
    # Live auth and notification tables must use the same transaction. The
    # rehearsal guard precedes SMTP configuration checks and password creation.
    if event_access.active_dataset(s) != 'live':
        _conflict(event_mail.REHEARSAL_DISABLED)
    if event_access.auth_db(s) is not s.DB:
        _conflict('Credential delivery requires the live authentication database.')
    key, recipient, _, _ = event_mail.validate({
        'idempotency_key': principal['idempotency_key'], 'recipient': args['recipient'],
        'subject': 'Credential delivery', 'message': 'Credential delivery',
    })
    digest = event_mail._tag(s, 'agent-credential-delivery', [operation, args])
    metadata = {}

    def prepare_issuance():
        item = issue()
        credential = item['credential']
        message = event_mail.credential_message(s, credential, item['password'])
        metadata.update((key, value) for key, value in credential.items() if key != 'last_used')
        return message

    status, result = event_mail.submit_delivery(s, {'credential_id': actor}, key, recipient, '', '',
                                                digest, bind_actor=True, issue=prepare_issuance)
    result = _mail_result(status, result)
    if metadata:
        s.audit(actor, 'credential-' + operation, metadata['id'])
    return {'credential': metadata or None, 'delivery': result['message']}


@command('credentials.issue', 'Generate a participant, judge or organizer password and email it to one approved recipient.',
         MAIL_SCOPES, {'oneOf': [
             obj({'role': choices('participant'), 'pair': TEAM, 'member': MEMBER, 'recipient': ADDRESS},
                 ('role', 'pair', 'member', 'recipient')),
             obj({'role': choices('judge'), 'station': STATION, 'label': string(80, 1), 'recipient': ADDRESS},
                 ('role', 'station', 'label', 'recipient')),
             obj({'role': choices('desk'), 'label': string(80, 1), 'recipient': ADDRESS},
                 ('role', 'label', 'recipient')),
         ]})
def _issue_credential(s, args, actor, principal):
    def issue():
        return event_access.create_credential(s, args['role'], args.get('label', 'Participant'),
                                              args.get('pair', 0), args.get('member', 0),
                                              station=args.get('station', 0))
    return _credential_delivery(s, 'issue', args, actor, principal, issue)


@command('credentials.reset', 'Replace an event password, invalidate sessions and email the replacement to one approved recipient.',
         MAIL_SCOPES, obj({'id': CREDENTIAL_ID, 'recipient': ADDRESS}, ('id', 'recipient')))
def _reset_credential(s, args, actor, principal):
    def issue():
        credential = _managed_credential(s, args['id'])
        if credential['role'] == 'participant':
            assignment = event_access.participant_assignment(s, credential)
            if not assignment or assignment['name'] != credential['label']:
                raise ValueError('The participant changed. Issue a password from the current roster.')
        db = event_access.auth_db(s)
        db.execute('DELETE FROM role_sessions WHERE credential_id=?', (credential['id'],))
        db.execute('DELETE FROM access_credentials WHERE id=?', (credential['id'],))
        result = event_access.create_credential(
            s, credential['role'], credential['label'], credential['pair'], credential['member'],
            cid=credential['id'], station=credential['station'], revision=credential['revision'] + 1)
        db.execute('UPDATE access_credentials SET created=? WHERE id=?', (credential['created'], credential['id']))
        result['credential']['created'] = credential['created']
        return result
    return _credential_delivery(s, 'reset', args, actor, principal, issue)


@resource('operations.readiness', 'Read verified event readiness and aggregate help counts without private messages.', 'event:read')
def _operations_readiness(s,args):
    import event_operations
    return event_operations.ai_projection(s,'organizer')

@resource('operations.desk', 'Read participant help requests and staff handovers in the current workspace.', ['event:read','attendees:read'])
def _operations_desk(s,args):
    import event_operations
    return event_operations.snapshot(s,{'role':'organizer'})

@command('operations.control', 'Claim or resolve help, save a handover or confirm a venue check. Read operations.desk for the exact current revision.', ['event:write','attendees:read'],
    {'oneOf':[
      obj({'action':{'const':'help_update'},'revision':{'type':'integer','minimum':0},'id':{'type':'string','minLength':1,'maxLength':64},'status':{'enum':['open','claimed','resolved']},'reply':{'type':'string','maxLength':500}},['action','revision','id','status']),
      obj({'action':{'const':'handover'},'revision':{'type':'integer','minimum':0},'note':{'type':'string','minLength':1,'maxLength':800}},['action','revision','note']),
      obj({'action':{'const':'check'},'revision':{'type':'integer','minimum':0},'key':{'enum':['screen','wifi','phone']},'done':{'type':'boolean'}},['action','revision','key','done'])]})
def _operations_control(s,args,actor,principal):
    import event_operations
    try:return event_operations.command(s,{'role':'organizer','label':principal.get('label',actor)},args)
    except event_operations.Conflict as error:raise CommandError('state_conflict',str(error),409) from None
    except PermissionError as error:raise CommandError('forbidden',str(error),403) from None
