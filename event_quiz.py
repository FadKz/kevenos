"""Workspace-scoped audience quiz. The Desk alone opens prompts and reveals the hall."""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from functools import lru_cache
import csv
import io
import json
import re
from pathlib import Path
from uuid import uuid4

DESK = '/api/desk/quiz'
PLAYER = '/api/participant/quiz'
KEY = 'event_quiz_v1'
RESET_ARCHIVE_KEY = 'event_quiz_reset_archive_v1'
ROUND_IDS = {'R1a','R1b','R2a','R2b','R3a','R3b','R4a','R4b'}
FINAL_IDS = {'F1','F2'}
PHASES = ('registration','welcome','mac','steps','demo','prep','rounds','break','final','awards','lunch')
PHASE_BASELINES = dict(zip(PHASES, ('09:30','10:00','10:10','10:25','10:40','10:50','11:40','12:40','12:50','13:25','14:00')))
BANK_PHASES = dict(zip(('Registration and coffee','Welcome','Solution at Work','The pitch in four steps','Live pitch',
    'Briefing and preparation','Pitch rounds','Break while scores are tallied','The final',
    'Awards, next three customers, close'), PHASES))
KEYS = ('prompt','options','correct','points','seconds','explanation','cue','time','phase','priority')


class Conflict(ValueError):
    pass


@lru_cache
def bank():
    data = json.loads(Path(__file__).with_name('quiz-bank.json').read_text(encoding='utf-8'))
    items = {q['id']: q | {'phase':BANK_PHASES[q['segment']]} for q in data['questions']}
    items['F1'].update(points=0, explanation='Audience prediction only. The judging panel decides the Final.')
    items['PR2']['time'] = '11:20'
    items['M4']['prompt'] = 'When an AI task runs entirely on the solution device, what is one benefit?'
    items['M4']['options'][0]['text'] = 'The task can be processed without sending its data off the device.'
    items['M4']['explanation'] = 'An entirely on-device task keeps its data on the solution device.'
    items['PQ1']['prompt'] = 'What is the current product vendor UAE starting price for laptop Neo?'
    items['R4a']['options'][0]['text'] = 'Compare total cost, the current product vendor UAE starting price of currency units 2,999, and trade-in or buy-back.'
    return items


def empty_quiz():
    return {'enabled':False,'rev':0,'edits':{},'runs':[], 'members':{},'answers':{},
            'delays':{},'hall':None,'release':None,'prize':''}


def load(s):
    return s.kv_get(KEY) or empty_quiz()


def current_bank(state):
    return [q | {'seconds':15} | state['edits'].get(qid,{}) for qid,q in bank().items()]


def score(runs, answers):
    earned = available = pending = elapsed = 0
    for run in runs:
        if run['status'] not in {'open','closed'} or not run['points']:
            continue
        if not run['keys']:
            pending += run['points']
            continue
        available += run['points']
        entry = answers.get(run['id'])
        if run['status']=='closed' and run.get('released',True) and entry and entry['key'] in run['keys']:
            earned += run['points']
            elapsed += entry['elapsed']
    return {'earned':earned,'available':available,'pending':pending,'time':elapsed}


def result_ready(run, post_closed):
    return bool(run['status'] == 'closed' and run['released'] and (run['kind'] != 'warmup' or post_closed))


def phase(s):
    agenda = s.agenda()
    if not agenda:
        return 'unmapped'
    item = agenda[s.program_now()['i']]
    scene = item.get('scene')
    by_scene = {'prep':'prep','pitch':'rounds','scores':'break','final':'final','awards':'awards'}
    if scene in by_scene:
        return by_scene[scene]
    title = item.get('title','').casefold()
    return next((phase for word,phase in (
        ('registration','registration'),('welcome','welcome'),('mac at work','mac'),
        ('four steps','steps'),('live pitch','demo'),('lunch','lunch')) if word in title),'unmapped')


def now(s):
    return s.time.time()


def expire(state, timestamp):
    for run in state['runs']:
        if run['status']=='open' and run['end'] is not None and timestamp >= run['end']:
            run['status']='closed'
            if run['kind'] not in {'waiting','warmup','post'} and not run['prediction'] and run['qid']!='F2':
                run['released']=True
    share = state.get('hall') or {}
    if share.get('kind') == 'spotlight' and timestamp >= share['display_until']:
        state['hall'] = share.get('resume')
        share = state['hall'] or {}
    if share.get('auto_results'):
        run = next((r for r in state['runs'] if r['id'] == share['id']), None)
        if not run or run['status'] in {'void','skipped'} or (
                run['status'] == 'closed' and timestamp >= run['end'] + ROOM_SECONDS):
            state['hall'] = None


def disable(state, timestamp):
    state['enabled'] = False
    for run in state['runs']:
        if run['status'] == 'open':
            if run['end'] is None:
                run['reveal_at'] = timestamp
            run['end'] = timestamp
    expire(state,timestamp)
    state.update(hall=None,invite=False)


def reset_archives(s):
    return s.kv_get(RESET_ARCHIVE_KEY, [])


def available_reset(state, archives):
    if not archives or archives[0]['reset_rev'] != state['rev']:
        return None
    original = archives[0]['state']
    expected = empty_quiz() | {key:original[key] for key in ('edits','members','prize')} | {'rev':archives[0]['reset_rev']}
    if state != expected:
        return None
    return archives[0]['id']


def change_reason(body, *, required=True):
    if not required and 'reason' not in body:
        return ''
    value = body.get('reason')
    if not isinstance(value,str) or not 8 <= len(value.strip()) <= 500:
        raise ValueError('Enter a reason between 8 and 500 characters.')
    return value.strip()


def confirm_action(body, phrase):
    if body.get('confirm') != phrase:
        raise ValueError(f'Confirm this action by typing {phrase}.')
    return change_reason(body)


def check_revision(state, body):
    if type(body.get('rev')) is not int or body['rev'] != state['rev']:
        raise Conflict('The quiz changed. Review the latest state and try again.')


def reset_change(s, state, body):
    check_revision(state,body)
    archives = reset_archives(s)
    if body['action'] == 'reset':
        reason = confirm_action(body,'RESET QUIZ')
        archived = {'id':uuid4().hex, 'at':now(s), 'reason':reason, 'reset_rev':state['rev']+1, 'state':deepcopy(state)}
        new_state = empty_quiz() | {key:state[key] for key in ('rev','edits','members','prize')}
        return new_state, [archived,*archives[:2]], archived['id'], reason
    reason = confirm_action(body,'RESTORE QUIZ')
    archive_id = available_reset(state,archives)
    if not archive_id or body.get('archive_id') != archive_id:
        raise Conflict('Only the latest reset can be restored, before anyone changes the quiz again.')
    restored = deepcopy(archives[0]['state'])
    restored['rev'] = state['rev']
    disable(restored, now(s))
    restored.pop('catchup_end',None)
    return restored, archives, archive_id, reason


def quiet(s):
    current = phase(s)
    program = s.program_now()
    elapsed = now(s) - program['since']
    return (current == 'prep' and elapsed >= 35*60) or (current == 'awards' and 10*60 <= elapsed < 30*60)


