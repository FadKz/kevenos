const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '..');
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const person = {name:'Synthetic Participant', pair:1, station:1, round:1, scenario:'S1', teammates:[]};
const agenda = (time='09:30') => [{time, title:'Welcome', line:'Join your team.', detail:'Join your team.', min:30}];
const guide = (version=1, time='09:30') => ({title:'Synthetic event', subtitle:'Practice', intro:'Welcome', date:'Today', venue:'Test hall', content_version:version, schedule:agenda(time), scenarios:[], essentials:[], scoring:[], reading:[], steps:[]});
const state = (version=1) => ({participant:clone(person), content_version:version, now:1794500000, action:{code:'program',title:'Welcome',instruction:'Meet your team.',detail:''}, program:{next_title:'Break'}, result:{status:'awaiting',label:'Not released'}, final:{status:'idle',qualified:null,pairs:[]}, announcement:null});

function page(kind, api) {
  const html = fs.readFileSync(path.join(root,'static',kind+'.html'),'utf8');
  const dom = new JSDOM(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,''), {url:'https://example.test/'+kind, runScripts:'outside-only', pretendToBeVisual:true});
  const w=dom.window, timers=[];
  w.setInterval=(fn,ms)=>{timers.push({fn,ms});return timers.length;};
  w.clearInterval=()=>{};
  const KEV={esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), api,
    context:'rehearsal-context',workspaceChanging:false,staff:{hosted:true},nav:{render(){}},profile:{revision:1,mount(hooks){this.hooks=hooks;},signOut(){}},
    closeHelp(){},bindHelp(){},finalStatus:f=>f?.status||'',finalLabel:()=> 'Final',mmss:s=>String(s),getResource(){}};
  w.KEV=KEV;
  const source=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
  w.eval(source);
  return {dom,w,KEV,timers,$:id=>w.document.getElementById(id),close:()=>w.close(),async settle(){await tick();await tick();},async run(name){await w.eval(name+'()');await tick();}};
}

test('Guest full schedule follows timing-only polls and preserves open sections', async () => {
  let current=agenda(), reads=0;
  const p=page('guest', async url=> url==='/api/event' ? {role:'guest',guide:guide(),now:{program:{index:0,items:current}}} : (reads++,{program:{index:0,items:current}}));
  try {
    await p.settle();
    p.w.scrollTo=()=>{};p.w.document.querySelector('[data-guest-tab=agenda]').click();
    const disclosure=p.$('agenda').closest('[data-guest-panel]');
    assert.match(p.$('agenda').textContent,/09:30/);
    current=agenda('09:45'); await p.run('refreshProgramme');
    assert.match(p.$('agenda').textContent,/09:45/);
    assert.doesNotMatch(p.$('agenda').textContent,/09:30/);
    assert.match(p.$('agenda').textContent,/30 min/);
    assert.equal(disclosure.hidden,false);assert.equal(reads,1);
    assert.doesNotMatch(p.$('attend-title').parentElement.textContent,/09:30|10:40|12:50|13:25|14:00/);
  } finally {p.close();}
});

test('Guest late programme response cannot restore content after profile ends', async () => {
  const gate=deferred();let polling=false;
  const p=page('guest',async url=>url==='/api/event'?{role:'guest',guide:guide(),now:{program:{index:0,items:agenda()}}}: (polling=true,gate.promise));
  try {
    await p.settle();const pending=p.w.eval('refreshProgramme()');await tick();assert(polling);
    p.KEV.profile.hooks.onEnd('Profile ended');
    gate.resolve({program:{index:0,items:[{time:'14:15',title:'Private old response',line:'Old data',min:20}]}});
    await pending;
    assert(p.$('overview').hidden);assert.equal(p.$('agenda').textContent,'');
    assert.doesNotMatch(p.dom.window.document.body.textContent,/Private old response/);
  } finally {p.close();}
});

