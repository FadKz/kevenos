import React, {useEffect, useRef, useState} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {createPortal} from 'react-dom';

type Choice = {key:string; text:string; count?:number};
type Run = {id:string; qid:string; prompt:string; options:Choice[]; points:number; kind:string; status:string;
  end:number|null; seconds:number; start?:number; session?:string; progress?:{position:number;total:number}; phase?:string; catchup?:boolean; prediction?:boolean; can_resend?:boolean; resend_blocked_reason?:string|null; room_results_ready?:boolean; can_show?:boolean; show_blocked_reason?:string|null; category?:string; selected?:string; correct?:string[]; keys?:string[]; explanation?:string; responses?:number; private?:boolean; bars?:Choice[]; released?:boolean};
type Question = {id:string; phase:string; prompt:string; options:Choice[]; type:string; points:number; seconds:number; priority:string;
  correct:string|null; explanation:string; cue:string; time:string; due:number|null; schedules?:{phase:string;opens:string;closes:string;points:number}[];
  can_send?:boolean; can_send_room?:boolean; send_blocked_reason?:string|null; cue_offset_minutes?:number};
type Score = {earned:number; available:number; pending:number};
type Standing = Score & {rank:number; name:string; pair:number; partner:string};
type Average = {name:string; count:number; average:number};
type Operator = {quiet?:boolean; invite_sent?:boolean; catchup_open?:boolean; can_champion?:boolean; champion_blocked_reason?:string|null;
  can_catchup?:boolean; catchup_blocked_reason?:string|null; can_standings?:boolean; standings_blocked_reason?:string|null;
  can_predictions?:boolean; predictions_blocked_reason?:string|null; can_people_choice?:boolean; people_choice_blocked_reason?:string|null;
  can_show_hall?:boolean; hall_blocked_reason?:string|null; can_show_champion?:boolean; show_champion_blocked_reason?:string|null};
type ResetArchive = {id:string;at:number;reason:string;run_count:number;answer_count:number};
type CueTime = {phase:string;time:string;offset_minutes?:number};
type RetimePreview = {rev:number;changes:{qid:string;prompt:string;before:CueTime;after:CueTime}[]};
type DeskState = {enabled:boolean; rev:number; now:number; phase:string; title:string; bank:Question[]; runs:Run[];
  can_spotlight?:boolean; individual_results_restricted?:boolean; operator?:Operator; phase_baselines?:Record<string,string>;
  reset_archives?:ResetArchive[]; restorable_reset_id?:string|null;
  standings:Standing[]; activity:{at:number;action:string;detail:string}[]; analytics:{summary:{joined:number;viewers:number;respondents:number;answers:number;released:number;skipped:number;voided:number};teams?:Average[]; companies?:Average[]; confidence:{before:number|null;after:number|null};
    competencies:{company:string;skills:{name:string;accuracy:number}[]}[];
    questions:Record<string,{correctPercent:number|null;medianSeconds:number|null;commonWrong:string|null;viewers:number;completion:number|null}>}; learning:{matched:number;before:number;after:number}; hall:{kind:string;id?:string}|null; hall_visible:boolean; release:unknown; prize:string};
type PlayerState = {phase?:string; enabled:boolean; joined:boolean; invited:boolean; busy:boolean; catchup_allowed?:boolean; now:number; questions:PlayerRun[]; history:PlayerRun[]; score:Score};
type Payload = Record<string,unknown>;
type Read = (path:string)=>Promise<any>;
type Write = (path:string, body:Payload)=>Promise<any>;

declare const KEV: {
  messages?:{replace(source:string, items:object[]):void};
  api(path:string,options?:{body:Payload}):Promise<PlayerState>;
  download(path:string,filename?:string):Promise<void>;
};
declare global { interface Window { EventQuiz:any } }
const deskPath = '/api/desk/quiz';
const playerPath = '/api/participant/quiz';
const actionMessage = (error:unknown) => error instanceof Error ? error.message : 'Try again.';
const clock = (seconds:number) => {const n=Math.max(0,Math.ceil(seconds)); return Math.floor(n/60)+':'+String(n%60).padStart(2,'0');};
const phases = ['registration','welcome','mac','steps','demo','prep','rounds','break','final','awards','lunch'];
const phaseNames = ['Registration','Welcome','Solution at Work','Four pitch steps','Live pitch','Pitch Prep','Preliminary rounds','Scoring break','The Final','Awards','Lunch'];

function useTime(server:number|undefined) {
  const [current,setCurrent]=useState(0);
  useEffect(()=>{
    if (!server) return;
    const start=performance.now();
    setCurrent(server);
    const timer=setInterval(()=>setCurrent(server+(performance.now()-start)/1000),300);
    return ()=>clearInterval(timer);
  },[server]);
  return current;
}

function Button({children,click,disabled=false,primary=false}:{children:React.ReactNode;click:()=>void;disabled?:boolean;primary?:boolean}) {
  return <button type="button" className={'btn '+(primary?'':'ghost')} disabled={disabled} onClick={click}>{children}</button>;
}

function quizTheme(category?:string) {
  switch (category) {
  case 'Qualification': return {name:'Pitch skills',style:'pitch'};
  case 'solution device and product vendor knowledge': return {name:'Solution at Work',style:'mac'};
  case 'Attention': return {name:'Event recap',style:'recap'};
  case 'Trivia': return {name:'Trivia',style:'trivia'};
  case 'Fun': return {name:'Audience vote',style:'vote'};
  case 'Pulse': return {name:'Quick poll',style:'pulse'};
  default: return {name:'Event quiz',style:'general'};
  }
}

type PlayerRun = Run & {result_state?:ResultState; earned?:number|null};
type ResultState = 'awaiting_close'|'awaiting_release'|'awaiting_post_test'|'awaiting_outcome'|'ready'|'cancelled';

function resultState(run:PlayerRun):ResultState {
  if (run.result_state) return run.result_state;
  if (run.status==='void') return 'cancelled';
  if (run.correct || run.explanation) return 'ready';
  if (run.kind==='warmup') return 'awaiting_post_test';
  if (run.prediction || ['F1','R3b'].includes(run.qid)) return 'awaiting_outcome';
  return run.status==='open' && !['post','waiting'].includes(run.kind) ? 'awaiting_close' : 'awaiting_release';
}

function resultTiming(run:PlayerRun) {
  switch (resultState(run)) {
  case 'awaiting_close': return 'Results appear after the question closes.';
  case 'awaiting_post_test': return 'Practice results appear after the event team releases the post-test answers.';
  case 'awaiting_outcome': return 'Results appear after the event team confirms the outcome and releases the prediction.';
  case 'awaiting_release': return run.qid==='F2' ? 'Results appear after the event team shares People’s Choice during Awards.' :
    'Results appear when the event team releases the answers.';
  case 'cancelled': return 'This question was cancelled and won’t count toward quiz points.';
  case 'ready': return 'Quiz result available.';
  }
}

function PlayerResults({runs,score,joined}:{runs:PlayerRun[];score:Score;joined:boolean}) {
  return <>
    {joined && <p className="quiz-results-points"><strong>{score.earned}</strong> quiz {score.earned===1?'point':'points'} so far</p>}
    <p>These results are for the optional quiz. Pitch scores are available in the participant Results tab.</p>
    {runs.length===0 && <p>Quiz answers will appear here as they’re submitted or released.</p>}
    <ol className="quiz-history">{[...runs].reverse().map(run=>{
      const state=resultState(run);
      const ready=state==='ready';
      const correct=ready ? run.options.filter(option=>run.correct?.includes(option.key)) : [];
      const chosen=run.options.find(option=>option.key===run.selected);
      const labels:Record<ResultState,string>={awaiting_close:'Answer saved',awaiting_release:'Awaiting release',
        awaiting_post_test:'Awaiting post-test results',awaiting_outcome:'Awaiting outcome',ready:'Not scored',cancelled:'Cancelled'};
      const label=ready && !chosen ? 'No answer' : ready && correct.length ?
        run.correct?.includes(chosen?.key || '') ? 'Correct' : 'Incorrect' : labels[state];
      return <li key={run.id}>
        <div className="quiz-player-result-top"><strong>{run.prompt}</strong><span className="quiz-player-result-label" data-state={state}>{label}</span></div>
        {chosen && <p>Your answer: {chosen.text}</p>}
        {ready && correct.length>0 && <p>{correct.length>1?'Correct answers':'Correct answer'}: {correct.map(option=>option.text).join(' / ')}</p>}
        {ready && run.points>0 && run.earned!=null && <p>{run.earned} of {run.points} quiz {run.points===1?'point':'points'}</p>}
        {run.kind==='warmup' && <small>Practice · No points</small>}
        {!ready && <p>{resultTiming(run)}</p>}
        {ready && run.explanation && (run.explanation.length>160 ?
          <details className="quiz-player-explanation"><summary>Why this answer?</summary><p>{run.explanation}</p></details> :
          <p className="quiz-player-explanation">{run.explanation}</p>)}
      </li>;
    })}</ol>
  </>;
}

