const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

test('completed runs show per-question aggregate history', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const state = {revision:0,activities:[],runs:[{id:'r',version:1,status:'completed',snapshot:{title:'Survey',questions:[
    {id:'q',prompt:'How useful?',type:'single',options:['Yes','No']}]},
    question_analytics:[{question_id:'q',respondents:6,denominator:6,distribution:[4,2],suppressed_below:5}]}]};
  const root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => state, async () => state);
  const history = [...root.querySelectorAll('details')].find(item => item.textContent.includes('Completed runs'));
  assert.match(history.textContent, /How useful/);
  assert.match(history.textContent, /6 responses/);
  assert.match(history.textContent, /Yes: 4/);
});

test('organizer can filter the moderation queue without changing its contents', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const state = {revision:0, activities:[], runs:[], qa:[
    {id:'a',text:'Where is lunch?',status:'pending',audience:'guest',votes:0},
    {id:'b',text:'When does it start?',status:'approved',audience:'participant',votes:1}]};
  const root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => state, async () => state);
  const status = root.querySelector('[name=qa-filter]'); status.value = 'pending';
  status.dispatchEvent(new w.Event('change', {bubbles:true}));
  const list = root.querySelector('[data-qa-list]');
  assert.match(list.textContent, /Where is lunch/);
  assert.doesNotMatch(list.textContent, /When does it start/);
  const search = root.querySelector('[name=qa-search]'); search.value = 'start';
  search.dispatchEvent(new w.Event('input', {bubbles:true}));
  assert.doesNotMatch(list.textContent, /Where is lunch/);
  assert.equal(state.qa.length, 2);
});

test('organizer can reorder questions without losing their unsaved text', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [], root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => ({revision:0, activities:[], runs:[]}),
    async (path, body) => { writes.push(body); return {revision:1, activities:[], runs:[]}; });
  root.querySelector('[name=title]').value = 'Ordered poll';
  root.querySelector('[name=prompt]').value = 'First';
  root.querySelector('[name=options]').value = 'A\nB';
  [...root.querySelectorAll('button')].find(item => item.textContent === 'Add question').click();
  const boxes = root.querySelectorAll('.engagement-editor-questions fieldset');
  boxes[1].querySelector('[name=prompt]').value = 'Second';
  boxes[1].querySelector('[name=options]').value = 'C\nD';
  [...boxes[1].querySelectorAll('button')].find(item => item.textContent === 'Move earlier').click();
  root.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(Array.from(writes[0].payload.questions, q => q.prompt), ['Second', 'First']);
  assert.deepEqual(Array.from(writes[0].payload.questions[0].options), ['C', 'D']);
});

test('duplicating a question copies current edits into an independent question', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [], root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => ({revision:0, activities:[], runs:[]}),
    async (path, body) => { writes.push(body); return {revision:1, activities:[], runs:[]}; });
  root.querySelector('[name=title]').value = 'Poll';
  root.querySelector('[name=prompt]').value = 'Draft question';
  root.querySelector('[name=options]').value = 'First\nSecond';
  [...root.querySelectorAll('button')].find(item => item.textContent === 'Duplicate question').click();
  const boxes = root.querySelectorAll('.engagement-editor-questions fieldset');
  assert.equal(boxes.length, 2);
  assert.equal(boxes[1].querySelector('[name=prompt]').value, 'Draft question');
  boxes[1].querySelector('[name=prompt]').value = 'Independent copy';
  root.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(Array.from(writes[0].payload.questions, q => q.prompt), ['Draft question', 'Independent copy']);
  assert.equal(writes[0].payload.questions[1].id, undefined);
});

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
  root.querySelector('[name=auto-reveal]').checked = true;
  [...root.querySelectorAll('button')].find(b => b.textContent === 'Open question').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes[0].payload.duration, 45);
  assert.equal(writes[0].payload.auto_reveal, true);
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

test('analytics label answer counts and do not invent views', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const state = {revision:3, activities:[], runs:[{id:'run', version:1,status:'open',active_question_id:'q1',
    snapshot:{title:'Poll',questions:[{id:'q1',prompt:'Question?',type:'single',options:['First','Second']}]},
    analytics:{respondents:5,denominator:5,distribution:[3,2],suppressed_below:5,viewed:null}}]};
  const root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => state, async () => state);
  assert.match(root.textContent, /First: 3/);
  assert.match(root.textContent, /Second: 2/);
  assert.match(root.textContent, /out of 5 respondents/);
  assert.doesNotMatch(root.textContent, /0 views/);
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