test('Guest older load cannot replace a newer schedule refresh', async () => {
  const gate=deferred();let calls=0;
  const p=page('guest',async()=>++calls===1?gate.promise:{role:'guest',guide:guide(2,'10:05'),now:{program:{index:0,items:agenda('10:05')}}});
  try {
    await tick();await p.run('load');assert.match(p.$('agenda').textContent,/10:05/);
    gate.resolve({role:'guest',guide:guide(),now:{program:{index:0,items:agenda()}}});await p.settle();
    assert.match(p.$('agenda').textContent,/10:05/);assert.doesNotMatch(p.$('agenda').textContent,/09:30/);
  } finally {p.close();}
});

test('Participant refreshes timing-only content once and keeps guide disclosure state', async () => {
  let version=1,time='09:30',eventReads=0;
  const p=page('participant',async url=>url==='/api/participant/state'?state(version):(eventReads++,{role:'participant',participant:clone(person),guide:guide(version,time),resources:[]}));
  try {
    await p.settle();assert.equal(eventReads,1);
    const quickstart=p.$('quickstart');quickstart.open=false;
    const disclosure=()=>p.$('agenda').querySelector('details');disclosure().open=true;
    const facts=p.$('facts').firstChild;
    version=2;time='09:50';await p.run('refresh');
    assert.equal(eventReads,2);assert.match(p.$('agenda').textContent,/09:50/);
    assert.match(p.$('agenda').textContent,/30 min/);
    assert.equal(quickstart.open,false);assert(disclosure().open);assert.equal(p.$('facts').firstChild,facts);
    await p.run('refresh');assert.equal(eventReads,2);
    assert(!p.$('guide').hidden);
  } finally {p.close();}
});

test('Participant does not combine a newer guide with an older polled revision', async () => {
  let currentVersion=1,guideVersion=1;
  const p=page('participant',async url=>url==='/api/participant/state'?state(currentVersion):{role:'participant',participant:clone(person),guide:guide(guideVersion,guideVersion===1?'09:30':'10:10'),resources:[]});
  try {
    await p.settle();currentVersion=2;guideVersion=3;await p.run('refresh');
    assert.match(p.$('agenda').textContent,/09:30/);assert.doesNotMatch(p.$('agenda').textContent,/10:10/);
    assert.equal(p.$('connection').textContent,'Updates interrupted');
    currentVersion=3;await p.run('refresh');assert.match(p.$('agenda').textContent,/10:10/);
  } finally {p.close();}
});

test('Participant late guide response cannot restore private DOM after profile ends', async () => {
  let version=1,hold=false,waiting=false;const gate=deferred();
  const p=page('participant',async url=>url==='/api/participant/state'?state(version):hold?(waiting=true,gate.promise):{role:'participant',participant:clone(person),guide:guide(),resources:[]});
  try {
    await p.settle();version=2;hold=true;
    const pending=p.w.eval('refresh()');await tick();await tick();assert(waiting);
    p.KEV.profile.hooks.onEnd('Profile ended');
    gate.resolve({role:'participant',participant:clone(person),guide:guide(2,'10:40'),resources:[]});await pending;
    assert(p.$('guide').hidden);assert.equal(p.$('agenda').textContent,'');
    assert.doesNotMatch(p.dom.window.document.body.textContent,/Synthetic Participant/);
  } finally {p.close();}
});

test('Participant ignores a guide from the old workspace context', async () => {
  let version=1,hold=false;const gate=deferred();
  const p=page('participant',async url=>url==='/api/participant/state'?state(version):hold?gate.promise:{role:'participant',participant:clone(person),guide:guide(),resources:[]});
  try {
    await p.settle();version=2;hold=true;
    const pending=p.w.eval('refresh()');await tick();await tick();
    p.KEV.context='changed-workspace-context';p.KEV.workspaceChanging=true;
    gate.resolve({role:'participant',participant:clone(person),guide:guide(2,'10:40'),resources:[]});await pending;
    assert.doesNotMatch(p.$('agenda').textContent,/10:40/);
  } finally {p.close();}
});