function PlayerQuiz({action}:{action:string}) {
  const [data,setData]=useState<PlayerState>();
  const [selection,setSelection]=useState<{id:string;key:string}>();
  const [notice,setNotice]=useState('');
  const [error,setError]=useState('');
  const [sending,setSending]=useState(false);
  const [stale,setStale]=useState(false);
  const viewed=useRef(new Set<string>());
  const unconfirmed=useRef<{id:string;key:string}|undefined>(undefined)
  const savedAnswer=useRef<string|undefined>(undefined);
  const order=useRef(0);
  const posting=useRef(false);
  const current=useTime(data?.now);

  useEffect(()=>{
    let active=true;
    let reading=false;
    async function poll() {
      if (document.hidden || reading || posting.current) return;
      reading=true;
      const seq=++order.current;
      try {
        const value:PlayerState=await KEV.api(playerPath);
        if (!active || seq!==order.current) return;
        const attempt=unconfirmed.current;
        if (!value.enabled) {
          savedAnswer.current=undefined;
          setNotice('');
          setError('');
          setSelection(undefined);
          unconfirmed.current=undefined;
        } else if (attempt) {
          if (value.history?.some(item=>item.id===attempt.id && item.selected===attempt.key)) {
            savedAnswer.current=attempt.id;
            setNotice('Answer saved.');
            setSelection(undefined);
            setError('');
          } else if (value.questions?.[0]?.id!==attempt.id) {
            setError('This question closed before we could confirm your answer.');
          } else {
            setError('Your answer wasn’t confirmed. Submit it again to retry.');
          }
          unconfirmed.current=undefined;
        }
        if (savedAnswer.current && !value.history?.some(item=>item.id===savedAnswer.current && item.status!=='void')) {
          savedAnswer.current=undefined;
          setNotice('');
        }
        setData(value);
        setStale(false);
      } catch {
        if (active && seq===order.current) setStale(true);
      } finally {reading=false;}
    }
    void poll();
    const timer=setInterval(poll,1200);
    document.addEventListener('visibilitychange',poll);
    return ()=>{
      active=false;
      order.current++;
      clearInterval(timer);
      document.removeEventListener('visibilitychange',poll);
    };
  },[]);

  const candidate=data?.questions?.[0];
  const run=candidate && (candidate.end===null || candidate.end>(current || data?.now || 0)) ? candidate : undefined;
  const catchup=data?.catchup_allowed && (!run || run.kind==='waiting');
  const blocked=Boolean(data?.busy) || (action==='paused' && !catchup) ||
    ['pitch','feedback','final_ready','final_pitch','final_paused','final_feedback'].includes(action);
  const id=run?.id;

  useEffect(()=>{
    const card=document.getElementById('event-quiz-player');
    if (!id || blocked || stale || !card || viewed.current.has(id)) return;
    function view() {
      if (!id || document.hidden || !card?.getClientRects().length || viewed.current.has(id)) return;
      viewed.current.add(id);
      void KEV.api(playerPath,{body:{action:'view',id}}).catch(()=>viewed.current.delete(id));
    }
    if (typeof IntersectionObserver==='undefined') {
      view();
      return;
    }
    const observer=new IntersectionObserver(entries=>{
      if (entries.some(entry=>entry.isIntersecting)) view();
    });
    observer.observe(card);
    return ()=>observer.disconnect();
  },[id,blocked,stale]);

  useEffect(()=>{
    const items:object[]=[];
    if (data?.enabled) {
      if (run && !blocked && !stale) {
        const key=run.progress ? run.session || run.id : run.id;
        items.push({id:'quiz:'+key,title:run.prompt,label:'Quiz question',state:'open',auto:true,root:'event-quiz-player'});
      } else if (!blocked && !stale && !data.joined && data.invited) {
        items.push({id:'quiz:invite',title:'Join the quiz',label:'Quiz invitation',state:'open',auto:true,root:'event-quiz-player'});
      }
      for (const item of data.history.slice(-20).reverse()) {
        if (item.id===run?.id) continue;
        const ready=resultState(item)==='ready';
        items.push({id:'quiz:'+item.id,title:item.prompt,label:'Quiz question',
          state:item.selected?'answered':'closed',results:ready});
        if (ready) items.push({id:'quiz-result:'+item.id,title:item.prompt,label:'Quiz result',results:true,text:'Results are available.'});
      }
    }
    KEV.messages?.replace('quiz',items);
  },[data,run?.id,blocked,stale]);

  async function send(body:Payload) {
    if (posting.current || stale || blocked) return;
    posting.current=true;
    setSending(true);
    setError('');
    const seq=++order.current;
    try {
      const value:PlayerState=await KEV.api(playerPath,{body});
      if (seq!==order.current) return;
      setData(value);
      if (body.action==='answer') {
        unconfirmed.current=undefined;
        savedAnswer.current=typeof body.id==='string' ? body.id : undefined;
        setSelection(undefined);
        setNotice('Answer saved.');
      } else if (body.action==='join') {
        setNotice('You joined the quiz.');
      } else {
        setNotice('');
      }
    } catch(reason) {
      if (seq!==order.current) return;
      if (body.action==='answer' && run && typeof body.key==='string') {
        const status=(reason as {status?:number})?.status;
        if (!status || status>=500) {
          unconfirmed.current={id:run.id,key:body.key};
          setError('Checking whether your answer was saved…');
        } else {
          setError('Answer not saved. '+actionMessage(reason));
        }
        setStale(true);
      } else {
        setError(actionMessage(reason));
      }
    } finally {
      posting.current=false;
      if (seq===order.current) setSending(false);
    }
  }

  if (!data?.enabled) return null;
  const selected=selection?.id===run?.id ? selection?.key : undefined;
  const resultRoot=document.getElementById('event-quiz-results');
  const timeLeft=run?.end==null ? null : Math.max(0,run.end-(current || data.now));
  const duration=run?.end==null ? 0 : Math.max(1,run.end-(run.start ?? run.end-run.seconds));
  const theme=quizTheme(run?.category);
  const canJoin=!data.joined && data.invited;
  const cardVisible=!blocked && Boolean(run || canJoin || notice || error || stale);
  return <>
    {cardVisible && <section className="card event-section participant-question-card quiz-question" data-quiz-theme={theme.style} aria-label="Quiz question">
      <p role="status" className="participant-question-receipt">{notice}</p>
      {error && <p role="alert" className="quiz-player-feedback">{error}</p>}
      {stale && <p className="small mut">Reconnecting to the quiz…</p>}
      {run && <form key={run.id} data-open-question onSubmit={event=>{
        event.preventDefault();
        if (selected) void send({action:'answer',id:run.id,key:selected});
      }}>
        <div className="quiz-question-top">
          <span className="quiz-topic"><i className="quiz-topic-mark" aria-hidden="true"/>
            {run.kind==='warmup'?'Practice':theme.name}</span>
          {run.progress && <span className="quiz-position">{run.progress.position} of {run.progress.total}</span>}
          <span className="quiz-point-value">{run.points ? run.points+' '+(run.points===1?'point':'points') : 'No points'}</span>
        </div>
        {timeLeft!==null && <div className="quiz-timing" data-urgent={timeLeft<=10}>
          <progress max={duration} value={Math.min(duration,timeLeft)} aria-hidden="true"/>
          <span role="timer" aria-live="off">{clock(timeLeft)} left</span>
        </div>}
        <fieldset disabled={sending || stale}>
          <legend>{run.prompt}</legend>
          <div className="quiz-choices">{run.options.map((option,index)=><label className="quiz-choice" key={option.key}>
            <input type="radio" name="quiz-answer" checked={selected===option.key}
              onChange={()=>{setSelection({id:run.id,key:option.key});setNotice('');}}/>
            <span className="quiz-choice-key" aria-hidden="true">{String.fromCharCode(65+index)}</span>
            <span>{option.text}</span>
          </label>)}</div>
        </fieldset>
        <button type="submit" className="btn quiz-submit" disabled={!selected || sending || stale}>{sending?'Submitting…':'Submit answer'}</button>
      </form>}
      {!run && canJoin && <div><h3>Join the quiz</h3><p>Answer questions to earn points.</p>
        <div className="row"><Button primary click={()=>send({action:'join'})} disabled={sending||stale}>Join quiz</Button>
        <Button click={()=>send({action:'dismiss'})} disabled={sending||stale}>Not now</Button></div>
      </div>}
    </section>}
    {resultRoot && (data.history.length>0 || data.joined || !canJoin) && createPortal(
      <details className="card event-section participant-question-history">
        <summary>Quiz answers and points</summary>
        <PlayerResults runs={data.history} score={data.score} joined={data.joined}/>
        {!data.joined && !blocked && <Button primary click={()=>send({action:'join'})} disabled={sending||stale}>Join quiz</Button>}
        {data.joined && !blocked && <Button click={()=>send({action:'leave'})} disabled={sending||stale}>Leave quiz</Button>}
      </details>,resultRoot)}
  </>;
}

const editFields = {prompt:'Question',options:'Answer choices',correct:'Correct answer',points:'Points',seconds:'Timer',
  explanation:'Explanation',cue:'Presenter cue',priority:'Priority'} as const;
type EditField = keyof typeof editFields;
const sameValue = (a:Question[EditField],b:Question[EditField]) =>
  a === b || (Array.isArray(a) && Array.isArray(b) && JSON.stringify(a) === JSON.stringify(b));
const displayEditValue = (value:Question[EditField]) => Array.isArray(value) ? value.map(o=>o.key+' · '+o.text).join('; ') : (value ?? 'None');
type EditSession = {item:Question; rev:number};

function EditQuestion({session,latest,stale,save,rebase,cancel,busy}:{session:EditSession;latest?:Question;stale:boolean;
  save:(changes:Payload)=>void;rebase:()=>void;cancel:()=>void;busy:boolean}) {
  const {item}=session;
  const [draft,setDraft]=useState(item);
  const [overlap,setOverlap]=useState<EditField[]>([]);
  const field=(key:keyof Question,value:unknown)=>setDraft(d=>({...d,[key]:value}));
  const changed=(Object.keys(editFields) as EditField[]).filter(key=>!sameValue(draft[key],item[key]));
  const changes=Object.fromEntries(changed.map(key=>[key,draft[key]]));
  const fixed=['F1','F2','R3b'].includes(item.id);
  return <form className="quiz-editor" onSubmit={event=>{event.preventDefault();
    if (!stale && changed.length) save(changes);}}>
    <p>Changes apply the next time this question is sent.</p>
    {stale && <div><p role="status">The quiz changed while you were editing. Your draft is still here.</p>
      <Button disabled={!latest || busy} click={()=>{
        if (!latest) return;
        setOverlap(changed.filter(key=>!sameValue(item[key],latest[key]) && !sameValue(draft[key],latest[key])));
        setDraft({...latest,...changes});
        rebase();
      }}>Review latest and keep edits</Button></div>}
    {overlap.length>0 && <div role="status"><p>Another organizer changed the same fields. Review their saved values before saving your version.</p>
      <ul>{overlap.map(key=><li key={key}>{editFields[key]}: {displayEditValue(item[key])}</li>)}</ul></div>}
    <label>Question<textarea required maxLength={240} value={draft.prompt} onChange={e=>field('prompt',e.target.value)}/></label>
    <div className="quiz-edit-grid">
      <label>Priority<select value={draft.priority} onChange={e=>field('priority',e.target.value)}>
        <option value="core">Core</option><option value="optional">Optional</option>
      </select></label>
      <label>Points<input type="number" min={0} max={10} required disabled={['F1','F2'].includes(item.id)} value={draft.points}
        onChange={e=>field('points',Number(e.target.value))}/></label>
      {['F1','F2'].includes(item.id) && <label>Timer in seconds<input type="number" min={10} max={120} required value={draft.seconds} onChange={e=>field('seconds',Number(e.target.value))}/></label>}
    </div>
    <fieldset><legend>Answer choices</legend>{draft.options.map((option,i)=><label key={option.key}>Choice {option.key}
      <input required maxLength={150} value={option.text} disabled={item.id==='F1'||item.id==='F2'}
        onChange={e=>field('options',draft.options.map((o,j)=>i===j?{...o,text:e.target.value}:o))}/></label>)}</fieldset>
    {!fixed && <label>Correct answer<select value={draft.correct || ''} onChange={e=>field('correct',e.target.value||null)}>
      <option value="">Unscored</option>{draft.options.map(o=><option key={o.key} value={o.key}>{o.key} · {o.text}</option>)}</select></label>}
    <label>Explanation<textarea maxLength={500} value={draft.explanation} onChange={e=>field('explanation',e.target.value)}/></label>
    <label>Presenter cue<textarea maxLength={280} value={draft.cue} onChange={e=>field('cue',e.target.value)}/></label>
    <div className="row"><button className="btn" disabled={busy || stale || !changed.length}>Save draft</button><Button click={cancel}>Cancel</Button></div>
  </form>;
}

