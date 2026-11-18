const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const html=fs.readFileSync('static/registration.html','utf8');
const script=fs.readFileSync('static/registration.js','utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture(){
 const dom=new JSDOM(html,{url:'https://event.example.com/registration#participants',runScripts:'outside-only'});
 const w=dom.window;w.setInterval=()=>0;w.scrollTo=()=>{};
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 let state={revision:'v1',roster_locked:false,rehearsal:true,teams:[
  {pair:1,partner:'Acme',station:2,round:1,scenario:'Customer A',members:[{id:1,name:'Zoë Example',checked_in:false,has_password:false},{id:2,name:'Sam Example',checked_in:true,checked_in_at:1800000000,has_password:true}]},
  {pair:10,partner:'Second company',station:3,round:2,scenario:'Customer B',members:[{id:1,name:'Alex Example',checked_in:false,has_password:true},{id:2,name:'',checked_in:false,has_password:false}]}
 ]};
 const calls=[];let pending,fail;
 w.confirm=()=>true;
 w.KEV={esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),profile:{mount(){}},api:async(path,options)=>{
  if(!options)return structuredClone(state);
  const b=options.body;calls.push(b);if(pending)await pending;if(fail)throw fail;
  const person=state.teams.find(t=>t.pair===b.pair)?.members.find(p=>p.id===b.member);
  if(b.action==='checkin'){person.checked_in=b.checked_in;person.checked_in_at=b.checked_in?Date.now()/1000:null;state.revision='v'+(calls.length+1);}
  return {ok:true,state:structuredClone(state)};
 }};
 w.eval(script);await tick();
 return {w,doc:w.document,calls,close:()=>w.close(),hold:()=>new Promise(resolve=>{pending=new Promise(r=>resolve(r));}),fail:error=>{fail=error;},state};
}

test('flat accessible table supports token search, exact team number and attendance filters',async()=>{
 const f=await fixture();try{
  assert.ok(f.doc.querySelector('table[aria-label="Participant arrivals"]'));
  const search=f.doc.querySelector('#registration-search');search.value='zoe acme';search.dispatchEvent(new f.w.Event('input'));
  assert.equal(f.doc.querySelectorAll('tbody tr').length,1);assert.match(f.doc.querySelector('tbody').textContent,/Zoë/);
  search.value='1';search.dispatchEvent(new f.w.Event('input'));assert.equal(f.doc.querySelectorAll('tbody tr').length,2);
  const filter=f.doc.querySelector('#registration-filter');filter.value='waiting';filter.dispatchEvent(new f.w.Event('change'));
  assert.equal(f.doc.querySelectorAll('tbody tr').length,1);assert.equal(f.doc.querySelectorAll('tbody [data-action="qr"]').length,0);
 }finally{f.close();}
});

test('check-in sends one revision-checked request and leaves a named handoff after row is filtered out',async()=>{
 const f=await fixture();try{
  const filter=f.doc.querySelector('#registration-filter');filter.value='waiting';filter.dispatchEvent(new f.w.Event('change'));
  let release;const pending=new Promise(r=>{release=r;});f.w.KEV.api=((old)=>async(...args)=>{if(args[1])await pending;return old(...args);})(f.w.KEV.api);
  const button=f.doc.querySelector('[data-action="checkin"][data-slot="1:1"]');button.click();button.click();
  assert.equal(button.disabled,true);release();await tick();await tick();
  assert.equal(f.calls.length,1);assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0])),{revision:'v1',action:'checkin',pair:1,member:1,checked_in:true});
  const receipt=f.doc.querySelector('#registration-receipt');assert.equal(receipt.hidden,false);assert.match(receipt.textContent,/Zoë Example/);assert.match(receipt.textContent,/Station 2/);
  assert.equal(f.doc.querySelector('tbody [data-slot="1:1"]'),null);
  f.doc.querySelector('#registration-next').click();assert.equal(receipt.hidden,true);assert.equal(f.doc.activeElement.id,'registration-search');
 }finally{f.close();}
});

test('participant details show teammates and require confirmation to undo arrival',async()=>{
 const f=await fixture();try{
  f.doc.querySelector('[data-action="details"][data-slot="1:2"]').click();
  const panel=f.doc.querySelector('#registration-detail');assert.equal(panel.open,true);assert.match(panel.textContent,/Zoë Example/);
  f.w.confirm=()=>false;panel.querySelector('[data-action="checkin"]').click();await tick();assert.equal(f.calls.length,0);
  f.w.confirm=()=>true;panel.querySelector('[data-action="checkin"]').click();await tick();assert.equal(f.calls[0].checked_in,false);
 }finally{f.close();}
});

