"""Workspace-local readiness, participant assistance and operational handovers."""
from copy import deepcopy
import hashlib
import json
import time
import uuid
import event_commands

KEY='event_operations_v1'
STAFF={'admin','organizer','scoring','registration','desk'}
CATEGORIES={'arrival':'Registration or team','access':'Sign-in or phone','judging':'Judging or station','event':'Event instructions'}
ROUTES={'arrival':{'registration','organizer','admin','desk'},'access':{'registration','organizer','admin','desk'},
        'judging':{'scoring','organizer','admin','desk'},'event':{'organizer','admin','desk'}}
CHECKS={'screen':'Room screen checked','wifi':'Venue Wi-Fi checked','phone':'Participant phone journey checked'}

class Conflict(ValueError): pass

def role(ident): return ident.get('dashboard') or ident['role']
def store(s): return deepcopy(s.kv_get(KEY) or {'revision':0,'requests':[],'notes':[],'checks':{}})
def stamp(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def clean(text,limit):
    if not isinstance(text,str) or not text.strip() or len(text)>limit: raise ValueError(f'Enter between 1 and {limit} characters.')
    return text.strip()

def subject(s,ident):
    assigned=s.event_access.participant_assignment(s,ident)
    if not assigned: raise ValueError('Ask Registration to check your participant assignment.')
    assigned=dict(assigned,member=ident['member'])
    return stamp([ident.get('account_id') or ident['credential_id'],s.event_access.active_dataset(s),
                  assigned['pair'],assigned['member'],assigned.get('name')]),assigned

def public_request(item):
    return {k:item[k] for k in ('id','category','message','status','reply','created','updated')}

def participant_state(s,ident):
    owner,_=subject(s,ident)
    return {'requests':[public_request(r) for r in store(s)['requests'] if r['owner']==owner][-20:],
            'categories':CATEGORIES,'notice':'For urgent help, go directly to the Event Desk. This is not an emergency channel.'}

def readiness(s):
    c=s.event_config.config(s,read_only=True);rows=s.event_config.list_roster(s)
    named=sum(bool(p['name'].strip()) for r in rows for p in r['members'])
    auth=s.event_access.auth_db(s);dataset=s.event_access.active_dataset(s)
    assigned={r[0] for r in auth.execute("SELECT DISTINCT g.station FROM account_grants g JOIN person_accounts a ON a.id=g.account_id "
        "WHERE a.active=1 AND g.role='judge' AND g.dataset=? AND EXISTS(SELECT 1 FROM access_credentials c WHERE c.account_id=a.id AND c.active=1)",(dataset,))}
    seen={r[0] for r in auth.execute("SELECT DISTINCT station FROM role_sessions WHERE role='judge' AND dataset=? AND expires>?",(dataset,time.time()))}
    issued=auth.execute("SELECT COUNT(*) FROM access_credentials WHERE active=1 AND role='participant' AND dataset=?",(dataset,)).fetchone()[0]
    used=auth.execute("SELECT COUNT(*) FROM access_credentials WHERE active=1 AND role='participant' AND dataset=? AND last_used IS NOT NULL",(dataset,)).fetchone()[0]
    attendees=s.kv_get('attendees') or {};checkins=sum(bool(r.get('checked_in')) for r in attendees.get('participants',{}).values())
    missing=sorted(set(range(1,c['judge_count']+1))-assigned)
    warnings=s.event_config.schedule_warnings(s,c)
    sheets=s.DB.execute("SELECT COUNT(DISTINCT pair) FROM sheets WHERE kind='round' AND voided=0").fetchone()[0]
    return {'capacity':c['participant_count'],'participants':named,'teams':len(rows),'team_capacity':c['team_count'],
            'checked_in':checkins,'judge_count':c['judge_count'],'missing_judge_stations':missing,
            'signed_in_judge_stations':len(seen),'participant_passwords':issued,'participant_access_used':used,
            'missing_judge_count':len(missing),'participant_passwords_unused':max(0,issued-used),
            'scored_teams':sheets,'running_clocks':sum(bool(json.loads(r[0]).get('running')) for r in s.DB.execute("SELECT v FROM kv WHERE k='clock' OR k LIKE 'sclock:%'")),
            'schedule_warnings':warnings,'workspace':dataset}

def current_request(s,item):
    rows=s.DB.execute('SELECT name FROM roster_members WHERE pair=? AND member=?',(item['pair'],item['member'])).fetchone()
    return bool(rows and rows[0]==item['name'])

def snapshot(s,ident):
    r=role(ident);data=store(s);facts=readiness(s);pr=s.program_now();agenda=s.agenda()
    requests=[dict(public_request(x),team=x['pair'],member=x['member'],name=x['name'],claimed_by=x.get('claimed_by',''))
              for x in data['requests'] if r in ROUTES[x['category']] and current_request(s,x)]
    result={'revision':data['revision'],'facts':facts,'requests':requests,'notes':[n for n in data['notes'] if n['role'] in (r,'organizer','desk','admin') or r in ('admin','organizer','desk')][-20:],
            'checks':{k:{'label':label,**data['checks'].get(k,{'done':False})} for k,label in CHECKS.items()},
            'program':{'index':pr['i'],'title':agenda[pr['i']]['title'] if agenda else '',
                       'next':agenda[pr['i']+1]['title'] if pr['i']+1<len(agenda) else None},
            'agenda':[{'index':i,'title':x['title'],'time':x.get('time'),'scene':x.get('scene','info')} for i,x in enumerate(agenda)],
            'can_change_phase':r in ('organizer','desk'),'can_check_venue':r in ('admin','organizer','desk'),
            'updated_at':time.time()}
    if r=='registration':
        for key in ('scored_teams','running_clocks','signed_in_judge_stations'):result['facts'].pop(key,None)
    from event_push import report
    result['push'] = report(s)
    if r=='admin':
        result['mail']={row[0]:row[1] for row in s.event_access.auth_db(s).execute('SELECT status,COUNT(*) FROM notifications GROUP BY status')}
    return result

def ai_projection(s,r):
    if r=='administration':r='admin'
    if r not in STAFF:return {}
    data=snapshot(s,{'role':r})
    facts=dict(data['facts']);facts['missing_judge_station_numbers']=facts.pop('missing_judge_stations')
    return {'facts':facts,'program':data['program'],
        'help_counts':{status:sum(x['status']==status for x in data['requests']) for status in ('open','claimed','resolved','cancelled')},
        'help_categories':{cat:sum(x['category']==cat and x['status'] in ('open','claimed') for x in data['requests']) for cat in CATEGORIES},
        'pending_venue_checks':[v['label'] for v in data['checks'].values() if not v['done']],
        'handover_note_count':len(data['notes']),'updated_at':data['updated_at'],
        'evidence_note':'Assignments and sign-ins are distinct. Venue checks are staff confirmations, not device telemetry. No inbox delivery/read confirmation is available.'}

def phase_preview(s,index):
    agenda=s.agenda()
    if type(index) is not int or not 0<=index<len(agenda):raise ValueError('Choose a current agenda section.')
    facts=readiness(s);target=agenda[index];blockers=[]
    if facts['running_clocks']:blockers.append('Pause judging clocks in Judging Operations before changing section.')
    if target.get('scene') in ('pitch','final'):
        if not facts['participants']:blockers.append('Register participants before beginning this section.')
        if facts['missing_judge_stations']:blockers.append('Assign judges to all stations before beginning this section.')
    if s.final_event.engaged((s.kv_get('final', {}) or {})) and target.get('scene')=='pitch':blockers.append('The Final has begun. Resolve its state in Judging Operations first.')
    facts_for_digest={**facts,'checks':store(s)['checks'],'final':(s.kv_get('final', {}) or {})}
    digest=stamp([s.WORKSPACE.context,s.kv_get('program'),agenda,index,facts_for_digest])
    return {'plan_digest':digest,'index':index,'target':target['title'],'time':target.get('time'),
            'blockers':blockers,'warnings':facts['schedule_warnings'],
            'effects':['Change the current agenda section for the room and participant pages.',
                       'Judging timers, question sending and result publication remain separate controls.']}

def command(s,ident,body):
    r=role(ident);action=body.get('action');data=store(s)
    reply_owner = None
    if r=='participant':
        owner,assigned=subject(s,ident)
        if action=='request_help':
            category=body.get('category');message=clean(body.get('message'),500)
            if category not in CATEGORIES:raise ValueError('Choose a help category.')
            request_id=str(uuid.UUID(str(body.get('request_id',''))))
            previous=next((x for x in data['requests'] if x['owner']==owner and x['request_id']==request_id),None)
            if previous:
                if (previous['category'],previous['message'])!=(category,message):raise Conflict('This request was already sent with different details.')
                return participant_state(s,ident)
            if sum(x['owner']==owner and x['status'] in ('open','claimed') for x in data['requests'])>=3:raise ValueError('You already have three open requests. Visit the Event Desk for urgent help.')
            if len(data['requests'])>=2000:raise ValueError('The help queue is full. Visit the Event Desk.')
            now=time.time();data['requests'].append({'id':uuid.uuid4().hex,'request_id':request_id,'owner':owner,
                'pair':assigned['pair'],'member':assigned['member'],'name':assigned.get('name','Participant'),
                'category':category,'message':message,'status':'open','reply':'','created':now,'updated':now})
        elif action=='cancel_help':
            item=next((x for x in data['requests'] if x['id']==body.get('id') and x['owner']==owner),None)
            if not item:raise ValueError('This help request is unavailable.')
            if item['status'] not in ('open','claimed'):raise Conflict('This request has already been closed.')
            item.update(status='cancelled',updated=time.time())
        else:raise PermissionError('This action is not available to participants.')
    else:
        if action in ('preview_phase','apply_phase'):
            if r not in ('organizer','desk'):raise PermissionError('Open Event Operations to change the programme.')
            plan=phase_preview(s,body.get('index'))
            if action=='preview_phase':return plan
            if body.get('plan_digest')!=plan['plan_digest']:raise Conflict('The event changed. Review a fresh section preview.')
            if plan['blockers']:raise Conflict('Resolve the preview blockers before changing section.')
            if body.get('confirm')!='CHANGE SECTION':raise ValueError('Confirm CHANGE SECTION.')
            event_commands._program_select(s,{'action':'set','id':event_commands._agenda_items(s.CONTENT)[body['index']]['id']},'staff:'+str(ident.get('credential_id','')),None)
            return snapshot(s,ident)
        if body.get('revision')!=data['revision']:raise Conflict('Another staff member updated this view. Refresh and try again.')
        if action=='help_update':
            item=next((x for x in data['requests'] if x['id']==body.get('id')),None)
            if not item or not current_request(s,item) or r not in ROUTES[item['category']]:raise PermissionError('This request belongs to another desk.')
            status=body.get('status')
            if status not in ('claimed','resolved','open'):raise ValueError('Choose Open, Claimed or Resolved.')
            if item['status']=='cancelled':raise Conflict('The participant cancelled this request.')
            reply=body.get('reply','')
            if reply:reply=clean(reply,500)
            if reply and reply != item['reply']:
                reply_owner = item['owner']
            item.update(status=status,reply=reply,updated=time.time(),claimed_by=ident.get('label','Staff') if status=='claimed' else item.get('claimed_by',''))
        elif action=='handover':
            note=clean(body.get('note'),800)
            data['notes'].append({'id':uuid.uuid4().hex,'note':note,'role':r,'author':ident.get('label','Staff'),'created':time.time()});data['notes']=data['notes'][-100:]
        elif action=='check':
            if r not in ('admin','organizer','desk'):raise PermissionError('Ask the Organizer to confirm venue checks.')
            key=body.get('key')
            if key not in CHECKS or type(body.get('done')) is not bool:raise ValueError('Choose a valid venue check.')
            data['checks'][key]={'done':body['done'],'by':ident.get('label','Staff'),'at':time.time()}
        else:raise ValueError('Choose a supported operations action.')
    data['revision'] += 1
    if reply_owner:
        from event_push import enqueue
        enqueue(s, 'help', owner=reply_owner, stored=(KEY, data))
    else:
        s.kv_set(KEY,data)
    s.audit(r,'operations-'+action,json.dumps({'request_id':body.get('id'),'category':body.get('category')}))
    return participant_state(s,ident) if r=='participant' else snapshot(s,ident)

def handle(s,h,path,body=None):
    if path not in ('/api/event-operations','/api/participant/help'):return False
    ident=s.event_access.session(s,h.headers)
    if not ident:h.send(401,{'error':'Sign in to continue.'});return True
    r=role(ident);allowed=r=='participant' if path.endswith('/help') else r in STAFF
    if not allowed:h.send(403,{'error':'This view is not included in your dashboard.'});return True
    if body is not None and ident.get('impersonation'):
        h.send(403,{'error':'Leave profile preview before changing help or event operations.'});return True
    try:
        with s.LOCK:
            result=command(s,ident,body) if body is not None else participant_state(s,ident) if r=='participant' else snapshot(s,ident)
        h.send(200,result)
    except PermissionError as e:h.send(403,{'error':str(e)})
    except Conflict as e:h.send(409,{'error':str(e)})
    except (ValueError,TypeError) as e:h.send(400,{'error':str(e)})
    return True
