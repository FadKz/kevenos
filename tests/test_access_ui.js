// Identity and UI boundaries for unified hosted access. No external network.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {JSDOM,VirtualConsole} = require('jsdom');
const root = path.resolve(__dirname,'..');
const common = fs.readFileSync(path.join(root,'static/common.js'),'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const sleep = () => new Promise(resolve=>setTimeout(resolve,15));
const identity = {authenticated:true,role:'participant',label:'Alex Example',pair:1,member:1,station:0,context:'identity-a',impersonation:null};
async function load(file='index.html',options={}) {
  const calls=[],errors=[],timers=[]; let current=clone(options.identity || identity), override=options.override;
  let html=file ? fs.readFileSync(path.join(root,'static',file),'utf8') : '<!doctype html><body><div id="private">Private</div><script src="/static/common.js"></script></body>';
  html=html.replace('<body','<body data-scoped="1" data-event-mode="live" data-event-context="event-a"');
  html=html.replace('<script src="/static/common.js"></script>',()=>'<script>'+common+'</script>');
  const vc=new VirtualConsole();vc.on('jsdomError',error=>{if(error.type!=='not-implemented')errors.push(error.message);});
  const dom=new JSDOM(html,{url:'https://example.test'+(options.url || '/'),runScripts:'dangerously',pretendToBeVisual:true,virtualConsole:vc,beforeParse(w){
    w.setInterval=(fn,ms)=>{timers.push({fn,ms});return timers.length;};w.confirm=()=>true;w.scrollTo=()=>{};
    w.URL.createObjectURL=()=> 'blob:fixture';w.URL.revokeObjectURL=()=>{};
    for(const [key,value]of Object.entries(options.storage || {}))w.sessionStorage.setItem(key,value);
    for(const [key,value]of Object.entries(options.local || {}))w.localStorage.setItem(key,value);
    w.fetch=async(url,opts={})=>{
      calls.push({url,opts});
      const own=override && await override(url,opts,current);const status=own?.status || 200;
      const data=own?.data || (url==='/api/access/me'?current:url==='/api/info'?{version:{app:'4.5.0',content:1}}:{});
      const headers={'X-Access-Context':own?.context || current.context,'X-Event-Context':'event-a',...(own?.headers || {})};
      return {ok:status<400,status,headers:{get:key=>headers[Object.keys(headers).find(name=>name.toLowerCase()===key.toLowerCase())] || null},json:async()=>clone(data),blob:async()=>new w.Blob(['PDF'])};
    };
  }});
  await sleep();await sleep();
  return {dom,calls,errors,timers,$:id=>dom.window.document.getElementById(id),eval:source=>dom.window.eval(source),setIdentity:value=>{current=clone(value);},close:()=>dom.window.close()};
}
async function test(name,run){try{await run();console.log('PASS '+name);}catch(error){console.error('FAIL '+name+': '+error.stack);process.exitCode=1;}}
(async()=>{
  await test('sign-in has one password field and resolves the user without a role selector',async()=>{
    const page=await load('index.html',{identity:{authenticated:false,context:'anonymous'}});
    try {assert.equal(page.$('signin').querySelector('select'),null);assert.equal(page.dom.window.document.querySelectorAll('input[type="password"]').length,1);assert(!page.$('pin'));}finally{page.close();}
  });
  await test('login sends the exact password without selecting a dashboard',async()=>{
    const page=await load('index.html',{identity:{authenticated:false,context:'anonymous'},override:(url)=>url==='/api/access/login'?{data:{next:'/home'},context:'admin-login'}:null});
    try{page.$('password').value=' Exact Password ';page.$('signin').requestSubmit();await sleep();const call=page.calls.find(call=>call.url==='/api/access/login');assert.deepEqual(JSON.parse(call.opts.body),{password:' Exact Password '});assert.equal(call.opts.headers['X-Profile-Token'],undefined);assert.equal(call.opts.headers['X-Dashboard'],undefined);assert.equal(page.calls.find(call=>call.url==='/api/access/me').opts.headers['X-Dashboard'],'home');assert.equal(page.$('password').value,'');}finally{page.close();}
  });
  await test('profile requests and PDF downloads carry effective identity and discard legacy bearer headers',async()=>{
    const me={...identity,context:'preview-a',actor:{role:'admin',label:'Owner'},impersonation:{id:'child-a',mode:'preview',expires:2000000000}};
    const page=await load(null,{identity:me,storage:{'kevenos:profile-token':'child-token','kevenos:profile-context':'preview-a'}});
    try{
      await page.eval('KEV.profile.mount()');await page.eval("KEV.api('/api/event',null,'old-judge-token')");
      page.eval('KEV.saveFile=()=>{}');await page.eval("KEV.download('/api/resources/participant-guide')");
      for(const call of page.calls.filter(call=>['/api/access/me','/api/event','/api/resources/participant-guide'].includes(call.url))){assert.equal(call.opts.headers['X-Profile-Token'],'child-token');assert.equal(call.opts.headers['X-Access-Context'],'preview-a');assert.equal(call.opts.headers['X-Token'],undefined);assert.equal(call.opts.headers['X-Dashboard'],undefined);assert.equal(call.opts.credentials,'same-origin');}
      assert.match(page.$('kevenos-profile').textContent,/Preview.*read-only/);assert.match(page.$('kevenos-profile').textContent,/Return to Admin/);
    }finally{page.close();}
  });
  await test('identity changes clear private content and stop further API requests',async()=>{
    const page=await load(null);
    try{
      await page.eval("KEV.profile.mount({onEnd:()=>document.getElementById('private').replaceChildren()})");
      page.setIdentity({...identity,context:'identity-b',label:'Different person'});
      await assert.rejects(page.eval("KEV.api('/api/event')"),/profile changed/i);
      assert.equal(page.$('private').textContent,'');assert(page.eval('KEV.profile.locked'));
      const count=page.calls.length;await assert.rejects(page.eval("KEV.api('/api/submit',{body:{pair:1}})"));assert.equal(page.calls.length,count);
    }finally{page.close();}
  });
  await test('a cross-tab profile-change signal clears the active private view immediately',async()=>{
    const page=await load(null);
    try{await page.eval("KEV.profile.mount({onEnd:()=>document.getElementById('private').replaceChildren()})");page.dom.window.dispatchEvent(new page.dom.window.StorageEvent('storage',{key:'kevenos:access-event',newValue:'changed'}));assert.equal(page.$('private').textContent,'');assert(page.eval('KEV.profile.locked'));}finally{page.close();}
  });
  await test('preview blocks mutation before HTTP while read-only judge sync remains available',async()=>{
    const me={...identity,role:'judge',station:1,context:'preview-a',draft_key:'draft-a',impersonation:{id:'child-a',mode:'preview',expires:2000000000},actor:{role:'admin'}};
    const page=await load(null,{identity:me,storage:{'kevenos:profile-token':'child-token','kevenos:profile-context':'preview-a'}});
    try{await page.eval('KEV.profile.mount()');const count=page.calls.length;await assert.rejects(page.eval("KEV.api('/api/submit',{body:{pair:1}})"),/read-only/);assert.equal(page.calls.length,count);await page.eval("KEV.api('/api/sync',{body:{}})");assert(page.calls.some(call=>call.url==='/api/sync'));}finally{page.close();}
  });
  await test('expired child profile returns to Admin without retaining stale per-tab credentials',async()=>{
    const rootIdentity={...identity,role:'admin',context:'root-a',pair:0,member:0};
    const page=await load(null,{identity:rootIdentity,storage:{'kevenos:profile-token':'expired-token','kevenos:profile-context':'expired-context'},override:(url,opts)=>opts.headers?.['X-Profile-Token'] && url!=='/api/info'?{status:401,data:{code:'profile_expired',error:'Profile expired'},context:'expired-context'}:null});
    try{await page.eval('KEV.profile.mount()');assert.match(page.$('kevenos-profile').textContent,/Return to Admin/);await page.eval('KEV.profile.end()');assert.equal(page.dom.window.sessionStorage.getItem('kevenos:profile-token'),null);assert.equal(page.eval('KEV.profile.identity.role'),'admin');assert.equal(page.eval('KEV.profile.context'),'root-a');const last=page.calls.filter(call=>call.url==='/api/access/me').at(-1);assert.equal(last.opts.headers['X-Profile-Token'],undefined);assert.equal(last.opts.headers['X-Access-Context'],undefined);}finally{page.close();}
  });
  await test('sign-out clears old auth keys and private values but retains isolated saved judge work',async()=>{
    const page=await load(null);
    try{await page.eval("KEV.profile.mount({onEnd:()=>document.getElementById('private').replaceChildren()})");page.dom.window.localStorage.setItem('kevenos:token:judge:1','old-token');page.dom.window.localStorage.setItem('kevenos:judge-identity:1','old-name');page.dom.window.localStorage.setItem('kevenos:workspace-draft:old','saved work');await page.eval('KEV.profile.signOut()');assert.equal(page.$('private').textContent,'');assert.equal(page.dom.window.localStorage.getItem('kevenos:token:judge:1'),null);assert.equal(page.dom.window.localStorage.getItem('kevenos:judge-identity:1'),null);assert.equal(page.dom.window.localStorage.getItem('kevenos:workspace-draft:old'),'saved work');}finally{page.close();}
  });
  await test('judge cookie identity owns station, draft context and requests without old tokens',async()=>{
    const content=JSON.parse(fs.readFileSync(path.join(root,'content.json'),'utf8'));
    const me={...identity,role:'judge',station:1,pair:0,member:0,context:'staff-a',draft_key:'draft-a'};
    const state={assignment:{kind:'round',pair:1,round:1,info:{pair:1,partner:'Example',members:[{id:1,name:'Alex'},{id:2,name:'Sam'}],badge:0}},scenario:'S1',clock:{kind:'round',round:1,running:false,elapsed:0,phase:'pitch',step:0,step_left:120,round_left:900,started_any:false,ended:false},final:{status:'idle'},mode:'self',has_next:true,submitted:false,versions:{app:'4.5.0',content:1}};
    const page=await load('judge.html',{url:'/judge/1',identity:me,override:url=>url==='/api/content'?{data:content}:url==='/api/sync'?{data:state}:null});
    try{await sleep();assert(!page.$('app').hidden);assert(page.$('judge-local-access').hidden);assert(page.eval('draftScope()').includes('staff-a'));assert(page.eval('draftScope()').includes('draft-a'));assert(page.calls.some(call=>call.url==='/api/sync'));assert(page.calls.filter(call=>call.url==='/api/sync').every(call=>call.opts.headers['X-Access-Context']==='staff-a' && !call.opts.headers['X-Token']));page.dom.window.dispatchEvent(new page.dom.window.StorageEvent('storage',{key:'kevenos:access-event',newValue:'new'}));assert(page.$('app').hidden);assert.equal(page.$('view').textContent,'');assert.equal(page.eval('D'),null);const count=page.calls.length;await page.eval('flushOutbox()');assert.equal(page.calls.length,count);assert.deepEqual(page.errors,[]);}finally{page.close();}
  });
  await test('participant preview loads own data without packaged event materials',async()=>{
    const guide=JSON.parse(fs.readFileSync(path.join(root,'event-guide.json'),'utf8'));
    const person={name:'Alex Example',pair:1,station:1,round:1,scenario:'S1',teammates:['Sam']};
    const me={...identity,context:'preview-a',actor:{role:'admin'},impersonation:{id:'child-a',mode:'preview',expires:2000000000}};
    const state={participant:person,now:1800000000,program:{next_title:'Welcome'},action:{code:'program',title:'Check in',instruction:'Meet your team.',detail:''},result:{status:'awaiting',label:'Awaiting your result.',kind:'round'},final:{qualified:null},announcement:null};
    const page=await load('participant.html',{url:'/participant',identity:me,storage:{'kevenos:profile-token':'child-token','kevenos:profile-context':'preview-a'},override:url=>url==='/api/participant/state'?{data:state}:url==='/api/event'?{data:{role:'participant',participant:person,guide,resources:guide.resources}}:null});
    try{await sleep();assert(!page.$('guide').hidden);assert.match(page.$('kevenos-profile').textContent,/Preview/);assert.equal(page.$('now-title').textContent,'Check in');assert.equal(page.$('documents').querySelector('[data-resource]'),null);assert.match(page.$('materials-note').textContent,/Ask the event desk/);assert.deepEqual(page.errors,[]);}finally{page.close();}
  });
  await test('an in-flight old response cannot restore private content after a profile change',async()=>{
    let release;const gate=new Promise(resolve=>{release=resolve;});
    const page=await load(null,{override:async url=>{if(url==='/api/event'){await gate;return {data:{private:'old identity'}};}return null;}});
    try{await page.eval("KEV.profile.mount({onEnd:()=>document.getElementById('private').replaceChildren()})");const request=page.eval("KEV.api('/api/event')");page.dom.window.dispatchEvent(new page.dom.window.StorageEvent('storage',{key:'kevenos:access-event',newValue:'new'}));release();await assert.rejects(request,/profile changed/);assert.equal(page.$('private').textContent,'');}finally{release();page.close();}
  });
  await test('judge preview never loads or replays another profile’s saved sheet',async()=>{
    const content=JSON.parse(fs.readFileSync(path.join(root,'content.json'),'utf8'));
    const me={...identity,role:'judge',station:1,context:'preview-a',draft_key:'preview-draft',actor:{role:'admin'},impersonation:{id:'child-a',mode:'preview',expires:2000000000}};
    const state={assignment:{kind:'round',pair:1,round:1,info:{pair:1,partner:'Example',members:[],badge:0}},scenario:'S1',clock:{kind:'round',round:1,running:false,elapsed:0,phase:'pitch',step:0,step_left:120,round_left:900,started_any:false,ended:false},final:{status:'idle'},mode:'self',has_next:true,submitted:false,versions:{app:'4.5.0',content:1}};
    const oldKey='kevenos:workspace-outbox:live:event-a:1:someone-else:old-profile';
    const page=await load('judge.html',{url:'/judge/1',identity:me,storage:{'kevenos:profile-token':'child-token','kevenos:profile-context':'preview-a'},local:{[oldKey]:JSON.stringify([{assignment:'round:1:1',status:'pending',body:{notes:'Another judge private note'}}]),'kevenos:token:judge:1':'old-bearer'},override:url=>url==='/api/content'?{data:content}:url==='/api/sync'?{data:state}:null});
    try{await sleep();assert(!page.$('app').hidden);assert.equal(page.eval('D.notes'),'');assert(page.dom.window.document.querySelector('.tick').disabled);page.dom.window.document.dispatchEvent(new page.dom.window.KeyboardEvent('keydown',{key:'1',bubbles:true}));assert.equal(page.eval('D.ticks[0]'),0);await page.eval('flushOutbox()');assert(!page.calls.some(call=>call.url==='/api/submit'));assert(page.dom.window.localStorage.getItem(oldKey).includes('Another judge private note'));assert(!Object.keys(page.dom.window.localStorage).some(key=>key.includes('preview-draft')));assert.deepEqual(page.errors,[]);}finally{page.close();}
  });
  await test('Admin recovery exports saved work only, never credentials, and rejects a non-Admin identity',async()=>{
    const me={...identity,role:'admin'};
    const page=await load(null,{identity:me,local:{'kevenos:workspace-draft:legacy':JSON.stringify({notes:'Saved draft'}),'kevenos:token:judge:1':'do-not-export','kevenos:judgeName':'private-name'}});
    try{await page.eval('KEV.profile.mount()');page.eval('window.Blob=class {constructor(parts){this.parts=parts}};KEV.saveFile=blob=>{window.recovered=JSON.parse(blob.parts[0]);}');assert.equal(await page.eval('KEV.profile.recoverSavedWork()'),1);const recovered=page.eval('window.recovered');assert.equal(recovered.export_only,true);assert.equal(recovered.saved[0].key,'kevenos:workspace-draft:legacy');assert(!JSON.stringify(recovered).includes('do-not-export'));page.setIdentity(identity);await assert.rejects(page.eval('KEV.profile.recoverSavedWork()'),/Admin profile/);}finally{page.close();}
  });
})().catch(error=>{console.error(error);process.exitCode=1;});
