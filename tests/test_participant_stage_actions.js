const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {JSDOM} = require('jsdom');

test('Today offers only the action for the current event stage', t => {
  const html = readFileSync('static/participant.html', 'utf8');
  const dom = new JSDOM(html, {runScripts:'outside-only'});
  t.after(() => dom.window.close());
  const w = dom.window;
  w.KEV = {profile:{isPreview:false}, esc:String, mmss:String};
  w.eval(readFileSync('static/participant-workspace.js','utf8'));

  const $ = id => w.document.getElementById(id);
  const agenda = JSON.parse(readFileSync('content.json','utf8')).agenda;
  const team = {team:1, steps:{}, news:[], nomination:{open:false,companies:[],revision:0}};
  const clock = {kind:'round',phase:'pitch',phase_left:600,started_any:true,running:true,ended:false,step:0};
  const render = (scene, code, stageClock=null, workspace=team, result={kind:'round',status:'awaiting'}) =>
    w.ParticipantWorkspace.receive({workspace,program:{scene},action:{code,clock:stageClock},result});

  for (const stage of agenda) {
    render(stage.scene,'program');
    const shouldPrepare = stage.scene === 'prep';
    assert.equal($('open-team-pitch').hidden,!shouldPrepare,stage.title);
    if (shouldPrepare) assert.equal($('open-team-pitch').textContent,'Prepare my pitch');
  }

  for (const code of ['ready','pitch','paused','final_ready','final_pitch','final_paused']) {
    render(code.startsWith('final_')?'final':'pitch',code,clock);
    assert.equal($('open-team-pitch').hidden,false,code);
    assert.equal($('open-team-pitch').textContent,'Open Pitch Mode',code);
  }
  for (const code of ['upcoming','feedback','finished','submitted','final_watch','final_upcoming','final_feedback','final_review']) {
    render(code.startsWith('final_')?'final':'pitch',code,clock);
    assert.equal($('open-team-pitch').hidden,true,code);
  }

  render('awards','program',null,{...team,nomination:{...team.nomination,open:true}},{kind:'final',status:'released'});
  assert.equal($('open-team-results').hidden,false);
  assert.equal($('open-team-nominations').hidden,false);
  assert.equal($('open-team-pitch').hidden,true);
});