function AnswerBars({run}:{run:Run}) {
  if (!run.bars?.length) return null;
  return <ul className="quiz-answer-bars">{run.bars.map(option=>{
    const percent=run.responses ? Math.round(100*(option.count || 0)/run.responses) : 0;
    const correct=run.status==='closed' && run.released && run.keys?.includes(option.key);
    return <li key={option.key} data-correct={Boolean(correct)}>
      <div><span>{option.text}{correct && <small> · Correct</small>}</span><b>{option.count || 0} · {percent}%</b></div>
      <progress value={percent} max={100} aria-label={option.text+' share of responses'}/>
    </li>;
  })}</ul>;
}

const activityLabels:Record<string,string>={
  send:'Sent question',resend:'Resent question',settings:'Quiz setting changed',skip:'Skipped question',delay:'Delayed question',
  close:'Closed question',extend:'Extended timer',edit:'Edited draft',void:'Voided question',restore:'Restored question',
  invite:'Sent opt-in',warmup:'Opened warm-up',post:'Opened post-test',catchup:'Opened catch-up',
  close_warmup:'Closed warm-up',close_post:'Released post-test answers',close_waiting:'Closed waiting-time set',
  show:'Shared question on hall',hide:'Returned hall to programme',standings:'Shared standings',
  predictions:'Resolved predictions',people_choice:'Shared People’s Choice',prize:'Saved prize',
  champion:'Revealed champion',show_champion:'Displayed champion',
  reset:'Reset quiz activity',restore_reset:'Restored quiz activity',reset_question:'Reset question',retime:'Changed cue timing'
};
const quizTime=new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'UTC'});

function QuizAnalytics({data}:{data:DeskState}) {
  const [search,setSearch]=useState('');
  const [status,setStatus]=useState('all');
  const [selected,setSelected]=useState('');
  const summary=data.analytics.summary;
  const sorted=[...data.runs].reverse();
  const filtered=sorted.filter(run=>(status==='all' || run.status===status) &&
    (run.qid+' '+run.prompt).toLowerCase().includes(search.toLowerCase().trim()));
  const run=filtered.find(item=>item.id===selected) || filtered[0];
  const metrics=run && data.analytics.questions[run.id];
  return <div className="quiz-analytics">
    <section className="card quiz-desk-section" aria-labelledby="quiz-analytics-heading">
      <p className="event-kicker">Organizer only</p><h2 id="quiz-analytics-heading">Quiz analytics</h2>
      <div className="quiz-kpis">
        {[[summary.viewers,'Participants who saw a prompt'],[summary.respondents,'Participants who answered'],
          [summary.answers,'Accepted answers'],[summary.joined,'Current opt-ins']].map(([value,label])=>
          <div key={label}><strong>{value}</strong><span>{label}</span></div>)}
      </div>
      <p className="quiz-note">{data.runs.filter(run=>run.status==='closed'&&run.released).length} released · {summary.skipped} skipped · {summary.voided} cancelled</p>
    </section>
    <div className="quiz-console-grid">
      <section className="card quiz-desk-section" aria-labelledby="quiz-answer-analysis">
        <h2 id="quiz-answer-analysis">Answer breakdown</h2>
        <div className="quiz-edit-grid">
          <label>Find a question<input type="search" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Question text or ID"/></label>
          <label>Status<select value={status} onChange={e=>setStatus(e.target.value)}>
            <option value="all">All runs</option><option value="open">Open</option><option value="closed">Closed</option>
            <option value="skipped">Skipped</option><option value="void">Cancelled</option>
          </select></label>
        </div>
        {run ? <>
          <label>Question run<select value={run.id} onChange={e=>setSelected(e.target.value)}>
            {filtered.map(item=><option key={item.id} value={item.id}>{item.qid} · {item.kind} · {item.prompt}</option>)}
          </select></label>
          <div className="quiz-line"><span>{run.qid} · {run.status}</span>
            <span>{run.responses || 0} {run.status==='void'?'discarded':'accepted'} answers</span></div>
          <h3>{run.prompt}</h3>
          {metrics && ['open','closed'].includes(run.status) && <p>{metrics.viewers} unique {metrics.viewers===1?'viewer':'viewers'}
            {metrics.completion!=null && (' · '+metrics.completion+'% of viewers answered')}</p>}
          <AnswerBars run={run}/>
          {run.status==='closed' && <>
            {metrics?.correctPercent!=null && <p>{metrics.correctPercent}% correct among respondents</p>}
            {metrics?.medianSeconds!=null && <p>Median answer received {metrics.medianSeconds}s after opening</p>}
            {metrics?.commonWrong && <p>Most common incorrect choice · {metrics.commonWrong}</p>}
          </>}
        </> : <p>No question runs match this view.</p>}
      </section>
      <section className="card quiz-desk-section" aria-labelledby="quiz-activity-heading">
        <h2 id="quiz-activity-heading">Desk activity</h2>
        {data.activity.length ? <ol className="quiz-activity">{data.activity.map((entry,i)=><li key={i}>
          <time dateTime={new Date(entry.at*1000).toISOString()}>{quizTime.format(entry.at*1000)}</time>
          <div><b>{activityLabels[entry.action] || entry.action}</b>{entry.detail && <small>{entry.detail}</small>}</div>
        </li>)}</ol> : <p>Desk actions will appear here.</p>}
      </section>
    </div>
    {data.learning.matched>0 && <section className="card quiz-desk-section"><h2>Learning gain</h2>
      <p>{data.learning.before}% before · {data.learning.after}% after · {data.learning.matched} matched person-question answers.</p>
    </section>}
  </div>;
}

type View = 'Run quiz'|'Question editor'|'Timeline'|'Results'|'Setup';
type Confirmation = {
  action:string; title:string; description:string; label:string; details?:Payload;
  phrase?:string; dismiss?:string; danger?:boolean; rev:number;
};
type Send = (action:string,details?:Payload,revision?:number)=>Promise<boolean>;
type Ask = (confirmation:Omit<Confirmation,'rev'>)=>void;
type DeskActions = {data:DeskState; send:Send; ask:Ask; blocked:boolean; timing:Payload};

const individualKinds = ['live','pulse','vote','waiting'];
const sectionName = (phase:string) => phaseNames[phases.indexOf(phase)] || 'Event programme';
const manualCue = (question:Question) => !question.schedules && question.type!=='reveal' && question.id!=='P00';
const responseCount = (run:Run) => (run.responses || 0) + (run.responses===1 ? ' response' : ' responses');
const runState = (run:Run) => run.status==='open' ? 'Accepting answers' : run.status==='void' ? 'Cancelled' :
  run.status==='skipped' ? 'Skipped' : run.released ? 'Released' : 'Closed · awaiting release';
const setNames:Record<string,string> = {warmup:'Practice warm-up',post:'Post-test',waiting:'Waiting-time questions'};
const resultKinds:Record<string,string> = {...setNames,live:'Live question',pulse:'Poll',vote:'Audience vote',skip:'Skipped cue'};
const activeSet = (runs:Run[],kind:string) => runs.filter(run=>run.kind===kind && ['open','closed'].includes(run.status));
const minuteOfDay = (time:string) => {
  const [hour,minute]=time.split(':').map(Number);
  return hour*60+minute;
};
const timeAtMinute = (minute:number) => [Math.floor(minute/60),minute%60].map(value=>String(value).padStart(2,'0')).join(':');
const offsetFor = (phase:string,time:string,baselines:Record<string,string>) =>
  baselines[phase] ? minuteOfDay(time)-minuteOfDay(baselines[phase]) : undefined;
const offsetLabel = (minutes:number) => minutes===0 ? 'At section start' :
  Math.abs(minutes)+' min '+(minutes < 0?'before':'after')+' section start';
const resultsAllowed = (data:DeskState) => data.operator?.can_show_hall!==false;

function QuizPill({children,tone='neutral'}:{children:React.ReactNode;tone?:'good'|'warm'|'neutral'}) {
  return <span className="quiz-status-pill" data-tone={tone}>{children}</span>;
}

function DeskDialog({title,children,dismiss,busy=false}:{title:string;children:React.ReactNode;dismiss:()=>void;busy?:boolean}) {
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{
    const element=dialog.current;
    if (!element) return;
    const focus=document.activeElement;
    element.showModal();
    return ()=>{
      if (element.open) element.close();
      if (focus instanceof HTMLElement && focus.isConnected) focus.focus({preventScroll:true});
    };
  },[]);
  return <dialog ref={dialog} className="quiz-desk-dialog" aria-labelledby="quiz-dialog-title"
    onCancel={event=>{event.preventDefault();if(!busy) dismiss();}}>
    <h2 id="quiz-dialog-title">{title}</h2>
    {children}
  </dialog>;
}

