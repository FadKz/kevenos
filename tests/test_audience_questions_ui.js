const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

const source = readFileSync(join(__dirname, '../static/audience-questions.js'), 'utf8');
const now = Date.UTC(2026, 10, 15, 8, 0);
const choice = {id: 'q1', type: 'choice', prompt: 'Which topic should we cover?', options: [{key: 'A', text: 'Practice'}, {key: 'B', text: 'Feedback'}]};
const rating = {id: 'q2', type: 'rating', prompt: 'How confident are you?'};
const written = {id: 'q3', type: 'text', prompt: 'Anything else?'};
const draft = {id: 'p1', status: 'draft', title: 'Check-in <img src=x>', description: 'A quick check-in.', questions: [choice, rating, written]};
const live = {...draft, status: 'live', openedAt: now, closesAt: now + 300000};

function fixture(t) {
  const dom = new JSDOM('<button id="quiz">Quiz</button><main></main>', {runScripts: 'outside-only', pretendToBeVisual: true});
  const {window} = dom;
  const root = window.document.querySelector('main');
  let clock = 0;
  window.performance.now = () => clock;
  window.HTMLDialogElement.prototype.showModal ??= function () { this.open = true; };
  window.HTMLDialogElement.prototype.close ??= function () {
    this.open = false;
    this.dispatchEvent(new window.Event('close'));
  };
  window.eval(source);
  t.after(() => { window.AudienceQuestions.clear(); window.close(); });
  const tick = () => new Promise(resolve => setImmediate(resolve));
  const buttons = (label, area = root) => [...area.querySelectorAll('button')]
    .filter(button => button.textContent === label && !button.closest('[hidden]'));
  const click = (label, area) => {
    const [button] = buttons(label, area);
    assert.ok(button, 'Missing ' + label);
    assert.equal(button.disabled, false, label + ' is disabled');
    button.click();
  };
  return {window, root, app: window.AudienceQuestions, tick, advance: () => { clock += 5000; }, buttons, click};
}

function useDesk(f, initial) {
  let current = initial;
  const writes = [];
  const get = async path => {
    assert.equal(path, '/api/desk/audience');
    return current;
  };
  const post = async (path, body) => {
    assert.equal(path, '/api/desk/audience');
    assert.equal(body.expectedRev, current.rev);
    writes.push(JSON.parse(JSON.stringify(body)));
    const updated = {
      save: () => ({...body.pack, id: 'p1', status: 'draft'}),
      send: () => ({...live, results: {[choice.id]: {count: 1, counts: {A: 1, B: 0}}, [written.id]: {count: 2, texts: ['<b>Written reply</b>']}}}),
      close: () => ({...current.packs[0], status: 'closed'}),
      cancel: () => ({...current.packs[0], status: 'cancelled'}),
      release: () => ({...current.packs[0], releasedAt: now + 300000})
    }[body.action]();
    current = {...current, rev: current.rev + 1, packs: [updated]};
    return current;
  };
  return {writes, open: () => f.app.desk(f.root, get, post), update: state => { current = state; }};
}

