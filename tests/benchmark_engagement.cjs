/* Local synthetic load rehearsal. This script starts its own disposable app;
   it never accepts a remote URL or touches an existing event database. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const {randomBytes} = require('node:crypto');
const {spawn} = require('node:child_process');
const {performance} = require('node:perf_hooks');

const root = path.resolve(__dirname, '..');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const id = () => randomBytes(16).toString('hex');
async function port() {
  return new Promise(resolve => { const server = net.createServer().listen(0, '127.0.0.1', () => {
    const value = server.address().port; server.close(() => resolve(value));
  }); });
}
function percentile(values, p) { return values.slice().sort((a,b) => a-b)[Math.ceil(values.length*p)-1]; }

(async () => {
  const data = await fs.mkdtemp(path.join(os.tmpdir(), 'kevenos-engagement-load-'));
  const secrets = path.join(data, 'secrets'); await fs.mkdir(secrets, {mode:0o700});
  const admin = 'Synthetic-Load-Admin9!', guest = 'Synthetic-Load-Guest9!';
  for (const [name,value] of Object.entries({KEVENOS_PASSWORD:admin, KEVENOS_GUEST_PASSWORD:guest,
    KEVENOS_SESSION_SECRET:'synthetic-load-session', KEVENOS_VIEW_KEY:'synthetic-load-screen'}))
    await fs.writeFile(path.join(secrets,name), value, {mode:0o600});
  const address = `http://127.0.0.1:${await port()}`;
  const app = spawn(process.env.KEVENOS_TEST_PYTHON || 'python3', ['server.py','--host','127.0.0.1','--port',address.split(':').at(-1),'--mode','self'],
    {cwd:root, env:{PATH:process.env.PATH, KEVENOS_DATA:data, KEVENOS_HOSTED:'1', KEVENOS_SECRETS_DIR:secrets,
      KEVENOS_ENGAGEMENT_V2:'1', KEVENOS_ENGAGEMENT_AI_ENABLED:'0', KEVENOS_GATEWAY_ENABLED:'0',
      KEVENOS_SMTP_ENABLED:'0', KEVENOS_ASSISTANT_ENABLED:'0', KEVENOS_DB_BACKEND:'sqlite', TZ:'UTC'},
      stdio:['ignore','ignore','pipe']});
  let errors = ''; app.stderr.on('data', chunk => { errors += chunk.toString(); });
  async function call(route, body, session={}) {
    const headers = {...session.headers};
    if (session.cookie) headers.Cookie = session.cookie;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(address + route, {method:body === undefined ? 'GET' : 'POST', headers,
      ...(body === undefined ? {} : {body:JSON.stringify(body)}), signal:AbortSignal.timeout(60000)});
    const text = await response.text();
    if (!response.ok) throw new Error(`${route} returned ${response.status}: ${text.slice(0, 200)}`);
    session.headers ||= {};
    for (const name of ['x-event-context','x-access-context']) if (response.headers.get(name)) session.headers[name] = response.headers.get(name);
    const cookies = response.headers.getSetCookie();
    const access = cookies.find(value => value.startsWith('pp_access='));
    if (access) session.cookie = access.split(';',1)[0];
    return JSON.parse(text);
  }
  try {
    let ready = false;
    for (let i=0; i<100 && !ready; i++) {
      try { ready = (await fetch(address+'/healthz')).ok; } catch (_) { /* Startup is still in progress. */ }
      if (!ready) await delay(100);
    }
    assert(ready, 'synthetic app did not start');
    const organizer = {}; await call('/api/access/login', {role:'admin',password:admin}, organizer);
    const command = async (action, payload, revision) => call('/api/desk/engagement', {action,payload,revision,request_id:id()}, organizer);
    let state = await command('save', {title:'Synthetic load poll',kind:'poll',audiences:['guest'],questions:[
      {type:'single',prompt:'Synthetic load question?',options:['A','B']}]}, 0);
    const activity = state.activities[0];
    state = await command('publish', {activity_id:activity.id}, state.revision);
    state = await command('open_run', {activity_id:activity.id,version:1}, state.revision);
    let run = state.runs[0];
    state = await command('open_question', {run_id:run.id,question_id:run.snapshot.questions[0].id,duration:null}, state.revision);
    run = state.runs[0];
    const sessions = [];
    for (let i=0; i<100; i++) { const session = {}; await call('/api/access/login', {role:'guest',password:guest}, session); sessions.push(session); }
    const samples = [], pollSamples = [], deskSamples = [], started = performance.now();
    const attendees = Promise.all(sessions.map(async (session,index) => {
      const pollStart = performance.now();
      const view = await call('/api/engagement/player', undefined, session);
      pollSamples.push(performance.now()-pollStart);
      assert.equal(view.question.delivery.id, run.delivery.id, 'attendee sees the active delivery');
      const begin = performance.now();
      const response = await call('/api/engagement/player', {run_id:run.id,delivery_id:run.delivery.id,choice:index%2}, session);
      samples.push(performance.now()-begin); return response.receipt.id;
    }));
    const deskReads = Promise.all(Array.from({length:10}, async () => {
      const begin = performance.now(); await call('/api/desk/engagement', undefined, organizer);
      deskSamples.push(performance.now()-begin);
    }));
    const [receipts] = await Promise.all([attendees,deskReads]);
    const elapsed = performance.now()-started;
    assert.equal(new Set(receipts).size, 100, 'all attendee receipts must be distinct');
    state = await call('/api/desk/engagement', undefined, organizer);
    assert.equal(state.runs[0].analytics.respondents, 100, 'all answers must be recorded');
    assert(!errors.includes('Traceback (most recent call last)'), 'server traceback during synthetic load');
    console.log(JSON.stringify({backend:'sqlite',attendees:100,successful:receipts.length,
      burst_ms:Math.round(elapsed),poll_p95_ms:Math.round(percentile(pollSamples,.95)),
      answer_p50_ms:Math.round(percentile(samples,.5)),answer_p95_ms:Math.round(percentile(samples,.95)),
      organizer_p95_ms:Math.round(percentile(deskSamples,.95))}));
  } finally {
    if (app.exitCode === null && app.signalCode === null) { app.kill('SIGTERM'); await new Promise(resolve => app.once('exit',resolve)); }
    await fs.rm(data,{recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