function ConfirmAction({item,send,close,busy,locked,notice,latestRev}:{item:Confirmation;send:Send;close:()=>void;busy:boolean;locked:boolean;notice:string;latestRev:number}) {
  const [phrase,setPhrase]=useState('');
  const [reason,setReason]=useState('');
  const changed=latestRev!==item.rev;
  const ready=!item.phrase || (phrase===item.phrase && reason.trim().length>=8);
  return <DeskDialog title={item.title} dismiss={close} busy={busy}>
    <p>{item.description}</p>
    <form onSubmit={async event=>{
      event.preventDefault();
      if (!ready || changed || locked) return;
      const details=item.phrase ? {...item.details,confirm:item.phrase,reason:reason.trim()} : item.details;
      if (await send(item.action,details,item.rev)) close();
    }}>
      {item.phrase && <><label>Reason for this change
        <textarea required minLength={8} maxLength={500} value={reason} onChange={e=>setReason(e.target.value)} aria-describedby="quiz-reason-help"/>
      </label><small id="quiz-reason-help">Use at least 8 characters.</small>
      <label>Type <strong>{item.phrase}</strong> to confirm
        <input type="text" required autoComplete="off" spellCheck={false} value={phrase} onChange={e=>setPhrase(e.target.value)}/>
      </label></>}
      {(changed || notice) && <p role="alert">{changed?'The quiz changed. Close this dialog and review the latest state.':notice}</p>}
      <div className="row quiz-dialog-actions">
        <Button click={close} disabled={busy}>{item.dismiss || 'Go back'}</Button>
        <button type="submit" className={'btn '+(item.danger?'bad':'')} disabled={locked||changed||!ready}>{item.label}</button>
      </div>
    </form>
  </DeskDialog>;
}

function PhonePreview({question,close}:{question:Question;close:()=>void}) {
  return <DeskDialog title="Participant phone preview" dismiss={close}>
    {['F1','F2'].includes(question.id) && <p>The Final lineup supplies the choices when this question goes live.</p>}
    <section className="quiz-device">
      <span>Event quiz</span>
      <h3>{question.prompt}</h3>
      <ol>{question.options.map((option,index)=><li key={option.key}><b aria-hidden="true">{'ABCDE'[index]}</b>{option.text}</li>)}</ol>
    </section>
    <div className="row quiz-dialog-actions"><Button click={close}>Close preview</Button></div>
  </DeskDialog>;
}

function RetimeCue({session,data,busy,locked,notice,close,reload,preview,send}:{
  session:EditSession;data:DeskState;busy:boolean;locked:boolean;notice:string;close:()=>void;reload:(question:Question)=>void;
  preview:(details:Payload,rev:number)=>Promise<RetimePreview|undefined>;send:Send;
}) {
  const {item:question,rev}=session;
  const baselines=data.phase_baselines || {};
  const [phase,setPhase]=useState(question.phase);
  const [offset,setOffset]=useState(String(question.cue_offset_minutes ?? offsetFor(question.phase,question.time,baselines) ?? 0));
  const [time,setTime]=useState(question.time);
  const [shift,setShift]=useState(false);
  const [plan,setPlan]=useState<{details:Payload;value:RetimePreview}>();
  const samePhase=phase===question.phase;
  const relative=Boolean(baselines[phase]);
  const minute=relative ? minuteOfDay(baselines[phase])+Number(offset) : minuteOfDay(time);
  const nextTime=Number.isInteger(minute) && minute>=0 && minute<1440 && (!relative || offset.trim()) ? timeAtMinute(minute) : '';
  const changed=rev!==data.rev || Boolean(plan && plan.value.rev!==data.rev);
  const current=data.bank.find(item=>item.id===question.id);
  const canReload=current && !data.runs.some(run=>run.qid===question.id && run.status!=='void');
  const lockPhase=/^(F[12]|R[1-4][ab])$/.test(question.id);
  const summary=(cue:CueTime) => {
    const minutes=cue.offset_minutes ?? offsetFor(cue.phase,cue.time,baselines);
    return sectionName(cue.phase)+' · '+(minutes===undefined?cue.time:offsetLabel(minutes));
  };
  return <DeskDialog title="Change time or section" dismiss={close} busy={busy}>
    <p className="quiz-dialog-prompt">{question.prompt}</p>
    <p>Quiz times are reminders. Questions send only when you choose Send.</p>
    <form aria-busy={busy} onSubmit={async event=>{
      event.preventDefault();
      if (!nextTime || locked || changed) return;
      const details={qid:question.id,time:nextTime,shift_following:samePhase && shift,phase};
      setPlan(undefined);
      const value=await preview(details,rev);
      if (value) setPlan({details,value});
    }}>
      <div className="quiz-edit-grid">
        <label>Event section<select value={phase} disabled={busy||lockPhase} onChange={e=>{
          setPhase(e.target.value);setShift(false);setPlan(undefined);
        }}>
          {phases.filter(p=>p!=='lunch').map(p=><option key={p} value={p}>{sectionName(p)}</option>)}
        </select></label>
        {relative ? <label>Minutes after section starts<input type="number" inputMode="numeric" required step={1} value={offset} disabled={busy}
          onChange={e=>{setOffset(e.target.value);setPlan(undefined);}}/></label> :
          <label>Planned cue time<input type="time" required value={time} disabled={busy} onChange={e=>{setTime(e.target.value);setPlan(undefined);}}/></label>}
      </div>
      {lockPhase && <small>Event rules keep this question in {sectionName(question.phase)}.</small>}
      {samePhase && <label className="quiz-setting"><input type="checkbox" checked={shift} disabled={busy}
        onChange={e=>{setShift(e.target.checked);setPlan(undefined);}}/> Also move later unsent questions in this section</label>}
      {changed && <div className="quiz-callout">
        <p role="alert">{canReload?'The quiz changed. Load the latest cue before reviewing new timings.':'This question was sent or skipped while you were editing.'}</p>
        {canReload && <Button disabled={locked} click={()=>reload(current)}>Load latest cue</Button>}
      </div>}
      {notice && <p role="status">{notice}</p>}
      {plan && !changed && <div className="quiz-retime-preview" aria-label="Timing changes" aria-live="polite">
        <h3>{plan.value.changes.length ? 'Before and after' : 'No timing changes'}</h3>
        <ul>{plan.value.changes.map(change=><li key={change.qid}><strong>{change.prompt}</strong>
          <span>Before · {summary(change.before)}</span><span>After · {summary(change.after)}</span>
        </li>)}</ul>
      </div>}
      <div className="row quiz-dialog-actions">
        <Button click={close} disabled={busy}>Go back</Button>
        <button type="submit" className={'btn '+(plan&&!changed?'ghost':'')} disabled={locked||changed||!nextTime}>Preview timing changes</button>
        {plan && !changed && plan.value.changes.length>0 && <Button primary disabled={locked} click={async()=>{
          if (await send('retime',plan.details,plan.value.rev)) close();
        }}>Apply timing changes</Button>}
      </div>
    </form>
  </DeskDialog>;
}

function RunControls({run,data,send,ask,blocked,current,timing}:{run:Run;current:number}&DeskActions) {
  if (!data.enabled) return <p className="quiz-note">Turn on the quiz in Setup to manage this question.</p>;
  if (run.end!==null && run.end<=current) return <p className="quiz-note">The timer ended. Updating the question status…</p>;
  const showing=data.hall?.id===run.id && data.hall_visible;
  return <div className="quiz-controls quiz-channel-grid">
    <section aria-label="Manage phone answers">
      <h4>Participant phones</h4>
      <div className="row"><Button primary disabled={blocked} click={()=>send('close',{id:run.id})}>Stop accepting answers</Button>
        {run.end!==null && <Button disabled={blocked} click={()=>send('extend',{id:run.id,seconds:15})}>Add 15 seconds</Button>}
        {run.can_resend && <Button disabled={blocked} click={()=>send('resend',{id:run.id,...timing})}>Resend to phones</Button>}
      </div>
      <small>Stopping keeps the answers already submitted.</small>
    </section>
    <section aria-label="Manage room display">
      <h4>Room screen</h4>
      <p>{showing?'This question is showing.':run.show_blocked_reason || 'Show the question when the presenter gives the cue.'}</p>
      {(run.can_show || showing) && <Button disabled={blocked} click={()=>send(showing?'hide':'show',showing?{}:{id:run.id})}>
        {showing?'Hide from room screen':'Show on room screen'}</Button>}
    </section>
    {!data.release && <details className="quiz-disclosure"><summary>Question recovery</summary>
      <Button disabled={blocked} click={()=>ask({action:'void',title:'Cancel this question?',
        description:'Cancel “'+run.prompt+'” and remove its answers and points from scoring.',
        label:'Confirm cancellation',dismiss:'Keep question',details:{id:run.id},danger:true})}>Cancel question</Button>
    </details>}
  </div>;
}

function RunCard({run,current,actions}:{run:Run;current:number;actions:DeskActions}) {
  const timeLeft=run.end===null ? 0 : Math.max(0,run.end-current);
  return <section className="quiz-panel quiz-focus-card" aria-label="Live question">
    <div className="quiz-card-top"><QuizPill tone="good">Accepting answers</QuizPill><strong>{responseCount(run)}</strong></div>
    <h3>{run.prompt}</h3>
    {run.end===null ? <p className="quiz-note">Open until you stop accepting answers.</p> : <div className="quiz-live-timer" data-urgent={timeLeft<=10}>
      <progress max={Math.max(1,run.end-(run.start ?? run.end-run.seconds))} value={timeLeft} aria-label="Answer time remaining"/>
      <time>{clock(timeLeft)} left</time>
    </div>}
    <RunControls run={run} current={current} {...actions}/>
  </section>;
}

type SetControl = {action:string; label:string; text:string; reason?:string; optional?:boolean; run:()=>void};
function setControls({data,send,ask}:DeskActions) {
  const operator=data.operator;
  const practice=activeSet(data.runs,'warmup');
  const post=activeSet(data.runs,'post');
  const waiting=activeSet(data.runs,'waiting');
  const catchup=operator?.catchup_open ?? waiting.some(run=>run.catchup && run.status==='open');
  const controls:SetControl[]=[];
  if (data.phase==='registration') {
    const invited=operator?.invite_sent ?? data.activity.some(entry=>entry.action==='invite');
    if (!invited) controls.push({action:'invite',label:'Invite people to join',text:'Participants can opt in on their phones.',
      run:()=>send('invite')});
    if (!practice.length) controls.push({action:'warmup',label:'Open the practice warm-up',
      text:'Give participants 15 minutes to try the quiz at their own pace. Practice answers carry no points.',
      run:()=>send('warmup',{minutes:15})});
  }
  if (practice.some(run=>run.status==='open')) controls.push({action:'close_warmup',label:'End the practice warm-up',
    text:'Close the practice questions and keep the answers for the learning comparison.',run:()=>send('close_warmup')});
  if (data.phase==='break') {
    const catchupEnded=waiting.some(run=>run.catchup && run.status==='closed') ||
      data.activity.some(entry=>entry.action==='close_waiting');
    if (!catchup) controls.push({action:'catchup',label:catchupEnded?'Reopen 2-minute catch-up':'Open 2-minute catch-up',optional:catchupEnded,
      text:'Give teams another chance to answer questions from the preliminary rounds.',
      reason:operator?.can_catchup===false ? operator.catchup_blocked_reason || 'Wait for preliminary judging to finish.' :
        !waiting.some(run=>!run.released) ? 'Send a waiting-time question during preliminary rounds first.' : undefined,
      run:()=>ask({action:'catchup',title:'Open catch-up?',
        description:'Open catch-up only after all judging is finished and any missing preliminary sheets have been accounted for.',
        label:'Confirm and open catch-up',details:{minutes:2,confirm_missing:true}})});
    if (catchup) controls.push({action:'close_waiting',label:'End catch-up and release answers',
      text:'Close the waiting-time questions and share their available answers with participants.',run:()=>send('close_waiting')});
    if (!post.length) controls.push({action:'post',label:'Open the post-test',
      text:'Give participants five minutes to answer the warm-up questions again for points.',run:()=>send('post',{minutes:5})});
  }
  if (post.some(run=>run.status==='open')) controls.push({action:'close_post',label:'End post-test and release answers',
    text:'Close the post-test and share its answers together with the practice answers.',run:()=>send('close_post')});
  return controls;
}