def prelim_busy(s, assignment, sheets):
    if not assignment:
        return False
    clock = s.clock_public(assignment['station'])
    return bool(clock['started_any'] and clock['round'] == assignment['round'] and
                assignment['pair'] not in sheets and not (s.kv_get('calib') or {}).get('active'))


def eligible(run, pair):
    return pair not in run.get('excluded',[])


def is_catchup_run(run, legacy_end):
    if run['kind'] != 'waiting':
        return False
    if 'catchup' in run:
        return run['catchup']
    # Older saved runs have no marker. Only a matching, recorded deadline
    # identifies those runs as part of the approved catch-up.
    return bool(legacy_end and run['end'] == legacy_end)


def public_run(run, own, results):
    data = {key:run[key] for key in ('id','qid','prompt','options','points','kind','status','end','seconds')}
    data['options'] = [{'key':o['key'],'text':o['text']} for o in run['options']]
    data['selected'] = (own or {}).get('key')
    if results:
        data.update(correct=run['keys'], explanation=run['explanation'])
    return data


def participant_result(run, own, post_closed):
    """A personal receipt, with answer keys only through the existing release gate."""
    ready = result_ready(run, post_closed)
    if run['status'] == 'void':
        state = 'cancelled'
    elif ready and (run['keys'] or not run['points']):
        state = 'ready'
    elif run['kind'] == 'warmup' and not post_closed:
        state = 'awaiting_post_test'
    elif run.get('prediction') and not run['keys']:
        state = 'awaiting_outcome'
    elif run['kind'] in {'waiting','post'} or run['status'] == 'closed':
        state = 'awaiting_release'
    else:
        state = 'awaiting_close'
    data = public_run(run, own, ready and state == 'ready')
    data['result_state'] = state
    data['earned'] = (run['points'] if own and own['key'] in run['keys'] else 0) if state == 'ready' else None
    return data


def current_prompt(runs, own, current):
    """Expose a single unanswered prompt from runs explicitly opened by the Desk."""
    opened = [run for run in runs if run['status'] == 'open' and
              (run.get('manual_delivery') or run['phase'] == current or (run['kind'] == 'waiting' and current == 'break'))]
    unanswered = [run for run in opened if run['id'] not in own]
    if not unanswered:
        return []
    # A live cue can interrupt a self-paced set. The set resumes at the
    # participant's saved position after the live response.
    live = next((run for run in unanswered if run['kind'] in {'live','pulse','vote'}), None)
    run = live or unanswered[0]
    set_kind = run['kind'] in {'warmup','post'} or (run['kind'] == 'waiting' and current == 'break')
    data = public_run(run, None, False)
    data.update(session=run['id'] + ':' + str(run.get('resends',0)),start=run['start'])
    if set_kind:
        series = [item for item in opened if item['kind'] == run['kind']]
        data.update(session=current + ':' + series[0]['id'],
                    progress={'position': series.index(run) + 1, 'total': len(series)})
    data['category'] = bank().get(run['qid'], {}).get('category_name','')
    return [data]


def participant(s, state, identity, assignment):
    key = identity['credential_id']
    member = state['members'].get(key,{})
    opted = member.get('joined',False)
    if not state['enabled']:
        return {'enabled':False}
    own = state['answers'].get(key,{})
    final = s.final_event.get(s)
    sheets = {row['pair'] for row in s.active_sheets('round')}
    current = phase(s)
    post = [r for r in state['runs'] if r['kind']=='post' and r['status']!='void']
    post_closed = bool(post) and all(r['status']=='closed' and r['released'] for r in post)
    pair = assignment['pair']
    runs = [r for r in state['runs'] if eligible(r,pair)]
    visible = [r for r in runs if opted or r.get('manual_delivery') or r['kind'] in {'pulse','vote'}]
    # The Desk has checked missing sheets and stopped the clocks. If judging
    # resumes, stop offering catch-up on reads and when validating answers.
    timestamp = now(s)
    legacy_end = state.get('catchup_end', 0)
    catchup_ids = {r['id'] for r in visible if r['status']=='open' and is_catchup_run(r,legacy_end) and r['end'] > timestamp}
    catchup_allowed = current == 'break' and bool(catchup_ids) and not s.event_config._running(s)
    station_busy = prelim_busy(s,assignment,sheets)
    final_busy = pair in final.get('pairs',[]) and final.get('status') in {'ready','pitch','collecting','review'}
    quiet_period = quiet(s)
    busy = (station_busy and not catchup_allowed) or final_busy or quiet_period
    if current == 'break':
        prompts = [r for r in visible if (r['kind']!='waiting' and not station_busy)
                   or (catchup_allowed and r['id'] in catchup_ids)]
    else:
        prompts = [r for r in visible if not is_catchup_run(r,legacy_end)]
    if quiet_period:
        prompts = [r for r in prompts if r.get('manual_delivery')]
    opened = [] if (station_busy and not catchup_allowed) or final_busy else current_prompt(prompts,own,current)
    busy = busy and not opened
    history = [participant_result(r,own.get(r['id']),post_closed)
               for r in visible if (r['id'] in own and r['status'] in {'open','closed','void'})
               or result_ready(r,post_closed)]
    totals = score(runs, own) if opted else {'earned':0,'available':0,'pending':0,'time':0}
    totals.pop('time')
    return {'enabled':True,'phase':current,'now':now(s),'joined':opted,'invited':state.get('invite',False) and current=='registration' and not member.get('seen',False),
            'busy':busy,'catchup_allowed':catchup_allowed and not busy,'questions':opened,'history':history,'score':totals,'rev':state['rev']}


def row_for(s, assignment, member):
    return {'name':assignment['name'],'pair':assignment['pair'],'partner':assignment['partner'],'member':member}


def player_write(s,state,body,identity,assignment):
    if not state['enabled']:
        raise Conflict('The event quiz is disabled.')
    action = body.get('action')
    key = identity['credential_id']
    existing = state['members'].get(key,{})
    if action in {'join','dismiss','leave'}:
        state['members'][key] = row_for(s,assignment,identity.get('member')) | {'joined': action=='join' or
            (action=='dismiss' and existing.get('joined',False)), 'seen':True}
        return
    if action not in {'answer','view'}:
        raise ValueError('Choose a quiz action.')
    run = next((r for r in state['runs'] if r['id']==body.get('id')),None)
    if not run:
        raise Conflict('This question is no longer available.')
    prior = state['answers'].get(key,{}).get(run['id'])
    if prior and prior['key']==body.get('key'):
        return  # same selection can be retried after a lost response
    if prior:
        raise Conflict('Your answer was already accepted.')
    view = participant(s,state,identity,assignment)
    if not any(q['id']==run['id'] for q in view['questions']):
        raise Conflict('This question is closed or unavailable to your team.')
    if action=='view':
        state.setdefault('viewed',{}).setdefault(run['id'],{})[key] = True
        return
    option = body.get('key')
    if option not in [o['key'] for o in run['options']]:
        raise ValueError('Choose one of the answer options.')
    member = state['members'].setdefault(key,row_for(s,assignment,identity.get('member')) | {'joined':False,'seen':False})
    member.setdefault('member',identity.get('member'))
    if run.get('manual_delivery') and run['points']:
        member.update(joined=True, seen=True)
    state['answers'].setdefault(key,{})[run['id']] = {
        'key':option, 'elapsed':round(max(0,now(s)-run['start']),3)}