test('AI suggestions require source disclosure and remain an unsaved draft', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close());
  w.confirm = () => true;
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const writes = [], state = {revision:0, activities:[], runs:[]};
  const suggestion = {title:'Suggested',kind:'poll',review_required:true,questions:[{type:'single',prompt:'Question?',
    options:['A','B'],correct:[],evidence:'Source text'}]};
  await w.EngagementDesk.mount(w.document.querySelector('#desk'), async () => state, async (path, body) => {
    writes.push({path,body}); return suggestion;
  }, {ai:true});
  const root = w.document.querySelector('#desk'), ai = root.querySelector('[data-ai-draft]');
  ai.querySelector('textarea').value = 'Source text';
  ai.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  assert.equal(writes.length, 0);
  ai.querySelector('[type=checkbox]').checked = true;
  ai.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes[0].path, '/api/desk/engagement-ai');
  assert.match(ai.textContent, /Source text/);
  [...ai.querySelectorAll('button')].find(button => button.textContent === 'Use suggestions').click();
  assert.equal(root.querySelector('[name=title]').value, 'Suggested');
  assert.equal(root.querySelector('[name=prompt]').value, 'Question?');
  assert.equal(writes.length, 1, 'AI output must not be saved or published automatically');
});

test('an organizer answer draft survives a screen refresh', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const state = {revision:1, activities:[], runs:[], qa:[{id:'q1',text:'Question?',status:'approved',audience:'guest'}]};
  const root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => state, async () => state);
  const answer = root.querySelector('[data-qa-list] textarea'); answer.value = 'My unfinished reply';
  answer.dispatchEvent(new w.Event('input', {bubbles:true}));
  [...root.querySelectorAll('button')].find(button => button.textContent === 'New activity').click();
  assert.equal(root.querySelector('[data-qa-list] textarea').value, 'My unfinished reply');
});

test('timer expiry updates run controls without discarding an editor draft', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  let state = {revision:4, activities:[], runs:[{id:'run',version:1,status:'open',
    delivery:{id:'d',status:'open'},snapshot:{title:'Quiz',questions:[{id:'q',prompt:'Question'}]}}]};
  const read = async () => state, root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, read, async () => state);
  root.querySelector('[name=title]').value = 'Unfinished';
  state = {...state, runs:[{...state.runs[0], delivery:{id:'d',status:'closed'}}]};
  await w.EngagementDesk.mount(root, read, async () => state);
  assert.equal(root.querySelector('[name=title]').value, 'Unfinished');
  assert.ok([...root.querySelectorAll('button')].some(button => button.textContent === 'Reveal answer'));
});

test('legacy inventory is explicitly read-only', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const root = w.document.querySelector('#desk'), calls = [];
  await w.EngagementDesk.mount(root, async path => {
    calls.push(path);
    return path.endsWith('legacy-preview') ? {quiz:{runs:2,answering_accounts:4,archives:1}, audience:{packs:3,draft_packs:1,answer_records:5}} : {revision:0,activities:[],runs:[]};
  }, async () => { throw new Error('Unexpected write'); });
  [...root.querySelectorAll('button')].find(button => button.textContent === 'Check legacy history').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(calls[1], '/api/desk/engagement/legacy-preview');
  assert.match(root.textContent, /2 legacy quiz runs/);
  assert.match(root.textContent, /No changes were made/);
});

test('copying a legacy draft requires confirmation and its preview digest', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close()); w.confirm = () => true;
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const root = w.document.querySelector('#desk'), writes = [], state = {revision:0,activities:[],runs:[]};
  await w.EngagementDesk.mount(root, async path => path.endsWith('legacy-preview') ?
    {quiz:{runs:0},audience:{packs:1,draft_packs:1,drafts:[{id:'a'.repeat(32),title:'Old poll',question_count:2,digest:'b'.repeat(64)}]}} : state,
    async (path, body) => { writes.push(body); return state; });
  [...root.querySelectorAll('button')].find(button => button.textContent === 'Check legacy history').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  [...root.querySelectorAll('button')].find(button => button.textContent === 'Copy draft').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes[0].action, 'copy_legacy_pack');
  assert.equal(writes[0].payload.source_digest, 'b'.repeat(64));
});

test('organizer can export aggregate analytics for one run', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close());
  let downloaded; w.KEV = {download: async path => { downloaded = path; }};
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const state = {revision:1, activities:[], runs:[{id:'a'.repeat(32),version:1,status:'open',snapshot:{title:'Poll',questions:[]}}]};
  const root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => state, async () => state);
  [...root.querySelectorAll('button')].find(button => button.textContent === 'Export run analytics').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(downloaded, '/api/desk/engagement/analytics.csv?run_id=' + 'a'.repeat(32));
});

test('an unpublished activity can be archived without deleting it', async t => {
  const dom = new JSDOM('<main id="desk"></main>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/organizer'});
  const w = dom.window; t.after(() => w.close()); w.confirm = () => true;
  w.eval(readFileSync(join(__dirname, '../static/engagement-desk.js'), 'utf8'));
  const state = {revision:1,runs:[],activities:[{id:'activity',draft:{id:'activity',title:'Draft',kind:'poll',questions:[
    {id:'question',type:'text',prompt:'Question?',options:[],correct:[]}]},versions:[]}]};
  const writes = [], root = w.document.querySelector('#desk');
  await w.EngagementDesk.mount(root, async () => state, async (path, body) => {writes.push(body);return state;});
  [...root.querySelectorAll('button')].find(button => button.textContent === 'Archive draft').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(writes[0].action, 'archive_draft');
  assert.equal(writes[0].payload.archive, true);
});