function SetAction({control,blocked,primary=false}:{control:SetControl;blocked:boolean;primary?:boolean}) {
  return <div className="quiz-set-action">
    <p>{control.text}</p>
    {control.reason ? <p className="quiz-callout">{control.reason}</p> :
      <Button primary={primary} disabled={blocked} click={control.run}>{control.label}</Button>}
  </div>;
}

function questionCueStatus(data:DeskState,question:Question) {
  if (question.id==='P00') return 'Invitation';
  if (question.type==='reveal') return data.release?'Announced':'Awards reveal';
  if (question.schedules) return 'Question set';
  const run=[...data.runs].reverse().find(item=>item.qid===question.id);
  if (run?.status==='open') return 'Live';
  if (run?.status==='closed') return 'Finished';
  if (run?.status==='skipped') return 'Skipped';
  if (run?.status==='void') return 'Cancelled';
  return 'Planned';
}

function sendReason(data:DeskState,question:Question,active:Run[]) {
  if (!data.enabled) return 'Turn on the quiz in Setup to send questions.';
  if (data.release) return 'Quiz scoring is locked after the champion announcement.';
  if (question.can_send!==undefined) return question.can_send ? '' : question.send_blocked_reason || 'This question is unavailable now.';
  if (question.phase!==data.phase) return 'Send this question when '+sectionName(question.phase)+' is the current section.';
  if (data.operator?.quiet) return 'The programme is in a quiet period. Wait for the next quiz cue.';
  if (question.phase!=='rounds' && active.some(run=>['live','pulse','vote'].includes(run.kind))) return 'Stop the live question before sending another.';
  return '';
}

function RunQuiz({actions,ordered,current,openTab,preview}:{actions:DeskActions;ordered:Question[];current:number;openTab:(view:View)=>void;preview:(question:Question)=>void}) {
  const {data,send,blocked,timing}=actions;
  const active=data.runs.filter(run=>run.status==='open');
  const activeSingle=active.filter(run=>individualKinds.includes(run.kind) && !run.catchup);
  const activeSeries=active.filter(run=>!activeSingle.includes(run));
  const queued=ordered.filter(q=>manualCue(q) && (q.phase===data.phase || q.can_send===true) &&
    !data.runs.some(run=>run.qid===q.id && run.status!=='void'));
  const available=queued.filter(q=>!sendReason(data,q,active));
  const dueNext=available.find(q=>q.due!=null && q.due<=current);
  const next=dueNext || available.find(q=>q.phase===data.phase) || available[0] || queued[0];
  const reason=next ? sendReason(data,next,active) : '';
  const sets=data.release ? [] : setControls(actions);
  const recommendedSet=!activeSingle.length ? sets.find(set=>!set.reason && !set.optional) : undefined;
  const showSetFirst=Boolean(recommendedSet && (!next || reason || (!dueNext && !activeSingle.length)));
  const awardNext=data.phase==='awards' && !next && !data.release;
  const recentlyClosed=[...data.runs].reverse().find(run=>run.status==='closed');
  return <div className="quiz-run-grid">
    <div className="quiz-stack">
      {activeSingle.map(run=><RunCard key={run.id} run={run} current={current} actions={actions}/>)}
      {activeSeries.length>0 && <section className="quiz-panel quiz-series-status" aria-label="Open question sets">
        <div className="quiz-card-top"><QuizPill tone="good">Open on phones</QuizPill><strong>{activeSeries.length} open</strong></div>
        {[...new Set(activeSeries.map(run=>run.kind))].map(kind=>{
          const runs=activeSeries.filter(run=>run.kind===kind);
          return <p key={kind}><strong>{setNames[kind]}</strong> · {clock(Math.max(...runs.map(run=>run.end ?? current))-current)} left</p>;
        })}
      </section>}
      {!data.enabled && <section className="quiz-panel quiz-focus-card"><h3>The quiz is off</h3>
        <p>Turn it on in Setup when you’re ready to include it in this event.</p><Button primary click={()=>openTab('Setup')}>Open Setup</Button>
      </section>}
      {data.enabled && showSetFirst && recommendedSet && <section className="quiz-panel quiz-focus-card">
        <div className="quiz-card-top"><QuizPill tone="warm">Next action</QuizPill></div>
        <h3>{recommendedSet.label}</h3><SetAction control={recommendedSet} blocked={blocked} primary/>
      </section>}
      {data.enabled && next && <section className={'quiz-panel '+(!activeSingle.length&&!showSetFirst?'quiz-focus-card':'')}>
        <div className="quiz-card-top"><QuizPill tone={reason?'neutral':'warm'}>{reason?'Up next':next.due!=null&&next.due<=current?'Cue due':'Up next'}</QuizPill>
          <span>{next.cue_offset_minutes===undefined?'':offsetLabel(next.cue_offset_minutes)}</span></div>
        <h3>{next.prompt}</h3>{next.cue && <p>{next.cue}</p>}
        {reason && <p className="quiz-callout">{reason}</p>}
        <div className="row">
          {!reason && next.can_send_room && <Button primary disabled={blocked}
            click={()=>send('send',{qid:next.id,...timing,room:true})}>Send to phones and projector</Button>}
          {!reason && <Button primary={!next.can_send_room&&!activeSingle.length&&!showSetFirst} disabled={blocked}
            click={()=>send('send',{qid:next.id,...timing})}>Send to phones</Button>}
          <Button click={()=>preview(next)}>Phone preview</Button>
          {queued.length>1 && <Button click={()=>openTab('Timeline')}>View timeline</Button>}
        </div>
      </section>}
      {data.enabled && !next && !activeSingle.length && !activeSeries.length && !showSetFirst &&
        <section className="quiz-panel quiz-focus-card">
          <h3>{awardNext?'Prepare the quiz results':'No questions queued in this section'}</h3>
          <p>{awardNext?'Review any answers still waiting for release, then announce the champion.':'Follow the host and check the timeline for the next quiz cue.'}</p>
          <Button primary click={()=>openTab(awardNext?'Results':'Timeline')}>{awardNext?'Review results':'View timeline'}</Button>
        </section>}
      {data.enabled && sets.some(set=>!showSetFirst||set!==recommendedSet) && <details className="quiz-panel quiz-disclosure">
        <summary>More round controls</summary>
        <div className="quiz-stack">{sets.filter(set=>!showSetFirst||set!==recommendedSet).map(set=><div key={set.action}>
          <h3>{set.label}</h3><SetAction control={set} blocked={blocked}/>
        </div>)}</div>
      </details>}
    </div>
    <aside className="quiz-panel quiz-run-summary" aria-label="Quiz at a glance">
      <h3>At a glance</h3>
      <dl><div><dt>Joined the quiz</dt><dd>{data.analytics.summary?.joined ?? 0}</dd></div>
        <div><dt>Questions open</dt><dd>{active.length}</dd></div></dl>
      {recentlyClosed && <div className="quiz-last"><small>Last finished question</small><strong>{recentlyClosed.prompt}</strong><span>{responseCount(recentlyClosed)}</span></div>}
      <div className="row"><Button click={()=>openTab('Results')}>View results</Button>
        <Button click={()=>openTab('Question editor')}>Edit questions</Button></div>
    </aside>
  </div>;
}

function QuestionEditor({ordered,blocked,edit,preview}:{ordered:Question[];blocked:boolean;edit:(question:Question)=>void;preview:(question:Question)=>void}) {
  const [search,setSearch]=useState('');
  const [phase,setPhase]=useState('all');
  const query=search.trim().toLowerCase();
  const questions=ordered.filter(question=>question.type!=='reveal' && question.id!=='P00' &&
    (phase==='all' || question.phase===phase) &&
    [question.id, question.prompt, sectionName(question.phase)].some(value=>value.toLowerCase().includes(query)));
  return <section className="quiz-panel" aria-labelledby="question-editor-heading">
    <div className="quiz-panel-heading"><div><h2 id="question-editor-heading">Question editor</h2>
      <p>Change the wording, answers and points for future sends. Questions already sent keep their original wording and scoring.</p></div></div>
    <div className="quiz-editor-filters">
      <label className="quiz-filter">Search questions<input type="search" value={search}
        placeholder="Question, ID or event section" onChange={event=>setSearch(event.target.value)}/></label>
      <label className="quiz-filter">Event section<select value={phase} onChange={event=>setPhase(event.target.value)}>
        <option value="all">All sections</option>{phases.map(section=><option key={section} value={section}>{sectionName(section)}</option>)}
      </select></label>
    </div>
    {!!questions.length && <p className="quiz-note">{questions.length} {questions.length===1?'question':'questions'}</p>}
    {!questions.length && <p role="status">No matching questions.</p>}
    {questions.map(question=><article className="quiz-bank-row quiz-editor-row" key={question.id}>
      <div><p className="quiz-note">{sectionName(question.phase)} · {question.id}</p>
        <h3>{question.prompt}</h3></div>
      <div className="row">
        {question.options.length>0 && <Button click={()=>preview(question)}>Phone preview</Button>}
        <Button primary disabled={blocked} click={()=>edit(question)}>Edit question</Button>
      </div>
    </article>)}
  </section>;
}

