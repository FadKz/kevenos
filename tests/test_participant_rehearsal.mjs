import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, readFile, writeFile} from 'node:fs/promises';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const temp = await mkdtemp(path.join(tmpdir(), 'kevenos-participant-review-'));
const port = await new Promise(resolve => {
  const socket = createServer().listen(0, '127.0.0.1', () => {
    const chosen = socket.address().port;
    socket.close(() => resolve(chosen));
  });
});
const origin = 'http://127.0.0.1:' + port;
const adminPassword = 'Synthetic-Rehearsal-Admin9!';
const registrationOnly = process.env.KEVENOS_REGISTRATION_ONLY === '1';
const devicePasswords = {a:'Rehearsal-Alex9!', b:'Rehearsal-Sam9!', judge:'Rehearsal-Judge9!'};
const secrets = path.join(temp, 'secrets');
await mkdir(secrets);
for (const [name, value] of Object.entries({KEVENOS_PASSWORD:adminPassword, KEVENOS_GUEST_PASSWORD:'Synthetic-Guest9!',
  KEVENOS_DESK_PIN:'861903', KEVENOS_VIEW_KEY:'synthetic-review-screen', KEVENOS_SESSION_SECRET:'synthetic-browser-test-session-key'})) {
  await writeFile(path.join(secrets, name), value, {mode:0o600});
}
const app = spawn(process.env.KEVENOS_TEST_PYTHON || 'python3', ['server.py','--host','127.0.0.1','--port',String(port),
  ...(registrationOnly ? [] : ['--rehearsal']),'--mode','self'],
  {cwd:root, env:{...process.env, KEVENOS_DATA:temp, KEVENOS_HOSTED:'1', KEVENOS_GATEWAY_ENABLED:'0', KEVENOS_SECRETS_DIR:secrets, TZ:'UTC'}, stdio:['ignore','ignore','inherit']});
