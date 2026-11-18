/* Start an isolated localhost rehearsal for a human review. */
const assert = require('node:assert/strict');
const {mkdtempSync,mkdirSync,writeFileSync,rmSync} = require('node:fs');
const {spawn} = require('node:child_process');
const {tmpdir} = require('node:os');
const path = require('node:path');
const port = Number(process.env.KEVENOS_QUIZ_REVIEW_PORT || 8768);
const origin = 'http://localhost:'+port;
const data = mkdtempSync(path.join(tmpdir(),'pitch-quiz-preview-'));
const secrets = path.join(data,'secrets');
mkdirSync(secrets,{mode:0o700});
const admin='Review-Quiz-Admin9!';
for (const [key,value] of Object.entries({KEVENOS_PASSWORD:admin,KEVENOS_GUEST_PASSWORD:'Review-Quiz-Guest9!',
  KEVENOS_DESK_PIN:'861903',KEVENOS_SESSION_SECRET:'review-quiz-disposable-localhost',KEVENOS_VIEW_KEY:'review-quiz-local-screen'})) {
  writeFileSync(path.join(secrets,key),value,{mode:0o600});
}
const app=spawn(process.env.KEVENOS_TEST_PYTHON || 'python3',['server.py','--host','127.0.0.1','--port',String(port),'--mode','self'],
  {cwd:path.resolve(__dirname,'..'),env:{...process.env,KEVENOS_DATA:data,KEVENOS_SECRETS_DIR:secrets,KEVENOS_HOSTED:'1',
    KEVENOS_GATEWAY_ENABLED:'0',KEVENOS_SMTP_ENABLED:'0',KEVENOS_ASSISTANT_ENABLED:'0',TZ:'UTC'},stdio:['ignore','ignore','inherit']});
app.once('exit',()=>rmSync(data,{recursive:true,force:true}));
process.on('SIGINT',()=>app.kill());
process.on('SIGTERM',()=>app.kill());

(async()=>{
  let up=false;
  for(let i=0;i<100 && !up;i++) {
    if(app.exitCode!==null) break;
    up=await fetch(origin+'/healthz').then(r=>r.ok).catch(()=>false);
    if(!up) await new Promise(resolve=>setTimeout(resolve,100));
  }
  assert(up && app.exitCode===null,'Rehearsal app did not start');
  const headers={'Content-Type':'application/json'};
  const call=async(route,body)=>{
    const r=await fetch(origin+route,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});
    assert(r.ok,route+' '+r.status+' '+await (r.ok?Promise.resolve(''):r.text()));
    for (const key of ['X-Access-Context','X-Event-Context']) if(r.headers.get(key)) headers[key]=r.headers.get(key);
    const session=r.headers.getSetCookie().find(s=>s.startsWith('pp_access='));
    if(session) headers.Cookie=session.split(';')[0];
    return r.json();
  };
  await call('/api/access/login',{role:'admin',password:admin});
  const issue=(role,password,fields)=>call('/api/admin/credentials',{action:'create',role,label:'Quiz review '+role,password,...fields});
  await issue('desk','Review-Quiz-Desk9!');
  await call('/api/admin/workspace',{mode:'rehearsal',confirm:'REHEARSAL'});
  await call('/api/desk/mode',{mode:'self'});
  const rows=(await call('/api/desk/roster')).roster;
  rows[0].members[0].name='Alex Example'; rows[1].members[0].name='Sam Example';
  await call('/api/desk/roster',{rows});
  for (const row of (await call('/api/desk/scores')).leaderboard) await call('/api/desk/sheet',{action:'void',id:row.sheet,why:'Disposable review starts before preliminary judging'});
  await issue('participant','Review-Quiz-Alex9!',{pair:1,member:1});
  await issue('participant','Review-Quiz-Sam9!',{pair:2,member:1});
  await issue('judge','Review-Quiz-Judge9!',{station:1});
  await call('/api/desk/clock',{action:'goto',station:1,round:1});
  await call('/api/access/logout',{});
  console.log('Quiz review ready at '+origin+'/. Open independent browser profiles to use multiple roles.');
})().catch(error=>{console.error(error.message);app.kill();process.exitCode=1;});