test('Organizer creates a mixed draft and edits its saved questions without scoring fields', async t => {
  const f = fixture(t);
  const desk = useDesk(f, {rev: 4, now, packs: []});
  desk.open();
  await f.tick();
  f.click('New question pack');
  f.root.querySelector('input[type="text"]').value = 'New pack';
  f.root.querySelector('fieldset textarea').value = choice.prompt;
  const fields = f.root.querySelectorAll('fieldset input');
  fields[0].value = 'Practice';
  fields[1].value = 'Feedback';
  f.click('Add question');
  f.click('Add question');
  const selects = f.root.querySelectorAll('fieldset select');
  selects[1].value = 'rating';
  selects[2].value = 'text';
  for (const select of selects) select.dispatchEvent(new f.window.Event('change'));
  f.root.querySelectorAll('fieldset textarea')[1].value = rating.prompt;
  f.root.querySelectorAll('fieldset textarea')[2].value = written.prompt;
  f.click('Save draft');
  await f.tick();
  assert.deepEqual(desk.writes, [{action: 'save', expectedRev: 4, pack: {
    title: 'New pack', description: '', questions: [
      {type: 'choice', prompt: choice.prompt, options: [{key: 'option-1', text: 'Practice'}, {key: 'option-2', text: 'Feedback'}]},
      {type: 'rating', prompt: rating.prompt}, {type: 'text', prompt: written.prompt}
    ]
  }}]);
  const preview = f.root.querySelector('details');
  assert.match(preview.textContent, /Preview questions.*Rating \(1–5\)/s);
  f.click('Edit draft');
  f.root.querySelector('input[type="text"]').value = 'Edited pack';
  f.click('Save draft');
  await f.tick();
  assert.equal(desk.writes[1].pack.title, 'Edited pack');
  assert.equal(desk.writes[1].pack.id, 'p1');
});

test('Organizer sends a manual pack, watches response totals and separately confirms close and release', async t => {
  const f = fixture(t);
  const desk = useDesk(f, {rev: 1, now, packs: [draft]});
  desk.open();
  await f.tick();
  assert.match(f.root.textContent, /Check-in <img src=x>/);
  assert.equal(f.root.querySelector('img'), null);
  f.click('Send to phones');
  await f.tick();
  assert.deepEqual(desk.writes[0], {action: 'send', packId: 'p1', expectedRev: 1});
  assert.match(f.root.textContent, /1 response/);
  assert.equal(f.buttons('Edit draft').length, 0);
  f.click('Cancel questions');
  assert.match(f.root.textContent, /removed from attendee view/);
  assert.deepEqual(desk.writes.map(body => body.action), ['send']);
  f.click('Go back');
  f.click('Stop accepting answers');
  f.click('Confirm close');
  await f.tick();
  assert.deepEqual(desk.writes.map(body => body.action), ['send', 'close']);
  assert.match(f.root.textContent, /Showing 1 of 2 written responses/);
  assert.match(f.root.textContent, /<b>Written reply<\/b>/);
  assert.equal(f.root.querySelector('b'), null);
  f.click('Share results to phones');
  assert.match(f.root.textContent, /Written answers stay with the organizers/);
  f.click('Confirm sharing');
  await f.tick();
  assert.match(f.root.textContent, /Results shared/);
  assert.deepEqual(desk.writes.map(body => body.action), ['send', 'close', 'release']);
});

test('Filtering packs keeps the answer-window draft and follows a timed send into Open', async t => {
  const f = fixture(t);
  const desk = useDesk(f, {rev: 1, now, packs: [draft]});
  desk.open();
  await f.tick();
  f.click('Drafts · 1');
  const timer = f.root.querySelector('select');
  timer.value = '2';
  timer.dispatchEvent(new f.window.Event('change', {bubbles: true}));
  const search = f.root.querySelector('input[type=search]');
  search.value = 'missing phrase';
  search.dispatchEvent(new f.window.Event('input', {bubbles: true}));
  assert.match(f.root.textContent, /No packs match this view/);
  search.value = 'Check-in';
  search.dispatchEvent(new f.window.Event('input', {bubbles: true}));
  assert.equal(f.root.querySelector('select').value, '2');
  f.click('Send to phones');
  await f.tick();
  assert.equal(f.buttons('Open · 1')[0].getAttribute('aria-pressed'), 'true');
  assert.equal(desk.writes[0].minutes, 2);
});

test('switching packs shows only the selected pack’s actions and retains keyboard focus', async t => {
  const f = fixture(t);
  const desk = useDesk(f, {rev: 1, now, packs: [draft, {...live, id:'p2', title:'Current poll'}]});
  desk.open();
  await f.tick();
  const nav = f.root.querySelector('.audience-pack-queue');
  assert.equal(nav.querySelector('[aria-current="true"]').dataset.audienceFocus, 'pack:p2');
  const other = nav.querySelector('[data-audience-focus="pack:p1"]');
  other.focus();
  other.click();
  assert.equal(f.window.document.activeElement.dataset.audienceFocus, 'pack:p1');
  assert.equal(f.root.querySelector('.audience-pack-detail h2').textContent, draft.title);
  assert.equal(f.buttons('Stop accepting answers').length, 0);
  assert.equal(f.buttons('Send to phones')[0].disabled, true);
});