const chrome = spawn(process.env.KEVENOS_TEST_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new','--disable-gpu','--disable-background-networking','--disable-background-timer-throttling','--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows','--no-first-run','--remote-debugging-port=0','--user-data-dir='+path.join(temp,'chrome'),'about:blank'], {stdio:'ignore'});
const sockets = [];
async function until(check, label, limit = 15000) {
  const end = Date.now() + limit;
  let error;
  do {
    try { const value = await check(); if (value) return value; } catch (e) { error = e; }
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < end);
  throw new Error('Timed out: ' + label + (error ? ' (' + error.message + ')' : ''));
}
async function connection(url) {
  const ws = new WebSocket(url);
  sockets.push(ws);
  await new Promise((resolve,reject) => {ws.addEventListener('open',resolve);ws.addEventListener('error',reject);});
  let id = 0;
  const pending = new Map(), exceptions = [];
  ws.addEventListener('message', ({data}) => {
    const event = JSON.parse(data);
    if (event.method === 'Runtime.exceptionThrown') exceptions.push(event.params.exceptionDetails.exception?.description || event.params.exceptionDetails.text);
    const action = pending.get(event.id);
    if (!action) return;
    pending.delete(event.id);
    if (event.error) action.reject(new Error(event.error.message)); else action.resolve(event.result);
  });
  return {exceptions, send(method, params = {}) {
    return new Promise((resolve,reject) => {pending.set(++id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
  }};
}

try {
  await until(async () => (await fetch(origin+'/healthz')).ok, 'rehearsal service');
  const debugPort = (await until(() => readFile(path.join(temp,'chrome','DevToolsActivePort'),'utf8'), 'Chrome')).split('\n')[0];
  const base = 'http://127.0.0.1:' + debugPort;
  const version = await (await fetch(base+'/json/version')).json();
  const browser = await connection(version.webSocketDebuggerUrl);
  async function device(width, height, mobile) {
    const {browserContextId} = await browser.send('Target.createBrowserContext');
    const {targetId} = await browser.send('Target.createTarget',{url:'about:blank',browserContextId});
    const target = await until(async () => (await (await fetch(base+'/json/list')).json()).find(page=>page.id===targetId), 'device target');
    const session = await connection(target.webSocketDebuggerUrl);
    await session.send('Page.enable');
    await session.send('Runtime.enable');
    await session.send('Network.enable');
    await session.send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile});
    if (mobile) await session.send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:5});
    await session.send('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:width===375?'dark':'light'}]});
    const js = async expression => {
      const response = await session.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
      if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
      return response.result.value;
    };
    return {...session, js,
      nav: url => session.send('Page.navigate',{url:origin+url}),
      visible: selector => js('Boolean(document.querySelector('+JSON.stringify(selector)+')?.getClientRects().length)'),
      click: selector => js('document.querySelector('+JSON.stringify(selector)+').click()'),
      text: selector => js('document.querySelector('+JSON.stringify(selector)+')?.textContent || ""'),
      input: (selector,value) => js('(()=>{const e=document.querySelector('+JSON.stringify(selector)+');e.value='+JSON.stringify(value)+';e.dispatchEvent(new Event("input",{bubbles:true}));})()'),
      api: (route,body) => js('KEV.api('+JSON.stringify(route)+','+(body ? JSON.stringify({body}) : 'null')+')'),
      async capture(name) {
        if (!process.env.KEVENOS_SHOTS) return;
        await mkdir(process.env.KEVENOS_SHOTS,{recursive:true});
        const result=await session.send('Page.captureScreenshot',{format:'png'});
        await writeFile(path.join(process.env.KEVENOS_SHOTS,name+'.png'),Buffer.from(result.data,'base64'));
      }
    };
  }
  async function login(device, role, password, page, ready) {
    await device.nav('/?access='+role);
    await until(() => device.visible('#password'),role+' sign-in');
    await device.input('#password',password);
    await device.js('document.querySelector("#signin").requestSubmit()');
    try {
      await until(async () => (await device.js('location.pathname'))===page && await device.visible(ready), role+' page');
    } catch (error) {
      throw new Error(error.message + ' · ' + await device.js('location.pathname') + ' · ' +
        await device.text('#signin-error') + ' · ' + await device.text('#login-help') + ' · ' +
        await device.text('#lerr') + ' · ' + device.exceptions.join('; '));
    }
    assert.equal(await device.js('document.documentElement.scrollWidth <= innerWidth'),true);
  }
  const desk = await device(1280,900,false);
  await login(desk,'admin',adminPassword,'/admin','#app');
  if (registrationOnly) {
    const registration = await desk.api('/api/admin/credentials',{action:'create',role:'registration',
      label:'Synthetic registration operator',password:'Synthetic-Registration9!'});
    await desk.api('/api/admin/workspace',{mode:'rehearsal',confirm:'REHEARSAL'}).catch(error => {
      if (!error.message.includes('dataset changed')) throw error;
    });
    await until(async () => {
      const status = await desk.api('/api/admin/workspace');
      return status.mode === 'rehearsal';
    }, 'rehearsal workspace');
    const {roster} = await desk.api('/api/desk/roster');
    const openTeam = roster.find(row=>row.pair===9);
    openTeam.members[1].name = '';
    openTeam.partner = 'Example Partner';
    await desk.api('/api/desk/roster',{rows:roster});
    const registrar = await device(1080,860,false);
    await login(registrar,'registration',registration.password,'/registration','#registration-app');
    await registrar.input('#registration-search','Example Partner');
    await registrar.capture('10-registration-desk');
    await registrar.click('#add-participant');
    await registrar.input('#registration-form [name=participant]','Walk-in Example');
    await registrar.js('document.querySelector("#registration-form").requestSubmit()');
    await registrar.input('#registration-search','Walk-in Example');
    await until(async()=> (await registrar.text('#registration-list')).includes('Checked in'),'walk-in check-in');
    await registrar.click('[data-action=qr]');
    await until(()=>registrar.visible('#registration-qr'),'QR visible');
    const image = await registrar.js('document.querySelector("#registration-qr").getAttribute("src")');
    assert.match(image,/^data:image\/svg\+xml;base64,/);
    const link = await registrar.js(`(async()=>{let copied;Object.defineProperty(navigator,'clipboard',{value:{
      writeText(value){copied=value;return Promise.resolve();}}});document.querySelector('#registration-copy-link').click();
      await Promise.resolve();return copied;})()`);
    assert.equal(new URL(link).pathname,'/join');
    assert(!new URL(link).search);
    await registrar.capture('11-registration-qr');
    const phone = await device(390,844,true);
    await phone.nav(link.slice(origin.length));
    await until(()=>phone.visible('#team-navigation'),'participant opened QR');
    const identity = await phone.api('/api/access/me');
    assert.equal(identity.role,'participant');
    assert.equal(identity.label,'Walk-in Example');
    assert.equal(identity.pair,9);
    await phone.capture('12-qr-participant');
    const replay = await device(390,844,true);
    await replay.nav(link.slice(origin.length));
    await until(async()=>(await replay.text('#join-status')).includes('expired or been used'),'QR replay blocked');
    for (const page of [desk,registrar,phone,replay]) assert.deepEqual(page.exceptions,[]);
    console.log('Passed Registration sign-in, walk-in check-in, private QR display, phone handoff, and replay rejection.');
    if (process.env.KEVENOS_KEEP_REVIEW === '1') console.log('Registration review seed: ' + temp);
    process.exitCode = 0;
  } else {
  const seeded = await desk.api('/api/desk/roster');
  const rows = seeded.roster;
  rows[0].partner='Rehearsal Partner';
  rows[0].members[0].name='Alex Example';
  rows[0].members[1].name='Sam Example';
  await desk.api('/api/desk/roster',{rows});
  const preliminary = (await desk.api('/api/desk/scores')).leaderboard;
  const seededSheet = preliminary.find(row => row.pair === 1);
  if (seededSheet) await desk.api('/api/desk/sheet',{action:'void',id:seededSheet.sheet,why:'Start the synthetic browser rehearsal with team 1 unscored'});
  const issue = async fields => (await desk.api('/api/admin/credentials',{action:'create',label:'Synthetic browser rehearsal',...fields})).password;
  const aPassword = await issue({role:'participant',pair:1,member:1,password:devicePasswords.a});
  const bPassword = await issue({role:'participant',pair:1,member:2,password:devicePasswords.b});
  const judgePassword = await issue({role:'judge',station:1,password:devicePasswords.judge});
  const phoneA = await device(390,844,true);
  const phoneB = await device(375,812,true);
  const judge = await device(1024,768,false);
  await login(phoneA,'participant',aPassword,'/participant','#team-navigation');
  await login(phoneB,'participant',bPassword,'/participant','#team-navigation');
  await login(judge,'judge',judgePassword,'/judge/1','#app');
  await judge.click('#judge-home-open');
  console.log('Signed in on two isolated mobile touch sessions and one judge viewport.');
  await phoneA.capture('01-participant-today');
  assert.equal(await phoneA.visible('#pitch-prep-card'),false);
  assert.equal(await phoneA.js('Boolean(window.PitchPrepPlayer)'),false);
  const prepIndex = JSON.parse(await readFile(path.join(root, 'content.json'), 'utf8')).agenda.findIndex(item=>item.scene==='prep');
  assert(prepIndex>=0);
  await desk.api('/api/desk/program',{action:'set',index:prepIndex});
  await until(async()=>await phoneA.visible('#pitch-prep-card') && await phoneB.visible('#pitch-prep-card'),'Desk starts Prep');
  assert.equal(await phoneA.visible('#pitch-prep-dialog'),false);
  await phoneA.capture('14-pitch-prep-card');
  await phoneA.click('#pitch-prep-card [data-watch-prep]');
  await until(()=>phoneA.visible('.prep-chapters'),'video loaded on demand');
  assert.equal(await phoneA.js('document.querySelectorAll(".prep-chapters button").length'),7);
  assert.equal(await phoneA.js('document.documentElement.scrollWidth <= innerWidth'),true);
  await phoneA.capture('15-pitch-prep-video');
  await phoneA.send('Page.bringToFront');
  await phoneA.click('.prep-controls button');
  await until(async()=> (await phoneA.text('.prep-controls button')) === 'Pause','video plays');
  await until(async()=> Number(await phoneA.js('document.querySelector(".prep-controls input").value'))>=1,'video advances');
  await phoneA.click('.prep-controls button');
  await until(async()=> (await phoneA.text('.prep-controls button')) === 'Play','video pauses');
  await phoneA.js('document.querySelector(".prep-chapters button:nth-child(5)").click()');
  await until(async()=> (await phoneA.text('#pitch-prep-player')).includes('Handle the concern'),'chapter seeks to Solve');
  await phoneA.click('.prep-example button:nth-child(2)');
  assert.match(await phoneA.text('.prep-example [role=status]'), /before making that promise/);
  await phoneA.click('.prep-example button:first-child');
  assert.match(await phoneA.text('.prep-example [role=status]'), /Good/);
  await phoneA.capture('16-pitch-prep-objection');
  await phoneA.click('.prep-chapter-action button');
  await until(async()=> (await phoneA.text('#worksheet-step')).startsWith('Solve') && !await phoneA.visible('#pitch-prep-dialog'),'jump to the Solve worksheet');
  assert.equal(await phoneA.js('document.querySelector("#worksheet-cues").value'),'');
  assert.equal(await phoneA.visible('#worksheet-prep-guide'),true);
  await phoneA.click('[data-panel=today]');
  await phoneB.click('#pitch-prep-card [data-watch-prep]');
  await until(()=>phoneB.visible('.prep-chapters'),'teammate opens guide independently');
  await desk.api('/api/desk/program',{action:'set',index:0});
  await until(async()=>!await phoneB.visible('#pitch-prep-card') && !await phoneB.visible('#pitch-prep-dialog'),'exit Prep closes prompt');
  await phoneB.click('[data-panel=more]');
  await phoneB.click('[data-open-panel=resources]');
  await phoneB.click('[data-team-panel=resources] [data-watch-prep]');
  await until(()=>phoneB.visible('.prep-chapters'),'video remains in Resources');
  await phoneB.click('#pitch-prep-close');
  assert(await phoneB.js('document.activeElement.matches("[data-watch-prep]")'));
  await phoneB.click('[data-panel=today]');
  await until(async()=> (await phoneA.text('#now-title')).includes('Registration'),'return to arrival');

  await phoneA.click('[data-panel=more]');
  await phoneA.click('[data-open-panel=agenda]');
  await phoneA.click('#agenda [aria-current=step] summary');
  assert((await phoneA.text('#agenda [aria-current=step] .timeline-detail')).includes('30 min'));
  await phoneA.capture('02-full-agenda');
  await phoneA.click('[data-panel=today]');
  await phoneA.click('[data-panel=more]');
  await phoneA.click('#quickstart summary');
  await phoneA.js('document.querySelector("#quickstart").scrollIntoView({block:"center"})');
  await phoneA.capture('11-quick-start');
  await phoneA.click('#quickstart summary');
  await phoneA.click('[data-panel=more]');
  await phoneA.click('[data-open-panel=resources]');
  await phoneA.js('scrollTo(0,0)');
  await phoneA.capture('10-participant-resources');
  await phoneA.click('[data-panel=today]');
  await Promise.all([phoneA.click('#open-team-pitch'),phoneB.click('#open-team-pitch')]);
  await phoneA.click('[data-step="0"]');
  await phoneB.input('#worksheet-cues','Sam has an unsaved alternative.');
  const cues = 'Ask why the refresh matters and who approves it.';
  await phoneA.input('#worksheet-cues',cues);
  await phoneA.input('#worksheet-speaker','Alex');
  await phoneA.click('#worksheet-save');
  await until(async () => (await phoneA.text('#worksheet-status'))==='Saved for the team','worksheet save');
  await until(() => phoneB.visible('#worksheet-reload'),'phone B conflict');
  await phoneB.click('#worksheet-reload');
  assert.equal(await phoneB.js('document.querySelector("#worksheet-cues").value'),cues);
  await phoneA.capture('02-team-worksheet');
  await judge.click('#ctl [data-a=start]');
  await until(async () => (await phoneA.text('#focus-status')).includes('Synced') && (await phoneB.text('#focus-status')).includes('Synced'),'both clocks live');
  assert.equal(await phoneA.text('#pitch-cues'),cues);
  assert.equal(await phoneB.text('#pitch-cues'),cues);
  const seconds = value => value.split(':').map(Number).reduce((a,b)=>a*60+b);
  const [first, second] = await Promise.all([phoneA.text('#focus-clock'),phoneB.text('#focus-clock')]);
  assert(Math.abs(seconds(first)-seconds(second))<=2);
  const [participantState, judgeState] = await Promise.all([phoneA.api('/api/participant/state'),judge.api('/api/sync',{})]);
  assert.equal(participantState.action.clock.round,judgeState.clock.round);
  assert(Math.abs(participantState.action.clock.phase_left-judgeState.clock.phase_left)<=2);
  await phoneB.capture('03-phone-pitch-dark');
  await judge.capture('04-judge-clock');
  await desk.api('/api/desk/clock',{action:'seek',station:1,elapsed:150});
  await until(async () => (await phoneA.text('#worksheet-step')).includes('Solution at Work'),'follow judge step');
  await phoneA.click('[data-step="0"]');
  await phoneA.capture('13-browse-cues');
  await desk.api('/api/desk/clock',{action:'seek',station:1,elapsed:330});
  await until(async () => (await phoneA.text('#team-step-tabs [aria-current=step]')).includes('Solve'),'judge moved to Solve');
  assert((await phoneA.text('#worksheet-step')).includes('Discover'));
  await phoneA.click('#follow-pitch');
  assert((await phoneA.text('#worksheet-step')).includes('Solve'));
  await desk.api('/api/desk/clock',{action:'seek',station:1,elapsed:4});
  await until(async () => (await phoneA.text('#worksheet-step')).includes('Discover'),'follow restored');
  await judge.click('#ctl [data-a=pause]');
  await until(async () => (await phoneA.text('#focus-status')).startsWith('Paused') && (await phoneB.text('#focus-status')).startsWith('Paused'),'both clocks paused');
  const paused = await phoneA.text('#focus-clock');
  await new Promise(resolve=>setTimeout(resolve,1200));
  assert.equal(await phoneA.text('#focus-clock'),paused);
  await judge.click('#ctl [data-a=start]');
  await until(async () => (await phoneA.text('#focus-status')).includes('Synced'),'judge resumed');
  await phoneB.send('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
  await until(async () => (await phoneB.text('#focus-status')).startsWith('Connection lost'),'offline cues',18000);
  assert.equal(await phoneB.text('#pitch-cues'),cues);
  assert.equal(await phoneB.text('#focus-clock'),'–:––');
  await phoneB.capture('05-offline-cues');
  await phoneB.send('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
  await until(async () => (await phoneB.text('#focus-status')).includes('Synced'),'clock resync');
  await judge.api('/api/session',{action:'feedback'});
  await until(async () => (await phoneA.text('#focus-phase'))==='Feedback','feedback clock');
  await judge.click('#bsub2');
  await until(() => judge.visible('#participant-approve'),'judge feedback form');
  for (let index=0; index<8; index++) await judge.click('[data-m="'+index+'"]');
  await judge.click('[data-rating="4"]');
  await judge.click('#spoke');
  const feedback = 'You made the benefits clear. Next time, agree who from IT will attend.';
  await judge.input('#fb',feedback);
  await judge.click('#participant-approve');
  await judge.click('#bsub2');
  if (await judge.visible('#mgo')) await judge.click('#mgo');
  await until(async () => Boolean((await judge.api('/api/sync',{})).submitted),'judge receipt');
  await judge.api('/api/session',{action:'finish'});
  await phoneA.click('[data-panel=results]');
  await until(async () => (await phoneA.text('#private-preliminary-body')).includes('under review'),'private result before release');
  assert(!(await phoneA.text('#private-preliminary-body')).includes(feedback));
  const guide = (await desk.api('/api/event')).guide;
  const breakIndex = guide.schedule.findIndex(item=>item.title.includes('Break while'));
  assert(breakIndex>=0);
  await desk.api('/api/desk/program',{action:'set',index:breakIndex});
  await until(async () => (await phoneA.text('#agenda-current [aria-current=step]')).includes('Break while'),
    'participant agenda follows the Desk');
  await desk.nav('/desk#participants');
  await until(() => desk.visible('#participant-news-form'),'Desk participant controls');
  await desk.input('#participant-news-form textarea','Follow the host for the awards.');
  await desk.js('document.querySelector("#participant-news-form").requestSubmit()');
  await until(async () => (await desk.text('#participant-desk-status'))==='Saved.','Desk update saved');
  await desk.send('Page.enable');
  const releaseClick = desk.click('#participant-release');
  await until(async () => {
    try { await desk.send('Page.handleJavaScriptDialog',{accept:true}); return true; } catch { return false; }
  },'private release confirmation');
  await releaseClick;
  await until(async () => (await phoneA.text('#private-preliminary-body')).includes(feedback),'released feedback');
  await phoneA.capture('06-private-results');
  await desk.click('#participant-nom-window');
  await phoneA.click('[data-panel=today]');
  await phoneB.click('[data-panel=today]');
  await until(() => phoneA.visible('#open-team-nominations'),'nominations open');
  assert((await phoneA.text('#team-news')).includes('Follow the host'));
  await phoneA.click('#team-news-card summary');
  await phoneA.js('document.querySelector("#team-news-card").scrollIntoView({block:"center"})');
  await phoneA.capture('12-desk-updates');
  await phoneA.click('#open-team-nominations');
  await phoneB.click('#open-team-nominations');
  for (let i=0; i<3; i++) {
    await phoneA.input('[data-index="'+i+'"][data-field=name]','Example Company '+(i+1));
    await phoneA.input('[data-index="'+i+'"][data-field=why]','Their team has a laptop refresh and would benefit from a solution device pilot.');
  }
  await phoneA.click('#nomination-draft');
  await until(async () => (await phoneB.js("document.querySelector('[data-index=\"0\"][data-field=name]').value"))==='Example Company 1','draft on teammate phone');
  await phoneB.click('#nomination-submit');
  await until(async () => (await phoneA.text('#nomination-status')).includes('Submitted for your team'),'shared nomination receipt');
  await phoneA.js('document.querySelector("#team-nomination").scrollIntoView()');
  await phoneA.capture('07-three-company-nomination');
  await until(async () => (await desk.api('/api/desk/participant-workspace')).nominations[0].companies.length===3,'organizer received three companies');
  await desk.capture('08-desk-participant-controls');
  await desk.api('/api/desk/final',{action:'set',pairs:[1,2,3],panel:[1]});
  await desk.api('/api/desk/final',{action:'start'});
  await desk.api('/api/desk/clock',{action:'start'});
  await until(async () => (await phoneA.api('/api/participant/state')).action.code==='final_pitch','participant Final clock');
  await phoneA.click('#open-team-pitch');
  await until(async () => (await phoneA.text('#focus-status')).includes('stage clock'),'Final Pitch Mode');
  assert.equal(await phoneA.text('#pitch-cues'),cues);
  assert(seconds(await phoneA.text('#focus-clock'))<=480);
  await phoneA.capture('09-final-pitch');
  for (const page of [desk,phoneA,phoneB,judge]) assert.deepEqual(page.exceptions,[]);
  console.log('Passed: shared cues and conflict recovery, judge start/pause/resume, offline cues, private feedback release, Desk news, shared nomination draft, three-company receipt and Final clock with original cues.');
  if (process.env.KEVENOS_KEEP_REVIEW === '1') console.log('Review seed: ' + temp);
  }
} finally {
  for (const socket of sockets) socket.close();
  chrome.kill();
  app.kill();
}
