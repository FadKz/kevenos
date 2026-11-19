// Drives the real pages in jsdom against the real request handler (through bridge.py): no network port needed.
//   NODE_PATH=/tmp/jsdom-test/node_modules node tests/test_ui.js
const {CookieJar, JSDOM, VirtualConsole} = require('jsdom');
const {spawn} = require('child_process');
const fs = require('fs'), path = require('path'), os = require('os');
const ROOT = path.join(__dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'kevenos-ui-'));
const py = spawn('python3', [path.join(__dirname, 'bridge.py')], {env: Object.assign({}, process.env, {KEVENOS_BRIDGE_DIR: tmp})});
let buf = '', waiters = {}, nid = 1, readyRes; const ready = new Promise(r => readyRes = r);
py.stdout.on('data', d => { buf += d; let i; while ((i = buf.indexOf('\n')) >= 0) { const l = buf.slice(0, i); buf = buf.slice(i + 1); if (!l.trim()) continue; const m = JSON.parse(l); if (m.ready) readyRes(m); else { waiters[m.id](m); delete waiters[m.id]; } } });
py.stderr.on('data', d => process.stderr.write(d));
// An unresolved readiness promise alone does not keep Node alive. A bridge
// that exits early must fail the suite instead of silently reporting success.
py.on('exit', (code, signal) => {
  console.error(`UI bridge exited unexpectedly (${signal || code}).`);
  process.exitCode = 1;
});
py.on('error', error => { console.error('UI bridge could not start:', error.message); process.exitCode = 1; });
const bridge = (method, p, body, token, extra = {}) => new Promise(res => { const id = nid++; waiters[id] = res; py.stdin.write(JSON.stringify({id, method, path: p, body, token, ...extra}) + '\n'); });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let fails = 0, errors = [];
const check = (n, c, x) => { console.log((c ? 'PASS ' : 'FAIL ') + n + (!c && x !== undefined ? ' :: ' + x : '')); if (!c) fails++; };

const HOSTED_ORIGIN = 'https://event.example.com';
async function hosted(method, pathname, body, jar, extra = {}) {
  const url = new URL(pathname, HOSTED_ORIGIN);
  const response = await bridge(method, url.pathname + url.search, body, null, {hosted: true, cookie: jar.getCookieStringSync(url.href), ...extra});
  for (const cookie of response.headers['set-cookie'] || []) jar.setCookieSync(cookie, url.href);
  return response;
}

async function load(file, urlPath, tokens, jar, fetchOverride) {
  const response = await (jar ? hosted('GET', urlPath, undefined, jar) : bridge('GET', urlPath));
  let html = Buffer.from(response.bytes, 'base64').toString('utf8');
  html = html.replace(/<script src="\/static\/(common\.js|accounts\.js|agenda\.js|participant-workspace\.js)"><\/script>/g,
    (_, script) => '<script>' + fs.readFileSync(path.join(ROOT, 'static', script), 'utf8') + '</script>');
  if (urlPath === '/organizer') html = html.replace(/<script src="\/static\/(event-operations\.js|participant-desk\.js)"><\/script>/g,
    (_, script) => '<script>' + fs.readFileSync(path.join(ROOT, 'static', script), 'utf8') + '</script>');
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => {
    if (e.type === 'not-implemented' && /navigation/.test(e.message)) return;
    errors.push(file + ': ' + (e.detail || e.message || e));
  });
  vc.on('error', e => errors.push(file + ' console.error: ' + e));
  const dom = new JSDOM(html, {url: (jar ? HOSTED_ORIGIN : 'http://pp.test') + urlPath, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      const intervals = [];
      const setInterval = w.setInterval.bind(w);
      const close = w.close.bind(w);
      let pending = 0;
      w.setInterval = (...args) => { const id = setInterval(...args); intervals.push(id); return id; };
      w.close = async () => {
        for (const id of intervals) w.clearInterval(id);
        await until(() => pending === 0);
        await sleep(0);
        close();
      };
      for (const [k, v] of Object.entries(tokens || {})) w.localStorage.setItem(k, v);
      w.fetch = async (u, o) => {
        pending++;
        try {
          const method = o?.method || 'GET';
          const body = o?.body ? JSON.parse(o.body) : undefined;
          const headers = o?.headers || {};
          const override = fetchOverride && await fetchOverride(method, u, body, headers);
          const extra = {browser: true, headers};
          const r = override || await (jar ? hosted(method, u, body, jar, extra) : bridge(method, u, body, null, extra));
          return {
            ok: r.code < 400,
            status: r.code,
            headers: {get: key => r.headers?.[key.toLowerCase()]?.[0]},
            json: async () => (typeof r.body === 'string' ? {} : r.body),
            blob: async () => new w.Blob([r.bytes ? Buffer.from(r.bytes, 'base64') : JSON.stringify(r.body)])
          };
        } finally {
          pending--;
        }
      };
      if (!w.HTMLDialogElement.prototype.showModal) w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
      if (!w.HTMLDialogElement.prototype.show) w.HTMLDialogElement.prototype.show = function () { this.open = true; };
      if (!w.HTMLDialogElement.prototype.close) w.HTMLDialogElement.prototype.close = function () {
        this.open = false;
        this.dispatchEvent(new w.Event('close'));
      };
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = () => {}; w.confirm = () => true; w.alert = () => {};
    }});
  return dom;
}
const SHOTS = process.env.KEVENOS_SHOTS;
function snap(dom, name) {   // static copy of the current DOM with the CSS inlined, for a WebKit screenshot
  if (!SHOTS) return; fs.mkdirSync(SHOTS, {recursive: true});
  let h = dom.serialize().replace(/<script[\s\S]*?<\/script>/g, '').replace('<link rel="stylesheet" href="/static/app.css">', '<style>' + fs.readFileSync(path.join(ROOT, 'static', 'app.css'), 'utf8') + '</style>');
  fs.writeFileSync(path.join(SHOTS, name + '.html'), h);
}
const $ = (dom, s) => dom.window.document.querySelector(s), $$ = (dom, s) => [...dom.window.document.querySelectorAll(s)];
const click = (dom, s) => { const e = typeof s === 'string' ? $(dom, s) : s; e.dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true})); };
const submit = (dom, selector) => $(dom, selector).requestSubmit();
async function until(fn, ms = 4000) { const t = Date.now(); while (Date.now() - t < ms) { try { if (await fn()) return true; } catch (e) {} await sleep(50); } return false; }

async function checkRoleHelp(dom, selector, title) {
  const localTrigger = $(dom, selector);
  const trigger = $(dom, '#kevenos-nav [data-kevenos-help="' + localTrigger?.id + '"]') || localTrigger;
  check(title + ' is optional', trigger && !trigger.hidden && !$(dom, '#role-help-dialog'));
  if (!trigger) return;
  trigger.focus(); click(dom, trigger);
  const dialog = $(dom, '#role-help-dialog');
  check(title + ' displays the authenticated quick start', await until(() => $(dom, '#role-help-dialog ol li')) &&
    dialog.open && $(dom, '#role-help-title').textContent === title && dialog.contains(dom.window.document.activeElement));
  if (!dialog) return;
  dialog.dispatchEvent(new dom.window.Event('cancel', {cancelable: true}));
  check(title + ' closes with focus returned', await until(() => !$(dom, '#role-help-dialog')) && dom.window.document.activeElement === trigger);
}

