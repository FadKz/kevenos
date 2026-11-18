const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const {readFileSync}=require('node:fs');
const tick=()=>new Promise(r=>setImmediate(r));
const source=readFileSync('static/event-operations.js','utf8');
function setup(participant=true){
 const dom=new JSDOM('<main id="host"></main>',{url:'https://fixture.test/participant',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,calls=[];let fail=false;
 const state={categories:{arrival:'Registration'},requests:[]};
 w.KEV={esc:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),profile:{revision:1},staff:{token:''},api:async(path,options)=>{calls.push(options.body||null);if(fail&&options.body)throw new Error('Network unavailable');return structuredClone(state);}};
 w.eval(source);w.KEV.operations.mount(w.document.getElementById('host'),participant);
 return {dom,w,calls,state,setFail:v=>fail=v};
}
test('help retry keeps draft and request identity; profile end clears all private content',async()=>{
 const t=setup();try{await tick();const form=t.w.document.querySelector('form');form.elements.message.value='Find my teammate';t.setFail(true);form.dispatchEvent(new t.w.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.equal(form.elements.message.value,'Find my teammate');const first=t.calls.at(-1);form.dispatchEvent(new t.w.Event('submit',{bubbles:true,cancelable:true}));await tick();assert.equal(first.request_id,t.calls.at(-1).request_id);t.w.KEV.operations.clear();assert.equal(t.w.document.querySelector('#host').textContent,'');}finally{t.dom.window.close();}
});
test('private message and staff reply render as text, never executable markup',async()=>{
 const t=setup();try{t.state.requests=[{id:'x',category:'arrival',message:'<img src=x onerror=alert(1)>',reply:'<script>bad()</script>',status:'resolved'}];await tick();t.w.document.querySelector('[data-refresh]').click();await tick();assert.equal(t.w.document.querySelector('img,script'),null);assert.match(t.w.document.body.textContent,/<script>bad/);}finally{t.dom.window.close();}
});

test('late response from disposed instance cannot clear replacement panel',async()=>{
 const t=setup();try{await tick();let resolve;const pending=new Promise(r=>resolve=r);const original=t.w.KEV.api;t.w.KEV.api=()=>pending;t.w.document.querySelector('[data-refresh]').click();t.w.KEV.operations.clear();t.w.KEV.profile.revision++;t.w.KEV.api=original;t.w.KEV.operations.mount(t.w.document.getElementById('host'),true);await tick();resolve(structuredClone(t.state));await tick();assert.match(t.w.document.getElementById('host').textContent,/Need help/);}finally{t.dom.window.close();}
});
test('blurred staff reply survives refresh and stale write rejection',async()=>{
 const t=setup();try{await tick();t.w.KEV.operations.clear();Object.assign(t.state,{revision:1,facts:{checked_in:0,participants:1,capacity:40,participant_access_used:0,participant_passwords:1,missing_judge_stations:[],schedule_warnings:[]},checks:{},notes:[],agenda:[],program:{index:0},requests:[{id:'a',name:'Fixture',team:1,status:'open',message:'Help',reply:''}]});t.w.KEV.operations.mount(t.w.document.getElementById('host'));await tick();const reply=t.w.document.querySelector('[data-reply]');reply.value='Unsent reply in progress';reply.dispatchEvent(new t.w.Event('input',{bubbles:true}));t.w.document.querySelector('[data-refresh]').click();await tick();assert.equal(t.w.document.querySelector('[data-reply]').value,'Unsent reply in progress');t.setFail(true);t.w.document.querySelector('[data-action="resolve"]').click();await tick();assert.equal(t.w.document.querySelector('[data-reply]').value,'Unsent reply in progress');}finally{t.dom.window.close();}
});

test('the Organizer help inbox exposes replies and delivery remains in its own view', async () => {
 const t=setup();try {
  await tick();
  t.w.KEV.operations.clear();
  Object.assign(t.state,{revision:1,requests:[{id:'a',name:'Test participant',team:1,status:'open',category:'arrival',message:'Where should I sit?',reply:''}],push:{devices:2,events:[]}});
  const host=t.w.document.getElementById('host');
  t.w.KEV.operations.mount(host,false,'help');
  await tick();
  assert.match(host.textContent,/Help inbox.*1 awaiting a reply/s);
  const reply = host.querySelector('[data-reply]');
  assert.equal(reply.value,'');
  reply.value = 'A private draft';
  reply.dispatchEvent(new t.w.Event('input',{bubbles:true}));
  host.querySelector('[data-action="claim"]').click();
  await tick();
  assert.equal(t.calls.at(-1).status, 'claimed');
  assert.equal(t.calls.at(-1).reply, '');
  assert.equal(host.querySelector('[data-reply]').value,'A private draft');
  assert.equal(host.querySelector('[data-push-history]'),null);
  t.w.KEV.operations.clear();
  t.w.KEV.operations.mount(host,false,'delivery');
  await tick();
  assert.match(host.textContent,/Notification delivery.*2 opted-in devices/s);
  assert.equal(host.querySelector('[data-reply]'),null);
 }finally{t.dom.window.close();}
});

test('Desk status redraw retains the exact operations host and blurred draft',()=>{
 const source=readFileSync('static/desk.html','utf8');const body=source.slice(source.indexOf('function vHome()'),source.indexOf('// ───────── Run'));
 const dom=new JSDOM('<main id="main"><div id="desk-home"><div id="operations-home"><textarea>Keep my handover</textarea></div></div></main>',{runScripts:'outside-only'});try{const w=dom.window;w.eval(`const $=id=>document.getElementById(id);let lastSig='old';const DS={program:{i:0},stations:[]},C={agenda:[{title:'Current'},{title:'Next',time:'10:00'}]},dashboard='organizer',TABS={display:1},KEV={esc:String,focusedControl:()=>()=>{},operations:{mount:()=>{}}};function select(){};${body};vHome();`);assert.equal(w.document.querySelector('textarea').value,'Keep my handover');assert.equal(w.document.querySelectorAll('#operations-home').length,1);}finally{dom.window.close();}
});
