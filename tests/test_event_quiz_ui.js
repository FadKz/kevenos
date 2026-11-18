const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

const bundle = readFileSync(join(__dirname, '../static/event-quiz.js'), 'utf8');
const question = {id:'W2',phase:'welcome',prompt:'How long is a pitch?',options:[{key:'A',text:'5 minutes'},{key:'B',text:'10 minutes'}],
  points:1,seconds:15,correct:'B',cue:'Original cue',time:'10:00',priority:'core',explanation:''};
const deskState = {enabled:true,rev:4,now:100,phase:'welcome',title:'Welcome',bank:[question],runs:[],standings:[],activity:[],
  individual_results_restricted:true,analytics:{summary:{joined:0,viewers:0,respondents:0,answers:0,released:0,skipped:0,voided:0},questions:{},confidence:{before:null,after:null},competencies:[]},
  learning:{matched:0},hall:null,release:null,prize:''};

function fixture(t) {
  const dom = new JSDOM('<main id="event-quiz-player"></main>',{runScripts:'outside-only',pretendToBeVisual:true});
  const w = dom.window, root = w.document.querySelector('main'), timers = new Map(), errors = [];
  // jsdom does not implement the dialog lifecycle used by the participant UI.
  w.HTMLDialogElement.prototype.showModal ??= function() {this.open=true;};
  w.HTMLDialogElement.prototype.close ??= function() {this.open=false;this.dispatchEvent(new w.Event('close'));};
  const setInterval = w.setInterval.bind(w);
  w.setInterval = (callback,ms,...args) => {
    if (![1200,2000].includes(ms)) return setInterval(callback,ms,...args);
    timers.set(ms,callback);
    return -ms;
  };
  w.addEventListener('error',event => errors.push(event.error));
  w.eval(bundle);
  t.after(() => {w.EventQuiz.clear();w.close();});
  const findButton = name => [...root.querySelectorAll('button')].find(button => button.textContent.trim()===name);
  async function until(check, label) {
    for (let i=0;i<50;i++) {
      const value = check();
      if (value) return value;
      if (errors.length) throw errors[0];
      await new Promise(resolve => setTimeout(resolve,10));
    }
    assert.fail('Timed out waiting for '+label);
  }
  async function click(name) {
    (await until(() => {const button=findButton(name); return button && !button.disabled && button;},name)).click();
  }
  function fill(field,value) {
    const proto = field.tagName==='TEXTAREA' ? w.HTMLTextAreaElement.prototype : w.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,'value').set.call(field,value);
    field.dispatchEvent(new w.Event('input',{bubbles:true}));
  }
  return {w,root,errors,findButton,until,click,fill,poll:ms=>timers.get(ms)()};
}

const openRun = {...question,id:'W2-5',qid:'W2',kind:'live',status:'open',start:100,end:115,responses:2,released:false,
  bars:[{...question.options[0],count:1},{...question.options[1],count:1}]};

test('Run quiz offers the next question, then separates stopping and cancelling', async t => {
  const f = fixture(t), writes = [];
  let latest = {...deskState,bank:[{...question,can_send:true}],hall_visible:false};
  f.w.EventQuiz.desk(f.root,async () => latest,async (_path, body) => {
    writes.push(JSON.parse(JSON.stringify(body)));
    const run = body.action==='send' ? {...openRun,end:null,can_resend:true} : {...latest.runs[0],status:body.action==='close'?'closed':'void'};
    latest = {...latest,rev:latest.rev+1,runs:[run]};
    return latest;
  });
  await f.until(() => f.root.querySelector('[role=tab][aria-selected=true]')?.textContent==='Run quiz','Run quiz is the default tab');
  await f.click('Send to phones');
  await f.until(() => /2 responses/.test(f.root.textContent),'the live response count');
  assert.match(f.root.textContent,/Open until you stop accepting answers/);
  assert.ok(f.findButton('Resend to phones'));
  assert.equal(f.findButton('Add 15 seconds'),undefined);
  await f.click('Cancel question');
  await f.until(() => f.root.querySelector('dialog[open]'),'the cancellation confirmation');
  assert.match(f.root.querySelector('dialog').textContent,/remove.*scoring/i);
  assert.deepEqual(writes.map(body=>body.action),['send']);
  await f.click('Keep question');
  await f.click('Stop accepting answers');
  await f.until(() => /answers kept/i.test(f.root.textContent),'the stop confirmation');
  assert.deepEqual(writes.map(body=>body.action),['send','close']);
});

