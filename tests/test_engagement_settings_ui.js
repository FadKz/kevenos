const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

test('Admin can disable quiz launches without disabling polls', async t => {
  const dom = new JSDOM('<section id="settings"></section>', {runScripts:'outside-only',pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close()); w.confirm = () => true;
  w.eval(readFileSync(join(__dirname,'../static/engagement-settings.js'),'utf8'));
  let state = {revision:0,quiz_enabled:true}, sent;
  const root = w.document.querySelector('#settings');
  await w.EngagementSettings.mount(root, async () => state, async (path,body) => {
    sent = body; state = {revision:1,quiz_enabled:false}; return state;
  });
  assert.match(root.textContent, /Quizzes are enabled/);
  root.querySelector('button').click();
  await new Promise(resolve => setTimeout(resolve,0));
  assert.equal(sent.action,'set_quiz_enabled'); assert.equal(sent.payload.enabled,false);
  assert.match(root.textContent,/Quizzes are disabled/);
  assert.match(root.textContent,/Polls and audience questions/);
});

test('uncertain setting writes retry the original request ID', async t => {
  const dom = new JSDOM('<section id="settings"></section>', {runScripts:'outside-only',pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close()); w.confirm = () => true;
  w.eval(readFileSync(join(__dirname,'../static/engagement-settings.js'),'utf8'));
  const root = w.document.querySelector('#settings'), writes = [];
  await w.EngagementSettings.mount(root, async () => ({revision:0,quiz_enabled:true}), async (path,body) => {
    writes.push(body); if (writes.length === 1) throw new Error('Connection lost');
    return {revision:1,quiz_enabled:false};
  });
  root.querySelector('button').click(); await new Promise(resolve => setTimeout(resolve,0));
  root.querySelector('button').click(); await new Promise(resolve => setTimeout(resolve,0));
  assert.equal(writes.length,2); assert.equal(writes[0].request_id,writes[1].request_id);
});

test('Admin can refresh after a conflicting setting change', async t => {
  const dom = new JSDOM('<section id="settings"></section>', {runScripts:'outside-only',pretendToBeVisual:true});
  const w = dom.window; t.after(() => w.close()); w.confirm = () => true;
  w.eval(readFileSync(join(__dirname,'../static/engagement-settings.js'),'utf8'));
  let reads = 0; const root = w.document.querySelector('#settings');
  await w.EngagementSettings.mount(root, async () => ({revision:reads++,quiz_enabled:true}), async () => {
    throw Object.assign(new Error('Activity changed. Refresh status.'), {status:409});
  });
  root.querySelector('button').click(); await new Promise(resolve => setTimeout(resolve,0));
  assert.match(root.textContent,/Activity changed/);
  [...root.querySelectorAll('button')].find(button => button.textContent === 'Refresh status').click();
  await new Promise(resolve => setTimeout(resolve,0));
  assert.match(root.textContent,/Status refreshed/); assert.equal(reads,2);
});
