const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {JSDOM} = require('jsdom');

const bundle = readFileSync(join(__dirname,'../static/event-quiz.js'),'utf8');
const path = '/api/participant/quiz';
const state = {enabled:true,joined:true,invited:false,busy:false,now:100,questions:[],history:[],
  score:{earned:0,available:0,pending:0}};
const first = {id:'warmup-1',qid:'PQ1',prompt:'What makes the pitch relevant?',kind:'warmup',points:0,seconds:30,end:130,
  status:'open',options:[{key:'A',text:'Customer needs'},{key:'B',text:'A feature list'}],session:'warmup',progress:{position:1,total:2}};
const second = {...first,id:'warmup-2',qid:'PQ2',prompt:'What should the next step be?',progress:{position:2,total:2}};

function fixture(t, initial, write) {
  const dom = new JSDOM('<main><input aria-label="Pitch notes"><dialog id="other"></dialog><div id="event-quiz-player"></div><div id="event-quiz-results"></div></main>',
    {runScripts:'outside-only',pretendToBeVisual:true});
  const w = dom.window;
  const root = w.document.querySelector('#event-quiz-player');
  const calls = [];
  const errors = [];
  let latest = initial, failRead = false, poll;
  w.HTMLDialogElement.prototype.showModal ??= function() {this.open=true;};
  w.HTMLDialogElement.prototype.close ??= function() {this.open=false;this.dispatchEvent(new w.Event('close'));};
  const setInterval=w.setInterval.bind(w);
  w.setInterval=(callback,ms,...args)=>{
    if (ms===1200) {poll=callback;return -ms;}
    return setInterval(callback,ms,...args);
  };
  w.KEV={async api(route,opts){
    assert.equal(route,path);
    if (opts?.body) {
      calls.push(JSON.parse(JSON.stringify(opts.body)));
      if (opts.body.action!=='view') return write?.(opts.body) ?? latest;
    } else if (failRead) throw new w.Error('Offline');
    return latest;
  }};
  w.addEventListener('error',event=>errors.push(event.error));
  w.eval(bundle);
  t.after(()=>{w.EventQuiz.clear();w.close();});
  async function until(check,label) {
    for (let i=0;i<70;i++) {
      if (errors.length) throw errors[0];
      const result=check();
      if (result) return result;
      await new Promise(resolve=>setTimeout(resolve,10));
    }
    assert.fail('Timed out waiting for '+label);
  }
  const button=(label,scope=root)=>[...scope.querySelectorAll('button')].find(item=>item.textContent.trim()===label);
  async function click(label,scope=root) {
    (await until(()=>{const item=button(label,scope);return item&&!item.disabled&&item;},label)).click();
  }
  const show=code=>w.EventQuiz.participant({workspace:{},action:{code}});
  const dialog=()=>root.querySelector('dialog[open]');
  return {w,root,calls,show,dialog,until,button,click,sync(value) {latest=value;return poll();},
    offline(value) {failRead=value;return poll();}};
}

test('a new question appears inline, keeps focus, and advances directly after a saved answer',async t=>{
  const initial={...state,questions:[first]};
  const saved={...state,questions:[second],history:[{...first,selected:'A',result_state:'awaiting_post_test'}]};
  const f=fixture(t,initial,()=>saved);
  const notes=f.w.document.querySelector('input');
  notes.focus();
  f.show('');
  await f.until(()=>f.root.querySelector('legend')?.textContent===first.prompt,'first inline prompt');
  assert.equal(f.dialog(),null);
  assert.equal(f.w.document.activeElement,notes);
  f.root.querySelector('input[type=radio]').click();
  await f.click('Submit answer');
  await f.until(()=>f.root.querySelector('legend')?.textContent===second.prompt,'next question');
  assert.match(f.root.textContent,/Answer saved/);
  assert.equal(f.button('Continue'),undefined);
  assert.equal(f.dialog(),null);
  assert.deepEqual(f.calls.filter(body=>body.action==='answer'),[{action:'answer',id:first.id,key:'A'}]);
});

test('a lost answer confirmation reconciles from saved history without sending again',async t=>{
  const f=fixture(t,{...state,questions:[first]},()=>{throw new Error('Connection interrupted');});
  f.show('');
  const choice=await f.until(()=>f.root.querySelector('input[type=radio]'),'first choice');
  choice.click();
  await f.click('Submit answer');
  await f.until(()=>/Checking whether your answer was saved/.test(f.root.textContent),'uncertain receipt');
  assert.equal(choice.checked,true);
  assert.equal(f.button('Submit answer').disabled,true);
  await f.sync({...state,history:[{...first,selected:'A',result_state:'awaiting_post_test'}]});
  await f.until(()=>/Answer saved/.test(f.root.textContent),'confirmed receipt');
  assert.equal(f.calls.filter(body=>body.action==='answer').length,1);
});

test('results stay in the optional history and unreleased answer keys stay hidden',async t=>{
  const history=[{...first,selected:'A',result_state:'awaiting_post_test',correct:['B'],explanation:'Private answer explanation'}];
  const f=fixture(t,{...state,history});
  f.show('');
  const results=f.w.document.querySelector('#event-quiz-results');
  const details=await f.until(()=>results.querySelector('details'),'results accordion');
  assert.equal(details.open,false);
  assert.match(details.textContent,/Practice results appear after/);
  assert.doesNotMatch(details.textContent,/Private answer explanation/);
  await f.sync({...state,now:200});
  await f.until(()=>!results.querySelector('.quiz-history li'),'old results clear');
});

test('a new question can expire without opening a dialog or displaying a receipt',async t=>{
  const f=fixture(t,{...state,questions:[first]});
  f.show('');
  await f.until(()=>f.root.querySelector('legend'),'question');
  await f.sync({...state,now:131});
  await f.until(()=>!f.root.querySelector('legend'),'expired card');
  assert.doesNotMatch(f.root.textContent,/Answer saved/);
});
