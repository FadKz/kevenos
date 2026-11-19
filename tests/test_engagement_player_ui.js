const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

test('live question appears and sends the selected answer once', async t => {
  const dom = new JSDOM('<main><textarea aria-label="Pitch notes">draft</textarea><div id="engagement-player"></div></main>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window;
  t.after(() => w.close());
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  const calls = [];
  const question = {id:'run', kind:'quiz', title:'Knowledge check', delivery:{id:'delivery', status:'open', ends_at:115},
    questions:[{id:'q1', type:'single', prompt:'Choose one', options:['First', 'Second']}]};
  let state = {question, answered:false, now:100};
  const api = async (path, options) => {
    assert.equal(path, '/api/engagement/player');
    if (options?.body) { calls.push(options.body); state = {...state, answered:true}; return {receipt:{id:'receipt'}}; }
    return state;
  };
  w.eval(readFileSync(join(__dirname, '../static/engagement-player.js'), 'utf8'));
  await w.EngagementPlayer.refresh(api);
  const root = w.document.querySelector('#engagement-player');
  assert.ok(root.querySelector('dialog[open]'));
  assert.match(root.textContent, /Choose one/);
  root.querySelectorAll('input[type=radio]')[1].click();
  root.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [{run_id:'run', delivery_id:'delivery', choice:1}]);
  assert.match(root.textContent, /Answer saved/);
  assert.equal(root.querySelectorAll('input[type=radio]')[1].checked, true);
  assert.equal(w.document.querySelector('textarea').value, 'draft');
  w.EngagementPlayer.clear();
  assert.equal(root.textContent, '');
});

test('unavailable feature is silent and cleared requests cannot restore content', async t => {
  const dom = new JSDOM('<div id="engagement-player"></div>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window;
  t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-player.js'), 'utf8'));
  await w.EngagementPlayer.refresh(async () => { throw Object.assign(new Error('Unavailable'), {status:404}); });
  assert.equal(w.document.querySelector('#engagement-player').textContent, '');
  let finish;
  const pending = w.EngagementPlayer.refresh(() => new Promise(resolve => { finish = resolve; }));
  w.EngagementPlayer.clear();
  finish({question:{title:'Private'}, answered:false});
  await pending;
  assert.equal(w.document.querySelector('#engagement-player').textContent, '');
});

test('a new question does not interrupt an attendee editing notes', async t => {
  const dom = new JSDOM('<textarea>draft</textarea><div id="engagement-player"></div>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window;
  t.after(() => w.close());
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.document.querySelector('textarea').focus();
  w.eval(readFileSync(join(__dirname, '../static/engagement-player.js'), 'utf8'));
  await w.EngagementPlayer.refresh(async () => ({now:100, answered:false, question:{id:'run', kind:'poll', title:'Poll',
    delivery:{id:'delivery', status:'open', ends_at:null}, questions:[{type:'single', prompt:'Question?', options:['A','B']}]}}));
  assert.equal(w.document.querySelector('dialog[open]'), null);
  assert.equal(w.document.activeElement, w.document.querySelector('textarea'));
  const button = w.document.querySelector('.engagement-reopen');
  assert.equal(button.hidden, false);
  button.click();
  assert.ok(w.document.querySelector('dialog[open]'));
});

test('a closed question is not presented as a new live prompt', async t => {
  const dom = new JSDOM('<div id="engagement-player"></div>', {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close());
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.eval(readFileSync(join(__dirname, '../static/engagement-player.js'), 'utf8'));
  await w.EngagementPlayer.refresh(async () => ({now:200, answered:false, question:{id:'run',kind:'poll',title:'Past',
    delivery:{id:'delivery',status:'open',ends_at:150},questions:[{type:'single',prompt:'Past question?',options:['A','B']}]}}));
  assert.equal(w.document.querySelector('dialog[open]'), null);
});