test('Question editor is easy to reach and can find a question while quiz is off', async t => {
  const f = fixture(t);
  f.w.EventQuiz.desk(f.root, async () => ({...deskState, enabled:false}), async () => assert.fail('Unexpected write'));
  await f.click('Question editor');
  const search = await f.until(() => f.root.querySelector('input[type=search]'),'question search');
  f.fill(search, 'unknown phrase');
  await f.until(() => f.root.textContent.includes('No matching questions'), 'empty search result');
  f.fill(search, 'W2');
  await f.click('Edit question');
  await f.until(() => f.root.querySelector('dialog[open]'),'question editor dialog');
  assert.equal(f.root.querySelector('dialog textarea').value, question.prompt);
});

test('Organizer Results renders the scoped payload and only offers the question CSV', async t => {
  const f = fixture(t), downloads = [];
  f.w.KEV = {download:async path => downloads.push(path)};
  f.w.EventQuiz.desk(f.root,async () => deskState,async () => assert.fail('Unexpected write'));
  await f.click('Results');
  await f.until(() => [...f.root.querySelectorAll('h2')].some(h=>h.textContent==='Quiz results'),'Results');
  assert.equal(f.findButton('Download participant CSV'),undefined);
  assert.doesNotMatch(f.root.textContent,/Team averages|Company averages/);
  await f.click('Download question CSV');
  assert.deepEqual(downloads,['/api/desk/quiz/questions.csv']);
  assert.deepEqual(f.errors,[]);
});

test('a quiz edit uses its opening revision and retains local changes through review', async t => {
  const f = fixture(t), writes = [];
  let latest = deskState;
  f.w.EventQuiz.desk(f.root,async () => latest,async (_path, body) => {
    writes.push(JSON.parse(JSON.stringify(body)));
    if (body.rev!==latest.rev) throw Object.assign(new f.w.Error('The quiz changed. Review the latest state and try again.'),{status:409});
    latest = {...latest,rev:latest.rev+1,bank:[{...latest.bank[0],...body.changes}]};
    return latest;
  });
  await f.click('Timeline');
  await f.click('Edit');
  const prompt = await f.until(() => f.root.querySelector('.quiz-editor textarea'),'the draft');
  f.fill(prompt,'My draft question');
  latest = {...latest,rev:5,bank:[{...question,prompt:'Other organizer’s question',cue:'New organizer cue'}]};
  // The other organizer writes before this tab receives its next poll.
  await f.click('Save draft');
  await f.until(() => /The quiz changed/.test(f.root.textContent),'the stale revision refusal');
  assert.equal(writes[0].rev,4);
  assert.equal(prompt.value,'My draft question');
  await f.poll(2000);
  await f.click('Review latest and keep edits');
  await f.until(() => /Other organizer’s question/.test(f.root.querySelector('.quiz-editor').textContent),'the overlapping question edit');
  assert.equal(prompt.value,'My draft question');
  assert.ok([...f.root.querySelectorAll('.quiz-editor textarea')].some(field=>field.value==='New organizer cue'));
  assert.equal(writes.length,1);
  await f.click('Save draft');
  await f.until(() => !f.root.querySelector('.quiz-editor'),'save completion');
  assert.deepEqual(writes[1],{action:'edit',qid:'W2',changes:{prompt:'My draft question'},rev:5});
});

