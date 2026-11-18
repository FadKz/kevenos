"""Final event lifecycle, receipt barriers and explicit result publication.

The scoring run freezes panel membership and each team's original assignment.
No public result is derived from a partial live tally.
"""
from copy import deepcopy
import hashlib
import json
import uuid

ENGAGED = {'ready', 'pitch', 'collecting', 'review', 'completed'}


def migrate(db):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(sheets)')}
    if 'final_run' not in columns:
        db.execute("ALTER TABLE sheets ADD COLUMN final_run TEXT NOT NULL DEFAULT ''")
    if 'wrap_rating' not in columns:
        db.execute("ALTER TABLE sheets ADD COLUMN wrap_rating INTEGER NOT NULL DEFAULT 0")
    if 'final_rating' not in columns:
        db.execute("ALTER TABLE sheets ADD COLUMN final_rating INTEGER NOT NULL DEFAULT 0")


def empty():
    return {'active':False, 'status':'idle', 'pairs':[], 'current':0, 'panel':[],
            'visited':[], 'waivers':{}, 'published':False}


def _team(s, pair):
    row = s.roster_row(pair)
    case = s.event_config.original_scenario(s, pair)
    return dict(row, scenario=case['id'], case=deepcopy(case))


def get(s):
    raw = s.kv_get('final')
    if not raw or not raw.get('pairs'):
        return empty()
    f = deepcopy(raw)
    # Upgrade existing final ballots without deleting or retagging history.
    if not f.get('run_id'):
        with s.LOCK:
            latest = s.kv_get('final')
            if latest and latest.get('run_id'):
                return get(s)
            f['run_id'] = 'legacy-' + uuid.uuid4().hex
            f['legacy'] = True
            f['panel'] = list(range(1,s.CFG['stations']+1))
            f['teams'] = {str(p):_team(s,p) for p in f['pairs']}
            old_ballots = [row for row in s.active_sheets('final') if row['pair'] in f['pairs']]
            found = [row['pair'] for row in old_ballots]
            f['legacy_ballot_ids'] = [row['id'] for row in old_ballots]
            f['visited'] = list(dict.fromkeys(f['pairs'][:f.get('current',0)+1] if f.get('active') else found))
            f['status'] = ('pitch' if (f.get('clock') or {}).get('running') else 'ready') if f.get('active') else ('review' if found else 'prepared')
            f['waivers'] = {}; f['published'] = False
            with s.DB:
                s.DB.executemany("UPDATE sheets SET final_run=? WHERE kind='final' AND final_run='' AND pair=?",[(f['run_id'],p) for p in f['pairs']])
            s.kv_set('final',f)
    if f.get('legacy') and 'legacy_ballot_ids' not in f:
        with s.LOCK:
            f = deepcopy(s.kv_get('final'))
            if 'legacy_ballot_ids' not in f:
                f['legacy_ballot_ids'] = [row['id'] for row in sheets(s,f)]
                s.kv_set('final',f)
    if f.get('status') == 'pitch' and s.final_clock(f,s.time.time())[2]:
        f['status'] = 'collecting'
    f['active'] = f.get('status') in {'ready','pitch','collecting'}
    return f


def engaged(f):
    return f.get('status') in ENGAGED


def current_pair(f):
    pairs=f.get('pairs',[])
    return pairs[f.get('current',0)] if pairs else None


def case_for(s, f, pair=None):
    pair=current_pair(f) if pair is None else pair
    if pair is None:
        return None
    return deepcopy(f.get('teams',{}).get(str(pair),{}).get('case') or s.event_config.original_scenario(s,pair))


def team_for(s, f, pair):
    team=deepcopy(f.get('teams',{}).get(str(pair)) or s.roster_row(pair))
    team.pop('case',None)
    return team


def sheets(s, f):
    return [row for row in s.active_sheets('final') if row.get('final_run') == f.get('run_id')
            and row['pair'] in f.get('pairs',[]) and row['station'] in f.get('panel',[])]


def legacy_ballots(s,f):
    flagged=set(f.get('legacy_ballot_ids',[]))
    return [row for row in sheets(s,f) if row['id'] in flagged]


def _judge_name(s,station):
    credential=s.event_access.auth_db(s).execute("SELECT label FROM access_credentials WHERE role='judge' AND station=? AND active=1 AND dataset=?",
        (station,s.event_access.active_dataset(s))).fetchone()
    if credential:
        return credential['label']
    row=s.DB.execute('SELECT judge FROM judge_invites WHERE station=?',(station,)).fetchone()
    return s.LIVE.get(station,{}).get('judge') or (row['judge'] if row else '') or ('Judge %d'%station)


