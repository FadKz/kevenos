"""Workspace-scoped audience questions and voluntary, unscored responses."""
from copy import deepcopy
import json
import re
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import event_quiz

DESK = '/api/desk/audience'
PLAYER = '/api/participant/audience'
WRITTEN = DESK+'/responses'
KEY = 'audience_questions_v1'
ANSWER_PREFIX = 'audience_answers_v1:'
SUMMARY_PREFIX = 'audience_summary_v1:'
EXCERPTS_PREFIX = 'audience_excerpts_v1:'
MAX_PACKS = 100
MAX_QUESTIONS = 10
MAX_MINUTES = 120
OPTION_PATTERN = r'^[A-Za-z0-9_-]{1,32}$'
ID_PATTERN = r'^[0-9a-f]{32}$'
TEXT_LIMIT = 1000
DESK_EXCERPT_LIMIT = 100
QUESTION_EXCERPT_LIMIT = 50
PARTICIPANT_HISTORY_LIMIT = 20


class Conflict(Exception):
    pass


def load(s):
    return s.kv_get(KEY, {'rev':0, 'packs':[]})


def now(s):
    return int(s.time.time() * 1000)


def status(pack, at):
    if pack['status'] == 'live' and pack.get('closesAt') is not None and at >= pack['closesAt']:
        return 'closed'
    return pack['status']


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or value.keys() - set(allowed) or set(required) - value.keys():
        raise ValueError('Use the supported Audience fields.')


def string(value, maximum, name, required=True):
    if not isinstance(value, str) or len(value) > maximum or required and not value.strip():
        raise ValueError(f'Enter {name} using at most {maximum} characters.')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError(f'Use valid text for {name}.') from None
    return value.strip()


def identifier(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('Choose a valid Audience identifier.')
    return value


def find_pack(state, key):
    identifier(key, ID_PATTERN)
    pack = next((p for p in state['packs'] if p['id'] == key), None)
    if pack is None:
        raise Conflict('This question pack is no longer available.')
    return pack


def build_pack(raw, prior):
    fields(raw, ('id','title','description','questions'), ('title','questions'))
    title = string(raw['title'], 120, 'a pack title')
    description = string(raw.get('description',''), 500, 'a description', required=False)
    questions = raw['questions']
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f'Include between 1 and {MAX_QUESTIONS} questions.')
    previous = {q['id'] for q in prior['questions']} if prior else set()
    seen = set()
    clean = []
    for item in questions:
        fields(item, ('id','type','prompt','options'), ('type','prompt'))
        kind = item['type']
        if kind not in ('choice','rating','text'):
            raise ValueError('Choose multiple choice, rating or free text.')
        qid = identifier(item['id'], ID_PATTERN) if 'id' in item else uuid4().hex
        if qid in seen or ('id' in item and qid not in previous):
            raise ValueError('Keep existing question identifiers and omit them for new questions.')
        seen.add(qid)
        question = {'id':qid, 'type':kind, 'prompt':string(item['prompt'],500,'a question prompt')}
        if kind == 'choice':
            options = item.get('options')
            if not isinstance(options,list) or not 2 <= len(options) <= 8:
                raise ValueError('Include between 2 and 8 options for each multiple choice question.')
            question['options'] = []
            used = set()
            for option in options:
                fields(option, ('key','text'), ('key','text'))
                key = identifier(option['key'], OPTION_PATTERN)
                if key in used:
                    raise ValueError('Use a different key for each option.')
                used.add(key)
                question['options'].append({'key':key,'text':string(option['text'],120,'an option label')})
        elif 'options' in item:
            raise ValueError('Only multiple choice questions use options.')
        clean.append(question)
    return {'id':prior['id'] if prior else uuid4().hex, 'title':title,
            'description':description,'questions':clean,'status':'draft'}


