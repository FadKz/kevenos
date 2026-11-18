// Focused UI acceptance for configurable events and Final lifecycle. No browser or network.
// NODE_PATH=/tmp/pitch-rehearsal-20261112/js/node_modules node tests/test_final_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {JSDOM, VirtualConsole} = require('jsdom');
const root = path.resolve(__dirname, '..');
const content = JSON.parse(fs.readFileSync(path.join(root, 'content.json')));
const clone = x => JSON.parse(JSON.stringify(x));
const config = {participant_count:12,team_size:3,judge_count:3,team_count:4,rounds:2,final_panel:[1,2,3],badge_cap:4};
const clock = {kind:'final',round:1,running:false,elapsed:0,round_left:480,phase:'pitch',started_any:false,ended:false};
const team = {pair:1,partner:'Example company',members:[{id:1,name:'Alex',coach:1},{id:2,name:'Sam',coach:0},{id:3,name:'Jo',coach:0}],p1:'Alex',p2:'Sam',scenario:'S1',station:1,round:1,badge:2};
const final = {status:'ready',active:true,pairs:[1,3],current:0,run_id:'example-final',panel:[1,2,3],expected_judges:3,case_name:content.scenarios[0].name,scenario:'S1',receipts:[{station:1,judge:'Judge A',submitted:true,seen:0},{station:2,judge:'Judge B',submitted:false,seen:0},{station:3,judge:'Judge C',submitted:false,seen:null}],missing:[2,3],complete:false,all_complete:false,can_publish:false,unresolved_ties:[],published:false,eligible:true,can_submit:true,total:2,visited:[1],pending_assignments:[]};
const publicFinal = {...final,pairs:[{...team,completed:false},{pair:3,partner:'Another company',completed:false}]};
const state = {config,configuration:config,rounds:2,pairs:4,mode:'self',clock,final,calib:{active:false},counts:{round:0,calib:0},public:'off',reveal:'hidden',versions:{app:'4.3.0',content:1},stations:[1,2,3].map(station=>({station,pair:1,info:team,clock,seen:0,submitted:false})),program:{i:9,mode:'manual',announce:'',scene:'auto',since:1},agenda:content.agenda};
const scores = {leaderboard:[],flags:[],final_state:final,final:[{...team,rank:1,judges:1,expected:3,missing:[2,3],avg_ticks:4,ai:0,badge:2,total:42,complete:false,receipts:final.receipts},{pair:3,partner:'Another company',rank:2,judges:0,expected:3,avg_ticks:0,ai:0,badge:0,total:0,complete:false,receipts:[]}]};
const publicState = {event:'Example event',clock,final:publicFinal,scene:'final',scene_override:'auto',reveal:'hidden',program:{index:9,items:content.agenda,total:content.agenda.length,since:1,announce:''},off:true,rows:[],scored:0,pairs:4,stations:[],mode:'self'};
const workspace = {mode:'live',summary:{registered_pairs:4,participants:12,expected_pairs:4,expected_participants:12,expected_judges:3,issued_participants:0,judge_sessions:0,judge_invites:0,round_sheets:0}};
const guide = JSON.parse(fs.readFileSync(path.join(root,'event-guide.json')));
const requests = [], errors = [];
async function load(file,pathname, overrides={}, hall=false) {
  let html=fs.readFileSync(path.join(root,'static',file),'utf8').replace('<body','<body data-scoped="1" data-event-mode="live"');
  html=html.replace('<script src="/static/common.js"></script>','<script>'+fs.readFileSync(path.join(root,'static/common.js'),'utf8')+'</script>');
  if(hall==='bundle')html=html.replace('<script src="/static/live-player.js"></script>',()=>'<script>'+fs.readFileSync(path.join(root,'static/live-player.js'),'utf8')+'</script>');
  const vc=new VirtualConsole();vc.on('jsdomError',e=>{if(e.type!=='not-implemented')errors.push(e.message);});
  const maps={
    '/api/info':{workspace:{mode:'live'},version:state.versions},'/api/access/me':{authenticated:true,role:file==='judge.html'?'judge':file==='participant.html'?'participant':file==='guest.html'?'guest':file==='desk.html'?'desk':'admin',label:'Judge A',station:1,draft_key:'fixture-v450',context:'fixture-context'},'/api/content':content, // gitleaks:allow -- fixed synthetic access marker
    '/api/desk/state':state,'/api/desk/scores':scores,'/api/desk/links':{hosted:true,stations:3},'/api/admin/workspace':workspace,
    '/api/admin/configuration':{configuration:config,editable:true,summary:{}},'/api/desk/roster':{roster:[team]},
    '/api/public':publicState,'/api/board':publicState,
    '/api/participant/state':{participant:{name:'Alex',pair:1,partner:team.partner,station:1,round:1,scenario:'S1',teammates:['Sam','Jo'],teammate:'Sam and Jo'},now:1800000000,rehearsal:false,program:{title:'Final',line:'',instructions:[],next_title:'Awards'},action:{code:'final_ready',title:'You are in the Final',instruction:'Use the same scenario for your 8-minute pitch.',detail:'Wait for the host to call your team.',station:1,round:1,clock},result:{status:'awaiting',label:'Final results not yet released',total:null,rank:null,kind:'final'},announcement:null,final:{...publicFinal,qualified:true}},
    '/api/admin/credentials':{credentials:[],participants:[]},
    '/api/event':{role:'participant',guide,participant:{name:'Alex',pair:1,partner:team.partner,station:1,round:1,scenario:'S1',teammates:['Sam','Jo'],teammate:'Sam and Jo'},resources:[],now:publicState},
    '/api/sync':{mode:'self',assignment:{kind:'final',pair:1,round:0,info:team},scenario:'S1',clock,final,submitted:false,versions:state.versions,has_next:false},...overrides
  };
  const dom=new JSDOM(html,{url:'https://example.test'+pathname,runScripts:'dangerously',pretendToBeVisual:true,virtualConsole:vc,beforeParse(w){
    w.localStorage.setItem('kevenos:token:judge:1','fixture');w.localStorage.setItem('kevenos:judge-identity:1',JSON.stringify({name:'Judge A',key:'fixture'}));
    w.matchMedia=q=>({matches:hall && q.includes('aspect-ratio'),addEventListener:()=>{}});
    w.ResizeObserver=class{observe(){} unobserve(){} disconnect(){}};
    if(hall===true)w.PitchHall={show:data=>{w.hallData=data;},clear:()=>{}};
    w.setInterval=()=>0;w.scrollTo=()=>{};w.confirm=()=>true;w.alert=()=>{};
    w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.show=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
    w.fetch=async(u,o={})=>{const url=new URL(u,w.location.href);requests.push({path:url.pathname,body:o.body?JSON.parse(o.body):null});const value=maps[url.pathname];return{ok:value!==undefined,status:value===undefined?404:200,headers:{get:()=>null},json:async()=>clone(value??{error:'Missing fixture '+url.pathname})};};
  }});
  await new Promise(r=>setTimeout(r,50));return dom;
}
const text = d=>d.window.document.body.textContent;
const $ = (d,s)=>d.window.document.querySelector(s);
const test=async(name,fn)=>{try{await fn();console.log('PASS '+name);}catch(e){console.error('FAIL '+name+' :: '+e.message);process.exitCode=1;}};
(async()=>{
  await test('all team members appear in shared labels',async()=>{const d=await load('board.html','/board');assert.equal(d.window.eval('KEV.members('+JSON.stringify(team)+')'),'Alex, Sam and Jo');d.window.close();});
  await test('Judge Final uses the original case and identifies the finalist',async()=>{const d=await load('judge.html','/judge/1');assert(text(d).includes('Finalist 1 of 2'));assert(text(d).includes('Team 1'));d.window.document.getElementById('bcustomer').click();assert(text(d).includes(content.scenarios[0].name));assert(!d.window.document.getElementById('sheet').textContent.includes('Saffron Table Group'));d.window.close();});
  await test('Judge is parked after publication',async()=>{const f={...final,status:'completed',published:true,can_submit:false};const j={...clone(state),assignment:{kind:'final',pair:1,round:0,info:team},scenario:'S1',final:f,submitted:true};const d=await load('judge.html','/judge/1',{'/api/sync':j});assert(text(d).includes('Judging complete'));assert(d.window.document.getElementById('bar').hidden);d.window.close();});
  await test('Desk shows configured panel receipts and blocks incomplete handover',async()=>{const d=await load('desk.html','/scoring#final');assert(text(d).includes('1 / 3'));assert(text(d).includes('Judge B'));assert(d.window.document.getElementById('fnext').disabled);assert(!text(d).includes('Saffron Table Group'));d.window.close();});
  await test('roster import rejects an old team-size template without modifying names',async()=>{
    const d=await load('admin.html','/admin#roster');
    const input=d.window.document.getElementById('rcsv');
    input.value='pair,partner,person1,person2,coach1,coach2\n1,Example,Alex,Sam,1,0';
    d.window.document.getElementById('rimp').click();
    assert(d.window.document.getElementById('rmsg').textContent.includes('template'));
    assert.equal(d.window.document.querySelector('[data-i="0"] [data-f="name"][data-member="3"]').value,'Jo');
    input.value='team,company,p1,p2,p3,coach1,coach2,coach3\n1,Example,Alex,Sam,Jo,1,0,';
    d.window.document.getElementById('rimp').click();
    assert.equal(d.window.document.querySelector('[data-i="0"] [data-f="name"][data-member="3"]').value,'Jo');
    assert(d.window.document.querySelector('[data-i="0"] [data-f="coach"][data-member="1"]').checked);d.window.close();
  });
  await test('Admin configuration supports participant team and judge count',async()=>{const d=await load('admin.html','/admin#system');assert(d.window.document.getElementById('event-config-form'));assert.equal(d.window.document.getElementById('config-team-size').value,'3');assert(text(d).includes('30 minutes'));d.window.close();});
  await test('Admin roster edits all configured members',async()=>{const d=await load('admin.html','/admin#roster');assert(d.window.document.querySelector('[data-member="3"]'));assert(text(d).includes('Original scenario'));d.window.close();});
  await test('legacy wrong-case sheets show reassessment guidance to Desk and Judge',async()=>{
    const f={...final,legacy_case_review_required:true,legacy_case_warning:'Earlier sheets used a different case; reassess before publishing.',needs_rescore:true,receipts:[{station:1,judge:'Judge A',submitted:false,needs_rescore:true}]};
    const desk=await load('desk.html','/scoring#final',{'/api/desk/state':{...state,final:f},'/api/desk/scores':{...scores,final_state:f}});
    assert(text(desk).includes('Needs original-case rescore'));assert(text(desk).includes(f.legacy_case_warning));desk.window.close();
    const judge=await load('judge.html','/judge/1#scoring',{'/api/sync':{...state,assignment:{kind:'final',pair:1,round:0,info:team},scenario:'S1',final:f,submitted:null}});
    assert(judge.window.document.getElementById('subbed').textContent.includes('Reassess this Final sheet'));judge.window.close();
  });
  await test('Participant Final retains original case with eight-minute stage instructions',async()=>{const d=await load('participant.html','/participant');assert(text(d).includes('You are in the Final'));assert(text(d).includes('8-minute'));assert(text(d).includes('same scenario'));assert(text(d).includes('Sam and Jo'));d.window.close();});
  await test('Guest overview shows current Final team',async()=>{const d=await load('guest.html','/guest',{'/api/admin/credentials':{credentials:[],participants:[]},
    '/api/event':{role:'guest',guide,now:publicState}});assert(text(d).includes('Team 1'));assert(text(d).includes(content.scenarios[0].name));d.window.close();});
  await test('Live and board park on results review and never show finalists coming up',async()=>{const data={...publicState,final:{...publicFinal,status:'review',active:false},clock};for(const file of ['live.html','board.html']){const d=await load(file,'/'+file.replace('.html',''),{'/api/public':data,'/api/board':data});d.window.eval('draw()');assert(text(d).includes('confirming results'));assert(!d.window.document.querySelector(file==='live.html'?'#stage':'#wrap').textContent.includes('Finalists coming up'));d.window.close();}});
  await test('Live honors explicit Desk scene during an engaged Final in both renderers',async()=>{
    const data={...publicState,scene:'info',scene_override:'info'};
    for(const hall of [false,true])for(const status of ['ready','completed']){
      const d=await load('live.html','/live?scene=pitch',{'/api/public':{...data,final:{...data.final,status},reveal:status==='completed'?'winners':'hidden'}},hall);d.window.eval('draw()');
      assert(d.window.document.body.classList.contains('sc-info'),'Explicit info scene must not be replaced by Final');
      d.window.close();
    }
  });
  await test('auto Final overrides stale URL scene in both renderers',async()=>{
    for(const hall of [false,true]){
      const d=await load('live.html','/live?scene=pitch',{'/api/public':publicState},hall);d.window.eval('draw()');
      if(hall)assert.equal(d.window.hallData.scene,'final');
      else assert(d.window.document.body.classList.contains('sc-final'));
      d.window.close();
    }
  });
  await test('room announcement remains visible alongside a quiz question on the hall screen',async()=>{
    const data={...publicState,scene:'info',final:{status:'idle',pairs:[]},program:{...publicState.program,announce:'Please remain seated.'},
      quiz:{kind:'question',prompt:'How confident do you feel?',status:'open',end:1800000060,options:[{key:'A',text:'Ready'},{key:'B',text:'Need practice'}]}};
    const d=await load('live.html','/live',{'/api/public':data},'bundle');d.window.eval('draw()');
    await new Promise(r=>setTimeout(r,100));
    assert.equal(d.window.document.getElementById('room-question').hidden,false);
    assert.equal(d.window.document.getElementById('ann').textContent,'Please remain seated.');
    d.window.close();
  });
  await test('partial last team never gains an extra member during roster save',async()=>{
    const c={...config,participant_count:11};const ds={...state,config:c,configuration:c};
    const d=await load('admin.html','/admin#roster',{'/api/desk/state':ds});
    const last=d.window.document.querySelectorAll('#rt tr[data-i]')[3];assert.equal(last.querySelectorAll('[data-f="name"]').length,2);assert.equal(last.querySelector('td[colspan]')?.colSpan,2);
    d.window.document.getElementById('rsave').click();await new Promise(r=>setTimeout(r,20));
    assert.equal(requests.filter(r=>r.path==='/api/desk/roster'&&r.body).at(-1).body.rows[3].members.length,2);d.window.close();
  });
  await test('Admin submits the three configured counts and warns about the published block',async()=>{
    const d=await load('admin.html','/admin#system');
    for(const [id,value] of [['participant-count',14],['team-size',3],['judge-count',2]]){const el=d.window.document.getElementById('config-'+id);el.value=value;el.dispatchEvent(new d.window.Event('input',{bubbles:true}));}
    assert(d.window.document.getElementById('config-summary').textContent.includes('45 minutes'));
    assert(d.window.document.getElementById('config-schedule').textContent.includes('does not change published times'));
    d.window.document.getElementById('event-config-form').requestSubmit();await new Promise(r=>setTimeout(r,20));
    assert.deepEqual(requests.filter(r=>r.path==='/api/admin/configuration'&&r.body).at(-1).body,{participant_count:14,team_size:3,judge_count:2});d.window.close();
  });
  await test('Judge uses the frozen case and restores its accepted receipt on another device',async()=>{
    const j={mode:'self',assignment:{kind:'final',pair:1,round:0,info:team},scenario:'S1',scenario_snapshot:content.scenarios[1],clock,final,submitted:{ticks:[1,1,0,0,0,0,0,0,0,0],notes:'Saved note',feedback:'Saved feedback',asked:[{q:'Q1',t:2}],raised:[]},versions:state.versions};
    const d=await load('judge.html','/judge/1',{'/api/sync':j});
    assert(d.window.document.getElementById('who').textContent.includes(content.scenarios[1].name));
    assert.equal(d.window.eval('nTicks()'),2);assert.equal(d.window.eval('D.notes'),'Saved note');
    assert(d.window.document.getElementById('subbed').textContent.includes('Accepted by the Desk'));d.window.close();
  });
  await test('Final submission identifies its scoring run and sends the panel rating',async()=>{
    const d=await load('judge.html','/judge/1#scoring',{'/api/submit':{ok:true}});
    d.window.eval("view=4;renderView();D.notes='Observed';D.feedback='Clear';");
    d.window.document.querySelector('[data-rating="4"]').click();
    await d.window.eval('doSubmit()');const body=requests.filter(r=>r.path==='/api/submit').at(-1).body;
    assert.equal(body.run_id,final.run_id);assert.equal(body.pair,1);assert.equal(body.final_rating,4);d.window.close();
  });
  await test('Desk requires a recorded tie decision before publishing',async()=>{
    const f={...final,status:'review',active:false,all_complete:true,can_publish:false,unresolved_ties:[[1,3]]};
    const d=await load('desk.html','/scoring#final',{'/api/desk/state':{...state,final:f},'/api/desk/scores':{...scores,final_state:f},'/api/desk/final':{ok:true}});
    assert(d.window.document.getElementById('fpublish').disabled);
    d.window.document.getElementById('tie-order').value='3, 1';d.window.document.getElementById('tie-reason').value='Panel agreed the stronger close';
    d.window.document.getElementById('final-ties').requestSubmit();await new Promise(r=>setTimeout(r,20));
    assert.deepEqual(requests.filter(r=>r.path==='/api/desk/final').at(-1).body,{action:'resolve',order:[3,1],reason:'Panel agreed the stronger close'});d.window.close();
  });
  await test('anonymous Live never renders member names and marks only accepted completion',async()=>{
    const data={...publicState,final:{...publicFinal,current:1,pairs:[{...team,completed:false},{pair:3,partner:'Another company',completed:false}]}};
    const d=await load('live.html','/live?names=0',{'/api/public':data});d.window.eval('draw()');
    const visible=d.window.document.getElementById('left').textContent+d.window.document.getElementById('right').textContent;
    assert(!visible.includes('Alex'));assert(!visible.includes('Sam'));assert(!d.window.document.getElementById('fl').textContent.includes('✓'));d.window.close();
  });
  await test('completed hidden podium parks both displays despite a stale scene lock',async()=>{
    const data={...publicState,steps:content.steps,bells:[],reveal:'hidden',scene:'awards',podium:[],final:{...publicFinal,status:'completed',published:true,active:false}};
    for(const file of ['live.html','board.html']){
      const d=await load(file,'/'+file.replace('.html','')+'?scene=pitch',{'/api/public':data,'/api/board':data});d.window.eval('draw()');
      if(file==='live.html')assert(d.window.document.body.classList.contains('sc-awards'));
      else {assert(d.window.document.getElementById('wrap').textContent.includes('Final complete'));assert(!d.window.document.getElementById('wrap').textContent.includes('Round '));}
      d.window.close();
    }
  });
  assert.deepEqual(errors,[]);
})().catch(e=>{console.error(e);process.exitCode=1;});
