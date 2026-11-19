const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

test('an attendee submits a question and sees that it awaits review', async t => {
  const dom = new JSDOM('<div id="questions"></div>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/'});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-questions.js'), 'utf8'));
  let sent, data = {enabled:true, questions:[], own:[]};
  const api = async (path, options) => {
    assert.equal(path, '/api/engagement/questions');
    if (options?.body) { sent = options.body; data = {...data, own:[{text:sent.text,status:'pending'}]}; return {receipt:{id:'q'}}; }
    return data;
  };
  const root = w.document.querySelector('#questions');
  await w.EngagementQuestions.mount(root, api);
  root.querySelector('textarea').value = 'Could you explain the next step?';
  root.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(sent.text, 'Could you explain the next step?');
  assert.match(sent.request_id, /^[0-9a-f]{32}$/);
  assert.match(root.textContent, /awaiting review/i);
  w.EngagementQuestions.clear(); assert.equal(root.textContent, '');
});

test('an attendee can support an approved question', async t => {
  const dom = new JSDOM('<div id="questions"></div>', {runScripts:'outside-only', pretendToBeVisual:true, url:'https://example.test/'});
  const w = dom.window; t.after(() => w.close());
  w.eval(readFileSync(join(__dirname, '../static/engagement-questions.js'), 'utf8'));
  let sent, data = {enabled:true, own:[], questions:[{id:'q1', text:'How does it work?', votes:0, upvoted:false}]};
  const api = async (path, options) => {
    if (options?.body) { sent = options.body; data = {...data, questions:[{...data.questions[0], votes:1, upvoted:true}]}; return {receipt:{id:'q1'}}; }
    return data;
  };
  const root = w.document.querySelector('#questions');
  await w.EngagementQuestions.mount(root, api);
  [...root.querySelectorAll('button')].find(button => button.textContent.includes('Support')).click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(JSON.parse(JSON.stringify(sent)), {question_id:'q1', upvote:true});
  assert.match(root.textContent, /1 support/);
});