def rows(s, f):
    by={}; old_by={}
    flagged=set(f.get('legacy_ballot_ids',[]))
    for sheet in sheets(s,f):
        target=old_by if sheet['id'] in flagged or not 1<=sheet.get('final_rating',0)<=5 else by
        target.setdefault(sheet['pair'],{})[sheet['station']]=sheet
    out=[]
    for pair in f.get('pairs',[]):
        ro=team_for(s,f,pair); ballot=by.get(pair,{})
        received=list(ballot.values()); n=len(received)
        waiver=f.get('waivers',{}).get(str(pair),{})
        missing=[st for st in f['panel'] if st not in ballot and str(st) not in waiver]
        complete=pair in f.get('visited',[]) and bool(n) and not missing and not old_by.get(pair)
        ratings=[sheet['final_rating'] for sheet in received]
        rating=round(sum(ratings)/n,2) if n else 0
        receipts=[]
        for st in f['panel']:
            seen=s.LIVE.get(st,{}).get('t')
            receipts.append({'station':st,'judge':_judge_name(s,st),'submitted':st in ballot,
                'seen':round(max(0,s.time.time()-seen),1) if seen else None,
                'needs_rescore':st in old_by.get(pair,{}),
                'waived':str(st) in waiver and st not in ballot,
                'reason':waiver.get(str(st),{}).get('reason','') if st not in ballot else ''})
        out.append(dict(ro,judges=n,expected=len(f['panel']),missing=missing,complete=complete,
            receipts=receipts,waived=sum(r['waived'] for r in receipts),final_rating=rating,
            total=rating,discover=0,solve=0))
    result=sorted(out,key=lambda row:(-row['final_rating'],row['pair']))
    for i,row in enumerate(result):
        row['rank']=i+1
        row['tie']=sum(other['final_rating']==row['final_rating'] for other in result)>1
    decision=f.get('decision') or {}
    if decision.get('fingerprint') == fingerprint(s,f):
        indices={pair:i for i,pair in enumerate(decision['order'])}
        result.sort(key=lambda r:indices[r['pair']])
        for i,row in enumerate(result):
            row['rank']=i+1; row['tie']=False; row['panel_decided']=True
    return result


def fingerprint(s,f):
    payload=[(row['id'],row['pair'],row['station'],row.get('final_rating',0)) for row in sheets(s,f)]
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def ties(ranked):
    groups={}
    for row in ranked:
        if row.get('complete'):
            groups.setdefault(row['final_rating'],[]).append(row['pair'])
    return [group for group in groups.values() if len(group)>1]


def state(s,f=None):
    f=get(s) if f is None else deepcopy(f)
    ranked=rows(s,f)
    current=next((row for row in ranked if row['pair']==current_pair(f)),{})
    unresolved=ties(ranked)
    decision=f.get('decision') or {}
    if decision.get('fingerprint') == fingerprint(s,f): unresolved=[]
    remaining=len(legacy_ballots(s,f))
    all_complete=bool(ranked) and all(row['complete'] for row in ranked) and not remaining
    case=case_for(s,f)
    result={k:v for k,v in f.items() if k not in ('teams','published_rows','decision','legacy_ballot_ids')}
    result.update(expected_judges=len(f.get('panel',[])),case_name=case['name'] if case else '',
        scenario=case['id'] if case else None,receipts=current.get('receipts',[]),missing=current.get('missing',[]),
        complete=bool(current.get('complete')),all_complete=all_complete,
        legacy_case_review_required=bool(remaining),legacy_ballots_remaining=remaining,
        legacy_case_warning=('Earlier Final sheets may use the previous shared case. Reassess each flagged sheet using this team’s original scenario, then submit a replacement before publishing.' if remaining else ''),
        unresolved_ties=unresolved,can_publish=f.get('status')=='review' and all_complete and not unresolved,
        published=f.get('status')=='completed',total=len(f.get('pairs',[])),stage_pair=current_pair(f))
    return result


def judge_view(s,station,f,pair=None):
    pair=current_pair(f) if pair is None else pair
    eligible=station in f.get('panel',[])
    flagged=set(f.get('legacy_ballot_ids',[]))
    done={r['pair'] for r in sheets(s,f) if r['station']==station and r['id'] not in flagged}
    needs_rescore=any(r['station']==station and r['pair']==pair for r in legacy_ballots(s,f))
    st=state(s,f)
    return {k:st[k] for k in ('active','status','run_id','current','total','stage_pair','published','case_name','scenario','legacy_case_review_required','legacy_case_warning') if k in st} | {
        'needs_rescore':needs_rescore,'eligible':eligible,'can_submit':eligible and f.get('status') in ENGAGED-{'completed'} and pair in f.get('visited',[]),
        'visited':[{'pair':p,'partner':team_for(s,f,p).get('partner','')} for p in f.get('visited',[])],
        'pending_assignments':[p for p in f.get('visited',[]) if p not in done] if eligible else []}