function Timeline({actions,ordered,edit,preview,retime,filter,setFilter}:{
  actions:DeskActions;ordered:Question[];edit:(question:Question)=>void;
  preview:(question:Question)=>void;retime:(question:Question)=>void;filter:string;setFilter:(phase:string)=>void;
}) {
  const {data,send,blocked,timing}=actions;
  const active=data.runs.filter(run=>run.status==='open');
  const sections=filter==='all'?phases:[filter];
  return <section className="quiz-panel quiz-timeline ops-quiz-timeline" aria-labelledby="quiz-timeline-title">
    <div className="quiz-panel-heading"><div><h2 id="quiz-timeline-title">Quiz timeline</h2>
      <p>Check the running order and move cues if the programme changes. The Desk sends questions manually.</p></div>
      <a href="#display" className="btn ghost">Manage event programme</a></div>
    <label className="quiz-filter">Event section<select value={filter} onChange={e=>setFilter(e.target.value)}>
      <option value="all">All sections</option>{phases.map(phase=><option key={phase} value={phase}>{sectionName(phase)}</option>)}
    </select></label>
    {sections.map(phase=>{
      const postTime=ordered.find(q=>q.schedules)?.schedules?.find(cue=>cue.phase==='post')?.opens;
      const section=ordered.filter(q=>q.phase===phase);
      const items=section.map(q=>({time:q.time,question:q as Question|undefined}));
      if (phase==='break' && postTime) items.push({time:postTime,question:undefined});
      items.sort((a,b)=>a.time.localeCompare(b.time));
      return <section key={phase} className="quiz-timeline-section" data-current={phase===data.phase}>
        <header><h3>{sectionName(phase)}</h3>{phase===data.phase && <QuizPill tone="good">Current section</QuizPill>}</header>
        {!items.length && <p>No quiz cues planned.</p>}
        {items.map(({question:q,time})=>{
          if (!q) {
            const offset=offsetFor(phase,time,data.phase_baselines || {});
            return <article key="post-set" className="quiz-bank-row"><div className="quiz-timeline-time">{offset===undefined?time:offsetLabel(offset)}</div>
              <div><h4>Post-test</h4><p>Five questions from the practice warm-up. Open this set from Run quiz during the scoring break.</p></div>
            </article>;
          }
          const latest=[...data.runs].reverse().find(item=>item.qid===q.id);
          const status=questionCueStatus(data,q);
          const queued=manualCue(q) && (!latest || latest.status==='void');
          const canManage=data.enabled && !data.release;
          const reason=queued ? sendReason(data,q,active) : '';
          const offset=q.cue_offset_minutes ?? offsetFor(q.phase,q.time,data.phase_baselines || {});
          return <article className="quiz-bank-row" key={q.id}>
            <div className="quiz-timeline-time">{offset===undefined?q.time:offsetLabel(offset)}</div>
            <div className="quiz-timeline-content">
              <div className="quiz-card-top"><small>{q.id}{manualCue(q)?' · '+(q.priority==='core'?'Core':'Optional'):''}</small>
                <QuizPill tone={status==='Live'?'good':status==='Skipped'||status==='Cancelled'?'warm':'neutral'}>{status}</QuizPill></div>
              <h4>{q.prompt}</h4>
              {q.cue && <p>{q.cue}</p>}
              <div className="row">
                {q.options.length>0 && <Button click={()=>preview(q)}>Phone preview</Button>}
                {q.type!=='reveal' && q.id!=='P00' && <Button disabled={blocked} click={()=>edit(q)}>Edit</Button>}
                {queued && <Button disabled={blocked} click={()=>retime(q)}>Change time or section</Button>}
                {queued && canManage && <Button disabled={blocked} click={()=>send('skip',{qid:q.id})}>Skip</Button>}
                {latest?.status==='skipped' && canManage && <Button disabled={blocked} click={()=>send('restore',{id:latest.id})}>Restore to timeline</Button>}
                {queued && !reason && q.can_send_room && <Button primary disabled={blocked}
                  click={()=>send('send',{qid:q.id,...timing,room:true})}>Send {q.id} to phones and projector</Button>}
                {queued && !reason && <Button primary={!q.can_send_room} disabled={blocked} click={()=>send('send',{qid:q.id,...timing})}>Send {q.id} to phones</Button>}
              </div>
              {latest?.status==='closed' && latest.can_resend && canManage && <Button primary disabled={blocked} click={()=>send('resend',{id:latest.id,...timing})}>{latest.released && latest.points?'Replay without points':'Resend '+q.id+' now'}</Button>}
              {queued && reason && <small className="quiz-note">{reason}</small>}
              {q.id==='P00' && <small>Send the invitation from Run quiz during registration.</small>}
              {q.type==='reveal' && <small>Announce the champion from Results during Awards.</small>}
            </div>
          </article>;
        })}
      </section>;
    })}
  </section>;
}

function ReleaseResults({actions}:{actions:DeskActions}) {
  const {data,send,ask,blocked}=actions;
  const operator=data.operator;
  if (!data.enabled) return <p className="quiz-callout">Turn on the quiz in Setup to release or share results.</p>;

  const waiting=activeSet(data.runs,'waiting');
  const post=activeSet(data.runs,'post');
  const missingWaiting=waiting.some(run=>run.status==='closed'&&!run.released&&!run.prediction);
  const missingPost=post.some(run=>!run.released);
  const needsPredictions=data.runs.some(run=>run.prediction && ['open','closed'].includes(run.status) && !run.released);
  const hasAudienceVote=data.runs.some(run=>run.qid==='F2' && ['open','closed'].includes(run.status));
  const showHall=resultsAllowed(data);
  const awardReason=data.phase!=='awards'?'Switch the event programme to Awards first.':
    operator?.can_champion!==true ? operator?.champion_blocked_reason?.replace('Release or void', 'Release or cancel') ||
      'Finish open questions and release pending answers first.' : '';
  const showChampion=operator?.can_show_champion ?? (data.phase==='awards' && showHall);
  const releaseButton=(label:string,action:string,why?:string|null) =>
    <div className="quiz-release-item"><strong>{label}</strong>
      {why ? <small>{why}</small> : <Button disabled={blocked} click={()=>send(action)}>{label}</Button>}</div>;
  return <div className="quiz-result-controls">
    {!data.release && (missingWaiting || missingPost || needsPredictions) && <div className="quiz-release-items">
      {missingWaiting && releaseButton('Release waiting-time answers','close_waiting',
        waiting.some(run=>run.status==='open')?'End the open waiting-time questions from Run quiz first.':undefined)}
      {missingPost && releaseButton(post.some(run=>run.status==='open')?'End post-test and release answers':'Release post-test answers','close_post')}
      {needsPredictions && releaseButton('Release predictions','predictions',
        operator?.can_predictions===true?undefined:operator?.predictions_blocked_reason || 'Resolve the prediction after the event results are confirmed.')}
    </div>}
    <div className="quiz-release-items">
      {data.hall && <div className="quiz-release-item"><strong>{data.hall_visible?'Quiz content is on the hall screen':'Quiz content is queued for the hall'}</strong>
        <Button disabled={blocked} click={()=>send('hide')}>Return hall to programme</Button></div>}
      {!data.release && hasAudienceVote && releaseButton('Show People’s Choice','people_choice',
        !showHall ? operator?.hall_blocked_reason || 'The hall is showing the event programme.' :
        operator?.can_people_choice===true?undefined:operator?.people_choice_blocked_reason || 'Close the vote and wait until Awards.')}
    </div>
    <div className="quiz-award">
      <h3>Championship reveal</h3>
      {data.release ? <><p>The quiz champion has been announced.</p>
        {showChampion ? <Button disabled={blocked} click={()=>send('show_champion')}>Show champion again</Button> :
          operator?.show_champion_blocked_reason && <p className="quiz-note">{operator.show_champion_blocked_reason}</p>}</> :
        awardReason ? <p className="quiz-note">{awardReason}</p> :
        <Button primary disabled={blocked} click={()=>ask({action:'champion',title:'Announce the quiz champion?',
          description:'Show the quiz champion and surprise prize on the hall screen and lock quiz scoring.',
          label:'Announce champion and prize'})}>Announce champion and prize</Button>}
    </div>
  </div>;
}

function ExtraResultActions({run,actions}:{run:Run;actions:DeskActions}) {
  const {data,send,ask,blocked,timing}=actions;
  const hallReason=run.can_show===false ? run.show_blocked_reason || 'This question is unavailable for the hall.' :
    !resultsAllowed(data) ? data.operator?.hall_blocked_reason || 'The hall is showing the event programme.' :
    run.status==='closed' && !run.released ? 'Release these answers before showing them on the hall.' :
    ['F1','F2'].includes(run.qid) && data.phase!=='awards' && run.status==='closed' ?
      'The audience votes can be shared during Awards.' : '';
  const hallEligible=!run.private && ['live','pulse','vote'].includes(run.kind) && ['open','closed'].includes(run.status);
  const liveKind=['live','pulse','vote'].includes(run.kind);
  const resendAvailable=run.status==='closed' && liveKind && (run.can_resend ??
    ((!run.phase || run.phase===data.phase) && !data.operator?.quiet && !['F1','F2'].includes(run.qid) &&
    !data.runs.some(item=>item.status==='open' && ['live','pulse','vote'].includes(item.kind))));
  const onHall=data.hall?.id===run.id;
  if (!data.enabled || !['open','closed'].includes(run.status)) return null;
  return <div className="quiz-result-action">
    {hallEligible && <div className="row">
      {onHall && data.hall_visible && <Button disabled={blocked} click={()=>send('hide')}>Hide from room screen</Button>}
      {hallReason ? <small>{hallReason}</small> : <>
        {(!onHall || !data.hall_visible) && <Button disabled={blocked} click={()=>send('show',{id:run.id})}>Show on room screen</Button>}
        {run.room_results_ready && <Button disabled={blocked} click={()=>send('show',{id:run.id,results:true})}>Show results on room screen</Button>}
      </>}
    </div>}
    {!data.release && <details className="quiz-disclosure">
      <summary>More question controls</summary>
      {run.status==='closed' && liveKind && !resendAvailable && run.resend_blocked_reason && <p>{run.resend_blocked_reason}</p>}
      {resendAvailable && <div className="quiz-resend">
        <p>{run.released && run.points ? 'Replay this question without changing the original scores.' :
          'Participants who haven’t answered get another chance. Existing answers stay recorded.'}</p>
        <Button disabled={blocked} click={()=>send('resend',{id:run.id,...timing})}>{run.released && run.points ? 'Replay without points' : 'Resend to unanswered participants'}</Button>
      </div>}
      <div className="row">
        <Button disabled={blocked} click={()=>ask({action:'void',details:{id:run.id},title:'Cancel this question?',
          description:'Cancel “'+run.prompt+'” and remove its answers and points from scoring.',
          label:'Confirm cancellation',dismiss:'Keep question',danger:true})}>Cancel question</Button>
        {individualKinds.includes(run.kind) && <Button disabled={blocked} click={()=>ask({action:'reset_question',details:{id:run.id},
          title:'Reset this question?',description:'Remove '+responseCount(run)+' from scoring and return “'+run.prompt+
          '” to the quiz timeline. You can send it again during its section.',label:'Reset question',phrase:'RESET QUESTION',danger:true})}>Reset question</Button>}
      </div>
    </details>}
  </div>;
}

