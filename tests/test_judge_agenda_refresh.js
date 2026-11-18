const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '..');
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));

async function judgePage() {
  const html = fs.readFileSync(path.join(root, 'static/judge.html'), 'utf8');
  const dom = new JSDOM(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '').replace(/<body(?=[ >])/, '<body data-scoped="1"'),
    {url:'https://example.test/judge/1', runScripts:'outside-only', pretendToBeVisual:true});
  const w = dom.window, doc = w.document;
  let content = JSON.parse(fs.readFileSync(path.join(root, 'content.json'), 'utf8')), ai;
  const state = {assignment:{kind:'round', pair:1, round:1, info:{partner:'Synthetic team'}}, scenario:content.scenarios[0].id,
    clock:{kind:'round', elapsed:135, running:false, started_any:true, phase:'pitch', step:1}, mode:'global', submitted:false};
  w.setInterval = () => 1;
  w.scrollTo = () => {};
  w.HTMLDialogElement.prototype.show = w.HTMLDialogElement.prototype.showModal = function () {this.open = true;};
  w.HTMLDialogElement.prototype.close = function () {this.open = false;};
  w.KEV = {context:'synthetic-event', workspaceMode:'rehearsal', workspaceChanging:false,
    esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    nav:{render(){}}, tokenKey:()=> 'unused', stamp(){}, members:()=> '', mmss:seconds=>String(seconds),
    focusedControl:()=>()=>{}, profile:{active:true, context:'synthetic-profile', revision:1, isPreview:false,
      mount(){}, previewControls(){}, session:async()=>({authenticated:true, role:'judge', station:1, label:'Synthetic Judge', draft_key:'synthetic-judge'})},
    api:async url=>{
      if (url === '/api/content') return clone(content);
      if (url === '/api/sync') return {...clone(state), versions:{content:content.content_version}};
      if (url === '/api/ai/tidy') return new Promise(resolve=>{ai=resolve;});
      throw Error('Unexpected API request: '+url);
    }};
  const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join('\n');
  w.eval(source+'\nwindow.judgeTest = {sync, tidyNotes, get draft(){return D;}, get key(){return dkey;}, get view(){return view;}, get follow(){return follow;}, get content(){return C;}};');
  await tick();await tick();
  assert.equal(doc.getElementById('offline').hidden, true);
  assert(w.judgeTest.draft, 'Initial judging state should load');
  return {w, doc, test:w.judgeTest, close:()=>w.close(), get content(){return content;}, set content(value){content=value;}, finishAI:value=>ai(value),
    $:id=>doc.getElementById(id), async sync(){await w.judgeTest.sync();assert(doc.getElementById('offline').hidden);}};
}

function enterDraft(p) {
  p.doc.querySelector('#view .tick').click();
  p.doc.querySelector('#steps [data-s="4"]').click();
  const feedback = p.$('fb');feedback.value='Unsent feedback';feedback.dispatchEvent(new p.w.Event('input'));
  p.$('bnote').click();
  const notes = p.$('notes');notes.value='Unsent notes';notes.dispatchEvent(new p.w.Event('input'));
  notes.focus();notes.setSelectionRange(2, 7);
  return {draft:p.test.draft, key:p.test.key, notes, feedback, saved:p.w.localStorage.getItem([...Object.keys(p.w.localStorage)].find(key=>key.includes('workspace-draft')))};
}

function retime(p) {
  // Key order and save provenance are not judging content.
  p.content = Object.fromEntries(Object.entries(clone(p.content)).reverse());
  p.content.agenda[0].time='09:45';p.content.agenda[0].min+=15;
  p.content.content_version++;p.content.content_saved=1794570000;p.content.content_note='Timing adjustment';p.content._source='edited';
}

test('Agenda-only update retains draft identity, unsent scoring, view, panels and input focus', async () => {
  const p = await judgePage();
  try {
    const before=enterDraft(p);retime(p);await p.sync();
    assert.equal(p.test.draft, before.draft, 'Timing changes must retain the in-memory draft identity');
    assert.equal(p.test.key, before.key);assert.equal(p.test.view,4);assert.equal(p.test.follow,false);
    assert(p.$('sheet').open);assert.equal(p.$('notes'),before.notes);assert.equal(p.$('fb'),before.feedback);
    assert.equal(p.doc.activeElement,before.notes);assert.equal(before.notes.selectionStart,2);assert.equal(before.notes.selectionEnd,7);
    assert.equal(p.test.draft.notes,'Unsent notes');assert.equal(p.test.draft.feedback,'Unsent feedback');
    assert.equal(p.test.draft.ticks.filter(Boolean).length,1);assert.equal(p.test.draft.sub,false);
    assert.equal(p.test.content.agenda[0].time,'09:45');assert.equal(p.test.content.content_version,p.content.content_version);
    assert.equal(p.w.localStorage.getItem([...Object.keys(p.w.localStorage)].find(key=>key.includes('workspace-draft'))),before.saved);
  } finally {p.close();}
});

test('In-flight notes assistance remains attached to the same draft after agenda retiming', async () => {
  const p = await judgePage();
  try {
    enterDraft(p);const pending=p.test.tidyNotes();await tick();retime(p);await p.sync();
    p.finishAI({source:'ai',text:'Tidied synthetic notes'});await pending;
    assert.equal(p.$('notes')?.value,'Tidied synthetic notes');assert.equal(p.test.draft.notes,'Tidied synthetic notes');
  } finally {p.close();}
});

for (const button of ['bhelp', 'bcustomer']) test(button+' reference and criterion disclosure remain open after retiming', async () => {
  const p = await judgePage();
  try {
    const criterion=p.doc.querySelector('#view [data-info]');criterion.click();
    const detail=p.$(criterion.getAttribute('aria-controls')), view=p.test.view, follow=p.test.follow;
    p.$(button).click();const body=p.$('lbody'), close=p.$('sx');close.focus();
    retime(p);await p.sync();
    assert(p.$('sheet').open);assert.equal(p.$('lbody'),body);assert.equal(p.doc.activeElement,close);
    assert.equal(p.$(button).getAttribute('aria-expanded'),'true');assert.equal(detail.hidden,false);
    assert.equal(p.test.view,view);assert.equal(p.test.follow,follow);
  } finally {p.close();}
});

for (const [section, edit] of [
  ['rubric', c=>{c.rubric[0].ticks[0].short='Updated judging criterion';}],
  ['questions', c=>{c.questions[0].items[0].text='Updated customer question';}],
  ['scenarios', c=>{c.scenarios[0].name='Updated customer scenario';}],
  ['judge_rules', c=>{c.judge_rules[0]='Updated judging rule';}],
  ['event', c=>{c.event.name='Updated event';}],
  ['unknown content', c=>{c.new_judging_rule='New substantive field';}]
]) test(section+' changes retain the existing full judging refresh', async () => {
  const p = await judgePage();
  try {
    const before=enterDraft(p);retime(p);edit(p.content);await p.sync();
    assert.notEqual(p.test.draft,before.draft);assert.equal(p.$('sheet').open,false);assert.notEqual(p.test.view,4);
    assert.equal(p.test.draft.notes,'Unsent notes');assert.equal(p.test.draft.feedback,'Unsent feedback');
    assert.equal(p.test.draft.ticks.filter(Boolean).length,1);
  } finally {p.close();}
});