def public_view(s,f,names):
    visible=engaged(f)
    case=case_for(s,f) if visible else None
    ranked={row['pair']:row for row in rows(s,f)}
    teams=[]
    if engaged(f):
        for pair in f['pairs']:
            row=team_for(s,f,pair)
            teams.append(s.attendee_pair(row,names) | {'completed':ranked[pair]['complete'],
                'scenario':row.get('scenario'),'case_name':case_for(s,f,pair)['name']})
    return {'active':f.get('active',False),'status':f.get('status','idle'),'current':f.get('current',0),
        'pairs':teams,'case_name':case['name'] if case else '', 'scenario':case['id'] if case else None,
        'published':f.get('status')=='completed','stage_pair':current_pair(f) if visible else None}


def _reason(body,key='override_reason'):
    value=body.get(key)
    return value.strip() if isinstance(value,str) and 8<=len(value.strip())<=500 else None


def _barrier(s,f,team_ids,body):
    pending=[row for row in rows(s,f) if row['pair'] in team_ids and not row['complete']]
    if not pending:return None
    reason=_reason(body)
    if body.get('override_reason') and not reason:
        return {'error':'Record a clear reason of 8–500 characters for the missing sheets.'},400
    if not reason:
        return {'error':'Wait for the configured judges to submit, or record an exception.',
            'missing':sorted({st for row in pending for st in row['missing']}),
            'missing_teams':[{'pair':row['pair'],'missing':row['missing']} for row in pending],
            'needs_override':True},409
    # An exception never substitutes zero received scores for a result.
    if any(row['pair'] not in f.get('visited',[]) or row['judges']==0 for row in pending):
        return {'error':'Every finalist must have pitched and have at least one accepted sheet before an exception can be recorded.'},409
    for row in pending:
        waiver=f.setdefault('waivers',{}).setdefault(str(row['pair']),{})
        for st in row['missing']:
            waiver[str(st)]={'reason':reason,'at':s.time.time()}
    s.audit('desk','final-override',json.dumps({'run_id':f['run_id'],'pairs':team_ids,'reason':reason}))
    return None


def preliminary_order(s):
    return [row['pair'] for row in s.leaderboard()[:3]]


