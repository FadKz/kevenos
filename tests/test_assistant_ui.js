// Run with the same jsdom dependency as the other KevenOS UI tests.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const {test} = require('node:test');

const source = name => readFileSync(path.join(__dirname, '../static', name), 'utf8');
const assistant = source('assistant.js');
const admin = source('admin.html');
const configSource = admin.slice(admin.indexOf('function configurationForm('), admin.indexOf('function workspaceSettings('));
const uuid = '00000000-0000-4000-8000-000000000001';
const tick = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));

function dom() {
  const page = new JSDOM('<!doctype html><main id="app"></main>', {
    runScripts: 'outside-only', url: 'https://event.example.test/admin'
  });
  const w = page.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  Object.defineProperty(w.crypto, 'randomUUID', {value: () => uuid});
  return page;
}

function chat({preview = false, messages = [], request, enabled = true, clock} = {}) {
  const page = dom();
  const w = page.window, doc = w.document;
  const calls = [];
  const timers = new Map();
  let now = clock, timerId = 0;
  if (clock !== undefined) {
    w.Date.now = () => now;
    w.setTimeout = (callback, delay = 0) => { const id = ++timerId; timers.set(id, {callback, at: now + delay}); return id; };
    w.clearTimeout = id => timers.delete(id);
  }
  const view = {
    enabled, role: 'desk', workspace: 'rehearsal', preview, can_apply: !preview,
    thread: {id: 'thread-1', messages}, suggestions: ['What should the Desk check?']
  };
  w.KEV = {
    staff: {token: 'fixture'},
    profile: {revision: 1, isPreview: preview, label: () => 'Organizer'},
    api: async (endpoint, options, token) => {
      assert.equal(token, 'fixture');
      const call = {endpoint, body: options?.body ? clone(options.body) : null};
      calls.push(call);
      return clone((await request?.(call, view)) || view);
    }
  };
  w.eval(assistant);
  w.KEV.assistant.mount(doc.querySelector('#app'), 'desk');
  return {page, w, doc, calls, view, timers,
    advance: async milliseconds => {
      const target = now + milliseconds;
      while (true) {
        const next = [...timers].filter(([, item]) => item.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        const [id, item] = next; timers.delete(id); now = item.at; item.callback(); await tick();
      }
      now = target; await tick();
    },
    button: text => [...doc.querySelectorAll('button')].find(button => button.textContent === text),
    open: async () => { await tick(); doc.querySelector('.ppa-launch').click(); await tick(); },
    send: async text => {
      doc.querySelector('textarea').value = text;
      doc.querySelector('.ppa-send').click();
      await tick();
    },
    close: () => page.window.close()};
}

const proposal = {
  id: 'p1', status: 'pending', command: 'program.announce',
  arguments: {message: '<img src=x onerror=alert(1)>'}, summary: 'Show the welcome message.',
  effects: ['The audience screen will change.']
};

test('only enabled operators see the launcher and focus returns when closed', async () => {
  const disabled = chat({enabled: false});
  const page = chat();
  try {
    await tick();
    assert(disabled.doc.querySelector('.ppa-launch').hidden);
    await page.open();
    assert(page.doc.querySelector('dialog').open);
    assert.equal(page.doc.activeElement.id, 'ppa-message');
    assert.match(page.doc.querySelector('.ppa-scope').textContent, /Organizer.*Rehearsal/);
    page.button('What should the Desk check?').click();
    assert.equal(page.doc.querySelector('textarea').value, 'What should the Desk check?');
    assert.equal(page.calls.filter(call => call.body).length, 0);
    page.button('Close').click();
    assert.equal(page.doc.activeElement, page.doc.querySelector('.ppa-launch'));
  } finally { disabled.close(); page.close(); }
});

test('exact proposal is rendered as text and applies only on click', async () => {
  const page = chat({
    messages: [{id: 'j1', role: 'assistant', text: '<script>bad()</script>', status: 'completed', job_id: 'j1', proposal}],
    request(call, view) {
      if (call.endpoint.endsWith('/confirm')) {
        view.thread.messages[0].proposal = {...proposal, status: 'completed', result: {ok: true, message: 'Action applied and checked.'}};
      }
    }
  });
  try {
    await page.open();
    const log = page.doc.querySelector('.ppa-log');
    assert.match(log.textContent, /<script>bad/);
    assert.match(log.textContent, /Show the welcome message/);
    assert.doesNotMatch(log.textContent, /program\.announce/);
    assert.match(log.textContent, /<img src=x/);
    assert.match(log.textContent, /audience screen/);
    assert(!log.querySelector('script, img'));
    assert.equal(page.calls.filter(call => call.body).length, 0);
    page.button('Apply action').click();
    await tick();
    assert.deepEqual(page.calls.filter(call => call.body), [{endpoint: '/api/assistant/confirm', body: {proposal_id: 'p1'}}]);
    assert.match(log.textContent, /Action applied and checked/);
    assert.equal(page.button('Apply action'), undefined);
  } finally { page.close(); }
});

test('preview can ask a question and never exposes Apply', async () => {
  const page = chat({preview: true, messages: [{id: 'j1', role: 'assistant', status: 'completed', proposal}]});
  try {
    await page.open();
    assert.equal(page.button('Apply action'), undefined);
    assert.match(page.doc.querySelector('.ppa-log').textContent, /Preview is read-only/);
    await page.send('Explain the Final');
    assert.deepEqual(page.calls.filter(call => call.body), [{endpoint: '/api/assistant/message', body: {message: 'Explain the Final', operation_id: uuid}}]);
  } finally { page.close(); }
});

for (const shift of [true, false]) test('retiming proposal uses clear following-section choice: '+shift, async () => {
  const effects = ['Welcome: 09:30–10:00 → 09:45–10:15 (30 min).', 'The event now finishes at 14:15.'];
  const page = chat({messages:[{role:'assistant', status:'completed', proposal:{
    id:'retime-proposal', status:'pending', command:'schedule.retime', title:'Adjust the welcome',
    arguments:{change:{id:'internal-agenda-id', start:'09:45', duration:30}, shift_following:shift,
      note:'Allow more arrival time.', plan_digest:'internal-plan-digest'}, effects
  }}]});
  try {
    await page.open();
    const card=page.doc.querySelector('.ppa-proposal');
    assert.deepEqual([...card.querySelectorAll('dt')].map(node=>node.textContent), ['Following sections','Note']);
    assert.match(card.textContent, shift ? /Shift with this change/ : /Keep their current times/);
    assert.match(card.textContent,/Allow more arrival time/);
    assert.doesNotMatch(card.textContent,/internal-agenda-id|internal-plan-digest|shift_following|"id"|"start"|"duration"|Plan digest/);
    assert.deepEqual([...card.querySelectorAll('.ppa-effects li')].map(node=>node.textContent),effects);
    assert(page.button('Apply action'));
  } finally {page.close();}
});

test('other proposal arguments keep their existing labels and values', async () => {
  const arguments_={change:{name:'Synthetic team'}, shift_following:true, note:'Keep details.', plan_digest:'hidden-digest'};
  const page=chat({messages:[{role:'assistant',status:'completed',proposal:{...proposal,command:'roster.update',arguments:arguments_}}]});
  try {
    await page.open();
    const rows=[...page.doc.querySelectorAll('.ppa-argument')];
    assert.deepEqual(rows.map(row=>row.querySelector('dt').textContent),['Change','Shift following','Note']);
    assert.deepEqual(rows.map(row=>row.querySelector('dd').textContent),[JSON.stringify(arguments_.change,null,2),'true','Keep details.']);
    assert.doesNotMatch(page.doc.querySelector('.ppa-proposal').textContent,/hidden-digest/);
  } finally {page.close();}
});

test('a lost message response reconciles by operation ID without resending', async () => {
  const page = chat({
    request(call, view) {
      if (call.endpoint.endsWith('/message')) {
        view.thread.messages.push({id: 'job-user', role: 'user', text: call.body.message}, {id: 'job', role: 'assistant', job_id: 'job', operation_id: uuid, status: 'submitting'});
        throw new Error('connection lost');
      }
    }
  });
  try {
    await page.open();
    await page.send('What now?');
    assert.equal(page.calls.filter(call => call.body).length, 1);
    assert.match(page.doc.querySelector('.ppa-notice').textContent, /Connecting/);
    assert(page.doc.querySelector('.ppa-send').disabled);
    assert.equal(page.doc.querySelector('textarea').value, '');
    assert.match(page.doc.querySelector('.ppa-log').textContent, /What now/);
  } finally { page.close(); }
});

test('waiting shows elapsed time, explains a longer answer and stops all timers when closed', async () => {
  const now = 1794572067000;
  const page = chat({clock: now, messages: [
    {id: 'j1', role: 'assistant', job_id: 'j1', status: 'running', created_at: (now - 12000) / 1000}
  ]});
  try {
    await page.open();
    assert.match(page.doc.querySelector('.ppa-notice').textContent, /Preparing your answer/);
    assert.equal(page.doc.querySelector('.ppa-elapsed').textContent, '12s elapsed');
    assert.equal(page.doc.querySelector('.ppa-elapsed').getAttribute('aria-live'), 'off');
    assert(!page.button('Cancel request').hidden);
    const requests = page.calls.length;
    await page.advance(1000);
    assert.equal(page.doc.querySelector('.ppa-elapsed').textContent, '13s elapsed');
    assert.equal(page.calls.length, requests + 1);
    page.button('Close').click();
    assert.equal(page.timers.size, 0);
    const closedRequests = page.calls.length;
    await page.advance(3000);
    assert.equal(page.calls.length, closedRequests);
    await page.open();
    assert.equal(page.doc.querySelector('.ppa-elapsed').textContent, '16s elapsed');
    page.w.KEV.assistant.unmount();
    assert.equal(page.timers.size, 0);
  } finally { page.close(); }
});

test('completed event facts show their source and clear waiting feedback', async () => {
  const now = 1794572067000;
  const page = chat({clock: now, messages: [
    {id: 'j1', role: 'assistant', job_id: 'j1', status: 'running', created_at: (now - 3000) / 1000}
  ]});
  try {
    await page.open();
    assert.equal(page.doc.querySelector('.ppa-elapsed').textContent, '3s elapsed');
    page.view.thread.messages[0] = {
      id: 'j1', role: 'assistant', job_id: 'j1', status: 'completed', created_at: (now - 3000) / 1000,
      text: 'Station 1: Synthetic Judge', source: 'event'
    };
    await page.advance(1000);
    assert.equal(page.doc.querySelector('.ppa-source').textContent, 'From current event data');
    assert.equal(page.doc.querySelector('.ppa-notice').textContent, '');
    assert(page.doc.querySelector('.ppa-elapsed').hidden);
    assert.equal(page.timers.size, 0);
    assert(page.button('Cancel request').hidden);
    assert(!page.doc.querySelector('.ppa-send').disabled);
  } finally { page.close(); }
});

test('model answers and missing timestamps do not claim a direct data source or elapsed time', async () => {
  const page = chat({messages: [
    {id: 'j1', role: 'assistant', job_id: 'j1', status: 'completed', text: 'A workflow explanation.', source: 'hermes'},
    {id: 'j2', role: 'assistant', job_id: 'j2', status: 'submitting', created_at: null}
  ]});
  try {
    await page.open();
    assert(!page.doc.querySelector('.ppa-source'));
    assert(page.doc.querySelector('.ppa-elapsed').hidden);
    assert.match(page.doc.querySelector('.ppa-notice').textContent, /Connecting/);
  } finally { page.close(); }
});

test('stale confirmation removes Apply and requires a fresh proposal', async () => {
  const page = chat({
    messages: [{id: 'j1', role: 'assistant', status: 'completed', job_id: 'j1', proposal}],
    request(call) {
      if (call.endpoint.endsWith('/confirm')) throw Object.assign(new Error('Stale'), {status: 409});
    }
  });
  try {
    await page.open();
    page.button('Apply action').click();
    await tick();
    assert.equal(page.button('Apply action'), undefined);
    assert.match(page.doc.querySelector('.ppa-log').textContent, /ask for a new proposal/);
    assert.equal(page.calls.filter(call => call.body).length, 1);
  } finally { page.close(); }
});

test('reset is explicit and a late response from an old sign-in cannot render', async () => {
  let finish;
  const page = chat({
    messages: [{id: 'm1', role: 'user', text: 'Old conversation'}],
    request(call) {
      if (call.endpoint.endsWith('/reset')) return new Promise(resolve => { finish = resolve; });
    }
  });
  try {
    await page.open();
    page.button('New conversation').click();
    assert.equal(page.calls.filter(call => call.body).length, 0);
    page.button('Start new').click();
    assert.deepEqual(page.calls.filter(call => call.body), [{endpoint: '/api/assistant/reset', body: {}}]);
    page.w.KEV.profile.revision++;
    page.w.KEV.assistant.unmount();
    finish({enabled: true, thread: {id: 'late', messages: [{text: 'Old profile private result'}]}});
    await tick();
    assert(!page.doc.querySelector('dialog'));
    assert(!page.doc.body.textContent.includes('Old profile private result'));
  } finally { page.close(); }
});

function resize({workspace = 'rehearsal', preview, post} = {}) {
  const page = dom();
  const w = page.window, doc = w.document, calls = [];
  const setup = {configuration: {participant_count: 24, team_size: 2, judge_count: 3}, editable: true};
  w.KEV = {
    profile: {isPreview: false},
    esc: value => String(value ?? '').replace(/[<>&"]/g, ch => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[ch]))
  };
  w.post = async (endpoint, body) => {
    calls.push({endpoint, body: clone(body)});
    if (endpoint.endsWith('/preview')) return preview || {
      changed: true, can_apply: true, plan_digest: 'a'.repeat(64), configuration: body,
      summary: {retained_members: 10}, warnings: ['Clocks pause.'], moves: [{from_pair: 3, to_pair: 2}]
    };
    return post?.();
  };
  w.get = async endpoint => { calls.push({endpoint, body: null}); };
  w.eval('const $ = id => document.getElementById(id); let sectionKey=1, C={agenda:[]}, roster, ROWS, RDIRTY; const vSystem=async()=>{}; const setStatus=()=>{};\n' + configSource);
  doc.querySelector('#app').innerHTML = w.configurationForm(setup, {mode: workspace});
  w.bindConfiguration(setup, {mode: workspace}, 1);
  return {w, doc, calls, close: () => page.window.close(), click: async id => { doc.getElementById(id).click(); await tick(); }};
}

test('rehearsal requires preview and uses the same exact digest on an explicit outcome check', async () => {
  let writes = 0;
  const page = resize({post: () => { if (++writes === 1) throw new Error('response lost'); }});
  try {
    page.doc.getElementById('config-participant-count').value = 30;
    await page.click('config-save');
    const body = {participant_count: 30, team_size: 2, judge_count: 3};
    assert.deepEqual(page.calls, [{endpoint: '/api/admin/configuration/preview', body}]);
    assert.match(page.doc.getElementById('config-plan').textContent, /Member records retained.*Move team 3 to slot 2/);
    assert.equal(page.doc.activeElement.id, 'config-plan-heading');
    await page.click('config-apply');
    assert.equal(page.doc.getElementById('config-apply').textContent, 'Check resize outcome');
    await page.click('config-apply');
    assert.deepEqual(page.calls, [
      {endpoint: '/api/admin/configuration/preview', body},
      {endpoint: '/api/admin/configuration', body: {...body, plan_digest: 'a'.repeat(64)}},
      {endpoint: '/api/admin/configuration', body: null},
      {endpoint: '/api/admin/configuration', body: {...body, plan_digest: 'a'.repeat(64)}}
    ]);
  } finally { page.close(); }
});

test('blocked resize shows escaped reasons and Live keeps the original save route', async () => {
  const blocked = resize({preview: {
    changed: true, can_apply: false, configuration: {participant_count: 1, team_size: 1, judge_count: 1},
    blockers: [{message: '<img> A protected team will not fit.'}]
  }});
  const live = resize({workspace: 'live'});
  try {
    await blocked.click('config-save');
    const panel = blocked.doc.getElementById('config-plan');
    assert.match(panel.textContent, /<img> A protected team/);
    assert(!panel.querySelector('img'));
    assert(blocked.doc.getElementById('config-apply').disabled);
    await live.click('config-save');
    assert.deepEqual(live.calls, [{endpoint: '/api/admin/configuration', body: {participant_count: 24, team_size: 2, judge_count: 3}}]);
  } finally { blocked.close(); live.close(); }
});

test('assistant assets are included on Admin and Desk only', () => {
  for (const page of ['admin.html', 'desk.html']) {
    assert.match(source(page), /\/static\/assistant\.css/);
    assert.match(source(page), /\/static\/assistant\.js/);
  }
  for (const page of ['guest.html', 'participant.html', 'judge.html', 'live.html', 'board.html']) {
    assert.doesNotMatch(source(page), /\/static\/assistant\./);
  }
});

test('operations prompt opens and prefills without submitting or mutating', async()=>{
 const page=chat();try{await tick();page.w.KEV.assistant.openWithPrompt('What needs attention?');await tick();assert(page.doc.querySelector('dialog').open);assert.equal(page.doc.querySelector('textarea').value,'What needs attention?');assert.equal(page.calls.filter(c=>c.body).length,0);}finally{page.close();}
});