test('conflicts keep search and never claim a successful check-in',async()=>{
 const f=await fixture();try{
  f.fail(Object.assign(new Error('Registration changed. Review before retrying.'),{status:409}));
  const search=f.doc.querySelector('#registration-search');search.value='Zoë';search.dispatchEvent(new f.w.Event('input'));
  f.doc.querySelector('[data-action="checkin"]').click();await tick();await tick();
  assert.equal(search.value,'Zoë');assert.equal(f.doc.querySelector('#registration-receipt').hidden,true);
  assert.match(f.doc.querySelector('#registration-status').textContent,/Review before retrying/);
  assert.equal(f.doc.querySelector('[data-action="checkin"]').disabled,false);
 }finally{f.close();}
});

test('a replaced participant cannot inherit the previous arrival receipt',async()=>{
 const f=await fixture();try{
  f.doc.querySelector('[data-action="checkin"][data-slot="1:1"]').click();await tick();
  assert.equal(f.doc.querySelector('#registration-receipt').hidden,false);
  f.state.teams[0].members[0].name='Replacement Example';
  f.doc.querySelector('#refresh-registration').click();await tick();
  assert.equal(f.doc.querySelector('#registration-receipt').hidden,true);
 }finally{f.close();}
});

test('Participants is a bookmarkable destination',async()=>{
 const f=await fixture();try{
  f.doc.querySelector('#registration-people-tab').click();
  assert.equal(f.w.location.hash,'#participants');assert.equal(f.doc.querySelector('#registration-workspace').hidden,false);
  f.doc.querySelector('#registration-home-tab').click();assert.equal(f.w.location.hash,'#today');
 }finally{f.close();}
});

test('access recovery errors remain visible inside participant details',async()=>{
 const f=await fixture();try{
  f.doc.querySelector('[data-action="details"][data-slot="1:2"]').click();
  f.fail(Object.assign(new Error('Ask Admin to manage this staff password.'),{status:409}));
  f.doc.querySelector('#registration-detail [data-action="reset"]').click();await tick();await tick();
  assert.equal(f.doc.querySelector('#registration-detail').open,true);
  assert.match(f.doc.querySelector('#registration-detail-error').textContent,/Ask Admin/);
 }finally{f.close();}
});

test('full teams explain unavailable moves inside details',async()=>{
 const f=await fixture();try{
  f.state.teams[1].members[1].name='Last arrival';
  f.doc.querySelector('#refresh-registration').click();await tick();
  f.doc.querySelector('[data-action="details"][data-slot="1:1"]').click();
  f.doc.querySelector('#registration-detail [data-action="move"]').click();
  assert.match(f.doc.querySelector('#registration-detail-error').textContent,/no open slots/i);
 }finally{f.close();}
});

test('walk-in from Today opens the participant workspace for the arrival handoff',async()=>{
 const f=await fixture();try{
  f.doc.querySelector('#registration-home-add').click();
  assert.equal(f.doc.querySelector('#registration-workspace').hidden,false);
  assert.equal(f.doc.querySelector('#registration-edit').open,true);
 }finally{f.close();}
});

test('organizer PDF uses a fresh full snapshot and prints a minimized report',async()=>{
 const f=await fixture();
 try{
  const search=f.doc.querySelector('#registration-search');
  search.value='nobody';
  search.dispatchEvent(new f.w.Event('input'));
  f.state.teams[1].members[0].name='Alex <Example>';
  f.state.teams[0].members[1].password='synthetic-secret';
  f.doc.getElementById('open-registration-report').click();
  await tick();
  const report=f.doc.getElementById('registration-report');
  const arrivals=report.querySelector('[aria-label="Alphabetical check-in list"]');
  assert.equal(report.hidden,false);
  assert.match(report.textContent,/Rehearsal.*Organizer copy.*event time/);
  assert.deepEqual(Array.from(arrivals.querySelectorAll('tbody th'), th=>th.textContent),['Alex <Example>','Sam Example','Zoë Example']);
  assert.match(arrivals.textContent,/Checked in/);
  assert.match(report.querySelector('[aria-label="Team assignments"]').textContent,/Open slot.*Station 3Round 2.*Customer B/);
  assert.doesNotMatch(report.textContent,/synthetic-secret|password|QR/i);
  let printed=false;
  f.w.print=()=>{printed=true;};
  f.doc.getElementById('print-registration-report').click();
  assert.equal(printed,true);
  f.doc.getElementById('close-registration-report').click();
  assert.equal(report.hidden,true);
  assert.equal(report.querySelector('article').textContent,'');
  assert.equal(f.doc.activeElement.id,'open-registration-report');
  assert.equal(f.calls.length,0);
 }finally{f.close();}
});

test('PDF preview stops when the registration session cannot retrieve the roster',async()=>{
 const f=await fixture();
 try{
  f.w.KEV.api=async()=>{throw Object.assign(new Error('Sign in to continue.'),{status:401});};
  f.doc.getElementById('open-registration-report').click();
  await tick();
  assert.equal(f.doc.getElementById('registration-report').hidden,true);
  assert.equal(f.doc.getElementById('registration-app').hidden,true);
  assert.equal(f.doc.getElementById('registration-signin').hidden,false);
 }finally{f.close();}
});