def aggregate(state, run):
    counts = Counter(answer[run['id']]['key'] for answer in state['answers'].values() if run['id'] in answer)
    return [{'key':o['key'],'text':o['text'],'count':counts[o['key']]} for o in run['options']]


def rank(state):
    rows = []
    for key, member in state['members'].items():
        if not member['joined']:
            continue
        runs = [r for r in state['runs'] if eligible(r,member['pair'])]
        totals = score(runs,state['answers'].get(key,{}))
        rows.append(member | totals)
    rows.sort(key=lambda row:(-row['earned'],row['name'].casefold()))
    last = None
    for i,row in enumerate(rows,1):
        placement = row['earned']
        if placement != last:
            rank_no = i
        row['rank'] = rank_no
        last = placement
    return rows


def masked(name):
    parts = name.split()
    return 'Participant' if not parts else parts[0] + (f' {parts[-1][0]}.' if len(parts)>1 else '')


ROOM_SECONDS = 15


def room_alias(row):
    team = 'Team ' + str(row['pair'])
    return f"{team} · Player {row['member']}" if row.get('member') else team


def room_standings(state, names=True):
    if not state['enabled']:
        return []
    # The quiz publishes first name + last initial on every hall link.
    # Other event projections still honor their separate names-hidden policy.
    return [{'rank': row['rank'], 'earned': row['earned'],
             'name': masked(row['name'])}
            for row in rank(state)[:10] if row['earned'] > 0]


def learning(state):
    phases = {kind:{r['qid']:r for r in state['runs'] if r['kind']==kind and r['status']=='closed'}
              for kind in ('warmup','post')}
    pairs = []
    for answers in state['answers'].values():
        for qid, before in phases['warmup'].items():
            after = phases['post'].get(qid)
            if after and before['id'] in answers and after['id'] in answers:
                pairs.append((answers[before['id']]['key'] in before['keys'],answers[after['id']]['key'] in after['keys']))
    return {'matched':len(pairs),'before':round(100*sum(x for x,_ in pairs)/len(pairs)) if pairs else None,
            'after':round(100*sum(y for _,y in pairs)/len(pairs)) if pairs else None}


def hall_blocked_reason(s, current, quiet_now, final):
    if current in {'rounds','lunch'} or quiet_now or final.get('status')=='pitch':
        return 'The hall follows the programme during judging, quiet periods, and lunch.'
    preliminary = {row['pair'] for row in s.active_sheets('round')}
    for station in range(1,s.CFG['stations']+1):
        clock = s.clock_public(station)
        pair = s.pair_for(clock['round'],station)
        if pair and clock['started_any'] and clock['phase'] in {'pitch','feedback'} and not clock['ended'] and pair not in preliminary:
            return 'The hall follows the programme while a preliminary session is unfinished.'
    return None


def hall(s, state=None, names=True):
    if state is None:
        state = load(s)
        expire(state,now(s))
    if not state['enabled'] or not state['hall'] or state['hall'].get('agenda') != s.room_agenda_key():
        return None
    share = state['hall']
    timed = share.get('auto_results') or share['kind'] == 'spotlight'
    if timed:
        program = s.program_now()
        if program.get('room_announce', program.get('announce')):
            return None
    if share['kind'] == 'spotlight':
        if now(s) >= share['display_until']:
            return None
        return {'kind':'spotlight','display_until':share['display_until'],
                'rows':room_standings(state, names)}
    if hall_blocked_reason(s,phase(s),quiet(s),s.final_event.get(s)):
        return None
    if (state['hall'].get('kind')=='champion' and phase(s)!='awards'):
        return None
    if share['kind'] in {'standings','champion'}:
        return share
    run = next((r for r in state['runs'] if r['id']==share['id']),None)
    if not run or run['status'] in {'void','skipped'} or (run['status']=='open' and not run.get('manual_delivery') and run['phase']!=phase(s)):
        return None
    view = {'kind':'question','id':run['id'],'prompt':run['prompt'],'status':run['status'],'end':run['end'],
            'options':[dict(o) for o in run['options']]}
    if share.get('auto_results') and run['status'] == 'closed':
        view['display_until'] = run['end'] + ROOM_SECONDS
        if now(s) >= view['display_until']:
            return None
        view['results_pending'] = not room_results_ready(s, run)
    if (share.get('results') or share.get('auto_results')) and room_results_ready(s, run):
        view.update(bars=aggregate(state,run),correct=run['keys'],explanation=run['explanation'])
    return view


def room_results_ready(s, run):
    return (run['status'] == 'closed' and run['released'] and now(s) >= run.get('reveal_at', run['end'])
            and (run['qid'] not in FINAL_IDS or phase(s) == 'awards'))


def desk_state(s,state):
    current = phase(s)
    program = s.program_now()
    agenda = s.agenda()
    context = operation_context(s,current,program)
    hall_reason = hall_blocked_reason(s,current,context['quiet'],context['final']) if state['enabled'] else ENABLE_MESSAGE
    questions = []
    for q in current_bank(state):
        offset = cue_offset(q['phase'],q['time'])
        due = max(program['since'] + offset*60,state['delays'].get(q['id'],0)) if q['phase']==current else None
        reason = send_blocked_reason(state,q,context)
        questions.append(q | {'due':due,'cue_offset_minutes':offset,'can_send':reason is None,'send_blocked_reason':reason,
                              'can_send_room':reason is None and hall_reason is None and question_kind(q) in LIVE_KINDS})
    runs = []
    for run in state['runs']:
        resend_reason = resend_blocked_reason(state,run,context)
        show_reason = show_blocked_reason(run,context,hall_reason)
        runs.append(run | {'responses':sum(run['id'] in answers for answers in state['answers'].values()),
                           'bars':aggregate(state,run),'room_results_ready':room_results_ready(s,run),'can_resend':resend_reason is None,'resend_blocked_reason':resend_reason,
                           'can_show':show_reason is None,'show_blocked_reason':show_reason})
    standings = rank(state)
    activity = [{'at':row['ts'],'action':row['action'][5:],'detail':row['detail']}
                for row in s.DB.execute("SELECT ts,action,detail FROM audit WHERE action LIKE 'quiz-%' ORDER BY id DESC LIMIT 50")]
    archives = reset_archives(s)
    return {'enabled':state['enabled'],'rev':state['rev'],'phase':current,'title':agenda[program['i']]['title'] if agenda else 'No agenda configured',
            'now':now(s),'bank':questions,'runs':runs,'hall':state['hall'],'hall_visible':bool(hall(s,state)),
            'standings':standings,'can_spotlight':state['enabled'] and any(row['earned'] for row in standings),'learning':learning(state),
            'release':state['release'],'prize':state['prize'],'analytics':analytics(state,standings),'activity':activity,
            'phase_baselines':PHASE_BASELINES, 'operator':operator_state(s,state,context,standings,hall_reason),
            'reset_archives':[{'id':a['id'],'at':a['at'],'reason':a['reason'],'run_count':len(a['state']['runs']),
                               'answer_count':sum(map(len,a['state']['answers'].values()))} for a in archives],
            'restorable_reset_id':available_reset(state,archives)}