def control(s, state, body, actor):
    action = body.get('action')
    supported = {'save':('pack',), 'send':('packId','minutes'), 'resend':('packId','minutes'), 'close':('packId',),
                 'cancel':('packId',), 'release':('packId',), 'show':('packId','questionId','results'), 'hide':()}
    if not isinstance(action, str) or action not in supported:
        raise ValueError('Choose a pack action.')
    required = ('packId','questionId') if action == 'show' else supported[action][:1]
    fields(body, ('action','expectedRev',*supported[action]), ('action',*required))
    if type(body.get('expectedRev')) is not int or body['expectedRev'] != state['rev']:
        raise Conflict('Audience questions changed. Review the latest state and try again.')
    at = now(s)
    if action == 'hide':
        pack = None
        state['hall'] = None
    elif action == 'save':
        raw = body['pack']
        prior = find_pack(state,raw['id']) if isinstance(raw,dict) and 'id' in raw else None
        if prior and status(prior,at) != 'draft':
            raise Conflict('A pack cannot be edited after it has been sent.')
        if not prior and len(state['packs']) >= MAX_PACKS:
            raise Conflict('This workspace reached the Audience pack limit.')
        pack = build_pack(raw, prior)
        if prior:
            state['packs'][state['packs'].index(prior)] = pack
        else:
            state['packs'].append(pack)
    else:
        pack = find_pack(state,body['packId'])
        current = status(pack,at)
        if action in ('send', 'resend'):
            minutes = body.get('minutes')
            if 'minutes' in body and (type(minutes) is not int or not 1 <= minutes <= MAX_MINUTES):
                raise ValueError(f'Choose a duration from 1 to {MAX_MINUTES} minutes.')
            if action == 'send' and current != 'draft':
                raise Conflict('Only draft packs can be sent.')
            if action == 'resend' and current not in ('live', 'closed'):
                raise Conflict('Only open or closed packs can be resent.')
            if any(p['id'] != pack['id'] and status(p,at) == 'live' for p in state['packs']):
                raise Conflict('Close the current Audience pack before sending another.')
            if action == 'resend' and 'releasedAt' in pack:
                if len(state['packs']) >= MAX_PACKS:
                    raise Conflict('This workspace reached the Audience pack limit.')
                pack = deepcopy(pack)
                pack.pop('releasedAt')
                pack['id'] = uuid4().hex
                state['packs'].append(pack)
            pack.update(status='live', openedAt=at, closesAt=at+minutes*60000 if minutes else None,
                        manual=minutes is None)
        elif action == 'show':
            question_id = identifier(body['questionId'], ID_PATTERN)
            if not any(q['id'] == question_id for q in pack['questions']):
                raise Conflict('Choose a question in this pack.')
            results = body.get('results', False)
            if type(results) is not bool:
                raise ValueError('Choose whether to show the released results.')
            if results and 'releasedAt' not in pack:
                raise Conflict('Release the results before showing them on the room screen.')
            if current not in ('live', 'closed'):
                raise Conflict('Send the pack to phones before showing a question on the room screen.')
            reason = room_blocked_reason(s)
            if reason:
                raise Conflict(reason)
            state['hall'] = {'packId': pack['id'], 'questionId': question_id, 'results': results,
                             'agenda': s.room_agenda_key()}
            s.clear_quiz_room()
        elif action == 'close':
            if current != 'live':
                raise Conflict('This Audience pack is no longer open.')
            pack.update(status='closed',closesAt=at)
        elif action == 'release':
            if current != 'closed' or 'releasedAt' in pack:
                raise Conflict('Close a pack before releasing its results. A release cannot be repeated.')
            pack.update(status='closed',releasedAt=at)
        elif action == 'cancel':
            if current not in ('draft','live','closed') or 'releasedAt' in pack:
                raise Conflict('A cancelled or released pack cannot be cancelled.')
            if current == 'live':
                pack['closesAt'] = at
            pack['status'] = 'cancelled'
            if (state.get('hall') or {}).get('packId') == pack['id']:
                state['hall'] = None
    state['rev'] += 1
    if action in ('send', 'resend', 'release'):
        from event_push import enqueue
        if action in ('send', 'resend'):
            enqueue(s, 'audience', ttl=min(300, minutes * 60) if minutes else 300, stored=(KEY, state))
        else:
            enqueue(s, 'results', stored=(KEY, state))
    else:
        s.kv_set(KEY,state)
    s.audit(actor,'audience-'+action,pack['id'] if pack else '')
    return pack


def answer_key(pack_id, account_key):
    return f'{ANSWER_PREFIX}{pack_id}:{account_key}'


def excerpt_key(pack_id, qid):
    return f'{EXCERPTS_PREFIX}{pack_id}:{qid}'


def result_for(s,pack,excerpt_budget):
    summary = s.kv_get(SUMMARY_PREFIX+pack['id'],{})
    result = {}
    for q in pack['questions']:
        qid = q['id']
        saved = summary.get(qid,{})
        row = result[qid] = {'count':saved.get('count',0)}
        if q['type'] == 'text':
            if excerpt_budget is not None:
                excerpts = s.kv_get(excerpt_key(pack['id'],qid),[]) if excerpt_budget[0] else []
                row['texts'] = sorted(excerpts[:excerpt_budget[0]])
                excerpt_budget[0] -= len(row['texts'])
        else:
            keys = [o['key'] for o in q['options']] if q['type'] == 'choice' else range(1,6)
            row['counts'] = {str(key):saved.get('counts',{}).get(str(key),0) for key in keys}
    return result


