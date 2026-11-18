// Maintained standalone browser acceptance harness. Not executed during the 4.5.0 agent session;
// actual Chrome qualification is performed through the authorized CUA workflow.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

const scratch = await mkdtemp(path.join(tmpdir(), 'kevenos-browser-'));
const port = 18469;
const origin = 'http://127.0.0.1:' + port;
const password = 'rehearsal-test-only';
const localOnlyPin = '618944'; // Required legacy local-kit configuration; never used for hosted sign-in.
const viewKey = 'rehearsal-view-only';
const secretsDir = path.join(scratch, 'secrets');
await mkdir(secretsDir);
for (const [file, value] of Object.entries({KEVENOS_PASSWORD: password, KEVENOS_GUEST_PASSWORD: 'Synthetic-Guest8!', KEVENOS_DESK_PIN: localOnlyPin, KEVENOS_VIEW_KEY: viewKey, KEVENOS_SESSION_SECRET: 'synthetic-secret-for-browser-only'})) {
  await writeFile(path.join(secretsDir, file), value, {mode: 0o600});
}
const app = spawn('python3', ['server.py', '--host', '127.0.0.1', '--port', String(port)], {
  cwd: new URL('..', import.meta.url).pathname,
  env: {...process.env, KEVENOS_DATA: scratch, KEVENOS_HOSTED: '1', KEVENOS_GATEWAY_ENABLED: '0', KEVENOS_SECRETS_DIR: secretsDir, TZ: 'UTC'},
  stdio: 'ignore',
});
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
  '--headless=new', '--disable-gpu', '--disable-background-networking', '--no-first-run',
  '--remote-debugging-port=0', '--user-data-dir=' + path.join(scratch, 'chrome'), 'about:blank',
], {stdio: 'ignore'});
const until = async f => {
  for (let i = 0; i < 100; i++) {
    try { const r = await f(); if (r) return r; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error('Timed out waiting for browser or app');
};
let ws;
try {
  await until(async () => (await fetch(origin + '/healthz')).ok);
  const debugPort = (await until(() => readFile(path.join(scratch, 'chrome', 'DevToolsActivePort'), 'utf8'))).split('\n')[0];
  const [page] = await (await fetch('http://127.0.0.1:' + debugPort + '/json/list')).json();
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve); ws.addEventListener('error', reject); });
  const queue = new Map(), scriptErrors = []; let id = 0;
  ws.addEventListener('message', ({data}) => {
    const r = JSON.parse(data);
    if (r.method === 'Runtime.exceptionThrown') scriptErrors.push(r.params.exceptionDetails.exception?.description || r.params.exceptionDetails.text);
    if (r.method === 'Runtime.consoleAPICalled' && r.params.type === 'error') scriptErrors.push(r.params.args.map(arg => arg.value || arg.description || '').join(' '));
    const pending = queue.get(r.id);
    if (!pending) return;
    queue.delete(r.id);
    if (r.error) pending.reject(new Error(JSON.stringify(r.error)));
    else pending.resolve(r.result);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    queue.set(++id, {resolve, reject});
    ws.send(JSON.stringify({id, method, params}));
  });
  const evalJs = async expression => {
    const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  };
  const nav = url => send('Page.navigate', {url: origin + url});
  const size = (width, height) => send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
  const save = async name => {
    const image = await send('Page.captureScreenshot', {format: 'png'});
    if (process.env.KEVENOS_SHOTS) {
      await mkdir(process.env.KEVENOS_SHOTS, {recursive: true});
      await writeFile(path.join(process.env.KEVENOS_SHOTS, name + '.png'), Buffer.from(image.data, 'base64'));
    }
  };


  await send('Runtime.enable');
  await send('Page.enable');
  // KEV.api supplies the active profile token and frozen access/event contexts.
  const api = (url, body) => evalJs(`(async()=>{try{return {status:200,data:await KEV.api(${JSON.stringify(url)},${body === undefined ? 'undefined' : JSON.stringify({body})})};}catch(error){return {status:error.status || 0,data:{error:error.message,code:error.code}};}})()`);
  const visiblePasswords = () => evalJs("[...document.querySelectorAll('input[type=password]')].filter(e=>e.getClientRects().length && !e.disabled).length");
  const fits = async () => assert.equal(await evalJs('document.documentElement.scrollWidth<=innerWidth'),true,'page must fit the viewport');
  const openSignIn = async () => {
    await nav('/');
    await until(()=>evalJs("location.pathname==='/' && !!document.querySelector('#password')"));
    assert.equal(await visiblePasswords(),1);
    assert.equal(await evalJs("!!document.querySelector('#pin')"),false);
    assert.equal(await evalJs("!!document.querySelector('#signin select')"),false);
    await fits();
  };
  const submit = async value => evalJs("document.querySelector('#password').value="+JSON.stringify(value)+";document.querySelector('#signin').requestSubmit()");
  const arrived = pathname => {
    const ready=pathname==='/participant'?'#guide':['/guest','/event'].includes(pathname)?'#overview':pathname==='/registration'?'#registration-app':'#app';
    return until(()=>evalJs("location.pathname==="+JSON.stringify(pathname)+" && document.querySelector("+JSON.stringify(ready)+")?.hidden===false"));
  };
  const profile = () => api('/api/access/me');
  const home = () => until(()=>evalJs("location.pathname==='/home' && document.querySelector('#home-dashboards')?.hidden===false"));
  const openDashboard = async (pathname,href=pathname) => {
    await home();
    await evalJs("document.querySelector('#home-dashboards a[href="+JSON.stringify(href)+"]').click()");
    await arrived(pathname);
  };
  const switchProfile = async () => {
    await until(()=>evalJs("!!document.querySelector('[data-profile=switch]')"));
    await evalJs("document.querySelector('[data-profile=switch]').click()");
    await until(()=>evalJs("location.pathname==='/' && !!document.querySelector('#password')"));
    assert.equal((await profile()).data.authenticated,false);
  };
  const returnToAdmin = async () => {
    await evalJs("document.querySelector('[data-profile=end]').click()");
    await arrived('/admin');
    assert.equal((await profile()).data.role,'admin');
    assert.equal(await evalJs("sessionStorage.getItem('kevenos:profile-token')"),null);
  };
  const openPreview = async (role, targetText) => {
    await nav('/admin#profiles');
    await until(()=>evalJs("!!document.querySelector('#profile-form')"));
    await evalJs("document.querySelector('#profile-role').value="+JSON.stringify(role)+";document.querySelector('#profile-role').dispatchEvent(new Event('change',{bubbles:true}));");
    await evalJs("(()=>{const target=[...document.querySelector('#profile-target').options].find(option=>option.textContent.includes("+JSON.stringify(targetText)+"));if(!target)throw new Error('Missing preview target');document.querySelector('#profile-target').value=target.value;document.querySelector('#profile-mode').value='preview';document.querySelector('#profile-form').requestSubmit()})()");
  };

  await size(390,844);
  await openSignIn();
  await save('signin-mobile');
  await submit('incorrect-synthetic-password');
  await until(()=>evalJs("!!document.querySelector('#signin-error').textContent"));
  assert.doesNotMatch(await evalJs("document.querySelector('#signin-error').textContent"),/PIN/);
  await submit('Synthetic-Guest8!');await openDashboard('/event');
  assert.equal((await api('/api/admin/credentials')).status,403);
  assert.equal((await profile()).data.role,'guest');
  assert.deepEqual((await profile()).data.dashboards.map(view=>view.url),['/event']);
  await switchProfile();

  await submit(password);await openDashboard('/admin');
  assert.equal((await profile()).data.role,'admin');
  assert(await evalJs('Boolean(KEV.profile.context)'));
  await size(1280,900);await fits();await save('admin-desktop');
  const roster=(await api('/api/desk/roster')).data.roster;
  roster[0].partner='Test Team';
  if(roster[0].members){roster[0].members[0].name='Alex Example';roster[0].members[1].name='Sam Example';}
  else {roster[0].p1='Alex Example';roster[0].p2='Sam Example';}
  assert.equal((await api('/api/desk/roster',{rows:roster})).status,200);
  const issued={};
  for(const role of ['organizer','admin','participant','judge']){
    const response=await api('/api/admin/credentials',{action:'create',role,label:'Example '+role,...(role==='participant'?{pair:1,member:1}:role==='judge'?{station:1}:{})});
    assert.equal(response.status,200);assert(response.data.password);issued[role]=response.data.password;
  }
  const accounts=(await api('/api/admin/accounts')).data.accounts;
  const judge=accounts.find(account=>account.label==='Example judge');
  assert.equal((await api('/api/admin/accounts',{action:'roles',id:judge.id,revision:judge.revision,reason:'Synthetic coverage',grants:[{role:'judge',station:1},{role:'registration'}]})).status,200);

  await size(390,844);await switchProfile();
  await submit(issued.participant);await arrived('/participant');
  assert.equal((await profile()).data.role,'participant');
  assert.equal((await api('/api/admin/credentials')).status,403);await fits();await save('participant-mobile');
  await switchProfile();

  await submit(issued.organizer);await openDashboard('/organizer');
  assert.equal((await profile()).data.dashboard,'organizer');
  assert.match(await evalJs("document.querySelector('#kevenos-profile').textContent"),/Organizer/);
  assert.equal((await api('/api/admin/credentials')).status,403);await fits();
  await nav('/judge/1#invite=retired-synthetic-code');
  await until(()=>evalJs("document.querySelector('#judge-hosted-access')?.hidden===false"));
  assert.match(await evalJs("document.querySelector('#login-help').textContent"),/Staff password/);
  assert.equal(await evalJs("document.querySelector('#judge-local-access').hidden"),true);
  await nav('/organizer');await arrived('/organizer');await switchProfile();
  await submit(issued.judge);await home();
  assert.deepEqual((await profile()).data.dashboards.map(view=>view.url).sort(),['/judge/1','/registration']);
  await openDashboard('/judge/1');
  const judgeIdentity=(await profile()).data;
  assert.equal(judgeIdentity.role,'judge');assert.equal(judgeIdentity.station,1);
  assert.equal(await evalJs("localStorage.getItem('kevenos:token:judge:1')"),null);
  assert.equal((await api('/api/admin/credentials')).status,403);await fits();await save('judge-station-mobile');
  await evalJs(`document.querySelector('[aria-controls=kevenos-nav-links]').click();document.querySelector('#kevenos-nav-links a[href="/registration"]').click()`);
  await arrived('/registration');
  assert.equal((await profile()).data.account_id,judgeIdentity.account_id);
  await switchProfile();

  await submit(issued.admin);await openDashboard('/admin');
  await openPreview('participant','Alex Example');await arrived('/participant');
  const participantPreview=(await profile()).data;
  assert.equal(participantPreview.role,'participant');assert.equal(participantPreview.actor.role,'admin');assert.equal(participantPreview.impersonation.mode,'preview');
  assert.match(await evalJs("document.querySelector('#kevenos-profile').textContent"),/Preview.*read-only/);
  assert.equal((await api('/api/submit',{pair:1})).data.code,'preview_read_only');
  assert.equal(await evalJs("document.querySelector('#documents a[href^=\"/api/resources/\"]')===null"),true);
  assert(await evalJs("Boolean(sessionStorage.getItem('kevenos:profile-token'))"));
  assert.equal(await evalJs("Object.keys(localStorage).some(key=>key==='kevenos:profile-token')"),false);
  await fits();await save('participant-preview-mobile');await returnToAdmin();
  await openPreview('judge','Station 1');await arrived('/judge/1');
  await until(()=>evalJs("document.querySelector('.tick')?.disabled===true"));
  assert.equal((await profile()).data.impersonation.mode,'preview');
  assert.equal((await api('/api/session',{action:'start'})).data.code,'preview_read_only');
  await fits();await save('judge-preview-mobile');await returnToAdmin();

  assert.deepEqual((await profile()).data.dashboards.map(view=>view.url),['/admin']);
  await openPreview('registration','Registration');await arrived('/registration');
  assert.equal((await profile()).data.impersonation.mode,'preview');
  await returnToAdmin();
  await switchProfile();
  await nav('/participant');
  await until(()=>evalJs("document.querySelector('#signin')?.hidden===false"));
  assert.match(await evalJs("document.querySelector('#signin').getAttribute('href')"),/^\//);
  assert.deepEqual(scriptErrors,[],'browser scripts must not report unexpected errors');
  console.log('PASS: password-only sign-in, Home, dashboard switching, scoped previews and mobile layouts.');

} finally {
  ws?.close(); chrome.kill(); app.kill();
  await Promise.all([chrome,app].map(child=>child.exitCode != null ? Promise.resolve() : new Promise(r=>child.once('exit',r))));
  await rm(scratch,{recursive:true,force:true,maxRetries:3});
}