function QuizResults({actions,download}:{actions:DeskActions;download:(kind:string)=>void}) {
  const {data}=actions;
  const [selected,setSelected]=useState('');
  const runs=[...data.runs].reverse().filter(run=>run.status!=='skipped');
  const run=runs.find(item=>item.id===selected) || runs.find(item=>item.status==='open') || runs[0];
  const summary=data.analytics.summary;
  const metrics=run && data.analytics.questions[run.id];
  return <div className="quiz-stack ops-quiz-results">
    <section className="quiz-panel ops-results-summary"><h2>Quiz results</h2>
      <dl className="quiz-kpis">{[[summary?.joined,'Joined'],[summary?.respondents,'Answered'],[summary?.answers,'Accepted answers'],
        [data.runs.filter(r=>r.status==='closed'&&r.released).length,'Questions released']].map(([value,label])=>
        <div key={label}><dt>{label}</dt><dd>{value ?? 0}</dd></div>)}</dl>
    </section>
    <section className="quiz-panel ops-results-questions"><h2>Responses by question</h2>
      {run ? <>
        <label>Question<select value={run.id} onChange={e=>setSelected(e.target.value)}>
          {runs.map(item=><option key={item.id} value={item.id}>{item.qid} · {resultKinds[item.kind] || 'Question'} · {item.prompt}</option>)}
        </select></label>
        <article className="quiz-response-card" key={run.id}>
          <div className="quiz-card-top"><QuizPill tone={run.status==='open'||run.released?'good':'warm'}>{runState(run)}</QuizPill>
            <strong>{run.status==='void'?'Removed from scoring':responseCount(run)}</strong></div>
          <h3>{run.prompt}</h3>
          {run.status!=='void' && <AnswerBars run={run}/>}
          {metrics && run.status==='closed' && <div className="quiz-result-metrics">
            {run.released && metrics.correctPercent!=null && <span>{metrics.correctPercent}% correct</span>}
            {metrics.viewers>0 && <span>{metrics.viewers} {metrics.viewers===1?'viewer':'viewers'}</span>}
            {metrics.medianSeconds!=null && <span>Median response time {metrics.medianSeconds}s</span>}
            {run.released && metrics.commonWrong && <span>Most common incorrect choice · {metrics.commonWrong}</span>}
          </div>}
          <ExtraResultActions run={run} actions={actions}/>
        </article>
      </> : <p>Responses appear after you send a question.</p>}
    </section>
    <section className="quiz-panel ops-results-release"><h2>Publish and display</h2>
      <p className="quiz-note">Review responses before sharing results with phones or the room.</p>
      <ReleaseResults actions={actions}/>
    </section>
    <section className="quiz-panel ops-results-reports">
      <details className="quiz-disclosure"><summary>Reports and downloads</summary>
        {data.learning.matched>0 && <p>Matched warm-up and post-test · {data.learning.before}% → {data.learning.after}%</p>}
        {data.analytics.confidence.before!==null && data.analytics.confidence.after!==null &&
          <p>Confidence before and after · {data.analytics.confidence.before} → {data.analytics.confidence.after} / 5</p>}
        {!data.individual_results_restricted && <>
          {data.standings.length>0 && <div className="quiz-table-wrap"><table className="quiz-table">
            <thead><tr><th>Rank</th><th>Participant</th><th>Team</th><th>Score</th></tr></thead>
            <tbody>{data.standings.map((p,i)=><tr key={i}><td>{p.rank}</td><td>{p.name}</td><td>{p.pair}</td>
              <td>{p.earned} / {p.available}{p.pending?' + '+p.pending+' pending':''}</td></tr>)}</tbody>
          </table></div>}
          <div className="quiz-edit-grid">{(['teams','companies'] as const).map(kind=>{
            const averages=data.analytics[kind];
            return averages?.length ? <div key={kind}><h3>{kind==='teams'?'Team averages':'Company averages'}</h3>
              <ul>{averages.map(row=><li key={row.name}>{row.name} · {row.average} pts ({row.count})</li>)}</ul></div> : null;
          })}</div>
        </>}
        {data.analytics.competencies.length>0 && <details className="quiz-disclosure"><summary>Qualification by company</summary>
          {data.analytics.competencies.map(company=><div key={company.company}><h3>{company.company}</h3><ul>
            {company.skills.map(skill=><li key={skill.name}>{skill.name} · {skill.accuracy}%</li>)}</ul></div>)}</details>}
        <p>Question CSV includes historical records for cancelled questions.</p>
        <div className="row">{!data.individual_results_restricted && <Button click={()=>download('people')}>Download participant CSV</Button>}
          <Button click={()=>download('questions')}>Download question CSV</Button></div>
      </details>
      <details className="quiz-disclosure"><summary>Operator activity and analytics</summary><QuizAnalytics data={data}/></details>
    </section>
  </div>;
}

function QuizSetup({actions,openTab}:{actions:DeskActions;openTab:(view:View)=>void}) {
  const {data,send,ask,blocked}=actions;
  const [draft,setDraft]=useState<{value:string;rev:number}>();
  const count=data.runs.length;
  const answers=data.runs.reduce((total,run)=>total+(run.responses ?? 0),0);
  const archives=data.reset_archives || [];
  const restorable=archives.find(item=>item.id===data.restorable_reset_id);
  const changed=draft && draft.rev!==data.rev && draft.value!==data.prize;
  return <div className="quiz-stack">
    <section className="quiz-panel"><h2>Quiz settings</h2>
      <label className="quiz-setting"><input type="checkbox" checked={data.enabled} disabled={blocked}
        onChange={event=>{
          if (event.target.checked) send('settings',{enabled:true});
          else ask({action:'settings',title:'Turn off the quiz?',description:'This closes open questions and keeps recorded answers.',
            label:'Turn off quiz',details:{enabled:false}});
        }}/> Enable event quiz</label>
      <form className="quiz-prize" onSubmit={async event=>{
        event.preventDefault();
        if (draft && !changed && !blocked && await send('prize',{prize:draft.value},draft.rev)) setDraft(undefined);
      }}>
        <label>Surprise prize<input type="text" maxLength={180} value={draft?.value ?? data.prize}
          onChange={e=>setDraft({value:e.target.value,rev:draft?.rev ?? data.rev})}/></label>
        <p>Shown on the hall display when you announce the champion.</p>
        {changed && <div className="quiz-callout">The quiz changed while you were editing. The saved prize is {data.prize || 'empty'}.
          <div className="row"><Button click={()=>setDraft(undefined)}>Use saved prize</Button>
            <Button click={()=>setDraft({...draft,rev:data.rev})}>Keep my draft</Button></div></div>}
        {!data.enabled && <p>Enable the quiz to save the prize.</p>}
        <button className="btn" type="submit" disabled={blocked||!data.enabled||!draft||changed||draft.value===data.prize}>Save prize</button>
      </form>
    </section>
    <section className="quiz-panel"><h2>Before the event</h2>
      <ul className="quiz-guide">
        <li>Check question wording, correct answers and points.</li>
        <li>Review the order and timing with the presenter.</li>
        <li>Enable the quiz when you are ready to send.</li>
      </ul>
      <div className="row"><Button click={()=>openTab('Question editor')}>Open question editor</Button>
        <Button click={()=>openTab('Timeline')}>Check the timeline</Button></div>
    </section>
    <details className="quiz-panel quiz-disclosure quiz-danger"><summary>Reset and recovery</summary>
      <p>Reset archives the quiz activity in this workspace. The question plan, prize, roster, pitch scores, programme, and accounts are retained.</p>
      <p>{count} {count===1?'quiz run':'quiz runs'} · {answers} {answers===1?'submitted answer':'submitted answers'}</p>
      <Button disabled={blocked} click={()=>ask({action:'reset',title:'Reset the quiz in this workspace?',
        description:'Archive '+count+' '+(count===1?'quiz run':'quiz runs')+' and '+answers+' '+(answers===1?'submitted answer':'submitted answers')+
          '. This clears invitations, answers, and results from the active quiz and turns it off. Quiz sign-ups are kept.',
        label:'Reset quiz',phrase:'RESET QUIZ',danger:true})}>Reset quiz</Button>
      {archives.length>0 && <div className="quiz-archives"><h3>Archived activity</h3>
        {!restorable && <p>Recovery is available only for the latest reset before new quiz activity starts.</p>}
        <ul>{[...archives].reverse().map(archive=><li key={archive.id}>
          <div><strong>{archive.run_count} {archive.run_count===1?'run':'runs'} · {archive.answer_count} {archive.answer_count===1?'answer':'answers'}</strong>
            <time dateTime={new Date(archive.at*1000).toISOString()}>{new Date(archive.at*1000).toLocaleString('en-GB',{timeZone:'UTC'})} event time</time>
            <span>{archive.reason}</span></div>
          {archive===restorable && <Button disabled={blocked} click={()=>ask({action:'restore_reset',details:{archive_id:archive.id},
            title:'Restore this quiz archive?',description:'Restore '+archive.run_count+' quiz runs and '+archive.answer_count+
              ' answers. The quiz stays off and restored questions stay closed.',label:'Restore quiz archive',phrase:'RESTORE QUIZ',danger:true})}>Restore quiz archive</Button>}
        </li>)}</ul>
      </div>}
    </details>
  </div>;
}