async function checkHostedStaff(info) {
  const adminJar = new CookieJar();
  const adminLogin = await hosted('POST', '/api/access/login', {role: 'admin', password: info.password}, adminJar);
  check('hosted Admin signs in through the access cookie', adminLogin.code === 200 && adminLogin.body.role === 'admin' && !adminLogin.body.token);
  await hosted('POST', '/api/desk/roster', {rows: [{pair: 1, partner: 'Example Company', p1: 'Alex Example', p2: 'Sam Example'}]}, adminJar);
  const admin = await load('admin.html', '/admin', {}, adminJar);
  check('hosted Admin exposes the management sidebar', await until(() => $$(admin, '#nav button').map(b => b.dataset.t).join(',') === 'overview,people,profiles,agenda,content,reports,notifications,activity,system'));
  check('Admin starts with roster, passwords, Judge access, Preview and Scoring in sequence', await until(() => $(admin, '.setup-list')) &&
    $$(admin, '.setup-list [data-jump], .setup-list a[href="/scoring"]').map(el => el.dataset.jump || 'scoring').join(',') === 'roster,credentials,access,profiles,scoring');
  await checkRoleHelp(admin, '#admin-help', 'How to set up the event');
  const deskAsAdmin = await load('desk.html', '/scoring', {}, adminJar);
  await until(() => !$(deskAsAdmin, '#app').hidden);
  await checkRoleHelp(deskAsAdmin, '#desk-help', 'How to run Judging Operations');
  await deskAsAdmin.window.close();
  snap(admin, 'admin-overview');
  click(admin, '#nav [data-t="people"]');
  await until(() => !!$(admin, '#main [data-account-index]'));
  click(admin, '#people-nav [data-person="credentials"]');
  check('Participant passwords only issues roster accounts', await until(() => !!$(admin, '#participant-form')) && !$(admin, '#guest-form,#staff-form,#staff-access-form'));

  snap(admin, 'admin-people');
  const closeIssue = async () => {
    click(admin, '#issue-done');
    return until(() => !$(admin, '#issue-dialog').open && !$(admin, '.issue-password'));
  };
  const waitIssue = count => until(() => $(admin, '#issue-dialog').open && $$(admin, '.issue-password').length === count);
  click(admin, '#nav [data-t="system"]');
  await until(() => !!$(admin, '#guest-form'));
  const newGuestPassword = 'Guest888';
  $(admin, '#guest-form [name="password"]').value = newGuestPassword;
  submit(admin, '#guest-form');
  check('guest reset shows its new password once', await waitIssue(1) && $(admin, '.issue-password').value === newGuestPassword);
  check('closing issuance removes the password fields', await closeIssue());
  const guestJar = new CookieJar();
  check('new guest password signs a guest in', (await hosted('POST', '/api/access/login', {password: newGuestPassword}, guestJar)).body.role === 'guest');
  const guest = await load('guest.html', '/event', {}, guestJar);
  await until(() => !$(guest, '#overview').hidden);
  check('Guest Today shows Live View and an optional Agenda', !!$(guest, '[data-guest-panel="today"] a[href="/live?names=0"]') &&
    $(guest, '[data-guest-panel="agenda"]').hidden);
  click(guest, '[data-guest-tab="agenda"]');
  check('Guest Agenda opens the event schedule', !$(guest, '[data-guest-panel="agenda"]').hidden && $$(guest, '#agenda li').length > 0);
  await checkRoleHelp(guest, '#guest-help', 'How to follow the event');
  await guest.window.close();
  const landing = await load('index.html', '/', {}, new CookieJar());
  check('Unified sign-in has one password field', $$(landing, '#signin input[type="password"]').length === 1 && !$(landing, '#signin select'));
  await landing.window.close();

  click(admin, '#nav [data-t="people"]');
  click(admin, '#people-nav [data-person="credentials"]');
  await until(() => !!$(admin, '#member-choice option[value="0"]'));
  $(admin, '#member-choice').value = '0';
  submit(admin, '#participant-form');
  check('one participant can be issued an individual password', await waitIssue(1) && $(admin, '#issue-rows').textContent.includes('Alex Example'));
  const participantJar = new CookieJar();
  await hosted('POST', '/api/access/login', {role: 'participant', password: $(admin, '.issue-password')?.value}, participantJar);
  const participant = await load('participant.html', '/participant', {}, participantJar);
  await until(() => !$(participant, '#guide').hidden);
  check('Participant assignment and PDFs precede the pitch breakdown', /Team\s*1/.test($(participant, '#assignment-heading').textContent) &&
    /Station\s*1/.test($(participant, '#facts').textContent) &&
    Boolean($(participant, '#materials-title').compareDocumentPosition($(participant, '#steps-title')) & participant.window.Node.DOCUMENT_POSITION_FOLLOWING));
  await checkRoleHelp(participant, '#participant-help', 'How to prepare for your pitch');
  await participant.window.close();
  await closeIssue();
  check('bulk action only offers remaining named participants', await until(() => $(admin, '#bulk-participants').textContent.endsWith('(1)')));
  click(admin, '#bulk-participants');
  check('bulk issuance returns the remaining participant', await waitIssue(1) && $(admin, '#issue-rows').textContent.includes('Sam Example'));
  await closeIssue();

  click(admin, '#people-nav [data-person="access"]');
  await until(() => !!$(admin, '#staff-access-form'));
  $(admin, '#staff-access-form [name="label"]').value = 'Example Operator';
  submit(admin, '#staff-access-form');
  check('Staff creation requires explicit roles', /Choose at least one/.test($(admin, '#staff-form-error').textContent));
  click(admin, '#staff-access-form [name="role"][value="organizer"]');
  submit(admin, '#staff-access-form');
  check('Admin can issue an Organizer password', await waitIssue(1) && $(admin, '#issue-rows').textContent.includes('Example Operator'));
  const deskPassword = $(admin, '.issue-password')?.value;
  await closeIssue();
  const deskJar = new CookieJar();
  const deskLogin = await hosted('POST', '/api/access/login', {password: deskPassword}, deskJar);
  check('issued Organizer password opens Home with Event Operations', deskLogin.code === 200 && deskLogin.body.next === '/home' && deskLogin.body.dashboards.map(view=>view.url).join(',') === '/organizer');
  const deskDom = await load('desk.html', '/organizer', {}, deskJar);
  check('hosted Organizer runs without local staff tokens or other dashboard shortcuts', await until(() => !!$(deskDom, '#desk-home')) && !$(deskDom, '#kevenos-nav-links') && !$(deskDom, '[data-t="scores"],[data-t="notifications"]') && deskDom.window.localStorage.getItem('kevenos:token:desk') === null);
  await checkRoleHelp(deskDom, '#desk-help', 'How to run Event Operations');
  check('Event Operations opens at the run-of-show overview', !!$(deskDom, '.ops-overview') &&
    $(deskDom, '.ops-overview').textContent.includes('Registration and coffee') && !!$(deskDom, '[data-home-tab="participant-help"]'));
  click(deskDom, '[data-home-tab="participant-help"]');
  check('Help inbox is directly accessible from Today', await until(() =>
    $(deskDom, '#participant-ops-help') && !$(deskDom, '#participant-ops-help').hidden &&
    $(deskDom, '#participant-ops-help').textContent.includes('Help inbox')));
  click(deskDom, '[data-participant-view="nominations"]');
  check('Participant Updates gives nominations their own area', await until(() => {
    const nominations = $(deskDom, '[data-participant-panel="nominations"]');
    return nominations && !nominations.hidden && $(deskDom, '[data-participant-panel="updates"]').hidden;
  }));
  click(deskDom, '#nav [data-t="display"]');
  check('Run of show focuses on cues and the room screen', await until(() => !!$(deskDom, '#agenda-steps')) &&
    !$(deskDom, '#announce-form') && !!$(deskDom, '#open-room-message'));
  click(deskDom, '#nav [data-t="messages"]');
  await until(() => !!$(deskDom, '#dann'));
  check('Messages brings the two announcement channels together', !!$(deskDom, '#announce-form') && !!$(deskDom, '#room-form') &&
    !!$(deskDom, '[data-participant-view="updates"]') && !!$(deskDom, '[data-participant-view="delivery"]'));
  $(deskDom, '#dann').value = 'Synthetic stage message';
  submit(deskDom, '#announce-form');
  check('hosted Desk can publish an announcement', await until(() => $(deskDom, '#display-status').textContent === 'Phone announcement sent.'));
  const deskInAdmin = await load('admin.html', '/admin', {}, deskJar);
  check('Organizer account sees the Admin access error', await until(() => !$(deskInAdmin, '#login').hidden && /dashboard is not assigned/.test($(deskInAdmin, '#err').textContent)));
  await deskInAdmin.window.close();

  const staffRow = () => $$(admin, '#staff-table tr').find(row => row.textContent.includes('Example Operator'));
  await until(() => !!staffRow());
  click(admin, staffRow().querySelector('[data-staff-action="reset"]'));
  check('Admin can reset an Organizer password and gets a new value', await waitIssue(1) && $(admin, '.issue-password').value !== deskPassword);
  const resetPassword = $(admin, '.issue-password')?.value;
  await closeIssue();
  const organizerHeaders = {headers:{'X-Dashboard':'organizer'}};
  check('reset Organizer session is rejected by the API', (await hosted('GET', '/api/desk/state', undefined, deskJar, organizerHeaders)).code === 401);
  await deskDom.window.close();
  check('reset password permits a new Organizer session', (await hosted('POST', '/api/access/login', {password: resetPassword}, deskJar)).code === 200);
  await until(() => !!staffRow());
  click(admin, staffRow().querySelector('[data-staff-action="revoke"]'));
  check('Admin can revoke the Organizer credential', await until(() => /Revoked/.test(staffRow().textContent)) && (await hosted('GET', '/api/desk/state', undefined, deskJar, organizerHeaders)).code === 401);

  click(admin, '#people-nav [data-person="access"]');
  await until(() => !!$(admin, '#staff-access-form'));
  $(admin, '#staff-access-form [name="label"]').value = 'Example Judge';
  click(admin, '#staff-access-form [name="role"][value="judge"]');
  submit(admin, '#staff-access-form');
  check('Judge role requires an assigned station', !$(admin, '#staff-access-form [name="station"]').validity.valid);
  $(admin, '#staff-access-form [name="station"]').value = '1';
  submit(admin, '#staff-access-form');
  check('Admin creates a managed Judge password', await waitIssue(1) && $(admin, '#main').textContent.includes('Example Judge'));
  const staffJar = new CookieJar();
  const staffLogin = await hosted('POST', '/api/access/login', {password: $(admin, '.issue-password')?.value}, staffJar);
  check('Judge password assigns its station dashboard', staffLogin.code === 200 && staffLogin.body.dashboards.map(view=>view.url).join(',') === '/judge/1');
  await closeIssue();

  click(admin, '#nav [data-t="activity"]');
  check('visitor activity lists navigation and credential changes', await until(() => $(admin, '#activity-table').textContent.includes('Password revoked') && $(admin, '#activity-table').textContent.includes('Opened Live View')));
  check('visitor activity shows totals and retention', $$(admin, '#activity-metrics .metric').length === 4 && $(admin, '#retention').textContent.includes('30 days'));
  $(admin, '#activity-role').value = 'guest';
  $(admin, '#activity-role').dispatchEvent(new admin.window.Event('change', {bubbles: true}));
  check('visitor activity filters by guest role', await until(() => $(admin, '#activity-table').textContent.includes('Event guests') && !$(admin, '#activity-table').textContent.includes('Password revoked')));
  snap(admin, 'admin-activity');
  check('activity never renders issued passwords', ![newGuestPassword, deskPassword, resetPassword].some(value => $(admin, '#activity-table').textContent.includes(value)));
  click(admin, '[data-profile="out"]');
  check('hosted Admin sign-out revokes the access cookie', await until(() => !$(admin, '#login').hidden) && (await hosted('GET', '/api/access/me', undefined, adminJar)).body.authenticated === false);
  await admin.window.close();
}