test('a revision received by polling cannot silently overwrite another organizer’s draft', async t => {
  const f = fixture(t);
  let latest = deskState;
  f.w.EventQuiz.desk(f.root,async () => latest,async () => assert.fail('A stale edit must not be sent'));
  await f.click('Timeline');
  await f.click('Edit');
  const prompt = await f.until(() => f.root.querySelector('.quiz-editor textarea'),'the draft');
  f.fill(prompt,'My draft question');
  latest = {...latest,rev:5,bank:[{...question,prompt:'Other organizer’s question'}]};
  await f.poll(2000);
  await f.until(() => /Other organizer’s question/.test(f.root.querySelector('.quiz-bank-row').textContent),'the polled question');
  assert.equal(prompt.value,'My draft question');
  assert.equal(f.findButton('Save draft').disabled,true);
  assert.ok(f.findButton('Review latest and keep edits'));
});

test('timing changes preview every shifted cue and apply only the reviewed revision', async t => {
  const f = fixture(t), writes = [];
  const nextQuestion = {...question,id:'W3',prompt:'How do you open a pitch?',time:'10:08',cue_offset_minutes:8};
  const bank = [{...question,cue_offset_minutes:0},nextQuestion];
  let latest = {...deskState,bank,phase_baselines:{welcome:'10:00',mac:'10:10'}};
  f.w.EventQuiz.desk(f.root,async () => latest,async (_path, body) => {
    writes.push(JSON.parse(JSON.stringify(body)));
    if (body.action==='retime_preview') return {rev:body.rev,changes:bank.map((item,index)=>({
      qid:item.id,prompt:item.prompt,before:{phase:item.phase,time:item.time,offset_minutes:item.cue_offset_minutes},
      after:{phase:item.phase,time:index?'10:11':'10:03',offset_minutes:item.cue_offset_minutes+3}
    }))};
    assert.equal(body.action,'retime');
    latest = {...latest,rev:latest.rev+1,bank:bank.map(item=>({...item,cue_offset_minutes:item.cue_offset_minutes+3}))};
    return latest;
  });
  await f.click('Timeline');
  await f.click('Change time or section');
  await f.until(() => f.root.querySelector('dialog[open]'),'timing editor');
  f.fill(f.root.querySelector('dialog input[type=number]'),'3');
  f.root.querySelector('dialog input[type=checkbox]').click();
  await f.click('Preview timing changes');
  await f.until(() => /After · Welcome · 11 min after section start/.test(f.root.textContent),'shifted later cue');
  assert.deepEqual(writes,[{action:'retime_preview',rev:4,qid:'W2',time:'10:03',shift_following:true,phase:'welcome'}]);
  await f.click('Apply timing changes');
  await f.until(() => !f.root.querySelector('dialog[open]'),'saved timing change');
  assert.deepEqual(writes.map(body=>[body.action,body.rev]),[['retime_preview',4],['retime',4]]);
});

test('a cue moved by another organizer requires reloading it before preview or apply', async t => {
  const f = fixture(t), writes = [];
  const bank = [{...question,cue_offset_minutes:0}];
  let latest = {...deskState,bank,phase_baselines:{welcome:'10:00',mac:'10:10'}};
  f.w.EventQuiz.desk(f.root,async () => latest,async (_path, body) => {
    writes.push(JSON.parse(JSON.stringify(body)));
    return {rev:body.rev,changes:[{qid:question.id,prompt:question.prompt,
      before:{phase:'mac',time:'10:12',offset_minutes:2},after:{phase:'mac',time:'10:13',offset_minutes:3}}]};
  });
  await f.click('Timeline');
  await f.click('Change time or section');
  const input = await f.until(() => f.root.querySelector('dialog input[type=number]'),'cue timing input');
  f.fill(input,'6');
  latest = {...latest,rev:5,bank:[{...bank[0],phase:'mac',time:'10:12',cue_offset_minutes:2}]};
  await f.poll(2000);
  await f.until(() => f.findButton('Preview timing changes').disabled,'preview blocked on changed cue');
  assert.equal(input.value,'6');
  assert.deepEqual(writes,[]);
  await f.click('Load latest cue');
  const fresh = await f.until(() => {
    const value = f.root.querySelector('dialog input[type=number]');
    return value?.value==='2' && value;
  },'new cue offset');
  assert.equal(f.root.querySelector('dialog select').value,'mac');
  f.fill(fresh,'3');
  await f.click('Preview timing changes');
  await f.until(() => f.findButton('Apply timing changes'),'an applicable reviewed preview');
  assert.deepEqual(writes,[{action:'retime_preview',rev:5,qid:'W2',time:'10:13',shift_following:false,phase:'mac'}]);
  latest = {...latest,rev:6};
  await f.poll(2000);
  await f.until(() => !f.findButton('Apply timing changes'),'apply blocked when the preview revision changed');
  assert.equal(writes.length,1);
});