def room_blocked_reason(s):
    final = s.final_event.get(s)
    if final.get('status') in {'ready','pitch','collecting','review'}:
        return 'The room follows the Final until results are published.'
    return event_quiz.hall_blocked_reason(s, event_quiz.phase(s), event_quiz.quiet(s), final)


def hall(s):
    state = load(s)
    shown = state.get('hall')
    if not shown or shown['agenda'] != s.room_agenda_key() or room_blocked_reason(s):
        return None
    pack = next((p for p in state['packs'] if p['id'] == shown['packId']), None)
    if not pack or status(pack, now(s)) not in ('live', 'closed'):
        return None
    question = next((q for q in pack['questions'] if q['id'] == shown['questionId']), None)
    if not question:
        return None
    view = {**question, 'kind':'audience', 'status':status(pack, now(s)),
            'end':pack['closesAt']/1000 if pack.get('closesAt') is not None else None}
    if question['type'] == 'rating':
        view['options'] = [{'key':str(n), 'text':str(n)} for n in range(1, 6)]
    if shown['results'] and 'releasedAt' in pack:
        view.update(result_for(s, pack, None)[question['id']])
    return view


def project(s, pack, at, *, participant_key=None, excerpt_budget=None, include_results=True):
    current = status(pack,at)
    public = participant_key is not None
    view = deepcopy(pack)
    view['status'] = current
    if public:
        own = s.kv_get(answer_key(pack['id'],participant_key),{}) if participant_key else {}
        if current != 'live' and 'releasedAt' not in pack and not own:
            return None
        view['answers'] = own
    if include_results and current in ('live','closed') and (not public or 'releasedAt' in pack):
        # Desk polling shows totals while open. Read bounded excerpts after close,
        # and never project written feedback to participants or the machine API.
        view['results'] = result_for(s,pack,excerpt_budget if current == 'closed' else None)
    return view


def desk_view(s,state,*, packs=None, include_text=True, include_results=True):
    at = now(s)
    budget = [DESK_EXCERPT_LIMIT] if include_text else None
    packs = state['packs'] if packs is None else packs
    # Spend excerpt allowance on the latest closed packs, preserve list order.
    newest_first = sorted(packs,key=lambda p:p.get('closesAt') or p.get('openedAt',0), reverse=True)
    views = {p['id']:project(s,p,at,excerpt_budget=budget,include_results=include_results)
             for p in newest_first}
    return {'rev':state['rev'],'now':at,'hall':state.get('hall'),'hall_blocked_reason':room_blocked_reason(s),'packs':[views[p['id']] for p in packs]}


def quiet_for(s, assignment, *, manual=False):
    if not manual and event_quiz.quiet(s):
        return True
    final = s.final_event.get(s)
    if assignment['pair'] in final.get('pairs',[]) and final.get('status') in {'ready','pitch','collecting','review'}:
        return True
    sheets = {row['pair'] for row in s.active_sheets('round')}
    return event_quiz.prelim_busy(s,assignment,sheets)


def participant_view(s,state,key,assignment):
    at = now(s)
    live = [p for p in state['packs'] if status(p,at) == 'live']
    live = [p for p in live if not quiet_for(s,assignment,manual=p.get('manual',False))]
    closed = sorted((p for p in state['packs'] if status(p,at) == 'closed'),
                    key=lambda p:p['closesAt'], reverse=True)[:PARTICIPANT_HISTORY_LIMIT]
    return {'now':at,'packs':[v for pack in sorted(closed,key=lambda p:p['closesAt'])+live
        if (v := project(s,pack,at,participant_key=key)) is not None]}