async function checkNotificationComposer(info) {
  const jar = new CookieJar();
  await hosted('POST', '/api/access/login', {role: 'admin', password: info.password}, jar);
  let enabled = false;
  let checks = 0;
  const history = [], requests = [], prompts = [];
  const mailEndpoint = '/api/admin/notifications';
  const mock = async (method, url, body) => {
    if (url === mailEndpoint + '/check') {
      checks++;
      return {code: 200, body: {ok: true}};
    }
    if (url !== mailEndpoint) return null;
    if (method === 'GET') return {code: 200, body: {
      sender: 'noreply@event.example.com', smtp_host: 'smtp.example.test', enabled, limit_per_day: 100,
      remaining: 100 - history.length, messages: history
    }};
    requests.push(body);
    if (requests.length === 1) throw new Error('Synthetic network disconnect');
    const uncertain = requests.length > 2;
    const message = {
      id: String(requests.length),
      recipient: body.recipient,
      subject: body.subject,
      status: uncertain ? 'uncertain' : 'sent',
      created: 1700000000,
      completed: 1700000001,
      error: uncertain ? 'Synthetic SMTP outcome unknown.' : ''
    };
    history.unshift(message);
    return {code: 200, body: {message}};
  };

  const admin = await load('admin.html', '/admin#notifications', {}, jar, mock);
  admin.window.confirm = message => { prompts.push(message); return true; };
  check('notification compose shows the fixed sender and disables unconfigured sending', await until(() => $(admin, '#mail-send').disabled && $(admin, '#main').textContent.includes('noreply@event.example.com')));
  enabled = true;
  click(admin, '#mail-refresh');
  await until(() => !$(admin, '#mail-send').disabled);
  click(admin, '#mail-check');
  check('SMTP check requests only the connection test', await until(() => $(admin, '#mail-check-status').textContent.includes('verified')) && checks === 1 && requests.length === 0);
  const fill = (subject) => {
    $(admin, '#mail-recipient').value = 'recipient@example.test';
    $(admin, '#mail-subject').value = subject;
    $(admin, '#mail-message').value = 'Example notification';
  };
  fill('Schedule update');
  submit(admin, '#mail-form');
  check('notification send confirms the recipient and allows a same-request retry after network failure', await until(() => $(admin, '#mail-send').textContent === 'Retry same request' && !$(admin, '#mail-send').disabled) && prompts[0].includes('recipient@example.test'));
  submit(admin, '#mail-form');
  check('retry uses the same idempotency key and success clears the draft', await until(() => $(admin, '#mail-send-status').textContent.includes('accepted the notification') && !$(admin, '#mail-recipient').value) && requests[0].idempotency_key === requests[1].idempotency_key);
  await until(() => $(admin, '#main table')?.textContent.includes('Schedule update'));
  fill('Room change');
  submit(admin, '#mail-form');
  check('a new draft gets a new key and unknown delivery stops resends', await until(() => $(admin, '#mail-send-status').textContent.includes('Delivery is unknown') && $(admin, '#mail-send').disabled) && requests[2].idempotency_key !== requests[1].idempotency_key);
  check('notification history displays the uncertain status', await until(() => $(admin, '#main table').textContent.includes('Delivery unknown')) && requests.length === 3);
  await admin.window.close();
}

async function checkFinalDisplay(desk, judge) {
  await bridge('POST', '/api/desk/reveal', {mode: 'top3'}, desk);
  await bridge('POST', '/api/desk/program', {action: 'scene', scene: 'auto'}, desk);
  await bridge('POST', '/api/desk/clock', {action: 'finish'}, desk);
  const prepared = await bridge('POST', '/api/desk/final', {action: 'set', pairs: [1, 3, 2], panel: [3]}, desk);
  check('Final setup accepts three finalists and an explicit panel', prepared.code === 200, JSON.stringify(prepared.body));
  const operator = await load('desk.html', '/desk#final', {'kevenos:token:desk': desk});
  check('Desk explains original scenarios and the paused eight-minute clock', await until(() => !!$(operator, '#fgo')) &&
    $(operator, '#main').textContent.includes('original customer scenario') && $(operator, '#main').textContent.includes('8:00'));
  click(operator, '#fgo');
  if (await until(() => $(operator, '#modal').open || $(operator, '#htt').textContent === '8:00')) {
    if ($(operator, '#prelim-reason')) {
      $(operator, '#prelim-reason').value = 'Synthetic fixture runs the Final with outstanding rounds.';
      submit(operator, '#prelim-exception');
    }
  }
  check('first finalist starts ready with a paused 8:00 clock', await until(() => $(operator, '#htt').textContent === '8:00' && !$(operator, '#cstart').hidden && $(operator, '#cpause').hidden));
  check('Desk cannot advance before the configured panel receipt', $(operator, '#fnext').disabled && $(operator, '#main').textContent.includes('0 / 1'));
  const [station, live, board] = await Promise.all([
    load('judge.html', '/judge/3', {'kevenos:token:judge:3': judge}), load('live.html', '/live'), load('board.html', '/board')
  ]);
  check('Judge, Live and board show the same ready finalist clock', await until(() => [$(station, '#tt'), $(live, '#stage #cd .t'), $(board, '#wrap .time')].every(node=>node?.textContent==='8:00')));
  const initial = (await bridge('GET','/api/state',null,judge)).body, original = initial.scenario_snapshot || (await bridge('GET','/api/content',null,judge)).body.scenarios.find(s=>s.id===initial.scenario);
  check('every view names the finalist original scenario', [$(station, '#who'),$(live, '#stage'),$(board, '#wrap')].every(node=>node.textContent.includes(original.name)));
  check('Board prioritizes the Final over preliminary qualifier podium', $(board,'#wrap').textContent.includes('On stage: Team 1') && !$(board,'#wrap .pod'));
  click(station, '#bcustomer');
  check('the Final judge customer panel uses the original case', $(station, '#lbody').textContent.includes(original.name) && !$(station, '#lbody').textContent.includes('Saffron Table Group'));
  click(station, '#sx'); click(operator, '#cstart');
  check('Desk starts one shared clock for every display', await until(() => [$(operator,'#htt'),$(station,'#tt'),$(live,'#stage #cd .t'),$(board,'#wrap .time')].every(node=>/^7:5[0-9]$/.test(node?.textContent)),6000));
  await bridge('POST','/api/desk/clock',{action:'finish'},desk);
  check('time up asks the audience to wait for scoring', await until(()=>$(live,'#stage').textContent.includes('Judges are scoring')));
  click(station, '#judge-home-open');
  const submitBallot = async (rating) => {
    await until(()=>!!$(station,'[data-rating="'+rating+'"]'));
    click(station, '#bnote');
    $(station,'#notes').value='Observed discovery';
    $(station,'#notes').dispatchEvent(new station.window.Event('input',{bubbles:true}));
    click(station, '#sx');
    click(station, '[data-rating="'+rating+'"]');
    click(station, '#bsub2');
  };
  await submitBallot(3);
  check('Final sheet is accepted with run identity and shows waiting guidance', await until(()=>$(station,'#subbed').textContent.includes('Accepted by the Desk') && $(station,'#subbed').textContent.includes('Wait for the Desk')));
  check('the accepted panel receipt enables handover', await until(()=>!$(operator,'#fnext').disabled));
  click(operator,'#fnext');
  check('next finalist resets paused and changes the original scenario', await until(()=>$(station,'#who').textContent.includes('Finalist 2 of 3') && $(station,'#who').textContent.includes('Team 3') && $(station,'#tt').textContent==='8:00'));
  const next = (await bridge('GET','/api/state',null,judge)).body;
  check('different finalist uses a different stored original case', next.scenario !== initial.scenario);
  station.window.eval("showFinalRecovery()"); $(station,'#recover-team').value='1'; click(station,'#recover-open');
  check('earlier accepted sheet recovers without moving the stage', await until(()=>$(station,'#cue').textContent.includes('stage remains on Team 3') && $(station,'#subbed').textContent.includes('Accepted by the Desk')) && (await bridge('GET','/api/desk/state',null,desk)).body.final.stage_pair===3);
  station.window.eval("showFinalRecovery()");click(station,'#recover-stage');
  await until(()=>$(station,'#who').textContent.includes('Team 3') && !$(station,'#cue').textContent.includes('Reviewing'));
  await submitBallot(4);
  check('the second accepted ballot enables handover', await until(()=>$(operator,'#fnext') && !$(operator,'#fnext').disabled));
  click(operator,'#fnext');
  check('the third finalist resets the stage clock to eight minutes', await until(()=>$(station,'#who').textContent.includes('Finalist 3 of 3') && $(station,'#who').textContent.includes('Team 2') && $(station,'#tt').textContent==='8:00'));
  await submitBallot(5);
  check('last finalist receipt enables review',await until(()=>!$(operator,'#fstop').disabled));click(operator,'#fstop');
  check('review parks both audience views while keeping judge correction available',await until(()=>$(live,'#stage')?.textContent.includes('confirming results') && $(board,'#wrap').textContent.includes('confirming results') && $(operator,'#fpublish') && !$(operator,'#fpublish').disabled));
  click(operator,'#fpublish');
  check('publication shows matching winners and parks judges',await until(()=>$(live,'#right').textContent.includes('Congratulations') && $(board,'#wrap').textContent.includes('Congratulations') && $(station,'#view').textContent.includes('Judging complete') && $(station,'#bar').hidden));
  await Promise.all([station,live,board,operator].map(dom=>dom.window.close()));
}