const views:View[]=['Run quiz','Question editor','Timeline','Results','Setup'];
const successNotice:Record<string,string> = {
  send:'Question sent to phones.',close:'Question closed. Answers kept.',extend:'Added 15 seconds.',void:'Question cancelled and removed from scoring.',
  reset_question:'Question reset. It is back on the timeline.',reset:'Quiz reset. Previous activity archived.',
  restore_reset:'Quiz archive restored. The quiz is off.',retime:'Timeline updated.',skip:'Question skipped.',restore:'Question restored.',
  invite:'Invitation sent.',warmup:'Practice warm-up opened.',post:'Post-test opened.',catchup:'Catch-up opened.',
  close_post:'Post-test answers released.',close_waiting:'Waiting-time answers released.',close_warmup:'Practice warm-up ended.',
  predictions:'Predictions released.',champion:'Quiz champion announced.',edit:'Question updated.',prize:'Prize saved.',
};

function DeskQuiz({read,write,editor}:{read:Read;write:Write;editor:boolean}) {
  const [data,setData]=useState<DeskState>();
  const [view,setView]=useState<View>(editor?'Question editor':'Run quiz');
  const [duration,setDuration]=useState('60');
  const [timelineFilter,setTimelineFilter]=useState<string>();
  const [editing,setEditing]=useState<EditSession>();
  const [editConflict,setEditConflict]=useState(false);
  const [preview,setPreview]=useState<Question>();
  const [retime,setRetime]=useState<EditSession>();
  const [confirmation,setConfirmation]=useState<Confirmation>();
  const [notice,setNotice]=useState('');
  const [dialogNotice,setDialogNotice]=useState('');
  const [connected,setConnected]=useState(true);
  const [busy,setBusy]=useState(false);
  const [staleRevision,setStaleRevision]=useState(false);
  const request=useRef(0);
  const posting=useRef(false);
  const tabs=useRef<(HTMLButtonElement|null)[]>([]);
  const current=useTime(data?.now) || data?.now || 0;
  useEffect(()=>{
    let active=true;
    let pending=false;
    async function poll() {
      if (posting.current || pending || document.hidden) return;
      pending=true;
      const seq=++request.current;
      try {
        const next=await read(deskPath);
        if (active && seq===request.current) {
          setData(next);
          setConnected(true);
          setStaleRevision(false);
        }
      } catch(error) {
        if (active && seq===request.current) {
          setConnected(false);
          setNotice(actionMessage(error));
        }
      } finally {pending=false;}
    }
    poll();
    const timer=setInterval(poll,2000);
    document.addEventListener('visibilitychange',poll);
    return ()=>{
      active=false;request.current++;clearInterval(timer);document.removeEventListener('visibilitychange',poll);
    };
  },[read]);

  async function post<T>(action:string,details:Payload,rev:number,onSuccess:(next:T)=>void) {
    if (posting.current || !connected || staleRevision) return false;
    posting.current=true;
    setBusy(true);
    setNotice('');
    setDialogNotice('');
    const seq=++request.current;
    try {
      const next=await write(deskPath,{action,rev,...details});
      if (seq!==request.current) return false;
      onSuccess(next);
      return true;
    } catch(error) {
      if (seq===request.current) {
        if ((error as {status?:number})?.status===409) {
          setStaleRevision(true);
          if (action==='edit') setEditConflict(true);
        }
        if (confirmation || retime) setDialogNotice(actionMessage(error));
        else setNotice(actionMessage(error));
      }
      return false;
    } finally {posting.current=false;setBusy(false);}
  }

  if (!data) return <p role="status">{notice || 'Loading the quiz…'}</p>;
  const send:Send=(action,details={},revision=data.rev)=>post<DeskState>(action,details,revision,next=>{
    setData(next);
    if (action==='edit') setEditing(undefined);
    setNotice(details.room ? 'Question sent to phones and projector.' : action==='spotlight' ?
      'Leaderboard shown for 15 seconds.' : successNotice[action] || 'Quiz updated.');
  });
  const previewRetime=async (details:Payload,rev:number)=>{
    let value:RetimePreview|undefined;
    await post<RetimePreview>('retime_preview',details,rev,next=>{value=next;});
    return value;
  };
  const blocked=busy || !connected || staleRevision;
  const timing = duration==='60' ? {} : {seconds:duration==='manual' ? null : Number(duration)};
  const actions:DeskActions={data,send,blocked,timing,ask:item=>{setDialogNotice('');setConfirmation({...item,rev:data.rev});}};
  const ordered=[...data.bank].sort((a,b)=>phases.indexOf(a.phase)-phases.indexOf(b.phase)||a.time.localeCompare(b.time));
  const staleEdit=Boolean(editing && editing.rev!==data.rev);
  const latestEdit=staleEdit && editing ? data.bank.find(q=>q.id===editing.item.id) : undefined;
  const changeTab=(tab:View,focus=false)=>{
    setView(tab);
    if (focus) tabs.current[views.indexOf(tab)]?.focus();
  };
  const openEditor=(question:Question)=>{setNotice('');setEditConflict(false);setEditing({item:question,rev:data.rev});};
  const openRetime=(question:Question)=>{setDialogNotice('');setRetime({item:question,rev:data.rev});};
  const openCount=data.runs.filter(run=>run.status==='open').length;

  return <div className="quiz-desk">
    <section className="quiz-desk-head" aria-label="Quiz and programme status"><div>
      <p className="event-kicker">Current event section</p><h2>{data.title}</h2></div>
      <div className="quiz-console-status">
        <QuizPill tone={data.enabled?'good':'warm'}>{data.enabled?'Quiz on':'Quiz off'}</QuizPill>
        {data.enabled && <QuizPill tone={openCount?'good':'neutral'}>{openCount?openCount+' open':'No open questions'}</QuizPill>}
        <QuizPill tone={data.hall_visible?'good':'neutral'}>{data.hall_visible?'Quiz on hall':data.hall?'Hall share paused':'Hall on programme'}</QuizPill>
        {data.can_spotlight && (data.hall?.kind==='spotlight' ?
          <Button disabled={blocked} click={()=>send('hide')}>Return hall to programme</Button> :
          <Button disabled={blocked} click={()=>send('spotlight')}>Show leaderboard for 15 seconds</Button>)}
        {data.enabled && ['Run quiz','Timeline','Results'].includes(view) && <label className="quiz-duration">Question timer <select value={duration} onChange={event=>setDuration(event.target.value)}>
          <option value="30">30 seconds</option><option value="60">60 seconds</option><option value="90">90 seconds</option>
          <option value="120">120 seconds</option><option value="manual">Until closed</option>
        </select></label>}
      </div>
    </section>
    <div className="quiz-desk-tabs" role="tablist" aria-label="Quiz controls"
      onKeyDown={event=>{
        const index=views.indexOf(view);
        const next=event.key==='ArrowRight'?(index+1)%views.length:
          event.key==='ArrowLeft'?(index+views.length-1)%views.length:event.key==='Home'?0:event.key==='End'?views.length-1:-1;
        if (next<0) return;
        event.preventDefault();
        changeTab(views[next],true);
      }}>
      {views.map((label,index)=><button key={label} className="btn ghost" type="button" ref={element=>{tabs.current[index]=element;}}
        role="tab" id={'quiz-tab-'+index} aria-controls={'quiz-panel-'+index} aria-selected={view===label} tabIndex={view===label?0:-1}
        onClick={()=>changeTab(label)}>{label}</button>)}
    </div>
    <p role="status" className="quiz-desk-notice">{!connected?'Connection interrupted. Quiz controls will return once the desk reconnects.':notice}</p>
    <div role="tabpanel" id={'quiz-panel-'+views.indexOf(view)} aria-labelledby={'quiz-tab-'+views.indexOf(view)}>
      {view==='Run quiz' && <RunQuiz actions={actions} ordered={ordered} current={current} openTab={changeTab} preview={setPreview}/>}
      {view==='Question editor' && <QuestionEditor ordered={ordered} blocked={blocked} edit={openEditor} preview={setPreview}/>}
      {view==='Timeline' && <Timeline actions={actions} ordered={ordered} preview={setPreview} retime={openRetime}
        filter={timelineFilter ?? (phases.includes(data.phase)?data.phase:'all')} setFilter={setTimelineFilter}
        edit={openEditor}/>}
      {view==='Results' && <QuizResults actions={actions} download={kind=>KEV.download(deskPath+'/'+kind+'.csv').catch((error:unknown)=>setNotice(actionMessage(error)))}/>}
      {view==='Setup' && <QuizSetup actions={actions} openTab={changeTab}/>}
    </div>
    {editing && <DeskDialog title="Edit quiz question" dismiss={()=>setEditing(undefined)} busy={busy}>
      <EditQuestion key={editing.item.id} session={editing} latest={latestEdit} stale={editConflict || staleEdit} busy={blocked}
        save={changes=>send('edit',{qid:editing.item.id,changes},editing.rev)} cancel={()=>setEditing(undefined)}
        rebase={()=>{if(latestEdit){setEditing({item:latestEdit,rev:data.rev});setEditConflict(false);setNotice('');}}}/>
      {notice && <p role="status">{notice}</p>}
    </DeskDialog>}
    {preview && <PhonePreview question={preview} close={()=>setPreview(undefined)}/>}
    {retime && <RetimeCue key={retime.rev} session={retime} data={data} busy={busy} locked={blocked}
      notice={dialogNotice} close={()=>setRetime(undefined)} reload={openRetime} preview={previewRetime} send={send}/>}
    {confirmation && <ConfirmAction item={confirmation} latestRev={data.rev} send={send} close={()=>setConfirmation(undefined)} busy={busy} locked={blocked} notice={dialogNotice}/>}
  </div>;
}

let playerRoot:Root|undefined,deskRoot:Root|undefined,deskNode:Element|undefined;
window.EventQuiz={
  participant(state:{workspace?:unknown;program?:{scene?:string};action?:{code?:string}}) {
    const node=document.getElementById('event-quiz-player');
    if (!node || !state.workspace) {playerRoot?.unmount();playerRoot=undefined;return;}
    playerRoot ||= createRoot(node);
    playerRoot.render(<PlayerQuiz action={state.action?.code || ''}/>);
  },
  desk(node:Element,read:Read,write:Write,editor=false) {
    if (deskNode!==node) {deskRoot?.unmount();deskNode=node;deskRoot=createRoot(node);}
    deskRoot?.render(<DeskQuiz read={read} write={write} editor={editor}/>);
  },
  clear() {
    playerRoot?.unmount(); playerRoot=undefined;
    deskRoot?.unmount(); deskRoot=undefined; deskNode=undefined;
  }
};