test('quiz reset and restore require a reason, the exact phrase and the current revision', async t => {
  const f = fixture(t), writes = [];
  let latest = {...deskState,runs:[openRun],reset_archives:[],restorable_reset_id:null};
  f.w.EventQuiz.desk(f.root,async () => latest,async (_path, body) => {
    writes.push(JSON.parse(JSON.stringify(body)));
    const archive = {id:'archived1',at:101,reason:'Finished rehearsal',run_count:1,answer_count:2};
    latest = {...latest,rev:latest.rev+1,enabled:false,runs:body.action==='reset'?[]:[{...openRun,status:'closed'}],
      reset_archives:[archive],restorable_reset_id:body.action==='reset'?archive.id:null};
    return latest;
  });
  await f.click('Setup');
  const danger = await f.until(() => f.root.querySelector('.quiz-danger'),'reset and recovery');
  assert.equal(danger.open,false);
  danger.open=true;
  await f.click('Reset quiz');
  let dialog = await f.until(() => f.root.querySelector('dialog[open]'),'reset confirmation');
  assert.match(dialog.textContent,/1 quiz run and 2 submitted answers/);
  assert.match(dialog.textContent,/sign-ups are kept/);
  const button = () => dialog.querySelector('button[type=submit]');
  assert.equal(button().disabled,true);
  f.fill(dialog.querySelector('textarea'),'Finished rehearsal');
  f.fill(dialog.querySelector('input'),'reset quiz');
  assert.equal(button().disabled,true);
  f.fill(dialog.querySelector('input'),'RESET QUIZ');
  (await f.until(() => !button().disabled && button(),'reset can be confirmed')).click();
  await f.until(() => !f.root.querySelector('dialog[open]'),'reset submitted');
  assert.deepEqual(writes[0],{action:'reset',rev:4,confirm:'RESET QUIZ',reason:'Finished rehearsal'});
  await f.click('Restore quiz archive');
  dialog = await f.until(() => f.root.querySelector('dialog[open]'),'restore confirmation');
  assert.match(dialog.textContent,/stays off/);
  f.fill(dialog.querySelector('textarea'),'Need rehearsal results');
  f.fill(dialog.querySelector('input'),'RESTORE QUIZ');
  (await f.until(() => !button().disabled && button(),'restore can be confirmed')).click();
  await f.until(() => !f.root.querySelector('dialog[open]'),'restore submitted');
  assert.deepEqual(writes[1],{action:'restore_reset',rev:5,archive_id:'archived1',confirm:'RESTORE QUIZ',reason:'Need rehearsal results'});
  assert.match(f.root.querySelector('.quiz-console-status').textContent,/Quiz off/);
});