async function checkJudgeReferences(desk, judge) {
  let helpAttempts = 0, pdfAttempts = 0, helpToken, pdfToken;
  const dom = await load('judge.html', '/judge/1', {'kevenos:token:judge:1': judge}, null, (method, url, body, headers) => {
    if (url === '/api/help') {
      helpToken = headers['X-Token'];
      if (++helpAttempts === 1) return {code: 503, body: {error: 'Synthetic guide outage'}};
    }
    if (url === '/api/resources/judge-app-guide') {
      pdfToken = headers['X-Token'];
      if (++pdfAttempts === 1) return {code: 503, body: {error: 'Synthetic PDF outage'}};
    }
    return null;
  });
  try {
    const panel = () => $(dom, '#sheet');
    const guide = () => $(dom, '#bhelp');
    const customer = () => $(dom, '#bcustomer');
    await until(() => !!$(dom, '#steps [data-s="0"]'));
    click(dom, '#judge-home-open');
    await until(() => !$(dom, '#judge-workspace').hidden);
    check('judge references are visible and closed before the pitch',
      guide()?.textContent === 'Stage guide' && customer()?.textContent === 'Customer scenario' &&
      panel().hidden && !panel().open && [guide(), customer()].every(b => b.getAttribute('aria-expanded') === 'false'));
    const content = (await bridge('GET', '/api/content', null, judge)).body;
    const scenario = content.scenarios.find(s => s.stations.includes(1));
    const activeStage = () => $(dom, '#steps [aria-pressed="true"]').dataset.s;
    const snapshot = () => JSON.stringify({score: $(dom, '#bsc').textContent, stage: activeStage(), storage: dom.window.localStorage});
    const appHelp = $(dom, '#judge-help');
    check('How to judge is available before the clock starts and help is opt-in', appHelp?.textContent === 'How to judge' &&
      !appHelp.hidden && !panel().open && helpAttempts === 0);
    if (appHelp) {
      const before = snapshot();
      appHelp.focus(); click(dom, appHelp);
      check('judge guide has a retry when help is unavailable', await until(() => $(dom, '#judge-materials button[data-help-retry]')) && helpToken === judge);
      click(dom, '#judge-materials button[data-help-retry]');
      check('judge help opens without requiring packaged event PDFs',
        panel().open && $(dom, '#helper-title').textContent === 'How to judge');
      $(dom, '#sx').dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      check('Escape closes app help, restores focus and preserves the draft', !panel().open && dom.window.document.activeElement === appHelp && snapshot() === before);
    }
    const stages = ['Discover', 'Solution at Work', 'Solve', 'Close', 'Wrap up'];
    const stageContent = [
      () => $(dom, '#qf') && panel().textContent.includes(content.rubric[0].ticks[0].counts),
      () => panel().textContent.includes(scenario.model.maw) && !$(dom, '#sheet details:not([open])'),
      () => $$(dom, '#sheet [data-raise]').length === 2,
      () => panel().textContent.includes(scenario.model.close) && !$(dom, '#sheet details:not([open])'),
      () => panel().textContent.includes('Submit the sheet')
    ];
    for (const [index, stage] of stages.entries()) {
      click(dom, '#steps [data-s="' + index + '"]');
      const before = snapshot();
      click(dom, guide());
      check('Stage guide opens directly to ' + stage, panel().open && guide().getAttribute('aria-expanded') === 'true' &&
        $(dom, '#helper-title').textContent === 'Stage guide' && $(dom, '#guide-stage')?.textContent === stage && stageContent[index]());
      click(dom, '#sx');
      click(dom, customer());
      check('Customer scenario opens directly during ' + stage, panel().open && customer().getAttribute('aria-expanded') === 'true' &&
        $(dom, '#helper-title').textContent === 'Customer scenario' && [scenario.name, scenario.line, scenario.card[0], scenario.persona, content.judge_rules[0]].every(t => panel().textContent.includes(t)) && !$(dom, '#guide-stage'));
      click(dom, '#sx');
      check('opening and closing references leaves ' + stage + ' scoring and storage unchanged', snapshot() === before);
    }
    const active = () => dom.window.document.activeElement;
    const press = (key, options = {}) => active().dispatchEvent(new dom.window.KeyboardEvent('keydown', {key, bubbles: true, cancelable: true, ...options}));
    const resize = width => { dom.window.innerWidth = width; dom.window.dispatchEvent(new dom.window.Event('resize')); };
    const inert = () => $(dom, '#app').hasAttribute('inert');
    const docked = () => panel().classList.contains('docked');
    resize(1099);
    customer().focus(); click(dom, customer());
    check('the narrow customer sheet opens as an inert modal with focus inside', panel().open && !docked() && inert() && panel().getAttribute('aria-modal') === 'true' && active() === $(dom, '#sx'));
    press('Tab', {shiftKey: true});
    check('Shift+Tab wraps from the narrow sheet Close button', active() === $(dom, '#reference-switch'));
    press('Tab');
    check('Tab wraps back to the narrow sheet Close button', active() === $(dom, '#sx'));
    if ($(dom, '#reference-switch')) click(dom, '#reference-switch');
    check('the narrow sheet switches directly from Customer scenario to Stage guide', panel().open && inert() && guide().getAttribute('aria-expanded') === 'true' && active() === $(dom, '#sx'));
    press('Escape');
    check('Escape closes the narrow sheet and restores its current trigger', !panel().open && !inert() && active() === guide());
    if (panel().open) click(dom, '#sx');

    resize(1100);
    click(dom, '#steps [data-s="0"]');
    click(dom, guide());
    check('the wide reference docks without disabling the rubric', panel().open && docked() && !inert() && !panel().hasAttribute('aria-modal'));
    click(dom, '.tick[data-i="0"]');
    check('a rubric tick can be scored with the reference docked', panel().open && $(dom, '.tick[data-i="0"]').getAttribute('aria-pressed') === 'true');
    click(dom, '.tick[data-i="0"]');
    click(dom, customer());
    check('the docked reference switches directly to Customer scenario', panel().open && docked() && customer().getAttribute('aria-expanded') === 'true' && guide().getAttribute('aria-expanded') === 'false');
    click(dom, '#steps [data-s="3"]');
    check('changing rubric stages keeps the docked customer visible', panel().open && panel().textContent.includes(scenario.name) && !$(dom, '#guide-stage'));
    click(dom, guide());
    check('switching back opens the selected stage guide', panel().open && $(dom, '#guide-stage')?.textContent === 'Close');
    click(dom, '#steps [data-s="1"]');
    check('a docked guide updates with manual stage changes', panel().open && $(dom, '#guide-stage')?.textContent === 'Solution at Work');
    resize(1099);
    check('resizing to narrow converts the open dock into an inert sheet', panel().open && !docked() && inert() && active() === $(dom, '#sx'));
    resize(1100);
    check('resizing to wide restores the open modeless reference', panel().open && docked() && !inert() && $(dom, '#guide-stage')?.textContent === 'Solution at Work' && active() === $(dom, '#sx'));
    click(dom, guide());
    check('clicking the open reference control closes it', !panel().open && !inert() && guide().getAttribute('aria-expanded') === 'false');

    click(dom, '#steps [data-s="0"]');
    click(dom, guide());
    const filter = $(dom, '#qf');
    filter.focus(); filter.value = 'laptop';
    filter.dispatchEvent(new dom.window.Event('input', {bubbles: true}));
    await bridge('POST', '/api/desk/clock', {action: 'seek', elapsed: 130}, desk);
    check('the clock advances while the focused question filter keeps its text', await until(() => $(dom, '#cue').textContent.includes('Solution at Work')) &&
      activeStage() === '0' && active() === filter && filter.value === 'laptop');
    $(dom, '#sx').focus();
    check('the docked stage guide follows the timed stage after the filter loses focus', await until(() => activeStage() === '1' && $(dom, '#guide-stage')?.textContent === 'Solution at Work') &&
      panel().open && !inert());
    click(dom, customer());
    await bridge('POST', '/api/desk/clock', {action: 'seek', elapsed: 310}, desk);
    check('a timed stage change leaves the docked customer open', await until(() => activeStage() === '2') && panel().open &&
      panel().textContent.includes(scenario.name) && customer().getAttribute('aria-expanded') === 'true');
    click(dom, customer());
    await bridge('POST', '/api/desk/clock', {action: 'seek', elapsed: 490}, desk);
    check('the clock still follows stages while both references remain closed', await until(() => activeStage() === '3') && !panel().open);

    click(dom, customer());
    click(dom, '#judge-help');
    check('How to judge opens as a modal even with a reference docked', panel().open && !docked() && inert() && $(dom, '#helper-title').textContent === 'How to judge');
    check('judge materials omit event-specific downloadable packs',
      $$(dom, '[data-help-resource]').length === 0);
    press('Escape');
    check('closing help restores focus to the local guide control', active() === $(dom, '#judge-help'));

    click(dom, customer());
    await bridge('POST', '/api/desk/calib', {active: true}, desk);
    check('a new assignment closes the previous customer reference', await until(() => $(dom, '#who').textContent.includes('Calibration')) && !panel().open &&
      !inert() && customer().getAttribute('aria-expanded') === 'false');
    click(dom, customer());
    check('calibration customer view opens to the mock pitch', panel().open && panel().textContent.includes(content.mock.intro) &&
      panel().textContent.includes(content.mock.lines[0].line) && !panel().textContent.includes(scenario.name));
    click(dom, guide());
    check('calibration stage guide shows rubric criteria without the customer transcript', panel().open &&
      panel().textContent.includes(content.rubric[activeStage()].ticks[0].counts) && !panel().textContent.includes(content.mock.intro));
  } finally {
    await dom.window.close();
    await bridge('POST', '/api/desk/calib', {active: false}, desk);
    await bridge('POST', '/api/desk/clock', {action: 'reset'}, desk);
  }
}

