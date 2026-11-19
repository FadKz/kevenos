/* Isolated browser rehearsal with synthetic data. No production services are used. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {createServer} = require('node:net');
const {spawn} = require('node:child_process');
const {chromium, webkit, devices} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, label, timeout=20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await check()) return; await delay(150); }
  throw new Error('Timed out: ' + label);
}
async function port() {
  return new Promise(resolve => { const socket = createServer().listen(0, '127.0.0.1', () => {
    const value = socket.address().port; socket.close(() => resolve(value));
  }); });
}

(async () => {
  const data = await fs.mkdtemp(path.join(tmpdir(), 'kevenos-engagement-rehearsal-'));
  const secretDir = path.join(data, 'secrets'); await fs.mkdir(secretDir, {mode:0o700});
  const adminPassword = 'Synthetic-Engagement-Admin9!', deskPassword = 'Synthetic-Engagement-Desk9!';
  const guestPassword = 'Synthetic-Engagement-Guest9!';
  for (const [name,value] of Object.entries({KEVENOS_PASSWORD:adminPassword, KEVENOS_GUEST_PASSWORD:guestPassword,
    KEVENOS_SESSION_SECRET:'synthetic-engagement-session', KEVENOS_VIEW_KEY:'synthetic-engagement-screen'}))
    await fs.writeFile(path.join(secretDir,name), value, {mode:0o600});
  const address = 'http://localhost:' + await port();
  const app = spawn(process.env.KEVENOS_TEST_PYTHON || 'python3', ['server.py','--host','127.0.0.1','--port',address.split(':').at(-1),'--mode','self'],
    {cwd:root, env:{...process.env, KEVENOS_DATA:data, KEVENOS_HOSTED:'1', KEVENOS_SECRETS_DIR:secretDir,
      KEVENOS_ENGAGEMENT_V2:'1', KEVENOS_ENGAGEMENT_AI_ENABLED:'0', KEVENOS_GATEWAY_ENABLED:'0', KEVENOS_SMTP_ENABLED:'0', KEVENOS_ASSISTANT_ENABLED:'0', TZ:'UTC'},
      stdio:['ignore','ignore','pipe']});
  let serverErrors = '';
  app.stderr.on('data', chunk => { serverErrors += chunk.toString(); });
  let browser;
  try {
    await until(() => fetch(address + '/healthz').then(r => r.ok).catch(() => false), 'server starts');
    const engine = process.env.KEVENOS_TEST_BROWSER === 'webkit' ? webkit : chromium;
    browser = await engine.launch(engine === chromium && process.env.KEVENOS_CHROME_CHANNEL
      ? {channel:process.env.KEVENOS_CHROME_CHANNEL} : {});
    const errors = [];
    async function session(role, password, device={viewport:{width:1440,height:1000}}) {
      const context = await browser.newContext(device);
      const login = await context.request.post(address + '/api/access/login', {data:{role,password}});
      assert.equal(login.status(), 200, role + ' sign-in');
      if (engine === webkit) await context.addCookies((await context.cookies()).map(cookie => ({...cookie, secure:false})));
      const headers = {};
      const sync = response => { for (const key of ['x-event-context','x-access-context']) if (response.headers()[key]) headers[key] = response.headers()[key]; };
      sync(login);
      const api = async (route, body, expected=200) => {
        const response = await context.request.fetch(address+route, {method:body?'POST':'GET', data:body, headers});
        assert.equal(response.status(), expected, route + ' ' + await response.text()); sync(response); return response.json();
      };
      const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
      page.on('dialog', dialog => dialog.accept());
      return {context,page,api};
    }
    const admin = await session('admin', adminPassword);
    await admin.api('/api/admin/credentials', {action:'create',role:'desk',label:'Synthetic organizer',password:deskPassword});
    await admin.api('/api/admin/workspace', {mode:'rehearsal',confirm:'REHEARSAL'});
    const roster = (await admin.api('/api/desk/roster')).roster;
    roster[0].members[0].name = 'Alex Example'; roster[1].members[0].name = 'Sam Example';
    await admin.api('/api/desk/roster', {rows:roster});
    const issue = async (role,label,fields) => {
      const password = `Synthetic-Engagement-${label}9!`;
      await admin.api('/api/admin/credentials', {action:'create',role,label,password,...fields}); return password;
    };
    const phone1 = await session('participant', await issue('participant','Alex',{pair:1,member:1}), devices['iPhone 13']);
    const phone2 = await session('participant', await issue('participant','Sam',{pair:2,member:1}), devices['Pixel 7']);
    const guest = await session('guest', guestPassword, devices['iPad Pro 11']);
    const judge = await session('judge', await issue('judge','Judge',{station:1}), {viewport:{width:1024,height:768}});
    const desk = await session('desk', deskPassword);
    const hall = await browser.newContext({viewport:{width:1920,height:1080}, reducedMotion:'reduce'});
    const tv = await hall.newPage(); tv.on('pageerror', error => errors.push(error.message));
    const key = (await admin.api('/api/desk/state')).view_key;
    await Promise.all([phone1.page.goto(address+'/participant'), phone2.page.goto(address+'/participant'),
      guest.page.goto(address+'/guest'), judge.page.goto(address+'/judge/1'), desk.page.goto(address+'/organizer#engagement'),
      tv.goto(address+'/live?key='+encodeURIComponent(key))]);
    const page = desk.page;
    await page.getByRole('heading', {name:'Create a draft'}).waitFor();
    await page.getByLabel('Activity title').fill('Synthetic knowledge check');
    await page.getByLabel('Activity type').selectOption('quiz');
    await page.getByLabel('Guests', {exact:true}).check();
    await page.getByLabel('Question', {exact:true}).fill('Which answer is correct?');
    await page.getByLabel('Options, one per line').fill('Correct answer\nOther answer');
    await page.getByLabel('Correct option numbers, separated by commas').fill('1');
    await page.getByLabel('Points for a correct answer').fill('3');
    await page.getByRole('button', {name:'Save draft'}).click();
    await page.getByRole('button', {name:'Publish draft'}).click();
    await page.getByRole('button', {name:'Start run'}).click();
    await page.getByLabel('Question duration in seconds (leave blank for no timer)').fill('120');
    await page.getByRole('button', {name:'Open question'}).click();
    await page.getByRole('button', {name:'Share on hall'}).click();
    await phone1.page.locator('dialog.engagement-dialog[open]').getByLabel('Correct answer').check();
    await page.getByRole('button', {name:'Pause question'}).click();
    await until(async () => (await phone1.api('/api/engagement/player')).question?.delivery?.status === 'paused', 'pause reaches phones');
    await until(async () => (await phone1.page.locator('dialog.engagement-dialog').innerText()).includes('paused'), 'phone shows pause');
    assert(await phone1.page.locator('dialog.engagement-dialog').getByLabel('Correct answer').isChecked(), 'pause preserves the selection');
    const paused = await phone1.api('/api/engagement/player');
    await phone1.api('/api/engagement/player', {run_id:paused.question.id, delivery_id:paused.question.delivery.id, choice:0}, 409);
    await page.getByRole('button', {name:'Resume question'}).click();
    const resumed = await phone1.api('/api/engagement/player');
    const firstDialog = phone1.page.locator('dialog.engagement-dialog[open]');
    await firstDialog.waitFor();
    assert(await firstDialog.getByLabel('Correct answer').isChecked(), 'resume preserves the selection');
    await page.getByRole('button', {name:'Add 15 seconds'}).click();
    const extended = await phone1.api('/api/engagement/player');
    assert.equal(extended.question.delivery.ends_at - resumed.question.delivery.ends_at, 15);
    await until(async () => Number((await firstDialog.locator('.engagement-timer').innerText()).split(' ')[0]) > 120, 'extended timer reaches phone');
    assert(await firstDialog.getByLabel('Correct answer').isChecked(), 'extension preserves the selected answer');
    assert(!JSON.stringify(extended).includes('"correct"'), 'answers remain private before reveal');
    for (const attendee of [phone1,phone2,guest]) {
      const dialog = attendee.page.locator('dialog.engagement-dialog[open]');
      await dialog.waitFor({timeout:20000});
      await dialog.getByLabel('Correct answer').check();
      await dialog.getByRole('button', {name:'Submit answer'}).click();
      await dialog.getByText('Answer saved.').waitFor();
      await dialog.getByRole('button', {name:'Return to event'}).click();
    }
    await until(async () => (await tv.locator('body').innerText()).includes('Which answer is correct?'), 'hall question');
    assert(!(await tv.locator('body').innerText()).includes('Correct answer'), 'hall hides options before reveal');
    const denied = await judge.context.request.get(address+'/api/engagement/player'); assert.equal(denied.status(),403);
    await page.getByRole('button', {name:'Close question'}).click();
    await page.getByRole('button', {name:'Reveal answer'}).click();
    await until(async () => (await tv.locator('body').innerText()).includes('Correct answer'), 'hall reveal');
    assert.deepEqual((await phone1.api('/api/engagement/player')).score, {earned:3,available:3});
    assert.deepEqual((await guest.api('/api/engagement/player')).score, {earned:3,available:3});
    await page.getByRole('button', {name:'Finish run'}).click();
    await page.getByText('Completed runs (1)').waitFor();
    assert.deepEqual((await phone1.api('/api/engagement/player')).score, {earned:3,available:3});
    await page.getByLabel('Allow participants').check(); await page.getByLabel('Allow guests').check();
    await page.getByRole('button', {name:'Save audience settings'}).click();
    await until(async () => await phone1.page.locator('#engagement-questions').isVisible(), 'participant Q&A');
    await until(async () => await guest.page.locator('#engagement-questions').isVisible(), 'guest Q&A');
    await guest.page.locator('#engagement-questions summary').click();
    await phone1.page.locator('#engagement-questions summary').click();
    await phone1.page.locator('#engagement-questions textarea').fill('Synthetic question for the host?');
    await phone1.page.getByRole('button', {name:'Send question'}).click();
    await until(async () => (await page.locator('body').innerText()).includes('Synthetic question for the host?'), 'moderation queue');
    await page.getByRole('button', {name:'Approve question'}).click();
    await until(async () => (await guest.page.locator('#engagement-questions').innerText()).includes('Synthetic question for the host?'), 'guest approved question');
    await guest.page.locator('#engagement-questions').getByRole('button', {name:/^Support/}).click();
    await until(async () => (await guest.page.locator('#engagement-questions').innerText()).includes('1 support'), 'guest vote saved');
    assert.deepEqual(errors, []);
    assert(!serverErrors.includes('Traceback (most recent call last)'), 'server traceback during rehearsal: ' + serverErrors);
    console.log(`Engagement rehearsal passed: ${process.env.KEVENOS_TEST_BROWSER || 'chrome'}, two phone profiles, guest tablet, judge and hall.`);
  } finally {
    if (browser) await browser.close();
    if (app.exitCode === null && app.signalCode === null) {
      app.kill('SIGTERM'); await new Promise(resolve => app.once('exit', resolve));
    }
    await fs.rm(data, {recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
