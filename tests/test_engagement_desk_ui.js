const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

test('organizer can save a draft and receives clear validation', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window;
  t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [];
  const read = async () => ({revision:0, activities:[], runs:[]});
  const write = async (path, body) => { writes.push(body); return {revision:1, activities:[], runs:[]}; };
  await w.EngagementDesk.mount(w.document.querySelector('#desk'), read, write);
  const root = w.document.querySelector('#desk');
  assert.equal(root.querySelector('[name=correct]').closest('label').hidden, true);
  root.querySelector('[name=kind]').value = 'quiz';
  root.querySelector('[name=kind]').dispatchEvent(new w.Event('change', {bubbles:true}));
  assert.equal(root.querySelector('[name=correct]').closest('label').hidden, false);
  root.querySelector('[name=kind]').value = 'poll';
  root.querySelector('[name=kind]').dispatchEvent(new w.Event('change', {bubbles:true}));
  root.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  assert.match(root.textContent, /Enter a title/);
  root.querySelector('[name=title]').value = 'Knowledge check';
  root.querySelector('[name=prompt]').value = 'Choose one';
  root.querySelector('[name=options]').value = 'First\nSecond';
  root.querySelector('[name=audience-guest]').checked = true;
  root.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes.length, 1);
  assert.equal(writes[0].action, 'save');
  assert.equal(writes[0].payload.title, 'Knowledge check');
  assert.equal(writes[0].payload.questions[0].prompt, 'Choose one');
  assert.deepEqual(JSON.parse(JSON.stringify(writes[0].payload.audiences)), ['participant','guest']);
  assert.match(writes[0].request_id, /^[0-9a-f]{32}$/);
});

test('uncertain save retries use the same request ID and block a different command', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window;
  t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [];
  await w.EngagementDesk.mount(w.document.querySelector('#desk'), async () => ({revision:0, activities:[], runs:[]}), async (path, body) => {
    writes.push(body);
    if (writes.length === 1) throw new Error('Network disconnected');
    return {revision:1, activities:[], runs:[]};
  });
  const root = w.document.querySelector('#desk');
  root.querySelector('[name=title]').value = 'Activity';
  root.querySelector('[name=prompt]').value = 'Question';
  root.querySelector('[name=options]').value = 'A\nB';
  const submit = () => root.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  submit(); await new Promise(resolve => setTimeout(resolve, 0));
  root.querySelector('[name=title]').value = 'Changed'; submit(); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes.length, 1);
  assert.match(root.textContent, /retry the original/i);
  root.querySelector('[name=title]').value = 'Activity'; submit(); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes.length, 2);
  assert.equal(writes[0].request_id, writes[1].request_id);
});

test('organizer chooses a question duration instead of a fixed timer', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window;
  t.after(() => w.close());
  w.confirm = () => true;
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [];
  const state = {revision:3, activities:[], runs:[{id:'run', version:1, status:'open', snapshot:{title:'Poll', questions:[{id:'q1', prompt:'Question?'}]}}]};
  await w.EngagementDesk.mount(w.document.querySelector('#desk'), async () => state, async (path, body) => { writes.push(body); return state; });
  const root = w.document.querySelector('#desk');
  assert.equal(root.querySelector('[name=duration]').value, '15');
  root.querySelector('[name=duration]').value = '45';
  [...root.querySelectorAll('button')].find(b => b.textContent === 'Open question').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes[0].payload.duration, 45);
});

test('live analytics refresh without discarding an unsaved draft', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window;
  t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  let state = {revision:3, activities:[], runs:[{id:'run', version:1, status:'open', snapshot:{title:'Poll', questions:[]},
    analytics:{respondents:0, denominator:0, distribution:null, suppressed_below:5}}]};
  const read = async () => state, write = async () => state, root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, read, write);
  root.querySelector('[name=title]').value = 'Unsaved draft';
  state = {...state, runs:[{...state.runs[0], analytics:{respondents:3, denominator:3, distribution:null, suppressed_below:5}}]};
  await w.EngagementDesk.mount(root, read, write);
  assert.equal(root.querySelector('[name=title]').value, 'Unsaved draft');
  assert.match(root.textContent, /3 responses/);
});

test('hall sharing requires an explicit confirmation', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window;
  t.after(() => w.close());
  let allowed = false; w.confirm = () => allowed;
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [], state = {revision:4, activities:[], runs:[{id:'run', version:1, status:'open', hall_shared:false,
    delivery:{id:'delivery', status:'open'}, snapshot:{title:'Poll', questions:[{id:'q', prompt:'Question?'}]}}]};
  await w.EngagementDesk.mount(w.document.querySelector('#desk'), async () => state, async (path, body) => { writes.push(body); return state; });
  const share = [...w.document.querySelectorAll('button')].find(b => b.textContent === 'Share on hall');
  share.click(); assert.equal(writes.length, 0);
  allowed = true; share.click(); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes[0].action, 'share_hall');
  assert.equal(writes[0].payload.share, true);
});

test('an unfinished draft cannot be discarded without confirming', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window;
  t.after(() => w.close());
  w.confirm = () => false;
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  await w.EngagementDesk.mount(w.document.querySelector('#desk'), async () => ({revision:0, activities:[], runs:[]}), async () => {});
  const input = w.document.querySelector('[name=title]');
  input.value = 'Unfinished'; input.dispatchEvent(new w.Event('input', {bubbles:true}));
  assert.equal(w.EngagementDesk.canLeave(), false);
  [...w.document.querySelectorAll('button')].find(b => b.textContent === 'New activity').click();
  assert.equal(w.document.querySelector('[name=title]').value, 'Unfinished');
  w.confirm = () => true;
  assert.equal(w.EngagementDesk.canLeave(), true);
});

test('organizer sees pending questions and can approve one', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close());
  w.confirm = () => true;
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [], state = {revision:1, activities:[], runs:[], qa_settings:{participants:true, guests:false},
    qa:[{id:'q1', text:'Can you explain this?', status:'pending', audience:'participant'}]};
  await w.EngagementDesk.mount(w.document.querySelector('#desk'), async () => state, async (path, body) => { writes.push(body); return state; });
  const root = w.document.querySelector('#desk');
  assert.match(root.textContent, /Can you explain this/);
  [...root.querySelectorAll('button')].find(b => b.textContent === 'Approve question').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes[0].action, 'moderate_question');
  assert.equal(writes[0].payload.decision, 'approve');
});