async function checkWorkspace(info) {
  const adminJar = new CookieJar();
  await hosted('POST', '/api/access/login', {role: 'admin', password: info.password}, adminJar);
  const admin = await load('admin.html', '/admin', {}, adminJar);
  check('Overview reports registered people and a manual backup from live data', await until(() =>
    $(admin, '.setup-list')?.textContent.includes('Registered teams') && $(admin, '.overview-tools')?.textContent.includes('Manual download') &&
    !!$(admin, '.overview-tools [data-export="backup"]')));
  click(admin, '#nav [data-t="system"]');
  check('System explains isolated Event Data and exposes rehearsal enable', await until(() => $(admin, '[data-workspace="rehearsal"]')) &&
    $(admin, '#main').textContent.includes('Reopen Live View and Board links'));
  click(admin, '[data-workspace="rehearsal"]');
  const confirmText = $(admin, '#workspace-confirm');
  check('workspace change opens a dialog with focused text confirmation', $(admin, '#workspace-dialog').open && $(admin, '#workspace-apply').disabled &&
    admin.window.document.activeElement === confirmText);
  confirmText.value = 'REHEARSAL';
  confirmText.dispatchEvent(new admin.window.Event('input', {bubbles: true}));
  click(admin, '#workspace-apply');
  check('explicit rehearsal confirmation activates sample data', await until(async () => (await hosted('GET', '/api/admin/workspace', undefined, adminJar)).body.mode === 'rehearsal'));
  check('a changed context reloads before applying new data to the old page', await until(() => admin.window.eval('KEV.workspaceChanging')) &&
    $(admin, '#workspace-badge').hidden && admin.window.eval('KEV.workspaceMode') === 'live');
  await admin.window.close();

  const rehearsal = await load('admin.html', '/admin', {}, adminJar);
  check('Rehearsal overview shows 30 registered pairs, 8 sample sheets and zero signed-in stations', await until(() => {
    const list = $(rehearsal, '.readiness-list');
    return list?.textContent.includes('30 / 30') && list.textContent.includes('0 / 8') &&
      $$(rehearsal, '.stat-grid .metric')[0]?.textContent === '8';
  }));
  click(rehearsal, '#nav [data-t="people"]');
  click(rehearsal, '#people-nav [data-person="credentials"]');
  check('rehearsal keeps participant issuance roster-only', await until(() =>
    !!$(rehearsal, '#participant-form') && !$(rehearsal, '#guest-form') && !$(rehearsal, '#staff-form')));
  click(rehearsal, '#people-nav [data-person="access"]');
  await until(() => !!$(rehearsal, '#staff-access-form'));
  const staffForm = $(rehearsal, '#staff-access-form');
  staffForm.elements.label.value = 'Rehearsal multi-role staff';
  click(rehearsal, '#staff-access-form [name="role"][value="judge"]');
  staffForm.elements.station.value = '2';
  click(rehearsal, '#staff-access-form [name="role"][value="registration"]');
  check('Rehearsal shared staff roles require explicit confirmation', !$(rehearsal, '#staff-shared-confirm').hidden && !staffForm.elements.confirm_shared_roles.validity.valid);
  staffForm.elements.confirm_shared_roles.click();
  submit(rehearsal, '#staff-access-form');
  check('Staff issuance shows every assigned role', await until(() => $(rehearsal, '#issue-dialog').open) && /Judge/.test($(rehearsal, '#issue-rows').textContent) && /Registration/.test($(rehearsal, '#issue-rows').textContent));
  click(rehearsal, '#issue-done');
  const sharedStaff = $$(rehearsal, '#staff-table tr').find(row=>row.textContent.includes('Rehearsal multi-role staff'));
  check('Shared staff password mutations are hidden in Rehearsal', !!sharedStaff && !sharedStaff.querySelector('[data-staff-action="reset"],[data-staff-action="revoke"]'));
  click(rehearsal, '#nav [data-t="notifications"]');
  check('rehearsal explains why email is disabled', await until(() => /Email is disabled in rehearsal/.test($(rehearsal, '#main').textContent)) && !$(rehearsal, '#mail-form'));
  click(rehearsal, '#nav [data-t="system"]');
  await until(() => !!$(rehearsal, '[data-workspace="reset-rehearsal"]'));
  click(rehearsal, '[data-workspace="reset-rehearsal"]');
  check('rehearsal reset asks for its distinct phrase', $(rehearsal, '#workspace-dialog').open && $(rehearsal, '#workspace-phrase').textContent === 'RESET REHEARSAL');
  click(rehearsal, '#workspace-cancel');
  const [live, board, judge] = await Promise.all([
    load('live.html', '/live?embed=1&hdr=0', {}, adminJar),
    load('board.html', '/board', {}, adminJar),
    load('judge.html', '/judge/1', {}, adminJar)
  ]);
  check('rehearsal is labeled on Admin, embedded Live View, Board and judge sign-in', [rehearsal, live, board, judge]
    .every(dom => $(dom, '#kevenos-nav #workspace-badge')?.textContent === 'Rehearsal' && !$(dom, '#workspace-badge').hidden && $(dom, '#workspace-badge').getAttribute('aria-label') === 'Rehearsal — sample data'));
  await Promise.all([rehearsal, live, board, judge].map(dom => dom.window.close()));
}