def analytics(state, standings):
    companies = defaultdict(list)
    pairs = defaultdict(list)
    for row in standings:
        if row['partner']:
            companies[row['partner']].append(row['earned'])
        pairs[row['pair']].append(row['earned'])
    metadata = bank()
    competency = defaultdict(lambda:defaultdict(list))
    confidence = {'before':[],'after':[]}
    answered = defaultdict(list)
    viewers_by_run = {run_id:set(members) for run_id,members in state.get('viewed',{}).items()}
    for key, answers in state['answers'].items():
        member = state['members'].get(key,{})
        company = member.get('partner')
        for run in state['runs']:
            entry = answers.get(run['id'])
            if not entry or run['status']=='void':
                continue
            qid = run['qid']
            answered[run['id']].append(entry)
            viewers_by_run.setdefault(run['id'],set()).add(key)
            if qid in {'P02','P09'}:
                confidence['before' if qid=='P02' else 'after'].append('ABCDE'.index(entry['key'])+1)
            if company and run['released'] and run['points'] and run['keys'] and metadata[qid]['category_name']=='Qualification':
                competency[company][metadata[qid]['competency']].append(entry['key'] in run['keys'])
    questions = {}
    for run in state['runs']:
        entries = answered[run['id']]
        times = sorted(e['elapsed'] for e in entries)
        correct = sum(e['key'] in run['keys'] for e in entries)
        wrong = Counter(e['key'] for e in entries if e['key'] not in run['keys'])
        viewers = len(viewers_by_run.get(run['id'],())) if run['status'] in {'open','closed'} else 0
        questions[run['id']] = {'correctPercent':round(100*correct/len(entries)) if entries and run['keys'] else None,
            'medianSeconds':round((times[(len(times)-1)//2]+times[len(times)//2])/2,1) if times else None,
            'viewers':viewers,'completion':round(100*len(entries)/viewers) if viewers else None,
            'commonWrong':next((o['text'] for o in run['options'] if wrong and o['key']==wrong.most_common(1)[0][0]),None) if run['keys'] else None}
    accepted = sum(map(len, answered.values()))
    valid_ids = {run['id'] for run in state['runs'] if run['status'] in {'open','closed'}}
    viewed = set().union(*(viewers_by_run.get(run_id,()) for run_id in valid_ids))
    engaged = sum(any(run in valid_ids for run in answers) for answers in state['answers'].values())
    summary = {'joined':len(standings),'viewers':len(viewed),'respondents':engaged,'answers':accepted,'released':len(valid_ids),
               'skipped':sum(run['status']=='skipped' for run in state['runs']),
               'voided':sum(run['status']=='void' for run in state['runs'])}
    return {'summary':summary,'competencies':[{'company':key,'skills':[{'name':skill,'accuracy':round(100*sum(values)/len(values))}
                        for skill,values in sorted(skills.items())]} for key,skills in sorted(competency.items())],
            'confidence':{key:round(sum(values)/len(values),1) if values else None for key,values in confidence.items()},
            'questions':questions,'teams':[{'name':f'Team {key}','count':len(values),'average':round(sum(values)/len(values),1)}
                    for key,values in sorted(pairs.items())],
            'companies':[{'name':key,'count':len(values),'average':round(sum(values)/len(values),1)}
                         for key,values in sorted(companies.items())]}


def text(value, limit, label, required=True):
    if not isinstance(value,str) or len(value.strip())>limit or (required and not value.strip()):
        raise ValueError(f'Enter {label} of up to {limit} characters.')
    return value.strip()


def whole(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be between {low} and {high}.')
    return value


def clock_minutes(value):
    hours, minutes = map(int,value.split(':'))
    return hours*60+minutes


def cue_offset(phase_name, time):
    return clock_minutes(time)-clock_minutes(PHASE_BASELINES[phase_name])


def ordinary_question(q):
    return q and q['id']!='P00' and q['type']!='reveal' and not q.get('schedules')


def retime_plan(state, body):
    bank = current_bank(state)
    q = next((item for item in bank if item['id']==body.get('qid')),None)
    used = {r['qid'] for r in state['runs'] if r['status']!='void'}
    if not ordinary_question(q) or q['id'] in used:
        raise Conflict('Choose a queued question that has not been sent or skipped.')
    new_phase = body.get('phase',q['phase'])
    time = body.get('time')
    shift = body.get('shift_following')
    if new_phase not in PHASES[:-1] or not isinstance(time,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',time):
        raise ValueError('Choose a valid quiz section and cue time.')
    if type(shift) is not bool:
        raise ValueError('Choose whether to move the following cues.')
    if new_phase != q['phase'] and (shift or q['id'] in ROUND_IDS | FINAL_IDS):
        raise Conflict('Move questions between sections individually. Waiting-time questions and Final votes stay in their own sections.')
    delta = clock_minutes(time)-clock_minutes(q['time'])
    changes = []

    def add(item, phase, time):
        if (item['phase'],item['time']) != (phase,time):
            changes.append({'qid':item['id'],'prompt':item['prompt'],
                'before':{'phase':item['phase'],'time':item['time'],'offset_minutes':cue_offset(item['phase'],item['time'])},
                'after':{'phase':phase,'time':time,'offset_minutes':cue_offset(phase,time)}})

    add(q,new_phase,time)
    if shift and delta:
        ordered = sorted((item for item in bank if item['phase']==q['phase']),key=lambda item:item['time'])
        for item in ordered[ordered.index(q)+1:]:
            if item['id'] in used or not ordinary_question(item):
                continue
            shifted = clock_minutes(item['time'])+delta
            if not 0 <= shifted < 24*60:
                raise Conflict('Moving the following cues would put one outside the same day.')
            add(item,item['phase'],f'{shifted//60:02d}:{shifted%60:02d}')
    return changes


def retime_preview(state, body):
    check_revision(state,body)
    return {'rev':state['rev'],'agenda_unchanged':True,'changes':retime_plan(state,body)}


def edit(state,q,body):
    patch = body.get('changes')
    if not isinstance(patch,dict) or not set(patch) <= set(KEYS):
        raise ValueError('Choose valid question fields.')
    merged = deepcopy(q | patch)
    for field, limit in [('prompt',240),('explanation',500),('cue',280)]:
        merged[field] = text(merged[field],limit,field,field=='prompt')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',str(merged['time'])) or merged['phase'] not in PHASES[:-1]:
        raise ValueError('Choose a valid agenda section and cue time.')
    if 'phase' in patch and q['id'] in ROUND_IDS | FINAL_IDS and merged['phase'] != bank()[q['id']]['phase']:
        raise Conflict('Waiting-time questions and Final votes stay in their own sections.')
    if merged['priority'] not in {'core','optional'}:
        raise ValueError('Choose core or optional.')
    whole(merged['points'],0,10,'Points')
    whole(merged['seconds'],10,120,'Timer')
    options = merged['options']
    if not isinstance(options,list) or not 2 <= len(options) <= 5:
        raise ValueError('Enter two to five choices.')
    allowed_keys = 'TF' if q['type']=='tf' else 'ABCDE'
    seen = set()
    for option in options:
        if not isinstance(option,dict) or set(option)!={'key','text'} or not isinstance(option['key'],str) or len(option['key'])!=1 or option['key'] not in allowed_keys or option['key'] in seen:
            raise ValueError('Each answer must have a unique choice key.')
        seen.add(option['key'])
        option['text'] = text(option['text'],150,'an answer choice')
    if q['id'] in FINAL_IDS or q['category']=='P':
        merged['points'] = 0
    if q['id'] in FINAL_IDS | {'R3b'}:
        if [o['key'] for o in options] != [o['key'] for o in bank()[q['id']]['options']]:
            raise ValueError('Prediction choices must keep the original option keys.')
        merged['correct'] = q['correct']
    elif merged['points'] and merged['correct'] not in seen:
        raise ValueError('Choose the correct answer for this scored question.')
    elif not merged['points'] and not q.get('schedules'):
        merged['correct'] = None
    state['edits'][q['id']] = {key:merged[key] for key in KEYS}


def run_blocked_reason(state, qid, kind, context):
    if any(r['qid']==qid and r['kind']==kind and r['status'] not in {'void','skipped'} for r in state['runs']):
        return 'This question already ran. Reset it to send a fresh copy.'
    if len(state['runs']) >= 250:
        return 'This workspace reached the quiz run limit.'
    if kind=='waiting' and context['rounds_end'] <= context['now']:
        return 'Round unlocks have ended. Open catch-up in the scoring break.'
    if qid in FINAL_IDS:
        final = context['final']
        if not context['final_engaged'] or len(final['pairs'])!=3:
            return 'Announce the three finalists before sending this vote.'
        if qid=='F1' and final['status']!='ready':
            return 'Open the winner prediction after announcing the finalists and before pitching begins.'
        if qid=='F2' and final['status'] not in {'review','completed'}:
            return 'Open People’s Choice after the finalist pitches.'
    return None


def operation_context(s,current,program):
    final = s.final_event.get(s)
    return {'phase':current,'quiet':quiet(s),'now':now(s),'rounds_end':program['since']+58*60,
            'final':final,'final_engaged':s.final_event.engaged(final)}


LIVE_KINDS = {'live','pulse','vote'}
MANUAL_KINDS = {'live', 'pulse'}
DEFAULT_QUESTION_SECONDS = 60
LOCK_MESSAGE = 'Quiz scoring is locked after the champion is announced.'
ENABLE_MESSAGE = 'Enable the event quiz in settings first.'


def question_kind(q):
    if q['id'] in ROUND_IDS:
        return 'waiting'
    if q['id'] in FINAL_IDS:
        return 'vote'
    return 'pulse' if q['category']=='P' else 'live'


def send_blocked_reason(state,q,context):
    if not ordinary_question(q):
        return 'Use the set or reveal controls for this question.'
    if state['release']:
        return LOCK_MESSAGE
    if not state['enabled']:
        return ENABLE_MESSAGE
    kind = question_kind(q)
    if kind not in MANUAL_KINDS and q['phase']!=context['phase']:
        return 'Select this question’s agenda section to send it.'
    if kind not in MANUAL_KINDS and context['quiet']:
        return 'Wait until the quiet period ends.'
    if any(r['qid']==q['id'] and r['status']=='skipped' for r in state['runs']):
        return 'Restore this skipped question before sending.'
    if context['phase']=='final' and context['final']['status']=='pitch':
        return 'Wait for the active Final pitch to end.'
    if kind in LIVE_KINDS and any(r['kind'] in LIVE_KINDS and r['status']=='open' for r in state['runs']):
        return 'Close the current live question first.'
    return run_blocked_reason(state,q['id'],kind,context)


def resend_blocked_reason(state,run,context):
    if state['release']:
        return LOCK_MESSAGE
    if not state['enabled']:
        return ENABLE_MESSAGE
    if run['kind'] not in LIVE_KINDS or run['status'] not in {'open','closed'}:
        return 'This question cannot be resent.'
    if any(r['id'] != run['id'] and r['kind'] in LIVE_KINDS and r['status'] == 'open'
           for r in state['runs']):
        return 'Close the current live question first.'
    if run['kind'] in MANUAL_KINDS:
        if run['released'] and run['points'] and run['keys'] and len(state['runs']) >= 250:
            return 'This workspace reached the quiz run limit.'
        return None
    if context['phase'] != run['phase'] or context['quiet']:
        return 'Return to this question’s agenda section first.'
    final = context['final']
    if run['qid'] in FINAL_IDS and (run['final_run']!=final.get('run_id') or final.get('status')=='pitch'):
        return 'The Final lineup changed or a pitch is in progress.'
    if run['qid']=='F1' and final.get('status')!='ready':
        return 'Resend the winner prediction before Final pitching begins.'
    if run['qid']=='F2' and (run['released'] or final.get('status') not in {'review','completed'}):
        return 'Resend People’s Choice after the finalist pitches and before sharing its results.'
    return None


def show_blocked_reason(run,context,hall_reason):
    if run['status'] not in {'open','closed'} or run['private'] or run['kind'] not in LIVE_KINDS:
        return 'This question cannot be shown on the hall screen.'
    if run['status']=='open' and not run.get('manual_delivery') and run['phase']!=context['phase']:
        return 'Return to this question’s agenda section before showing it.'
    if run['qid'] in FINAL_IDS and run['status']=='closed' and context['phase']!='awards':
        return 'Reveal the audience votes during Awards.'
    return hall_reason


def build_run(s,state,q,kind,minutes,context=None):
    context = context or operation_context(s,phase(s),s.program_now())
    qid = q['id']
    reason = run_blocked_reason(state,qid,kind,context)
    if reason:
        raise Conflict(reason)
    final = context['final']
    options = deepcopy(q['options'])
    excluded = []
    if qid=='R3b':
        options = [{'key':key,'text':scenario['name'],'scenario':scenario['id']}
                   for key,scenario in zip('ABCD',s.CONTENT['scenarios'])]
    if qid in FINAL_IDS:
        excluded = list(final['pairs'])
        options = [{'key':key,'text':f'Team {pair}'} for key,pair in zip('ABC',excluded)]
    correct = [q['correct']] if q['correct'] in [o['key'] for o in options] else []
    timestamp = context['now']
    end = timestamp + (minutes*60 if minutes else q['seconds'])
    if kind=='waiting':
        end = min(end,context['rounds_end'])
    run = {'id':f'{qid}-{state["rev"]+1}-{len(state["runs"])+1}', 'qid':qid,'kind':kind,'status':'open',
           'start':timestamp,'end':end,'reveal_at':end,'seconds':q['seconds'],'points':0 if kind=='warmup' else q['points'],
           'prompt':q['prompt'],'options':options,'keys':correct,'explanation':q['explanation'],
           'phase':'break' if kind=='post' else q['phase'],'excluded':excluded,'prediction':qid in {'R3b','F1'},'released':False,
           'private':qid in {'PR1','PR2'}, 'final_run':final.get('run_id') if qid in FINAL_IDS else None,
           'catchup':False}
    state['runs'].append(run)
    return run


def resolve_predictions(s,state):
    final = s.final_event.get(s)
    reveal = s.kv_get('reveal','hidden')
    candidates = [r for r in state['runs'] if r['prediction'] and r['status']=='closed' and not r['released']]
    if not candidates:
        raise Conflict('Close a prediction before revealing results.')
    if any(r['qid']=='F1' for r in candidates) and final.get('status')!='completed':
        raise Conflict('Publish the confirmed Final winners first.')
    for run in candidates:
        if run['qid']=='F1':
            if run['final_run'] != final.get('run_id'):
                raise Conflict('The Final lineup changed. Void the old prediction.')
            winner = next((row.get('pair') for row in final.get('published_rows',[]) if row.get('rank')==1),None)
            if winner not in run['excluded']:
                raise Conflict('The published Final has no winner for this prediction. Check the Final results or void the prediction.')
            run['keys'] = [key for key,pair in zip('ABC',run['excluded']) if pair==winner]
        else:
            if reveal not in {'scores','top3','winners'} and s.public_mode()!='live':
                raise Conflict('Release preliminary results before resolving the scenario prediction.')
            scenarios = defaultdict(list)
            for row in s.leaderboard():
                scenarios[row['scenario']].append(row['total'])
            means = {key:Fraction(sum(values),len(values)) for key,values in scenarios.items()}
            if not means:
                raise Conflict('Preliminary results are unavailable.')
            run['keys'] = [option['key'] for option in run['options'] if means.get(option['scenario']) == max(means.values())]
        run['released'] = True


def catchup_targets(state):
    return [r for r in state['runs'] if r['kind']=='waiting' and r['status'] in {'open','closed'} and not r['released']]


def catchup_blocked_reason(s,state,context):
    if context['phase']!='break' or context['quiet']:
        return 'Select the scoring break before opening catch-up.'
    if not catchup_targets(state):
        return 'Send at least one waiting-time question during preliminary rounds.'
    if s.event_config._running(s):
        return 'Finish active judging clocks before opening catch-up.'
    return None


def people_choice_run(state):
    return next((r for r in reversed(state['runs']) if r['qid']=='F2' and r['status']=='closed'),None)


def people_choice_blocked_reason(state,current,hall_reason):
    if current!='awards':
        return 'Reveal People’s Choice during Awards.'
    if not people_choice_run(state):
        return 'Close People’s Choice before showing results.'
    return hall_reason


def show_champion_blocked_reason(state,current,hall_reason):
    if not state['release'] or current!='awards':
        return 'Choose Awards and announce the champion before showing it again.'
    return hall_reason


def standings_blocked_reason(state,context,action,standings):
    current = context['phase']
    if action=='standings':
        return None if current in {'prep','break','awards'} else 'Select preparation, the scoring break, or awards to show standings.'
    if current!='awards':
        return 'Select awards before announcing the champion.'
    if state['release']:
        return 'The champion was already announced. Use Show champion to display it again.'
    if any(r['points'] and r['status']=='open' for r in state['runs']):
        return 'Close open scored questions before announcing the champion.'
    if any(r['points'] and r['status']=='closed' and (not r['released'] or not r['keys']) for r in state['runs']):
        return 'Release or void closed scored questions before announcing the champion. Release the post-test or waiting-time answers, or resolve pending predictions.'
    if any((r['prediction'] or r['qid']=='F2') and r['status'] in {'open','closed'} and not r['released'] for r in state['runs']):
        return 'Release or cancel open and pending predictions and People’s Choice before announcing the champion.'
    final = context['final']
    if final.get('pairs') and final.get('status')!='completed':
        return 'Publish the confirmed Final results before announcing the quiz champion.'
    return None if standings and standings[0]['earned'] else 'No participant has scored quiz points.'


def operator_state(s,state,context,standings,audience):
    enabled = state['enabled']
    available = None if enabled else ENABLE_MESSAGE
    unlocked = available or (LOCK_MESSAGE if state['release'] else None)
    catchup = unlocked or catchup_blocked_reason(s,state,context)
    predictions = unlocked
    if not unlocked:
        if context['phase']!='awards':
            predictions = 'Reveal predictions during Awards.'
        else:
            # Prediction resolution updates only copies of the run rows and reads
            # the official results. This avoids duplicating its release rules.
            try:
                resolve_predictions(s,{'runs':[r.copy() for r in state['runs'] if r.get('prediction')]})
            except Conflict as error:
                predictions = str(error)
    people_choice = unlocked or people_choice_blocked_reason(state,context['phase'],audience)
    reasons = {'catchup':catchup,'champion':standings_blocked_reason(state,context,'champion',standings) or audience,
               'standings':standings_blocked_reason(state,context,'standings',standings) or audience,
               'predictions':predictions,'people_choice':people_choice,
               'show_champion':available or show_champion_blocked_reason(state,context['phase'],audience)}
    return {**{f'can_{key}':reason is None for key,reason in reasons.items()},
            **{f'{key}_blocked_reason':reason for key,reason in reasons.items()},
            'can_show_hall':audience is None,'hall_blocked_reason':audience,'quiet':context['quiet'],
            'invite_sent':bool(state.get('invite')),
            'catchup_open':context['phase']=='break' and catchup is None and any(
                r['status']=='open' and is_catchup_run(r,state.get('catchup_end',0)) for r in catchup_targets(state))}


def command(s,state,body):
    action = body.get('action')
    check_revision(state,body)
    current = phase(s)
    q = next((q for q in current_bank(state) if q['id']==body.get('qid')),None)
    run = next((r for r in state['runs'] if r['id']==body.get('id')),None)
    if state['release'] and action in {'send','resend','void','reset_question','catchup','warmup','post','predictions','people_choice','close_waiting','close_post'}:
        raise Conflict(LOCK_MESSAGE)
    if action == 'settings':
        if type(body.get('enabled')) is not bool:
            raise ValueError('Choose whether to enable the quiz.')
        if body['enabled']:
            state['enabled'] = True
        else:
            disable(state,now(s))
        return
    if action == 'edit' and q and q['type']!='reveal' and q['id']!='P00':
        edit(state,q,body)
        return
    if action=='retime':
        for change in retime_plan(state,body):
            qid = change['qid']
            cue = change['after']
            state['edits'].setdefault(qid,{}).update(time=cue['time'],phase=cue['phase'])
            state['delays'].pop(qid,None)
        return
    if not state['enabled']:
        raise Conflict(ENABLE_MESSAGE)
    if action=='skip' and len(state['runs'])>=250:
        raise Conflict('This workspace reached the quiz run limit.')
    if action == 'hide':
        state['hall'] = None
    elif action == 'prize':
        state['prize'] = text(body.get('prize'),180,'the prize',False)
    elif action in {'delay','skip'} and q:
        if action=='delay':
            state['delays'][q['id']] = now(s) + whole(body.get('seconds',60),15,300,'Delay')
        else:
            if any(r['qid']==q['id'] and r['status']!='void' for r in state['runs']):
                raise Conflict('This question already ran. Use Void to remove it from scoring.')
            state['runs'].append({'id':f"skip-{q['id']}-{state['rev']}",'qid':q['id'],'kind':'skip','status':'skipped',
                'prompt':q['prompt'],'options':[],'points':0,'keys':[],'private':True,'prediction':False})
    elif action == 'restore' and run and run['status']=='skipped':
        state['runs'].remove(run)
    elif action == 'invite':
        if current!='registration':
            raise Conflict('The opt-in prompt belongs in registration. Participants can join from the badge any time.')
        state['invite'] = True
    elif action in {'warmup','post','catchup'}:
        if action=='catchup':
            reason = catchup_blocked_reason(s,state,operation_context(s,current,s.program_now()))
            if reason:
                raise Conflict(reason)
            assigned = {row['pair'] for row in s.event_config.list_roster(s) if any(m['name'].strip() for m in row['members'])}
            received = {row['pair'] for row in s.active_sheets('round')}
            if assigned - received and body.get('confirm_missing') is not True:
                raise Conflict('Some teams have no accepted sheet. Finish judging or confirm an exception before opening catch-up.')
            end = now(s)+whole(body.get('minutes',2),1,10,'Catch-up minutes')*60
            for r in catchup_targets(state):
                r.update(status='open',end=end,catchup=True)
            state['catchup_end'] = end
        else:
            if current!=('registration' if action=='warmup' else 'break') or quiet(s):
                raise Conflict('This set belongs in a different agenda section.')
            for qid in ('PQ1','PQ2','PQ3','PQ4','PQ5'):
                item = next(q for q in current_bank(state) if q['id']==qid)
                build_run(s,state,item,action,whole(body.get('minutes',5),1,20,'Set minutes'))
    elif action=='send' and q:
        context = operation_context(s,current,s.program_now())
        reason = send_blocked_reason(state,q,context)
        if reason:
            raise Conflict(reason)
        kind = question_kind(q)
        seconds = body.get('seconds', DEFAULT_QUESTION_SECONDS)
        if seconds is not None:
            seconds = whole(seconds,10,120,'Timer')
        sent = build_run(s,state,q,kind,0,context)
        end = None if seconds is None else sent['start'] + seconds
        if kind == 'waiting':
            end = min(end, context['rounds_end']) if end is not None else context['rounds_end']
        sent.update(end=end, reveal_at=end, seconds=seconds or sent['seconds'])
        if kind in MANUAL_KINDS:
            sent['manual_delivery'] = True
        return sent
    elif action == 'resend' and run:
        context = operation_context(s,current,s.program_now())
        reason = resend_blocked_reason(state,run,context)
        if reason:
            raise Conflict(reason)
        manual = run['kind'] in MANUAL_KINDS
        seconds = body.get('seconds', DEFAULT_QUESTION_SECONDS)
        timestamp = now(s)
        end = None if seconds is None else timestamp + whole(seconds,10,120,'Timer')
        if manual and run['released'] and run['points'] and run['keys']:
            # Keep the official answers and score intact after its answer key was shared.
            run = deepcopy(run)
            run.update(id=f'{run["qid"]}-{state["rev"]+1}-{len(state["runs"])+1}',
                       points=0, resends=0)
            state['runs'].append(run)
        run.update(status='open',start=timestamp,end=end,reveal_at=end,
                   released=False,resends=run.get('resends',0)+1)
        if seconds is not None:
            run['seconds'] = seconds
        if manual:
            run['manual_delivery'] = True
        return run
    elif action in {'extend','close','void','reset_question','show'} and run:
        if action=='extend' and run['status']=='open':
            if is_catchup_run(run,state.get('catchup_end', 0)):
                run['catchup'] = True
            if run['end'] is None:
                raise Conflict('This question stays open until you close it.')
            run['end'] += whole(body.get('seconds',15),5,120,'Extension')
            run['reveal_at'] = run['end']
        elif action=='close' and run['status']=='open':
            share = state.get('hall') or {}
            if share.get('kind') == 'spotlight':
                share = share.get('resume') or {}
            if run['end'] is None or (share.get('id') == run['id'] and share.get('auto_results')):
                run['reveal_at'] = now(s)
            run.update(status='closed',end=now(s),released=not run['prediction'] and run['qid']!='F2' and run['kind'] not in {'warmup','post','waiting'})
        elif action in {'void','reset_question'} and run['status'] in {'open','closed'}:
            if action=='reset_question':
                confirm_action(body,'RESET QUESTION')
                if run['kind'] in {'warmup','post'}:
                    raise Conflict('This question belongs to a set and cannot be restarted on its own.')
            else:
                change_reason(body,required=False)
            run['status'] = 'void'
            if action=='reset_question':
                state['delays'].pop(run['qid'],None)
            if state['hall'] and state['hall'].get('id')==run['id']:
                state['hall'] = None
        elif action=='show':
            context = operation_context(s,current,s.program_now())
            reason = show_blocked_reason(run,context,hall_blocked_reason(s,current,context['quiet'],context['final']))
            if reason:
                raise Conflict(reason)
            results = body.get('results', False)
            if type(results) is not bool:
                raise ValueError('Choose whether to show released results.')
            if results and not room_results_ready(s,run):
                raise Conflict('Wait for the results to be released before showing them on the room screen.')
            state['hall'] = {'kind':'question','id':run['id'],'results':results}
        else:
            raise Conflict('That action is unavailable for this question.')
    elif action in {'close_warmup','close_post','close_waiting'}:
        kind = action.split('_')[1]
        targets = [r for r in state['runs'] if r['kind']==kind and r['status'] in {'open','closed'}]
        if not targets:
            raise Conflict('Open the set before closing it.')
        if kind=='waiting' and any(r['status']=='open' for r in targets) and not state.get('catchup_end'):
            raise Conflict('Open catch-up before releasing waiting-time answers.')
        for r in targets:
            if r['status']=='open':
                r.update(status='closed',end=now(s))
            if not r['prediction']:
                r['released'] = True
        if kind=='post':
            for r in state['runs']:
                if r['kind']=='warmup' and r['status'] in {'open','closed'}:
                    if r['status']=='open':
                        r.update(status='closed',end=now(s))
                    r['released'] = True
    elif action == 'predictions':
        if current!='awards':
            raise Conflict('Reveal predictions during Awards.')
        resolve_predictions(s,state)
    elif action == 'people_choice':
        reason = people_choice_blocked_reason(state,current,hall_blocked_reason(s,current,quiet(s),s.final_event.get(s)))
        if reason:
            raise Conflict(reason)
        run = people_choice_run(state)
        run['released'] = True
        state['hall'] = {'kind':'question','id':run['id']}
    elif action == 'spotlight':
        if not room_standings(state, names=False):
            raise Conflict('Release a scored quiz answer before showing the leaderboard.')
        previous = state.get('hall') or {}
        resume = previous.get('resume') if previous.get('kind') == 'spotlight' else previous
        state['hall'] = {'kind':'spotlight','display_until':now(s) + ROOM_SECONDS,
                         'resume':resume if resume and resume.get('auto_results') else None}
    elif action in {'standings','champion'}:
        standings = rank(state)
        context = operation_context(s,current,s.program_now())
        reason = standings_blocked_reason(state,context,action,standings) or hall_blocked_reason(s,current,context['quiet'],context['final'])
        if reason:
            raise Conflict(reason)
        rows = [{key:row[key] for key in ('rank','earned')} | {'name':masked(row['name'])} for row in standings[:5]]
        view = {'kind':action,'rows':rows,'learning':learning(state)}
        if action=='champion':
            view['tied'] = sum(row['rank']==1 for row in standings)>1
            view['prize'] = state['prize']
            state['release'] = view
        state['hall'] = view
    elif action=='show_champion':
        reason = show_champion_blocked_reason(state,current,hall_blocked_reason(s,current,quiet(s),s.final_event.get(s)))
        if reason:
            raise Conflict(reason)
        state['hall'] = state['release']
    else:
        raise ValueError('Choose a valid Desk quiz action.')


def csv_data(state, kind):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    def safe(value):
        return "'" + value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value
    if kind=='people':
        writer.writerow(['participant','team','partner','rank','earned','available','pending'])
        for row in rank(state):
            writer.writerow([safe(row[k]) for k in ('name','pair','partner','rank','earned','available','pending')])
    else:
        writer.writerow(['question','run','status','responses','correct','most common incorrect choice'])
        for r in state['runs']:
            bars = aggregate(state,r)
            wrong = max((b for b in bars if b['key'] not in r['keys'] and b['count']),key=lambda b:b['count'],default=None)
            writer.writerow([r['qid'],r['id'],r['status'],sum(b['count'] for b in bars),
                sum(b['count'] for b in bars if b['key'] in r['keys']),safe(wrong['text']) if wrong and r['keys'] else ''])
    return buffer.getvalue()


def apply_desk_action(s, state, body, actor):
    """Apply and record the same Organizer operation for browser and agent callers."""
    action = body['action']
    if action in {'reset','restore_reset'}:
        new_state, archives, archive_id, reason = reset_change(s,state,body)
        new_state['rev'] += 1
        # The generic KV and audit helpers each commit. Use one transaction so
        # neither a reset nor its recovery record can persist on its own.
        with s.DB:
            s.DB.executemany('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                ((RESET_ARCHIVE_KEY,json.dumps(archives)),(KEY,json.dumps(new_state))))
            ident = getattr(s.REQUEST_AUTH,'identity',None) or {}
            real = ident.get('actor') or ident
            profile = ident.get('impersonation') or {}
            s.DB.execute('''INSERT INTO audit(ts,actor,action,detail,actor_credential,actor_label,
                            effective_credential,effective_role,profile_id,impersonation_mode)
                            VALUES(?,?,?,?,?,?,?,?,?,?)''',
                         (now(s),actor,'quiz-'+action,f'{archive_id}: {reason}'[:500],real.get('credential_id',''),
                          real.get('label',''),ident.get('credential_id',''),ident.get('role',''),profile.get('id',''),profile.get('mode','')))
        state.clear()
        state.update(new_state)
        return archive_id
    room = body.get('room', False)
    if type(room) is not bool or (room and action not in {'send','resend'}):
        raise ValueError('Choose whether to send the question to the room screen.')
    run = command(s,state,body)
    if room:
        current = phase(s)
        context = operation_context(s,current,s.program_now())
        reason = show_blocked_reason(run,context,hall_blocked_reason(s,current,context['quiet'],context['final']))
        if reason:
            raise Conflict(reason)
        state['hall'] = {'kind':'question','id':run['id'],'auto_results':True}
    if room or action in {'show','people_choice','standings','champion','show_champion','spotlight'}:
        state['hall'] = {**state['hall'], 'agenda': s.room_agenda_key()}
        if action == 'people_choice':
            state['hall']['results'] = True
        s.clear_audience_room()
    state['rev'] += 1
    s.kv_set(KEY,state)
    detail = str(body.get('qid') or body.get('id') or '')
    if action=='settings':
        detail = 'Enabled' if state['enabled'] else 'Disabled'
    if action in {'void','reset_question'} and body.get('reason'):
        detail += ': '+body['reason'].strip()
    s.audit(actor,'quiz-'+action,detail)
    return None


def handle(s,h,path,body=None):
    exports = {DESK+'/people.csv':'people',DESK+'/questions.csv':'questions'}
    if path not in {DESK,PLAYER,*exports}:
        return False
    with s.LOCK:
        ident = s.event_access.session(s,h.headers)
        if not ident:
            h.send(401,{'error':'Sign in to continue.'})
            return True
        allowed = {'participant'} if path==PLAYER else {'desk','admin'}
        if ident['role'] not in allowed:
            h.send(403,{'error':'This quiz view is not available to your role.'})
            return True
        state = load(s)
        # Capture notification transitions before expiry or answer mutations.
        # The event lock stays held through kv_set, so this is the same prior
        # state that a second database read would return.
        quiz_before = s.event_push.quiz_transition_snapshot(state) if path==PLAYER and body is not None else None
        expire(state,now(s))
        try:
            if path in exports and body is None:
                h.send(200,csv_data(state,exports[path]),'text/csv',
                    {'Content-Disposition':f'attachment; filename=quiz-{exports[path]}.csv'})
                return True
            if path==PLAYER:
                assignment = s.event_access.participant_assignment(s,ident)
                if not assignment or not assignment['name'].strip():
                    raise Conflict('Your participant assignment is unavailable.')
                if body is not None:
                    player_write(s,state,body,ident,assignment)
                    s.kv_set(KEY,state,quiz_before=quiz_before)
                data = participant(s,state,ident,assignment)
            else:
                if body is not None and body.get('action')=='retime_preview':
                    h.send(200,retime_preview(state,body))
                    return True
                if body is not None:
                    apply_desk_action(s,state,body,ident['role'])
                data = desk_state(s,state)
                if ident.get('dashboard')=='organizer':
                    data['standings']=[]
                    data['analytics']={k:v for k,v in data['analytics'].items() if k not in ('people','companies','teams','participants')}
                    data['individual_results_restricted']=True
            h.send(200,data)
        except Conflict as exc:
            h.send(409,{'error':str(exc)})
        except ValueError as exc:
            h.send(400,{'error':str(exc)})
    return True