def action(s,body,actor='desk'):
    op=body.get('action'); f=get(s)
    if op=='set':
        if engaged(f):return {'error':'This Final has started. Its teams and judging panel are fixed.'},409
        raw=body.get('pairs',[]); panel=body.get('panel',s.event_config.config(s)['final_panel'])
        if not isinstance(raw,list) or len(raw)!=3 or any(type(p) is not int for p in raw):
            return {'error':'Choose exactly three finalist teams.'},400
        if len(set(raw))!=len(raw) or any(not 1<=p<=s.CFG['pairs'] for p in raw):
            return {'error':'Choose different finalist team numbers from this event.'},400
        if not isinstance(panel,list) or not panel or any(type(st) is not int or not 1<=st<=s.CFG['stations'] for st in panel) or len(set(panel))!=len(panel):
            return {'error':'Choose the eligible judges for this Final.'},400
        f={'active':False,'status':'prepared','pairs':raw,'current':0,'panel':panel,
            'run_id':uuid.uuid4().hex,'teams':{str(p):_team(s,p) for p in raw},'visited':[],
            'preliminary_order':preliminary_order(s),
            'waivers':{},'clock':s._fresh_clock(),'published':False}
        s.kv_set('reveal','hidden')
    elif op=='start':
        if f.get('status')!='prepared':return {'error':'Set the finalists and panel before starting the Final.'},409
        if any(st>s.CFG['stations'] for st in f['panel']) or any(p>s.CFG['pairs'] for p in f['pairs']):
            return {'error':'Event capacity changed. Prepare the finalists and panel again.'},409
        clocks = [s.clock_now(st) for st in (range(1, s.CFG['stations'] + 1) if s.mode() == 'self' else [None])]
        if any(raw['running'] and elapsed < s.FEEDBACK_END for raw, elapsed in clocks):
            return {'error': 'Pause or finish the preliminary pitch and feedback before starting the Final.'}, 409
        if 'preliminary_order' in f and f['preliminary_order'] != preliminary_order(s):
            return {'error': 'The preliminary qualifiers changed. Review the scores and save the finalist selection again.',
                    'code': 'preliminary_order_changed'}, 409
        registered = [row for row in s.preliminary_receipts() if row['registered']]
        changes, error = s.review_preliminary(registered, body, 'start-final', [f['run_id'], [raw for raw, _ in clocks]], actor)
        if error:
            return error
        f.update(status='ready',active=True,current=0,visited=[f['pairs'][0]],clock=s._fresh_clock())
        s.write_judging_state({'final': f, 'calib': {'active': False}, **changes})
        if changes:
            s.audit(actor, 'preliminary-exception', 'start-final: ' + body['override_reason'].strip())
        s.clear_room_shares()
    elif op in ('next','goto'):
        if f.get('status') not in ENGAGED-{'completed'}:return {'error':'Start the Final before selecting a finalist.'},409
        index=f['current']+1 if op=='next' else body.get('index')
        if type(index) is not int or not 0<=index<len(f['pairs']):return {'error':'There is no finalist at that position.'},409
        if index!=f['current']:
            if s.final_clock_public(f)['running']:return {'error':'Finish or pause the current pitch before moving to another team.'},409
            barrier=_barrier(s,f,[current_pair(f)],body)
            if barrier:return barrier
            f.update(current=index,status='ready',active=True,clock=s._fresh_clock())
            if f['pairs'][index] not in f['visited']:f['visited'].append(f['pairs'][index])
    elif op in ('stop','review'):
        if f.get('status') not in ENGAGED-{'completed'}:return {'error':'Start the Final before reviewing results.'},409
        if s.final_clock_public(f)['running']:return {'error':'Finish or pause the pitch before reviewing results.'},409
        barrier=_barrier(s,f,f['pairs'],body)
        if barrier:return barrier
        _,elapsed,_=s.final_clock(f,s.time.time())
        f.update(active=False,status='review',clock=dict(s._fresh_clock(),elapsed=elapsed,ended=True))
    elif op=='resolve':
        if f.get('status')!='review' or not state(s,f)['all_complete']:
            return {'error':'Collect all required sheets and open results review first.'},409
        reason=_reason(body,'reason'); order=body.get('order')
        if not reason or not isinstance(order,list) or any(type(p) is not int for p in order) or sorted(order)!=sorted(f['pairs']):
            return {'error':'Provide each finalist once in the agreed order and record the panel decision (8–500 characters).'},400
        ranked=rows(s,f); by={row['pair']:row for row in ranked}
        values=[by[p]['final_rating'] for p in order]
        if values!=sorted(values,reverse=True):return {'error':'Only reorder teams that remain tied on the panel’s average Final rating.'},400
        f['decision']={'order':order,'reason':reason,'at':s.time.time(),'fingerprint':fingerprint(s,f)}
    elif op=='publish':
        if body.get('confirm')!='PUBLISH':return {'error':'Confirm PUBLISH after reviewing the final results.'},400
        if not state(s,f)['can_publish']:return {'error':'Results require all expected sheets, results review and a recorded decision for remaining ties.'},409
        f['published_rows']=deepcopy(rows(s,f)); f['published_at']=s.time.time()
        f.update(active=False,status='completed',published=True)
        s.kv_set('reveal','winners')
        awards = next((i for i, item in enumerate(s.agenda()) if item.get('scene') == 'awards'), None)
        if awards is None:
            s.program_scene('awards')
        else:
            s.select_program(awards)
    elif op=='reopen':
        reason=_reason(body,'reason')
        if not reason:return {'error':'Record why the published results need correction (8–500 characters).'},400
        if f.get('status')!='completed':return {'error':'Only published results need reopening.'},409
        f.setdefault('publication_history',[]).append({'at':f.get('published_at'),'rows':f.get('published_rows',[]),'reopened_at':s.time.time(),'reason':reason})
        f.pop('published_rows',None);f.pop('decision',None)
        f.update(status='review',active=False,published=False)
        s.kv_set('reveal','hidden')
        pr=s.program_now();pr['scene']='auto';s.kv_set('program',pr)
    else:return {'error':'Choose a valid Final action.'},400
    if op == 'publish':
        from event_push import enqueue
        enqueue(s, 'results', stored=('final', f))
    elif op != 'start':
        s.kv_set('final',f)
    s.audit(actor,'final-'+str(op),json.dumps({k:v for k,v in body.items() if k in ('pairs','panel','index','reason')}))
    return state(s,f),200


def before_submit(s,f,station,body):
    if station not in f.get('panel',[]):return {'error':'You are not assigned to this Final panel.'},403
    if f.get('status') not in ENGAGED-{'completed'}:return {'error':'Final scoring is closed. Ask the Desk to reopen results if a correction is needed.'},409
    if not body.get('run_id') or body.get('run_id')!=f.get('run_id'):
        return {'error':'This sheet has no matching Final run. Keep or export the saved sheet, reload the updated app and contact the Desk.'},409
    pair=body.get('pair')
    if type(pair) is not int or pair not in f.get('visited',[]):return {'error':'This team has not pitched in this Final.'},409
    return None


def invalidate_decision(s,f):
    if f.get('decision'):
        f.pop('decision',None);s.kv_set('final',f)
