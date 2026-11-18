const assert = require('node:assert/strict');
const fs = require('node:fs');
const {mkdtemp, mkdir, writeFile, rm} = require('node:fs/promises');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {createServer} = require('node:net');
const {spawn} = require('node:child_process');
const {chromium, webkit, devices} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root=path.resolve(__dirname,'..');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const playerPath='/api/participant/quiz';
const deskPath='/api/desk/quiz';
const output=process.env.KEVENOS_SHOTS;
async function screenshot(page,name) {
  if (output) await page.screenshot({path:path.join(output,name+'.png'),animations:'disabled',fullPage:name==='13-desk-analytics'});
}
async function port() {
  return new Promise(resolve=>{
    const socket=createServer().listen(0,'127.0.0.1',()=>{const value=socket.address().port;socket.close(()=>resolve(value));});
  });
}
async function until(check,label,limit=20000) {
  const stop=Date.now()+limit;
  while(Date.now()<stop) {if(await check()) return;await delay(150);}
  throw new Error('Timed out: '+label);
}

(async()=>{
  const data=await mkdtemp(path.join(tmpdir(),'pitch-quiz-rehearsal-'));
  const secrets=path.join(data,'secrets');
  await mkdir(secrets,{mode:0o700});
  if(output) await mkdir(output,{recursive:true});
  const adminPassword='Synthetic-Quiz-Admin9!';
  const organizerPassword='Synthetic-Quiz-Desk9!';
  for(const [key,value] of Object.entries({KEVENOS_PASSWORD:adminPassword,KEVENOS_GUEST_PASSWORD:'Synthetic-Quiz-Guest9!',
    KEVENOS_DESK_PIN:'861903',KEVENOS_VIEW_KEY:'synthetic-quiz-screen',KEVENOS_SESSION_SECRET:'synthetic-quiz-session-key'})) {
    await writeFile(path.join(secrets,key),value,{mode:0o600});
  }
  const address='http://localhost:'+await port();
  const app=spawn(process.env.KEVENOS_TEST_PYTHON||'python3',['server.py','--host','127.0.0.1','--port',address.split(':').at(-1),'--mode','self'],
    {cwd:root,env:{...process.env,KEVENOS_DATA:data,KEVENOS_HOSTED:'1',KEVENOS_GATEWAY_ENABLED:'0',KEVENOS_SMTP_ENABLED:'0',KEVENOS_ASSISTANT_ENABLED:'0',
      KEVENOS_SECRETS_DIR:secrets,TZ:'UTC'},stdio:['ignore','ignore','inherit']});
  let browser;
  try {
    await until(()=>fetch(address+'/healthz').then(r=>r.ok).catch(()=>false),'rehearsal starts');
    browser=process.env.KEVENOS_TEST_BROWSER==='webkit' ? await webkit.launch() : await chromium.launch({channel:process.env.KEVENOS_CHROME_CHANNEL||'chrome',args:['--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding','--disable-background-timer-throttling']});
    const errors=[];
    async function session(role,password,screen) {
      const context=await browser.newContext(process.env.KEVENOS_TEST_BROWSER==='webkit' && screen ? {...screen,isMobile:true} : screen);
      context.on('response',response=>{if(response.status()>=400 && response.url().includes('/static/')) errors.push('Static '+response.status()+' '+new URL(response.url()).pathname);});
      context.on('requestfailed',req=>{if(req.url().includes('/static/')) errors.push('Static '+new URL(req.url()).pathname+' '+req.failure()?.errorText);});
      const login=await context.request.post(address+'/api/access/login',{data:{role,password}});
      assert(login.ok(),role+' signs in');
      if(process.env.KEVENOS_TEST_BROWSER==='webkit') await context.addCookies((await context.cookies()).map(c=>({...c,secure:false}))); // Disposable HTTP fixture only.
      const headers={};
      const sync=response=>{for(const key of ['x-event-context','x-access-context']) if(response.headers()[key])headers[key]=response.headers()[key];};
      sync(login);
      const page=await context.newPage();
      page.on('pageerror',e=>errors.push(e.message));
      const api=async(route,body,code=200)=>{
        const response=await context.request.fetch(address+route,{method:body?'POST':'GET',data:body,headers});
        assert.equal(response.status(),code,route+' '+await response.text());
        sync(response);
        return response.json();
      };
      return {page,context,api};
    }
    const owner=await session('admin',adminPassword);
    await owner.api('/api/admin/credentials',{action:'create',role:'desk',label:'Rehearsal Organizer',password:organizerPassword});
    await owner.api('/api/admin/workspace',{mode:'rehearsal',confirm:'REHEARSAL'});
    const roster=(await owner.api('/api/desk/roster')).roster;
    const used=(await owner.api('/api/desk/scores')).leaderboard;
    for(const row of used) await owner.api('/api/desk/sheet',{action:'void',id:row.sheet,why:'Clear seeded sheets for disposable quiz rehearsal'});
    roster[0].members[0].name='Alex Example';
    roster[1].members[0].name='Sam Example';
    await owner.api('/api/desk/roster',{rows:roster});
    await owner.api('/api/desk/mode',{mode:'self'});
    const seed=async(role,label,fields)=>{
      const password='Synthetic-Quiz-'+label+'9!';
      await owner.api('/api/admin/credentials',{action:'create',role,label,password,...fields});
      return password;
    };
    const first=await session('participant',await seed('participant','Alex',{pair:1,member:1}),devices['mobile device 13']);
    const second=await session('participant',await seed('participant','Sam',{pair:2,member:1}),devices['Pixel 7']);
    const assignment=(await first.api('/api/participant/state')).participant;
    const station=assignment.station;
    await owner.api('/api/desk/clock',{action:'goto',station,round:assignment.round});
    const judge=await session('judge',await seed('judge','Judge',{station}),{viewport:{width:1024,height:768}});
    const desk=await session('desk',organizerPassword,{viewport:{width:1440,height:1000}});
    const hall=await browser.newContext({viewport:{width:1920,height:1080}, reducedMotion:'reduce'});
    const tv=await hall.newPage();
    const presenter=(await desk.api('/api/desk/state')).view_key;
    await first.page.goto(address+'/participant');
    await first.page.locator('#team-navigation').waitFor();
    await second.page.goto(address+'/participant');
    await second.page.locator('#team-navigation').waitFor();
    await judge.page.goto(address+'/judge/'+station+'#scoring');
    await desk.page.goto(address+'/desk#quiz');
    await tv.goto(address+'/live?key='+presenter);
    const {page}=desk;
    const button=(tab,label)=>tab.getByRole('button',{name:label,exact:true});
    const firstSheet=first.page.getByRole('dialog',{name:'Event quiz'});
    const secondSheet=second.page.getByRole('dialog',{name:'Event quiz'});
    const setting=page.getByLabel('Enable event quiz');
    const get=()=>desk.api(deskPath);
    const mutate=async(action,fields={},code=200)=>desk.api(deskPath,{action,rev:(await get()).rev,...fields},code);
    const programme=async(index)=>{
      await desk.api('/api/desk/program',{action:'set',index});
      await until(async()=>(await desk.api('/api/desk/state')).program.i===index,'agenda step '+index);
    };
    const choose=async(sheet,answer)=>{
      await sheet.getByText(answer,{exact:true}).click();
      await button(sheet,'Submit answer').click();
      await sheet.getByRole('heading',{name:'Answer saved',exact:true}).waitFor();
    };
    const close=sheet=>button(sheet,'Close').click();
    await page.bringToFront();
    console.log('Signed in to isolated rehearsal roles.');
    try {await setting.waitFor({timeout:6000});} catch(e) {console.log(await page.locator('body').innerText(),errors); throw e;}
    assert.equal(await setting.isChecked(),false);
    assert.deepEqual(await first.api(playerPath),{enabled:false});
    await judge.api(playerPath,undefined,403);
    await setting.click();
    await until(async()=>(await get()).enabled,'enable setting saves');
    await button(page,'Send opt-in').click();
    await until(async()=>(await first.api(playerPath)).invited,'invite command saves');
    for(const phone of [first,second]) {
      await phone.page.bringToFront();
      const sheet=phone.page.getByRole('dialog',{name:'Event quiz'});
      try {await sheet.waitFor({timeout:6000});}
      catch(error) {
        console.log({quiz:await phone.api(playerPath), page:(await phone.page.locator('body').innerText()).slice(0,700), errors});
        throw error;
      }
    }
    await screenshot(first.page,'01-participant-opt-in');
    await button(firstSheet,'Yes, count me in').click();
    await button(secondSheet,'Not now').click();
    await secondSheet.waitFor({state:'hidden'});
    await close(firstSheet);
    await second.page.getByRole('button',{name:'Join the quiz',exact:true}).click();
    await button(secondSheet,'Yes, count me in').click();
    await close(secondSheet);
    await page.bringToFront();
    await button(page,'Open warm-up').click();
    await until(async()=>(await first.api(playerPath)).questions[0]?.progress?.total===5,'warm-up open');
    const warm=(await first.api(playerPath)).questions[0];
    assert(!('correct' in warm));
    const warmNext=(await get()).runs.filter(q=>q.kind==='warmup')[1];
    await first.page.bringToFront();
    await firstSheet.getByText(warm.prompt,{exact:true}).waitFor({timeout:6000});
    await firstSheet.getByText('Practice round · No points',{exact:true}).waitFor();
    await firstSheet.getByText('Question 1 of 5',{exact:true}).waitFor();
    assert.equal(await firstSheet.locator('.quiz-score').count(),0);
    assert.equal(await firstSheet.getByRole('radio').count(),warm.options.length);
    assert(await firstSheet.evaluate(node=>Math.abs(node.getBoundingClientRect().width-innerWidth)<2));
    assert(await firstSheet.evaluate(node=>Math.abs(node.getBoundingClientRect().height-innerHeight)<2));
    await screenshot(first.page,'11-warm-up-fullscreen');
    await choose(firstSheet,warm.options[1].text);
    await button(firstSheet,'Next question').click();
    await firstSheet.getByText(warmNext.prompt,{exact:true}).waitFor();
    await firstSheet.getByText('Question 2 of 5',{exact:true}).waitFor();
    await first.page.keyboard.press('Escape');
    await firstSheet.waitFor({state:'hidden'});
    await first.page.waitForTimeout(1500);
    assert.equal(await firstSheet.isVisible(),false);
    await first.page.locator('.quiz-badge').click();
    await firstSheet.getByText(warmNext.prompt,{exact:true}).waitFor();
    const warmRuns=(await get()).runs.filter(q=>q.kind==='warmup');
    for(const [index,q] of warmRuns.slice(1).entries()) {
      await firstSheet.getByText(q.prompt,{exact:true}).waitFor();
      await choose(firstSheet,q.options[0].text);
      await button(firstSheet,index===warmRuns.length-2?'Continue':'Next question').click();
    }
    await firstSheet.getByRole('heading',{name:'No question is open right now',exact:true}).waitFor();
    assert.equal(await firstSheet.getByRole('radio').count(),0);
    assert.equal(await firstSheet.getByRole('button',{name:/Next question|Next available question/i}).count(),0);
    await screenshot(first.page,'12-warm-up-waiting');
    await close(firstSheet);
    await mutate('close_warmup');
    assert(!('correct' in (await first.api(playerPath)).history.find(r=>r.id===warm.id)));
    await programme(1);
    await page.bringToFront();
    await button(page,'Next').click();
    await button(page,'Send W2 now').waitFor();
    await screenshot(page,'02-desk-next');
    await mutate('edit',{qid:'W2',changes:{prompt:'How long is your station pitch?',seconds:10}});
    await page.getByText('How long is your station pitch?',{exact:true}).waitFor();
    const w1=page.locator('.quiz-bank-row').filter({has:button(page,'Send W1 now')});
    await button(w1,'Delay cue 1 min').click();
    await until(async()=>(await get()).rev >= 6,'delay saved');
    await button(w1,'Skip').click();
    await button(page,'Send W1 now').waitFor({state:'hidden',timeout:6000});
    assert.equal((await first.api(playerPath)).score.available,0);
    await page.getByText('How long is your station pitch?',{exact:true}).waitFor();
    await button(page,'Send W2 now').click();
    await first.page.bringToFront();
    await until(async()=>(await get()).runs.some(r=>r.qid==='W2'&&r.status==='open'),'Desk sends W2');
    try {await firstSheet.getByText('How long is your station pitch?').waitFor({timeout:6000});}
    catch(error){console.log({data:await first.api(playerPath), body:(await first.page.locator('body').innerText()).slice(0,650),errors});throw error;}
    await second.page.bringToFront();
    await secondSheet.getByText('How long is your station pitch?').waitFor();
    const live=(await first.api(playerPath)).questions.find(q=>q.qid==='W2');

    await screenshot(second.page,'03-participant-live-question');
    await mutate('show',{id:live.id});
    assert.equal((await get()).hall_visible,true);
    await tv.bringToFront();
    await tv.getByRole('heading',{name:'How long is your station pitch?'}).waitFor();
    assert.equal(await tv.locator('.hall-quiz progress').count(),0);
    await first.page.bringToFront();
    await choose(firstSheet,'10');
    await first.api(playerPath,{action:'answer',id:live.id,key:'B'});
    await first.api(playerPath,{action:'answer',id:live.id,key:'A'},409);
    await second.page.bringToFront();
    await choose(secondSheet,'5');
    await mutate('close',{id:live.id});
    await tv.bringToFront();
    await until(async()=>(await get()).runs.find(run=>run.id===live.id)?.room_results_ready,'quiz room results ready');
    await mutate('show',{id:live.id,results:true});
    await tv.locator('.hall-quiz progress').first().waitFor({timeout:20000});
    await screenshot(tv,'04-hall-answer-bars');
    assert.equal((await first.api(playerPath)).score.earned,1);
    await first.page.bringToFront();
    await close(firstSheet);
    await second.page.bringToFront();
    await close(secondSheet);
    console.log('Registration, live question and hall reveal passed.');
    await programme(6);
    assert.equal((await get()).hall_visible,false);
    await judge.page.locator('#ctl [data-a=start]').click();
    await until(async()=>(await first.api(playerPath)).busy,'judge clock suppresses quiz for pitching team');
    await mutate('send',{qid:'R1a'});
    await mutate('send',{qid:'R2a'});
    const rounds=(await get()).runs.filter(q=>q.kind==='waiting');
    assert.equal(rounds.length,2);
    assert.equal((await second.api(playerPath)).questions.length,1);
    assert.equal((await first.api(playerPath)).questions.length,0);
    await first.api(playerPath,{action:'answer',id:rounds[0].id,key:'B'},409);
    assert.equal(await firstSheet.isVisible(),false);
    await screenshot(judge.page,'05-judge-pitch-active');
    await second.page.bringToFront();
    await secondSheet.getByText(rounds[0].prompt,{exact:true}).waitFor();
    await choose(secondSheet,'Type B');
    await screenshot(second.page,'06-waiting-team-receipt');
    await close(secondSheet);
    await judge.api('/api/session',{action:'feedback'});
    assert.equal((await first.api(playerPath)).busy,true);
    await judge.api('/api/submit',{kind:'round',pair:1,wrap_rating:4,ticks:Array(10).fill(1),spoke:1,feedback:'Rehearsal feedback'});
    await judge.api('/api/session',{action:'finish'});
    await programme(7);
    await first.page.bringToFront();
    const nextAgenda = (await first.api('/api/participant/state')).program.title;
    await first.page.locator('#now-title').getByText(nextAgenda,{exact:true}).waitFor();
    await mutate('catchup',{minutes:2,confirm_missing:true});
    await until(async()=>(await first.api(playerPath)).questions.some(q=>q.id===rounds[0].id),'last team can catch up');
    await first.page.bringToFront();
    await firstSheet.getByText(rounds[0].prompt,{exact:true}).waitFor();
    await choose(firstSheet,'Type B');
    await close(firstSheet);
    await mutate('close_waiting');
    await mutate('post',{minutes:5});
    const post=(await first.api(playerPath)).questions.find(q=>q.qid===warm.qid);
    await first.page.bringToFront();
    await firstSheet.getByText(post.prompt,{exact:true}).waitFor();
    await choose(firstSheet,post.options[0].text);
    await button(firstSheet,'Next question').click();
    await firstSheet.getByText('Question 2 of 5',{exact:true}).waitFor();
    await close(firstSheet);
    await mutate('close_post');
    const scores=await first.api(playerPath);
    assert.equal(scores.score.earned,6);
    assert.equal(scores.score.available,17);
    assert(scores.history.some(r=>r.id===warm.id&&r.correct?.length));
    await mutate('standings');
    await tv.bringToFront();
    await tv.getByRole('heading',{name:'Quiz top five'}).waitFor();
    assert.match(await tv.locator('.hall-quiz').innerText(),/Alex E\./);
    assert(!(await tv.locator('.hall-quiz').innerText()).includes('Alex Example'));
    await screenshot(tv,'07-hall-standings');
    await page.bringToFront();
    await button(page,'Results').click();
    await page.getByRole('heading',{name:'Quiz results',exact:true}).waitFor();
    assert.equal(await button(page,'Download participant CSV').count(),0);
    assert.equal(await button(page,'Download question CSV').count(),1);
    await screenshot(page,'08-desk-results');
    await button(page,'Analytics').click();
    await page.getByRole('heading',{name:'Quiz analytics',exact:true}).waitFor();
    await page.getByRole('searchbox',{name:'Find a question'}).fill('W2');
    await page.getByText('50% correct among respondents',{exact:true}).waitFor();
    await page.getByText('2 unique viewers · 100% of viewers answered',{exact:true}).waitFor();
    await page.getByRole('heading',{name:'Desk activity'}).waitFor();
    await page.getByText('Participants who answered',{exact:true}).waitFor();
    await screenshot(page,'13-desk-analytics');
    await button(page,'Now').click();
    await screenshot(page,'14-desk-console');
    await second.context.setOffline(true);
    await second.page.bringToFront();
    if (!await secondSheet.isVisible()) await second.page.locator('.quiz-badge').click();
    await secondSheet.getByText('Connection interrupted.',{exact:false}).waitFor();
    await second.context.setOffline(false);
    await secondSheet.getByText('Connection interrupted.',{exact:false}).waitFor({state:'hidden'});
    await close(secondSheet);
    console.log('Judge quiet mode, catch-up, post-test and reconnect passed.');
    await programme(8);
    await desk.api('/api/desk/final',{action:'set',pairs:[1,3,4],panel:[station]});
    await desk.api('/api/desk/final',{action:'start'});
    await mutate('send',{qid:'F1'});
    const vote=(await second.api(playerPath)).questions.find(q=>q.qid==='F1');
    assert.equal(vote.points,0);
    assert(!(await first.api(playerPath)).questions.some(q=>q.qid==='F1'));
    await first.api(playerPath,{action:'answer',id:vote.id,key:'A'},409);
    await second.api(playerPath,{action:'answer',id:vote.id,key:'A'});
    await mutate('close',{id:vote.id});
    for(const [index,pair] of [1,3,4].entries()) {
      if(index) await desk.api('/api/desk/final',{action:'next'});
      await desk.api('/api/desk/clock',{action:'start'});
      await desk.api('/api/desk/clock',{action:'finish'});
      const final=(await desk.api('/api/desk/scores')).final_state;
      await judge.api('/api/submit',{kind:'final',pair,run_id:final.run_id,final_rating:5-index,ticks:Array(10).fill(0),spoke:1});
    }
    await desk.api('/api/desk/final',{action:'review'});
    await mutate('send',{qid:'F2'});
    const choice=(await second.api(playerPath)).questions.find(q=>q.qid==='F2');
    await second.api(playerPath,{action:'answer',id:choice.id,key:'A'});
    await mutate('close',{id:choice.id});
    await desk.api('/api/desk/final',{action:'publish',confirm:'PUBLISH'});
    await programme(9);
    await mutate('predictions');
    await mutate('people_choice');
    await until(async()=>((await desk.api('/api/public')).quiz?.bars?.[0]?.count===1),'final vote results after timer');
    await mutate('prize',{prize:'Rehearsal-only surprise prize'});
    assert(!JSON.stringify(await desk.api('/api/public')).includes('Rehearsal-only surprise prize'));
    await mutate('champion');
    await tv.bringToFront();
    await tv.getByRole('heading',{name:'Quiz champion'}).waitFor();
    await screenshot(tv,'09-hall-champion');
    await page.bringToFront();
    page.once('dialog',dialog=>dialog.accept());
    await setting.click();
    await until(async()=>!(await first.api(playerPath)).enabled,'setting disables participant quiz');
    await tv.bringToFront();
    await tv.locator('.hall-quiz').waitFor({state:'hidden'});
    assert.equal((await get()).standings[0].earned,6);
    await screenshot(page,'10-desk-quiz-disabled');
    assert.deepEqual(errors,[]);
    await owner.api('/api/admin/workspace',{mode:'live',confirm:'LIVE'});
    assert.equal((await owner.api(deskPath)).enabled,false);
    assert.equal((await owner.api(deskPath)).runs.length,0);
    console.log('Passed: two isolated mobile participant browsers, judge tablet, Organizer Desk and 1080p hall. Verified editing, manual launch, scoring, duplicate protection, quiet pitch, catch-up, reconnect, finalist vote exclusion, masked standings, prize gating, off switch, and workspace separation.');
  } finally {
    if(browser) await browser.close();
    app.kill();
    await rm(data,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