test('Attendee saves choice and rating, skips a question and keeps in-progress writing during refresh', async t => {
  const f = fixture(t);
  const sent = [];
  const quiz = f.window.document.getElementById('quiz');
  const past = {...draft, id: 'past', status: 'closed'};
  let current = {now, packs: [{...live, answers: {}}]};
  const api = async (path, {body}) => {
    assert.equal(path, '/api/participant/audience');
    if (body) {
      sent.push(JSON.parse(JSON.stringify(body)));
      current.packs[0].answers[body.questionId] = body.value;
    }
    return JSON.parse(JSON.stringify(current));
  };
  quiz.focus();
  f.app.participant(f.root, api);
  await f.tick();
  assert.equal(f.window.document.activeElement, quiz);
  assert.equal(f.root.querySelector('dialog'), null);
  assert.match(f.root.textContent, /Audience question.*No points/);
  assert.match(f.root.textContent, /Open until 08:05:00 event time/);
  const [choiceForm, ratingForm, textForm] = f.root.querySelectorAll('form');
  assert.deepEqual([...f.root.querySelectorAll('legend')].map(legend => legend.textContent),
    ['1. ' + choice.prompt, '2. ' + rating.prompt, '3. ' + written.prompt]);
  assert.deepEqual([...f.root.querySelectorAll('form:not([hidden])')], [choiceForm]);
  choiceForm.querySelector('[value="B"]').click();
  f.click('Submit answer', choiceForm);
  await f.tick();
  assert.match(choiceForm.textContent, /Answer saved · Feedback/);
  assert.deepEqual([...f.root.querySelectorAll('form:not([hidden])')], [ratingForm]);
  ratingForm.querySelector('[value="4"]').click();
  f.click('Submit answer', ratingForm);
  await f.tick();
  assert.match(ratingForm.textContent, /Answer saved · 4/);
  assert.equal(f.buttons('Submit answer', ratingForm).length, 0);
  assert.match(f.root.textContent, /Question 3 of 3/);
  assert.deepEqual([...f.root.querySelectorAll('form:not([hidden])')], [textForm]);
  const textarea = textForm.querySelector('textarea');
  textarea.focus();
  textarea.value = 'Typing…';
  current.packs.push({...past, releasedAt: now, results: {[written.id]: {count: 1, texts: ['Desk only']}}});
  f.advance();
  f.app.participant(f.root, api);
  await f.tick();
  assert.equal(f.window.document.activeElement, textarea);
  assert.equal(textarea.value, 'Typing…');
  assert.match(f.root.textContent, /Recent audience question results/);
  assert.doesNotMatch(f.root.textContent, /Desk only/);
  f.click('Skip question', textForm);
  await f.tick();
  assert.match(textForm.textContent, /Skipped/);
  assert.match(choiceForm.textContent, /Answer saved · Feedback/);
  assert.deepEqual(sent.map(body => body.value), ['B', 4, null]);

  current = {now, packs: []};
  f.advance();
  f.app.participant(f.root, api);
  await f.tick();
  assert.equal(f.root.hidden, true);
  assert.equal(f.root.querySelector('form'), null);
});

test('Sign-out clears attendee data and late responses cannot bring the card back', async t => {
  const f = fixture(t);
  let finish;
  f.app.participant(f.root, () => new Promise(resolve => { finish = resolve; }));
  f.app.clear();
  finish({now, packs: [live]});
  await f.tick();
  assert.equal(f.root.hidden, true);
  assert.equal(f.root.childNodes.length, 0);
});