test('the timeline starts with the current section and preserves an explicit selection', async t => {
  const f = fixture(t);
  let latest = {...deskState,bank:[question,{...question,id:'M1',phase:'mac',prompt:'Choose the right solution device'}]};
  f.w.EventQuiz.desk(f.root,async () => latest,async () => assert.fail('Unexpected write'));
  await f.click('Timeline');
  const field = await f.until(() => f.root.querySelector('.quiz-filter select'),'timeline filter');
  assert.equal(field.value,'welcome');
  assert.doesNotMatch(f.root.textContent,/Choose the right solution device/);
  field.value='all';
  field.dispatchEvent(new f.w.Event('change',{bubbles:true}));
  await f.until(() => /Choose the right solution device/.test(f.root.textContent),'all planned questions');
  await f.click('Run quiz');
  latest = {...latest,phase:'mac',title:'Solution at Work'};
  await f.poll(2000);
  await f.click('Timeline');
  await f.until(() => f.root.querySelector('.quiz-filter select')?.value==='all','selection preserved');
  assert.match(f.root.querySelector('.quiz-timeline-section[data-current=true]').textContent,/Choose the right solution device/);
});

test('the desk explains unavailable send, hall and award actions from current readiness', async t => {
  const f = fixture(t), writes = [];
  let latest = {...deskState,bank:[{...question,can_send:false,send_blocked_reason:'Wait for the active Final pitch to end.'}]};
  f.w.EventQuiz.desk(f.root,async () => latest,async (_path, body) => {writes.push(body);return latest;});
  await f.until(() => /Wait for the active Final pitch to end/.test(f.root.textContent),'send blocker');
  assert.equal(f.findButton('Send to phones'),undefined);
  latest = {...latest,phase:'awards',rev:5,runs:[{...openRun,status:'closed',released:true,can_show:false,
    show_blocked_reason:'The hall is busy with the programme.'}],
    operator:{can_champion:false,champion_blocked_reason:'Publish the confirmed Final results first.'}};
  await f.poll(2000);
  await f.click('Results');
  await f.until(() => /Publish the confirmed Final results first/.test(f.root.textContent),'champion blocker');
  assert.match(f.root.textContent,/The hall is busy with the programme/);
  assert.equal(f.findButton('Show results on room screen'),undefined);
  assert.equal(f.findButton('Announce champion and prize'),undefined);
  assert.equal(writes.length,0);
});

test('a cancelled question sends the reviewed run ID and removes it from scoring', async t => {
  const f = fixture(t), writes = [];
  const latest = {...deskState,runs:[openRun]};
  f.w.EventQuiz.desk(f.root,async () => latest,async (_path, body) => {
    writes.push(JSON.parse(JSON.stringify(body)));
    return {...latest,rev:5,runs:[{...openRun,status:'void'}]};
  });
  await f.click('Cancel question');
  assert.equal(writes.length,0);
  await f.click('Confirm cancellation');
  await f.until(() => /Question cancelled and removed from scoring/.test(f.root.textContent),'confirmed cancellation');
  assert.deepEqual(writes,[{action:'void',rev:4,id:'W2-5'}]);
});

test('a confirmed catch-up can appear over a paused preliminary screen but never an active pitch', async t => {
  const f = fixture(t);
  let latest = {enabled:true,joined:true,now:100,busy:true,questions:[],history:[],score:{earned:0,available:0,pending:0}};
  f.w.KEV = {api:async () => latest};
  const view = code => f.w.EventQuiz.participant({workspace:{},action:{code}});
  view('paused');
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(f.root.querySelector('.quiz-badge'),null);
  latest = {...latest,busy:false,catchup_allowed:true,questions:[{...question,id:'waiting-1',kind:'waiting',end:120,session:'catchup'}]};
  await f.poll(1200);
  await f.until(() => f.root.querySelector('legend'),'a catch-up prompt for the paused team');
  assert.match(f.root.textContent,/How long is a pitch\?/);
  assert.equal(f.root.querySelector('dialog'),null);
  view('pitch');
  await f.until(() => !f.root.querySelector('legend'),'the quiz hides during an active pitch');
  assert.equal(f.root.querySelector('.quiz-badge'),null);
});
