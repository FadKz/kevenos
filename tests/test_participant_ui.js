// Focused participant companion behavior against mocked HTTP boundaries.
// NODE_PATH=/tmp/pitch-rehearsal-20261112/js/node_modules node tests/test_participant_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {JSDOM, VirtualConsole} = require('jsdom');
const root = path.resolve(__dirname, '..');
const clone = value => JSON.parse(JSON.stringify(value));
const guide = JSON.parse(fs.readFileSync(path.join(root, 'event-guide.json'), 'utf8'));
const participant = {name:'Alex Example',pair:7,partner:'Example company',station:3,round:2,scenario:'S2',teammates:['Sam Example','Jo Example'],team_size:3};
const snapshot = () => ({participant:clone(participant),now:1794500000,rehearsal:false,program:{title:'Pitch rounds',line:'Teams pitch when called.',instructions:[],next_title:'Break'},action:{code:'wait',title:'Wait for your team to be called',instruction:'Stay at your table and practise quietly.',detail:'Follow the host and your judge.',station:3,round:2,clock:null},result:{status:'awaiting',label:'Your result will appear after the Desk releases it.',total:null,rank:null,kind:'round'},announcement:null,final:{status:'idle',pairs:[],qualified:null}});
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
const errors = [];
async function load(options={}) {
  let state = options.state || snapshot(), role = options.role || 'participant', failed = options.failed || null, held = null;
  const calls = [], timers = [];
  let html = fs.readFileSync(path.join(root,'static/participant.html'),'utf8');
  html = html.replace('<script src="/static/common.js"></script>','<script>'+fs.readFileSync(path.join(root,'static/common.js'),'utf8')+'</script>');
  const vc = new VirtualConsole(); vc.on('jsdomError',error=>{if(error.type !== 'not-implemented') errors.push(error.message);});
  const dom = new JSDOM(html,{url:'https://example.test/participant',runScripts:'dangerously',pretendToBeVisual:true,virtualConsole:vc,beforeParse(w){
    w.setInterval=(fn,ms)=>{timers.push({fn,ms});return timers.length;};
    w.fetch=async(url,opts)=>{
      calls.push({url,opts});
      if(url === '/api/participant/state' && held) await held;
      if(url === '/api/participant/state' && failed){if(failed instanceof Error)throw failed;return {ok:false,status:failed,headers:{get:()=>null},json:async()=>({error:'Unavailable'})};}
      const data = url === '/api/info' ? {version:{app:'4.4.0',content:1}} : url === '/api/participant/state' ? state : url === '/api/event' ? {role,guide,participant:state.participant,resources:guide.resources,now:{}} : {};
      return {ok:true,status:200,headers:{get:()=>null},json:async()=>clone(data)};
    };
    if(options.beforeParse) options.beforeParse(w);
  }});
  await tick(); await tick();
  return {dom,calls,timers,$:id=>dom.window.document.getElementById(id),setState:value=>{state=value;},fail:value=>{failed=value;},setRole:value=>{role=value;},hold:value=>{held=value;},async poll(){dom.window.eval('refresh()');await tick();await tick();},close:()=>dom.window.close()};
}
async function test(name,run){try{await run();console.log('PASS '+name);}catch(error){console.error('FAIL '+name+': '+error.stack);process.exitCode=1;}}
(async()=>{
  await test('participant sees current action and a no-preparation starting point',async()=>{
    const page=await load();
    try{assert.equal(page.$('now-title').textContent,'Wait for your team to be called');assert.match(page.$('quickstart').textContent,/no preparation needed/i);assert.match(page.$('team-members').textContent,/Sam Example/);assert.match(page.$('team-members').textContent,/Jo Example/);}finally{page.close();}
  });

  await test('sanitized package exposes no event-specific downloadable materials',async()=>{
    const page=await load();
    try {
      const links=[...page.$('documents').querySelectorAll('[data-resource]')];
      assert.deepEqual(links.map(link=>link.dataset.resource),[]);
      assert.match(page.$('materials-note').textContent,/Ask the event desk/);
      assert.equal(page.$('quickstart').open,false);
      assert.equal(page.$('quickstart').closest('[data-team-panel]').dataset.teamPanel,'more');
      assert.match(page.$('quickstart').textContent,/Every team member must speak/);
      assert.match(page.$('quickstart').textContent,/10 minutes to pitch, 3 minutes for feedback/);
      assert.match(page.dom.window.document.body.textContent,/Optional product vendor reading/);
      assert(!page.$('checklist').textContent.includes('Read the Participant Guide, try'));
      assert(!/60 participants|8 judges|with your partner|both.*speak/i.test(page.$('quickstart').textContent));
      assert(page.$('materials-title').compareDocumentPosition(page.$('steps-title')) & page.dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
    } finally {page.close();}
  });
  await test('results are gated by release and disappear again if release is withdrawn',async()=>{
    const state=snapshot(); state.result={status:'under_review',label:'Under review.',total:99,rank:1,kind:'round'};
    const page=await load({state});
    try {
      assert(page.$('result-score').hidden);assert.equal(page.$('result-score').textContent,'');
      state.result.status='released';page.setState(state);await page.poll();
      assert.equal(page.$('result-score').textContent,'99 points · Current place 1');assert(!page.$('result-score').hidden);assert.match(page.$('result-title').textContent,/preliminary/);assert.match(page.$('result-label').textContent,/placing can change/);
      state.result.status='under_review';page.setState(state);await page.poll();
      assert.equal(page.$('result-score').textContent,'');assert(page.$('result-score').hidden);
      assert(!page.calls.some(call=>call.url.startsWith('/api/public')));
    } finally {page.close();}
  });
  await test('Next follows the participant session instead of skipping ahead in the programme',async()=>{
    const state=snapshot();state.action.code='pitch';
    const page=await load({state});
    try {
      assert.match(page.$('next-title').textContent,/feedback/);assert(!page.$('next-title').textContent.includes('Break'));
      state.action.code='feedback';page.setState(state);await page.poll();assert.match(page.$('next-title').textContent,/return to your table/);
      state.action.code='program';page.setState(state);await page.poll();assert.equal(page.$('next-title').textContent,'Break');assert.equal(page.$('next-label').textContent,'Next in the programme');
    } finally {page.close();}
  });
  await test('network failure shows stale instruction and retry, without an advancing clock',async()=>{
    const state=snapshot();state.action.clock={kind:'round',phase:'pitch',running:true,elapsed:120,round_left:780,step_left:180,started_any:true,ended:false};
    const page=await load({state});
    try {
      assert.match(page.$('now-clock').textContent,/8:00 left at last update/);
      const clock=page.$('now-clock').textContent;
      page.timers.find(timer=>timer.ms===1000).fn();assert.equal(page.$('now-clock').textContent,clock);
      page.fail(new Error('offline'));await page.poll();
      assert.equal(page.$('connection').textContent,'Updates interrupted');assert.match(page.$('last-updated').textContent,/event time/);
      assert.equal(page.$('now-label').textContent,'Last known instruction');assert(page.$('now-clock').hidden);assert(!page.$('retry-updates').hidden);
      page.fail(null);page.$('retry-updates').click();await tick();await tick();
      assert.equal(page.$('connection').textContent,'Connected');assert(!page.$('now-clock').hidden);
    } finally {page.close();}
  });
  await test('stalled updates become visibly stale after fifteen seconds',async()=>{
    const page=await load();
    try {
      const now=page.dom.window.Date.now();page.dom.window.Date.now=()=>now+16000;
      page.timers.find(timer=>timer.ms===1000).fn();
      assert.equal(page.$('connection').textContent,'Updates interrupted');
    } finally {page.close();}
  });
  await test('polls do not overlap and hidden tabs refresh once on returning',async()=>{
    const page=await load();let resolve;
    try {
      const gate=new Promise(done=>{resolve=done;});page.hold(gate);
      const before=page.calls.filter(call=>call.url==='/api/participant/state').length;
      await page.poll();await page.poll();
      assert.equal(page.calls.filter(call=>call.url==='/api/participant/state').length,before+1);
      resolve();page.hold(null);await tick();await tick();
      Object.defineProperty(page.dom.window.document,'hidden',{value:true,configurable:true});await page.poll();
      assert.equal(page.calls.filter(call=>call.url==='/api/participant/state').length,before+1);
      Object.defineProperty(page.dom.window.document,'hidden',{value:false,configurable:true});
      page.dom.window.document.dispatchEvent(new page.dom.window.Event('visibilitychange'));await tick();await tick();
      assert.equal(page.calls.filter(call=>call.url==='/api/participant/state').length,before+2);
      for(const call of page.calls.filter(call=>['/api/event','/api/participant/state'].includes(call.url))){assert.equal(call.opts.credentials,'same-origin');assert.equal(call.opts.cache,'no-store');}
    } finally {if(resolve)resolve();page.close();}
  });
  await test('expired session removes private DOM and stops participant polling',async()=>{
    const page=await load();
    try {
      page.fail(401);await page.poll();
      assert(page.$('guide').hidden);assert(!page.$('signin').hidden);assert(page.$('participant-help').hidden);
      assert(!page.dom.window.document.body.textContent.includes('Alex Example'));assert(!page.dom.window.document.body.textContent.includes('Sam Example'));
      assert.equal(page.$('documents').children.length,0);
      const count=page.calls.length;await page.poll();assert.equal(page.calls.length,count);
    } finally {page.close();}
  });
  await test('wrong role cannot open the participant guide',async()=>{
    const page=await load({failed:403});
    try {assert(page.$('guide').hidden);assert(!page.$('switch-access').hidden);assert(!page.calls.some(call=>call.url==='/api/event'));assert.equal(page.$('documents').children.length,0);}finally{page.close();}
  });
  await test('initial network failure provides a working retry',async()=>{
    const page=await load({failed:new Error('offline')});
    try {assert(page.$('guide').hidden);assert(!page.$('retry').hidden);page.fail(null);page.$('retry').click();await tick();await tick();assert(!page.$('guide').hidden);}finally{page.close();}
  });
  await test('changed assignment refreshes team details without packaged event materials',async()=>{
    const page=await load();
    try {
      const state=snapshot();state.participant={...state.participant,station:2,scenario:'S1',teammates:['New teammate']};page.setState(state);await page.poll();
      assert.match(page.$('team-members').textContent,/New teammate/);assert(!page.$('team-members').textContent.includes('Sam Example'));
      assert.equal(page.$('documents').children.length,0);
      assert.equal(page.calls.filter(call=>call.url==='/api/event').length,2);
      await page.poll();assert.equal(page.calls.filter(call=>call.url==='/api/event').length,2);
    } finally {page.close();}
  });
  await test('announcement is escaped, timestamped in event time, and absent when cleared',async()=>{
    const state=snapshot();state.announcement={text:'<img src=x onerror=alert(1)> Meet near the stage',updated_at:1794500000};
    const page=await load({state});
    try {
      assert(!page.$('announcement').hidden);assert.equal(page.$('announcement-text').querySelector('img'),null);
      assert.match(page.$('announcement-time').textContent,/event time/);assert(page.$('announcement-time').dateTime.endsWith('Z'));
      const liveNode=page.$('live-changes').firstChild;
      state.now+=5;page.setState(state);await page.poll();assert.equal(page.$('live-changes').firstChild,liveNode);
      state.announcement=null;page.setState(state);await page.poll();assert(page.$('announcement').hidden);assert.equal(page.$('announcement-text').textContent,'');
    } finally {page.close();}
  });
  await test('finalists retain original materials and all four parts share eight minutes',async()=>{
    const state=snapshot();state.final={status:'pitch',qualified:true,pairs:[{pair:7},{pair:8}],current:0};
    const page=await load({state});
    try {
      assert.match(page.$('participant-final').textContent,/same scenario you pitched earlier/);
      assert.match(page.$('assignment-note').textContent,/main stage/);assert(!page.$('assignment-note').textContent.includes('Station 3'));assert.match(page.$('facts').textContent,/Preliminary station/);
      assert(page.$('participant-final').compareDocumentPosition(page.$('quickstart')) & page.dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
      assert.match(page.$('steps-title').textContent,/8 minutes in total/);assert.equal(page.$('steps').querySelectorAll('.pill').length,0);
      assert.equal(page.$('documents').children.length,0);
      state.final={status:'pitch',qualified:false,pairs:[{pair:8}],current:0};page.setState(state);await page.poll();assert.match(page.$('participant-final').textContent,/audience/);
    } finally {page.close();}
  });
  await test('Final review and publication park stage calls even if the programme still points to a break',async()=>{
    const state=snapshot();state.final={status:'review',qualified:true,pairs:[{pair:7}],current:0};state.action.code='final_review';
    const page=await load({state});
    try {
      assert.match(page.$('participant-final').textContent,/panel is confirming/);assert(!page.$('participant-final').textContent.includes('Go to the main stage'));
      state.final.status='completed';state.action.code='final_complete';state.result={status:'released',label:'Published Final result.',kind:'final',total:95,rank:2};page.setState(state);await page.poll();
      assert.match(page.$('next-title').textContent,/awards and closing/);assert(!page.$('next-title').textContent.includes('Break'));
      assert.match(page.$('participant-final').textContent,/published result is shown below/);assert(!page.$('participant-final').textContent.includes('Go to the main stage'));
      assert.equal(page.$('result-score').textContent,'95 points · Place 2');assert(!page.$('result-label').textContent.includes('placing can change'));
    } finally {page.close();}
  });
  assert.deepEqual(errors,[]);
})().catch(error=>{console.error(error);process.exitCode=1;});
