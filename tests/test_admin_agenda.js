const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

const script = () => readFileSync(join(__dirname, '../static/agenda.js'), 'utf8');
const item = (id, title, time, min) => ({id, title, time, min, line: '', who: '', do: [], scene: 'info'});
const first = item('opening', 'Welcome', '09:00', 20);
const next = item('pitches', 'Pitches <img src=x onerror=alert(1)>', '09:20', 30);
const schedule = {items: [first, next], active_item_id: first.id, clock_timezone: 'GST', warnings: []};
const preview = {
  can_apply: true, changed: true, blockers: [], warnings: ['Judging timers are unchanged.'],
  changes: [
    {title: 'Break', kind: 'added', before: null, after: {start: '09:20', end: '09:30', duration_minutes: 10}},
    {title: next.title, kind: 'updated', before: {start: '09:20', end: '09:50', duration_minutes: 30}, after: {start: '09:30', end: '10:00', duration_minutes: 30}}
  ],
  old_event_end: '09:50', new_event_end: '10:00', clock_timezone: 'GST', plan_digest: 'a'.repeat(64),
  program: {mode: 'manual', active_item_before: first, active_item_after: first, selection_changes: false}
};
const envelope = (data, revision = 'r2') => ({ok: true, data, context: 'rehearsal-context', revision});
const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture(respond) {
  const dom = new JSDOM('<main></main>', {runScripts: 'outside-only', url: 'https://event.example/admin'});
  const w = dom.window, root = w.document.querySelector('main'), requests = [];
  w.KEV = {profile: {revision: 1, active: true}, esc: value => String(value ?? '').replace(/[&<>"']/g,
    char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]))};
  const request = async (body, signal) => {
    const requestBody = JSON.parse(JSON.stringify(body));
    requests.push(requestBody);
    if (respond) {
      const response = respond(requestBody, requests, signal);
      if (response !== undefined) return response;
    }
    if (body.resource === 'schedule.items') return envelope(schedule, 'r1');
    if (body.resource === 'schedule.edit_preview') return envelope(preview);
    if (body.action === 'prepare') return envelope({confirmation_id: 'confirmation'});
    if (body.action === 'execute') return envelope({applied: true, schedule, version: 4});
    throw new Error('Unexpected agenda request');
  };
  try { w.eval(script()); } catch (error) { w.close(); throw error; }
  const page = w.KEV.agenda;
  const click = label => {
    const button = [...root.querySelectorAll('button')].find(node => node.textContent.trim() === label);
    assert.ok(button, `Missing button ${label}`);
    button.click();
  };
  const edit = (name, value) => {
    const field = root.querySelector(`[name="${name}"]`);
    assert.ok(field, `Missing field ${name}`);
    field.value = value;
    field.dispatchEvent(new w.Event('input', {bubbles: true}));
  };
  const breakPreview = async () => {
    click('Add break');
    edit('before_id', 'pitches');
    click('Preview changes');
    await tick();
  };
  return {page, root, w, requests, click, edit, breakPreview, mount: () => page.mount(root, {request}), close: () => w.close()};
}

test('break preview shows shifted timings and applies the exact reviewed operation', async t => {
  const f = fixture(); t.after(f.close);
  await f.mount();
  assert.equal(f.root.querySelector('img'), null);
  await f.breakPreview();
  assert.match(f.root.textContent, /09:20–09:30/);
  assert.match(f.root.textContent, /09:30–10:00/);
  assert.match(f.root.textContent, /09:50.*10:00/);
  assert.match(f.root.textContent, /GST/);
  assert.equal(f.root.querySelector('img'), null);
  assert.equal(f.requests.some(body => body.action === 'execute'), false);
  f.click('Apply changes');
  await tick();
  const [read, review, prepare, execute] = f.requests;
  assert.equal(read.resource, 'schedule.items');
  assert.equal(review.resource, 'schedule.edit_preview');
  assert.equal(review.arguments.edit.action, 'insert');
  assert.equal(review.arguments.edit.before_id, 'pitches');
  assert.deepEqual([review.arguments.edit.item.title, review.arguments.edit.item.min], ['Break', 10]);
  assert.deepEqual(prepare.arguments, {...review.arguments, plan_digest: preview.plan_digest});
  assert.deepEqual(execute.arguments, prepare.arguments);
  assert.equal(execute.revision, 'r2');
  assert.equal(execute.context, 'rehearsal-context');
  assert.equal(execute.confirmation_id, 'confirmation');
  assert.equal(execute.confirm, 'EXECUTE');
  assert.match(execute.idempotency_key, /^[0-9a-f-]{36}$/);
  assert.match(f.root.textContent, /Agenda updated/);
  assert.equal(f.page.dirty, false);
});

const responseError = (status, code) => Object.assign(new Error(code), {status, code});

test('editing clears the preview and a stale confirmation leaves a fresh review', async t => {
  let rejected = false;
  const f = fixture(body => {
    if (body.action === 'prepare' && !rejected) {
      rejected = true;
      throw responseError(409, 'revision_changed');
    }
  });
  t.after(f.close);
  await f.mount();
  await f.breakPreview();
  f.edit('min', '12');
  assert.equal(f.root.querySelector('[data-agenda="apply"]'), null);
  f.click('Preview changes');
  await tick();
  f.click('Apply changes');
  await tick();
  assert.equal(f.requests.filter(body => body.resource === 'schedule.items').length, 2);
  assert.equal(f.requests.filter(body => body.resource === 'schedule.edit_preview').at(-1).arguments.edit.item.min, 12);
  assert.equal(f.requests.some(body => body.action === 'execute'), false);
  assert.match(f.root.textContent, /Review the refreshed preview/);
  f.click('Apply changes');
  await tick();
  assert.equal(f.requests.filter(body => body.action === 'execute').length, 1);
});

test('an interrupted Apply checks the original receipt before allowing the same request', async t => {
  let attempts = 0;
  const f = fixture(body => {
    if (body.action === 'execute' && !attempts++) throw new TypeError('Connection lost');
    if (body.action === 'receipts') throw responseError(404, 'receipt_not_found');
  });
  t.after(f.close);
  await f.mount();
  await f.breakPreview();
  f.click('Apply changes');
  await tick();
  assert.equal(f.page.unresolved, true);
  assert.equal(f.root.querySelector('[data-agenda="retry"]'), null);
  f.click('Check outcome');
  await tick();
  f.click('Retry same request');
  await tick();
  const [original, retry] = f.requests.filter(body => body.action === 'execute');
  assert.deepEqual(retry, original);
  assert.equal(f.requests.find(body => body.action === 'receipts').idempotency_key, original.idempotency_key);
  assert.equal(f.page.unresolved, false);
});

test('a late preview is discarded after a profile change and unmount', async t => {
  let reply, signal;
  const f = fixture((body, _, requestSignal) => {
    if (body.resource === 'schedule.edit_preview') {
      signal = requestSignal;
      return new Promise(resolve => { reply = resolve; });
    }
  });
  t.after(f.close);
  await f.mount();
  f.click('Add section');
  f.edit('title', 'Lunch');
  f.click('Preview changes');
  assert.equal(f.page.busy, true);
  f.w.KEV.profile.revision++;
  f.page.unmount();
  assert.equal(signal.aborted, true);
  reply(envelope(preview));
  await tick();
  assert.equal(f.root.children.length, 0);
  assert.equal(f.page.busy, false);
  assert.equal(f.page.dirty, false);
});