(async () => {
  const info = await ready;
  const R = async (m, p, b, t) => (await bridge(m, p, b, t));
  const desk = (await R('POST', '/api/login', {role: 'desk', pin: info.desk_pin})).body.token;
  const jt = {}; for (let s = 1; s <= 8; s++) jt[s] = (await R('POST', '/api/login', {role: 'judge', station: s, name: 'Judge ' + s})).body.token;
  const csv = Array.from({length: 30}, (_, i) => `${i + 1}, Partner ${String.fromCharCode(65 + Math.floor(i / 2))}, Aya ${i + 1}, Customer contact ${i + 1}, ${i % 3 === 0 ? 1 : 0}, 0`).join('\n');
  await R('POST', '/api/desk/roster', {csv}, desk);
  await checkJudgeReferences(desk, jt[1]);
  if (process.env.KEVENOS_JUDGE_REFERENCES_ONLY) {
    check('no judge reference script errors', errors.length === 0, errors.join(' || '));
    py.kill(); process.exit(fails ? 1 : 0);
  }
  await R('POST', '/api/desk/clock', {action: 'start'}, desk); await R('POST', '/api/desk/clock', {action: 'seek', elapsed: 130}, desk);

  // ── index
  let d = await load('index.html', '/');
  check('local landing navigation links to each workspace and a names-safe audience screen', await until(() => ['/admin', '/desk', '/judge', '/live?names=0'].every(h => !!$(d, '#kevenos-nav a[href="' + h + '"]'))));
  await d.window.close();

  // ── judge page: station list
  const jl = await load('judges.html', '/judge', {'kevenos:token:judge:2': jt[2]});
  check('judge page lists 8 stations', await until(() => $$(jl, '.st').length === 8), $$(jl, '.st').length);
  check('your own station is marked', $$(jl, '.st.mine').length === 1 && $(jl, '.st.mine b').textContent === '2');
  click(jl, $$(jl, '.st')[4]); check('choosing a free station asks for a name', !$(jl, '#m').hidden && $(jl, '#mt').textContent === 'Station 5');
  click(jl, '#go'); $(jl, '#nm').value = ''; click(jl, '#go'); check('a name is required', await until(() => $(jl, '#err').textContent.includes('name')));
  await jl.window.close();

  // ── judge page: login screen
  d = await load('judge.html', '/judge/1');
  check('judge page asks for a name when not signed in', await until(() => !$(d, '#login').hidden));
  $(d, '#lname').value = ''; click(d, '#lgo');
  check('a name is required', await until(() => $(d, '#lerr').textContent.includes('name')));
  $(d, '#lname').value = 'Layla'; click(d, '#lgo');
  check('a name opens the judge screen', await until(() => !$(d, '#app').hidden && $$(d, '.tick[data-i]').length > 0), $(d, '#lerr').textContent);
  check('judges arrive on Today with shortcuts inactive', !$(d, '#judge-home').hidden && $(d, '#judge-workspace').hidden);
  click(d, '#judge-home-open');
  check('header shows pair 1, the members and the badge', $(d, '#who').textContent.includes('Team 1') && $(d, '#who').textContent.includes('Aya 1') && $(d, '#who').textContent.includes('badge +2'), $(d, '#who').textContent);
  check('step strip: four steps plus wrap up', $$(d, '#steps button').length === 5);
  check('screen follows the clock (2:10 = Solution at Work)', $(d, '#steps button.sel').textContent.includes('Solution at Work') && $$(d, '.tick[data-i]').length === 2, $(d, '#steps button.sel').textContent);
  check('cue names the current step', $(d, '#cue').textContent.includes('Solution at Work'), $(d, '#cue').textContent);
  check('timer shows time left in the pitch', /^\d+:\d\d$/.test($(d, '#tt').textContent), $(d, '#tt').textContent);
  check('ticks show a short label only', $(d, '.tick .lab').textContent.length < 90 && !$(d, '.counts:not([hidden])'));
  click(d, '.info'); check('the i button explains what counts', !!$(d, '.counts:not([hidden])') && $(d, '.counts').textContent.includes('Counts when'));
  click(d, '.info');

  check('rubric exposes an accessible pressed state and separate info button', $(d, '.tick').getAttribute('aria-pressed') === 'false' && $(d, '.info').tagName === 'BUTTON' && !$(d, '.tick .info'));
  const focusedTick = $(d, '.tick'); focusedTick.focus();
  await sleep(2200);
  check('judge polling preserves focus', d.window.document.activeElement === focusedTick);

  // ticks and the score bar (Discover step)
  click(d, '#steps button[data-s="0"]');
  check('Discover shows its 3 ticks', $$(d, '.tick[data-i]').length === 3);
  click(d, '.tick[data-i="0"]'); click(d, '.tick[data-i="2"]');
  check('two ticks = 20 points + badge 2', $(d, '#bsc').textContent === '22 pts', $(d, '#bsc').textContent);
  check('step chip counts 2/3', $(d, '#steps button[data-s="0"]').textContent.includes('2/3'));
  click(d, '.tick[data-i="2"]'); check('tapping again unticks', $$(d, '.tick.on[data-i]').length === 1);
  d.window.document.dispatchEvent(new d.window.KeyboardEvent('keydown', {key: '2', bubbles: true}));
  check('keyboard 2 ticks item 2', $$(d, '.tick.on[data-i]').length === 2);
  click(d, '.tick[data-i="0"]'); click(d, '.tick[data-i="1"]'); // clear

  // Discover question logging
  $(d, '#bhelp').focus(); click(d, '#bhelp'); check('the Discover guide opens on Questions', await until(() => !$(d, '#sheet').hidden && $$(d, '#ql .q').length === 24), $$(d, '#ql .q').length);
  check('the narrow stage guide is modal and makes the background inert', $(d, '#sheet').open && $(d, '#app').hasAttribute('inert') && $(d, '#sheet').contains(d.window.document.activeElement));
  const scoreBeforeHelper = $(d, '#bsc').textContent;
  d.window.document.dispatchEvent(new d.window.KeyboardEvent('keydown', {key: '1', bubbles: true}));
  check('rubric keyboard shortcuts are suspended inside a helper', $(d, '#bsc').textContent === scoreBeforeHelper);
  click(d, '#reference-switch'); check('the customer switch shows the assigned scenario', $(d, '#lbody').textContent.includes('Scenario A'));
  click(d, '#reference-switch');
  const question = $(d, '#ql .q[data-q="Q1"]');
  check('question logging is a keyboard-operable toggle', question.tagName === 'BUTTON' && question.getAttribute('aria-pressed') === 'false');
  question.focus(); click(d, question);
  check('question logging updates its pressed state and retains focus', $(d, '#ql .q[data-q="Q1"]').getAttribute('aria-pressed') === 'true' && d.window.document.activeElement === $(d, '#ql .q[data-q="Q1"]'));
  check('asking Q1 reveals the synthetic answer', $(d, '#ql .q[data-q="Q1"] .rev').textContent.includes('Synthetic answer for Q1'), $(d, '#ql .q[data-q="Q1"]').textContent);
  click(d, '#ql .q[data-q="Q6"]'); click(d, '#ql .q[data-q="Q7"]');
  check('Q6 and Q7 do not suggest the why-now criterion', !$(d, '.tick.sug[data-i="1"]'));
  click(d, '#ql .q[data-q="Q6"]'); click(d, '#ql .q[data-q="Q7"]');
  click(d, '#ql .q[data-q="Q3"]');
  check('Q1 and Q3 suggest why-now only', !$(d, '.tick.sug[data-i="0"]') && !!$(d, '.tick.sug[data-i="1"]'));
  click(d, '#ql .q[data-q="Q3"]'); click(d, '#ql .q[data-q="Q13"]');
  check('Q13 also suggests why-now', !!$(d, '.tick.sug[data-i="1"]'));
  click(d, '#ql .q[data-q="Q13"]'); click(d, '#ql .q[data-q="Q2"]');
  check('Q1 and Q2 suggest the people criterion without Q3', !!$(d, '.tick.sug[data-i="0"]') && !$(d, '.tick.sug[data-i="1"]'));
  click(d, '#ql .q[data-q="Q3"]');
  check('question logging updates the starred count in the Discover guide', $(d, '#lbody .pill').textContent.includes('★ 3 of'));
  click(d, '#sx'); check('sheet closes and restores focus', !$(d, '#sheet').open && !$(d, '#app').hasAttribute('inert') && d.window.document.activeElement === $(d, '#bhelp'));
  check('Q1 and Q2 suggest the people criterion', !!$(d, '.tick.sug[data-i="0"]') && !!$(d, '#bacc'));
  click(d, '#bacc'); check('accept suggestions ticks them', $$(d, '.tick.on[data-i]').length >= 1 && !$(d, '#bacc')); snap(d, 'judge-questions');

  // worries during Solve
  click(d, '#steps button[data-s="2"]'); click(d, '#bhelp');
  check('the Solve guide opens on Worries', await until(() => $$(d, '.wcard').length === 2));
  click(d, '[data-raise="IT"]');
  check('raised worry shows three checks', await until(() => $$(d, '[data-w="IT"]').length === 3));
  $$(d, '[data-w="IT"]').slice(0, 2).forEach(c => { c.checked = true; c.dispatchEvent(new d.window.Event('change', {bubbles: true})); });
  click(d, '#sx');
  check('understood + solution device answer suggests Solve tick 1', $$(d, '.tick.sug:not(.on)').length === 1 && $(d, '.tick.sug:not(.on)').dataset.i === '5');
  click(d, '#bacc'); snap(d, 'judge-worries');

  // notes sheet
  click(d, '#bnote'); click(d, '#nchips button'); check('step tag inserted in notes', $(d, '#notes').value.startsWith('[Discover '), $(d, '#notes').value);
  click(d, '#sx');

  // wrap up: AI tick, both spoke, feedback
  click(d, '#bsub2'); check('Wrap up opens from the bar', await until(() => !!$(d, '#fb')) && $(d, '#bsub2').textContent === 'Submit sheet');
  check('Wrap up uses the single labeled Notes control in the bar', $(d, '#bnote').textContent === 'Notes' && !$(d, '#wnote'));
  check('wrap-up toggles have native buttons and exposed state', ['#aitick', '#spoke'].every(s => $(d, s).tagName === 'BUTTON' && $(d, s).getAttribute('aria-pressed') === 'false'));
  click(d, '#aitick'); click(d, '#spoke');
  check('wrap-up toggles update their pressed state', ['#aitick', '#spoke'].every(s => $(d, s).getAttribute('aria-pressed') === 'true'));
  const nOn = $$(d, '.mini button.on').length, expTotal = nOn * 10 + 2 + 2;
  check('score = ticks x10 + AI 2 + badge 2', $(d, '#bsc').textContent === expTotal + ' pts' && nOn >= 2, $(d, '#bsc').textContent + ' vs ' + expTotal);
  click(d, '.mini button[data-m="9"]'); click(d, '.mini button[data-m="9"]');
  check('wrap-up ticks can be corrected', $$(d, '.mini button.on').length === nOn);

  click(d, '#fbd');
  check('feedback falls back to a usable template', await until(() => $(d, '#fb').value.length > 30 && $(d, '#fbs').textContent.includes('template')));
  click(d, '#bsub2');
  check('submitting without a rating prompts for a 1 to 5 star score', $(d, '#modal').open && $(d, '#mbody').textContent.includes('Choose an overall impression and confidence rating'));
  click(d, '#mno');
  click(d, '[data-rating="4"]');
  check('the selected tiebreak rating is accessible', $(d, '[data-rating="4"]').getAttribute('aria-checked') === 'true');
  click(d, '#bsub2');
  check('submit goes through and shows the banner', await until(() => !$(d, '#subbed').hidden), $(d, '#mbody').textContent);
  snap(d, 'judge-submitted');
  let sc = (await R('GET', '/api/desk/scores', null, desk)).body.leaderboard;
  check('desk leaderboard has the sheet with the same total', sc.length === 1 && sc[0].total === expTotal && sc[0].pair === 1, JSON.stringify(sc) + ' vs ' + expTotal);
  const sheets = (await R('GET', '/api/desk/sheets', null, desk)).body.sheets;
  check('asked questions, notes and the tiebreak rating were saved', sheets[0]?.asked.length === 3 && sheets[0].notes.length > 0 && sheets[0].spoke === 1 && sheets[0].wrap_rating === 4);

  // nudge dialog: tick everything, no feedback -> modal
  d.window.localStorage.clear();
  await d.window.close();

  // second judge: offline retry
  let submitResult = 'offline';
  const d2 = await load('judge.html', '/judge/2', {'kevenos:token:judge:2': jt[2], 'kevenos:judgeName': 'Sami'}, null, (method, url) => {
    if (method !== 'POST' || url !== '/api/submit') return null;
    if (submitResult === 'offline') throw new Error('Synthetic network outage');
    return {code: 409, body: {error: 'The round moved on.'}};
  });
  check('station 2 opens with a stored sign-in', await until(() => $$(d2, '.tick[data-i]').length > 0));
  click(d2, '#judge-home-open');
  click(d2, '#bsub2'); await until(() => !!$(d2, '.mini'));
  click(d2, '[data-rating="3"]');
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9].forEach(i => click(d2, '.mini button[data-m="' + i + '"]'));
  click(d2, '#bsub2'); check('nudge dialog warns about all ten ticks and the empty feedback', await until(() => !$(d2, '#modal').hidden) && $(d2, '#mbody').textContent.includes('All ten'), $(d2, '#mbody').textContent);
  click(d2, '#mno'); check('"go back" closes the dialog', $(d2, '#modal').hidden);
  click(d2, '#bsub2'); click(d2, '#mgo');
  check('offline send shows Pending on this device, without a submission receipt', await until(() => /Pending on this device/.test($(d2, '#subbed').textContent)) && !/Accepted|Submitted/.test($(d2, '#subbed').textContent));
  submitResult = 'conflict';
  check('rejected queued sheet is retained for Desk review', await until(() => /Needs Desk review/.test($(d2, '#subbed').textContent), 4500) && !!$(d2, '[data-sheet-download]'));
  await d2.window.close();

  // ── desk
  const dk = await load('desk.html', '/desk#run', {'kevenos:token:desk': desk});
  check('desk opens with the stored sign-in', await until(() => !$(dk, '#app').hidden && $$(dk, '.grid .st').length === 8), $$(dk, '.grid .st').length);
  snap(dk, 'desk-run');
  check('desk shows the round and links back to Admin', $(dk, '#hr').textContent.includes('Round 1') && !!$(dk, '#kevenos-nav-links a[href="/admin"]'));
  check('desk lists operational sections and omits management', $$(dk, '#nav button').map(b => b.dataset.t).join(',') === 'home,run,scores,quiz,audience,participants,display,final,calib');
  check('station 1 card shows submitted', $$(dk, '.grid .st')[0].textContent.includes('Submitted'), $$(dk, '.grid .st')[0].textContent);
  for (const [t, needle] of [['scores', 'Leaderboard'], ['final', 'Final control'], ['calib', 'Calibration']]) {
    click(dk, '#nav button[data-t="' + t + '"]');
    check('desk tab renders: ' + t, await until(() => $(dk, '#main').textContent.includes(needle), 5000), $(dk, '#main').textContent.slice(0, 120));
  }
  click(dk, '#nav button[data-t="scores"]');
  check('scores table has pair 1 with its total', await until(() => $(dk, '#main table') && $(dk, '#main table').textContent.includes(String(expTotal)))); snap(dk, 'desk-scores');
  click(dk, '#nav button[data-t="display"]');
  check('live display controls are available', await until(() => !!$(dk, '[data-scene="prep"]')));
  click(dk, '[data-rev="top3"]');
  check('reveal state saved', await until(async () => $(dk, '#display-status').textContent === 'Display updated.' && (await R('GET', '/api/board')).body.reveal === 'top3'));
  check('share link starts anonymous', new URL($(dk, '#present-link').value).searchParams.get('names') === '0');
  click(dk, '#present-toggle');
  const presenter = $(dk, '#present-frame');
  snap(dk, 'desk-display');
  check('opening preview uses the anonymous embed link', !presenter.hidden && new URL(presenter.src).searchParams.get('embed') === '1' && new URL(presenter.src).searchParams.get('names') === '0');
  click(dk, '[data-scene="prep"]');
  check('display controls reflect a saved scene', await until(() => $(dk, '#display-status').textContent === 'Display updated.' && !$(dk, '[data-scene="prep"]').classList.contains('ghost')));
  check('preview stays open across display updates', $(dk, '#present-frame') === presenter && !presenter.hidden);
  $(dk, '#present-names').checked = true;
  $(dk, '#present-names').dispatchEvent(new dk.window.Event('change', {bubbles: true}));
  check('operator can explicitly enable names for this link', new URL(presenter.src).searchParams.get('key') === info.view_key && !new URL(presenter.src).searchParams.has('names'));
  click(dk, '#nav button[data-t="run"]');
  click(dk, '#nav button[data-t="display"]');
  check('preview and link choices survive switching tabs', await until(() => !$(dk, '#present-frame').hidden && $(dk, '#present-names').checked));
  check('active section is exposed to assistive technology', $(dk, '#nav [data-t="display"]').getAttribute('aria-current') === 'page');
  await R('POST', '/api/desk/program', {action: 'set', index: 0}, desk);
  await dk.window.close();

  // ── board
  const bd = await load('board.html', '/board');
  check('board shows the podium after reveal', await until(() => $(bd, '#wrap').textContent.includes('Top three'), 5000), $(bd, '#wrap').textContent);
  await R('POST', '/api/desk/reveal', {mode: 'hidden'}, desk);
  check('board hides scores again', await until(() => $(bd, '#wrap').textContent.includes('Round 1'), 5000), $(bd, '#wrap').textContent);
  snap(bd, 'board'); check('board shows a countdown', /\d+:\d\d/.test($(bd, '#wrap').textContent));
  await bd.window.close();

  // ── content editor
  const ce = await load('admin.html', '/admin', {'kevenos:token:desk': desk});
  check('Admin opens on the event overview', await until(() => $(ce, '#main').textContent.includes('Current assignments submitted')));
  check('Admin exposes the distinct event Desk link', !!$(ce, '#kevenos-nav-links a[href="/desk"]') && !$(ce, '#nav [data-t="run"]'));
  click(ce, '#nav [data-t="reports"]');
  check('Admin lists all event reports', $$(ce, '#main [data-export]').length === 3);
  click(ce, '#nav [data-t="system"]');
  check('Admin system has the local network and reset controls', await until(() => $(ce, '#main').textContent.includes('/judge') && !!$(ce, '#rtx')));
  check('Admin has an Event content section', $(ce, '#nav [data-t="content"]').textContent === 'Event content');
  click(ce, '#nav [data-t="content"]');
  check('content form lists fields', await until(() => $$(ce, '#main details').length > 5 && $$(ce, '#main [data-p]').length > 50), $$(ce, '#main details').length);
  const fld = $(ce, '#main [data-p=\'["event","name"]\']'); fld.value = 'KevenOS (edited)'; fld.dispatchEvent(new ce.window.Event('input', {bubbles: true}));
  $(ce, '#ctnote').value = 'ui test'; check('save enabled after an edit', !$(ce, '#ctsave').disabled);
  click(ce, '#ctsave');
  check('content saved from the form', await until(async () => (await R('GET', '/api/info')).body.version.content >= 2 && (await R('GET', '/api/desk/content', null, desk)).body.content.event.name === 'KevenOS (edited)', 5000));
  await ce.window.close();
  const jc = await load('judge.html', '/judge/2', {'kevenos:token:judge:2': jt[2]});
  check('judge page shows the version stamp', await until(() => /content v\d/.test(($(jc, '#ppver') || {}).textContent || ''), 5000), ($(jc, '#ppver') || {}).textContent);
  await jc.window.close();

  // ── team names table; changes require paused event clocks.
  await R('POST', '/api/desk/clock', {action: 'pause'}, desk);
  const rt = await load('admin.html', '/admin', {'kevenos:token:desk': desk});
  await until(() => !!$(rt, '#nav [data-t="people"]'));
  click(rt, '#nav [data-t="people"]');
  check('pairs table has one row per pair', await until(() => $$(rt, '#rt tr[data-i]').length === 30), $$(rt, '#rt tr[data-i]').length);
  check('table shows round and station', $$(rt, '#rt tr[data-i]')[9].textContent.includes('R2 · S2'));
  const row = $$(rt, '#rt tr[data-i]')[0], pi = row.querySelector('[data-f="name"][data-member="1"]'); pi.value = 'Layla Test'; pi.dispatchEvent(new rt.window.Event('input', {bubbles: true}));
  const pc = row.querySelector('[data-f="coach"][data-member="2"]'); pc.checked = true; pc.dispatchEvent(new rt.window.Event('change', {bubbles: true}));
  check('unsaved changes are shown', $(rt, '#rsum').textContent.toLowerCase().includes('unsaved'));
  click(rt, '#rsave');
  check('pairs saved from the table', await until(async () => { const r = (await R('GET', '/api/desk/roster', null, desk)).body.roster[0]; return r.p1 === 'Layla Test' && r.coach2 === 1; }, 5000));
  await until(() => $(rt, '#section-status').textContent.includes('Saved'));
  click(rt, $$(rt, '[data-same]')[0]);
  check('same-company button copies the company', await until(() => $$(rt, '#rt tr[data-i]')[1].querySelector('[data-f="partner"]').value === $$(rt, '#rt tr[data-i]')[0].querySelector('[data-f="partner"]').value));
  await rt.window.close();

  // ── Live View: the admin chooses the scene
  await R('POST', '/api/desk/public', {mode: 'live'}, desk);
  const lv = await load('live.html', '/live');
  const agendaLength = (await R('GET', '/api/public')).body.program.items.length;
  check('Live View starts on instructions', await until(() => $(lv, '#now h2') && $$(lv, '#todo li').length >= 1 && $$(lv, '#tl div').length === agendaLength && $(lv, '#live').textContent === 'CONNECTED', 5000), $(lv, '#left').innerHTML.slice(0, 120));
  check('registration instructions appear once even when summary punctuation differs', !$(lv, '#now .line') && $(lv, '#todo li').textContent.includes('Collect your badge and your pack'));
  const cueWindow = $$(lv, '#today .agenda-window li');
  check('instructions scene centres the current cue with two upcoming slots',
    cueWindow.length === 5 && cueWindow[2].getAttribute('aria-current') === 'step' &&
    cueWindow[3].textContent.includes('Welcome') && $(lv, '#today .agenda-edge').textContent.includes('later'));
  await R('POST', '/api/desk/program', {action: 'set', index: 6}, desk);
  check('prep step shows a countdown', await until(() => $(lv, '#cd .t') && /^\+?\d+:\d\d$/.test($(lv, '#cd .t').textContent), 5000), $(lv, '#right').innerHTML.slice(0, 120));
  await R('POST', '/api/desk/program', {action: 'set', index: 7}, desk); await R('POST', '/api/desk/program', {action: 'announce', text: 'Final in 5 minutes'}, desk);
  check('pitch step shows the four steps and station progress', await until(() => $$(lv, '#steps div').length === 4 && $$(lv, '#stn div').length === 8, 5000), $(lv, '#now h2') && $(lv, '#now h2').textContent);
  check('announcement reaches the screen', await until(() => !$(lv, '#ann').hidden && $(lv, '#ann').textContent.includes('Final in 5')));
  await R('POST', '/api/desk/program', {action: 'scene', scene: 'scores'}, desk);
  check('admin switches the screen to scores', await until(() => $$(lv, '#list .r').length > 0 && /teams scored/.test($(lv, '#pg').textContent), 5000), $(lv, '#right').innerHTML.slice(0, 120));
  check('scores show points and respect the row limit', /pts/.test($(lv, '#list').textContent) && $$(lv, '#list .r').length <= 8);
  const scoreList = $(lv, '#list');
  await sleep(1100);
  check('score rows keep their containers across clock ticks', $(lv, '#list') === scoreList);
  await R('POST', '/api/desk/public', {mode: 'off'}, desk);
  check('hiding scores shows a waiting message', await until(() => !$(lv, '#empty').hidden, 6000), $(lv, '#empty').textContent);
  await R('POST', '/api/desk/public', {mode: 'live'}, desk);
  await R('POST', '/api/desk/program', {action: 'scene', scene: 'final'}, desk);
  check('final scene shows the stage', await until(() => !!$(lv, '#stage') && !!$(lv, '#fl'), 5000));
  await R('POST', '/api/desk/program', {action: 'scene', scene: 'awards'}, desk);
  check('winners stay hidden until revealed', await until(() => !!$(lv, '.hero') && !$(lv, '.pod'), 5000));
  await R('POST', '/api/desk/reveal', {mode: 'top3'}, desk);
  check('revealing shows the podium', await until(() => $$(lv, '.pod > div').length >= 1, 5000), $(lv, '#right').innerHTML.slice(0, 120));
  await R('POST', '/api/desk/reveal', {mode: 'hidden'}, desk); await R('POST', '/api/desk/program', {action: 'announce', text: ''}, desk); await R('POST', '/api/desk/program', {action: 'set', index: 0}, desk);
  snap(lv, 'live-info'); await lv.window.close();
  const lk = await load('live.html', '/live?scene=pitch&top=5');
  check('a screen can be locked to one scene from its address', await until(() => $$(lk, '#stn div').length === 8, 5000));
  await lk.window.close();

  // ── judge-paced sessions
  await R('POST', '/api/desk/mode', {mode: 'self'}, desk); await R('POST', '/api/desk/clock', {action: 'reset'}, desk);
  let selfOffline = false, skipped = 0;
  const sj = await load('judge.html', '/judge/5', {'kevenos:token:judge:5': jt[5]}, null, (method, url, body) => {
    if (url === '/api/session' && body.action === 'next') skipped++;
    if (url === '/api/submit' && selfOffline) throw new Error('Synthetic network outage');
    return null;
  });
  check('self: judge sees the start button', await until(() => $(sj, '#ctl') && !$(sj, '#ctl').hidden && $(sj, '#ctl [data-a="start"]')), $(sj, '#ctl') && $(sj, '#ctl').innerHTML);
  check('self: timer says press Start', $(sj, '#tph').textContent.includes('Press Start'), $(sj, '#tph').textContent);
  click(sj, '#ctl [data-a="start"]');
  check('self: clock runs after the judge presses Start', await until(() => $(sj, '#tph').textContent === 'Pitch'), $(sj, '#tph').textContent);
  check('self: Pause, End pitch and End session offered', !!$(sj, '#ctl [data-a="pause"]') && !!$(sj, '#menu [data-a="feedback"]') && !!$(sj, '#menu [data-a="finish"]'));
  check('clock recovery is grouped under a closed disclosure', $(sj, '#menu details:not([open]) summary')?.textContent === 'Clock controls' &&
    !!$(sj, '#menu details [data-a="reset"]'));
  const skip = $(sj, '#menu [data-a="skip"]');
  check('before submission the menu labels the recovery action explicitly', skip?.textContent === 'Move on without submitting' && !$(sj, '#ctl [data-a="next"]'));
  if (skip) {
    const confirm = sj.window.confirm;
    let warning;
    sj.window.confirm = message => { warning = message; return false; };
    click(sj, skip);
    check('canceling the skip confirmation leaves the current assignment untouched', /without submitting/.test(warning) && skipped === 0);
    sj.window.confirm = confirm;
  }
  check('self: another station is still idle', (await R('GET', '/api/state', null, jt[6])).body.clock.running === false);
  click(sj, '#menu [data-a="feedback"]');
  check('self: End pitch moves to feedback', await until(() => $(sj, '#tph').textContent === 'Feedback'), $(sj, '#tph').textContent);
  const clockStatus = $(sj, '#clock-status');
  check('the clock announces feedback without repeating rubric instructions', clockStatus?.getAttribute('role') === 'status' &&
    clockStatus.textContent === 'Feedback' && !$(sj, '#cue').textContent);
  const statusNode = clockStatus?.firstChild;
  await sleep(550);
  check('the clock does not repeat the announcement on each tick', statusNode && clockStatus.firstChild === statusNode);
  click(sj, '#menu [data-a="finish"]');
  check('self: End session stops the clock', await until(() => $(sj, '#tph').textContent === 'Session ended'), $(sj, '#tph').textContent);
  check('the clock announces when the session ends', clockStatus?.textContent === 'Session ended');
  selfOffline = true;
  click(sj, '#judge-home-open');
  click(sj, '#bsub2');
  click(sj, '[data-rating="3"]');
  click(sj, '#bsub2'); click(sj, '#mgo');
  check('judge-paced pending status has no accepted receipt or primary next action', await until(() => /Pending on this device/.test($(sj, '#subbed').textContent)) &&
    !$(sj, '#ctl [data-a="next"]') && !/Accepted/.test($(sj, '#subbed').textContent));
  selfOffline = false;
  click(sj, '#sretry');
  check('after retry is accepted only the header offers Next pair', await until(() => /Accepted by the Desk/.test($(sj, '#subbed').textContent) && $(sj, '#ctl [data-a="next"]')) &&
    $$(sj, '[data-a="next"], #snext').length === 1 && !$(sj, '#menu [data-a="skip"]') && $(sj, '#bsub2').classList.contains('alt'));
  selfOffline = true;
  click(sj, '#bsub2'); click(sj, '#mgo');
  check('a pending resubmission replaces the older receipt and hides primary Next pair', await until(() => /Pending on this device/.test($(sj, '#subbed').textContent)) &&
    !$(sj, '#ctl [data-a="next"]') && !/Accepted/.test($(sj, '#subbed').textContent));
  const dk2 = await load('desk.html', '/desk#run', {'kevenos:token:desk': desk});
  check('self: desk shows judge-paced and per-station cards', await until(() => $(dk2, '#hr').textContent.includes('Judge-paced') && $$(dk2, '[data-sa]').length >= 8), $(dk2, '#hr').textContent);
  const recovery = $(dk2, '.st details');
  check('Desk keeps live status visible with recovery controls collapsed', recovery?.textContent.includes('Station controls') && !recovery.open &&
    !$(dk2, '.st [data-sc]').closest('details') && $$(dk2, '.st [data-sa]').every(button => !!button.closest('.st details')));
  await dk2.window.close();
  const bd2 = await load('board.html', '/board');
  check('self: board shows station timers', await until(() => $$(bd2, '.cell').length === 8, 5000), $(bd2, '#wrap').textContent);
  await bd2.window.close(); await sj.window.close();

  // regression: elements with the hidden attribute must really be hidden, or an invisible layer eats every click
  const hd = await load('judge.html', '/judge/3', {'kevenos:token:judge:3': jt[3]});
  await until(() => $$(hd, '.tick[data-i]').length > 0);
  const st = hd.window.document.createElement('style'); st.textContent = fs.readFileSync(path.join(ROOT, 'static', 'app.css'), 'utf8'); hd.window.document.head.appendChild(st);
  check('closed dialog is display:none', hd.window.getComputedStyle($(hd, '#modal')).display === 'none', hd.window.getComputedStyle($(hd, '#modal')).display);
  check('app.css has the [hidden] rule', /\[hidden\]\s*\{\s*display\s*:\s*none\s*!important\b/.test(fs.readFileSync(path.join(ROOT, 'static', 'app.css'), 'utf8')));
  await hd.window.close();
  await checkFinalDisplay(desk, jt[3]);
  await bridge('POST', '/api/desk/reset', {confirm:'RESET'}, desk);
  await checkHostedStaff(info);
  await checkNotificationComposer(info);
  await checkWorkspace(info);
  check('no script errors on any page', errors.length === 0, errors.join(' || '));
  console.log(fails ? '\n' + fails + ' failed' : '\nall passed');
  py.kill(); process.exit(fails ? 1 : 0);
})().catch(error => {
  console.error(error);
  py.kill();
  process.exit(1);
});