test('Expired answers keep an actionable error after the question disappears', async t => {
  const f = fixture(t);
  let current = {now, packs: [{...live, answers: {}}]};
  const api = async (_path, {body}) => {
    if (body) throw Object.assign(new Error('This Audience question is closed or unavailable right now.'), {status: 409});
    return current;
  };
  f.app.participant(f.root, api);
  await f.tick();
  f.root.querySelector('[value="A"]').click();
  f.click('Submit answer');
  await f.tick();
  assert.match(f.root.textContent, /closed or unavailable right now/);
  assert.doesNotMatch(f.root.textContent, /Answer saved/);
  current = {now: live.closesAt, packs: []};
  f.advance();
  f.app.participant(f.root, api);
  await f.tick();
  assert.equal(f.root.querySelector('form'), null);
  assert.equal(f.root.hidden, false);
  assert.match(f.root.querySelector('[role="alert"]').textContent, /closed or unavailable right now/);
});

test('Organizer cancellation requires confirmation and the attendee card disappears on refresh', async t => {
  const f = fixture(t);
  const desk = useDesk(f, {rev: 1, now, packs: [live]});
  desk.open();
  await f.tick();
  f.click('Cancel questions');
  assert.equal(desk.writes.length, 0);
  f.click('Confirm cancellation');
  await f.tick();
  assert.deepEqual(desk.writes.map(body => body.action), ['cancel']);
  assert.match(f.root.textContent, /Cancelled/);
  assert.equal(f.buttons('Share results to phones').length, 0);
  let current = {now, packs: [{...live, answers: {}}]};
  const api = async () => current;
  f.app.participant(f.root, api);
  await f.tick();
  assert.equal(f.root.hidden, false);
  current = {now, packs: []};
  f.advance();
  f.app.participant(f.root, api);
  await f.tick();
  assert.equal(f.root.hidden, true);
  assert.equal(f.root.querySelector('form'), null);
});

test('Organizer opens the bounded written detail and can page through escaped responses', async t => {
  const f = fixture(t);
  const reads = [];
  const closed = {...live, status: 'closed', results: {[written.id]: {count: 54, texts: ['Preview text']}}};
  f.app.desk(f.root, async path => {
    if (path === '/api/desk/audience') return {now, rev: 2, packs: [closed]};
    const url = new URL(path, 'https://example.test');
    assert.equal(url.pathname, '/api/desk/audience/responses');
    assert.equal(url.searchParams.get('packId'), closed.id);
    assert.equal(url.searchParams.get('questionId'), written.id);
    const offset = Number(url.searchParams.get('offset'));
    const limit = Number(url.searchParams.get('limit'));
    reads.push(offset);
    assert.equal(limit, 50);
    return {offset, limit, total: 54, texts: Array.from({length: Math.min(limit, 54 - offset)}, (_, index) => '<b>Reply ' + (offset + index + 1) + '</b>')};
  }, () => assert.fail('Read-only view must not write'));
  await f.tick();
  f.click('View all written responses');
  await f.tick();
  const dialog = f.root.querySelector('dialog[open]');
  assert.ok(dialog);
  assert.match(dialog.textContent, /Responses 1–50 of 54/);
  assert.equal(dialog.querySelectorAll('li').length, 50);
  assert.equal(dialog.querySelector('b'), null);
  f.click('Next page', dialog);
  await f.tick();
  assert.match(dialog.textContent, /Responses 51–54 of 54/);
  assert.equal(dialog.querySelector('ol').start, 51);
  assert.equal(dialog.querySelectorAll('li').length, 4);
  assert.equal(f.buttons('Next page', dialog)[0].disabled, true);
  f.click('Previous page', dialog);
  await f.tick();
  assert.match(dialog.textContent, /Responses 1–50 of 54/);
  assert.deepEqual(reads, [0, 50, 0]);
  f.click('Close', dialog);
  assert.equal(f.root.querySelector('dialog'), null);
  assert.equal(f.window.document.activeElement, f.buttons('View all written responses')[0]);
});
