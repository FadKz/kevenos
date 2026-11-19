const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

test('operator sends published questions and retries the same uncertain request', async t => {
  const dom = new JSDOM('<p id="operator-status"></p><button id="operator-retry" hidden>Retry</button><main id="operator-content"></main>',
    {runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close()); w.confirm = () => true;
  const sent = [];
  const state = {revision:3, quiz_enabled:true, activities:[], runs:[{id:'run',status:'open',respondents:0,
    snapshot:{title:'Published',kind:'poll',questions:[{id:'q',prompt:'Choose?'}]}}]};
  let fail = true, onEnd;
  w.KEV = {profile:{mount(hooks) { onEnd = hooks.onEnd; }}, async api(path, opts) {
    if (!opts) return state;
    sent.push(opts.body); if (fail) { fail = false; throw new Error('Disconnected'); }
    return {...state,revision:4};
  }};
  w.eval(readFileSync(join(__dirname,'../static/engagement-operator.js'),'utf8'));
  await new Promise(resolve => w.setTimeout(resolve, 0));
  const root = w.document.querySelector('#operator-content');
  [...root.querySelectorAll('button')].find(item => item.textContent === 'Send: Choose?').click();
  await new Promise(resolve => w.setTimeout(resolve, 0));
  assert.equal(sent[0].action, 'open_question');
  assert.equal(sent[0].payload.duration, 15);
  assert.equal(w.document.querySelector('#operator-retry').hidden, false);
  [...root.querySelectorAll('button')].find(item => item.textContent === 'Send: Choose?').click();
  assert.equal(sent.length, 1);
  w.document.querySelector('#operator-retry').click();
  await new Promise(resolve => w.setTimeout(resolve, 0));
  assert.equal(sent.length, 2);
  assert.equal(sent[0].request_id, sent[1].request_id);
  assert.equal(w.document.querySelector('#operator-retry').hidden, true);
  onEnd(); assert.equal(root.textContent, '');
});