def respond(s,state,body,key,assignment):
    fields(body, ('packId','questionId','value'), ('packId','questionId','value'))
    pack = find_pack(state,body['packId'])
    if pack['status'] in ('draft','cancelled'):
        raise Conflict('This Audience pack is unavailable.')
    qid = identifier(body['questionId'],ID_PATTERN)
    question = next((q for q in pack['questions'] if q['id']==qid),None)
    if question is None:
        raise Conflict('This Audience question is unavailable.')
    value = body['value']
    if value is not None:
        if question['type'] == 'text':
            value = string(value,TEXT_LIMIT,'a response')
        elif question['type'] == 'rating':
            if type(value) is not int or not 1 <= value <= 5:
                raise ValueError('Choose a rating from 1 to 5.')
        elif value not in [o['key'] for o in question['options']]:
            raise ValueError('Choose one of the answer options.')
    own_key = answer_key(pack['id'],key)
    own = s.kv_get(own_key,{})
    if qid in own:
        if own[qid] != value:
            raise Conflict('Your response was already accepted.')
        return
    if status(pack,now(s)) != 'live' or quiet_for(s,assignment,manual=pack.get('manual',False)):
        raise Conflict('This Audience question is closed or unavailable right now.')
    own[qid] = value
    writes = {own_key:own}
    if value is not None:
        summary_key = SUMMARY_PREFIX+pack['id']
        summary = s.kv_get(summary_key,{})
        row = summary.setdefault(qid,{'count':0})
        row['count'] += 1
        if question['type'] == 'text':
            text_key = excerpt_key(pack['id'],qid)
            excerpts = s.kv_get(text_key,[])
            if len(excerpts) < QUESTION_EXCERPT_LIMIT:
                writes[text_key] = excerpts+[value]
        else:
            counts = row.setdefault('counts',{})
            counts[str(value)] = counts.get(str(value),0)+1
        writes[summary_key] = summary
    # A lost reply can safely be retried: record the saved answer and totals in
    # one transaction, so they can never be taken from different writes.
    with s.DB:
        s.DB.executemany('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                        [(k,json.dumps(v)) for k,v in writes.items()])


def written_page(s,state,query):
    params = parse_qs(query,keep_blank_values=True,max_num_fields=4)
    fields(params,('packId','questionId','offset','limit'),('packId','questionId'))
    if any(len(values) != 1 for values in params.values()):
        raise ValueError('Use one value for each written response filter.')
    pack = find_pack(state,params['packId'][0])
    if status(pack,now(s)) != 'closed':
        raise Conflict('Written responses are available only after an Audience pack closes.')
    qid = identifier(params['questionId'][0],ID_PATTERN)
    if not any(q['id'] == qid and q['type'] == 'text' for q in pack['questions']):
        raise ValueError('Choose a written question from this pack.')

    def number(name, default, lower, upper):
        raw = params.get(name,[str(default)])[0]
        if not re.fullmatch(r'[0-9]{1,5}',raw) or not lower <= int(raw) <= upper:
            raise ValueError('Choose a valid written response page.')
        return int(raw)

    offset, limit = number('offset',0,0,10000), number('limit',50,1,50)
    base = ANSWER_PREFIX+pack['id']
    summary = s.kv_get(SUMMARY_PREFIX+pack['id'],{})
    values = []
    for row in s.DB.execute("SELECT v FROM kv WHERE k >= ? AND k < ?", (base+':',base+';')):
        try:
            value = json.loads(row[0]).get(qid)
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(value, str):
            values.append(value)
    values.sort()
    return {'texts':values[offset:offset+limit],'total':summary.get(qid,{}).get('count',0),
            'offset':offset,'limit':limit}


def handle(s,h,path,body=None):
    if path not in (DESK,PLAYER,WRITTEN):
        return False
    with s.LOCK:
        ident = s.event_access.session(s,h.headers)
        if not ident:
            h.send(401,{'error':'Sign in to continue.'})
            return True
        role = ident.get('dashboard',ident['role'])
        if role not in ({'participant'} if path == PLAYER else {'desk','organizer','admin'}):
            h.send(403,{'error':'Audience questions are not available to this dashboard.'})
            return True
        profile = ident.get('impersonation') or {}
        if body is not None and profile.get('mode') == 'preview':
            h.send(403,{'error':'This profile is a read-only preview.'})
            return True
        state = load(s)
        try:
            if path == WRITTEN:
                if body is not None:
                    h.send(405,{'error':'Read this page with GET.'})
                    return True
                data = written_page(s,state,urlparse(h.path).query)
            elif path == PLAYER:
                assignment = s.event_access.participant_assignment(s,ident)
                if not assignment or not assignment['name'].strip():
                    raise Conflict('Your participant assignment is unavailable.')
                key = ident.get('account_id')
                if body is not None:
                    if not key:
                        raise Conflict('Your participant sign-in is unavailable.')
                    respond(s,state,body,key,assignment)
                data = participant_view(s,state,key or '',assignment)
            else:
                if body is not None:
                    control(s,state,body,role)
                data = desk_view(s,state)
            h.send(200,data)
        except ValueError as error:
            h.send(400,{'error':str(error)})
        except Conflict as error:
            h.send(409,{'error':str(error)})
    return True
