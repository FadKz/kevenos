const {webkit}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const base=process.env.KEVENOS_REVIEW_URL||'http://localhost:8778';
const out=process.env.KEVENOS_SHOTS||'/tmp/pitch-582-screens';
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await webkit.launch();
 const errors=[];
 try {
 for(const [name,width,height] of [['phone',390,844],['ipad',820,1180],['desktop',1440,1000]]) {
 const context=await browser.newContext({viewport:{width,height},isMobile:name==='phone',hasTouch:name!=='desktop',reducedMotion:'reduce'});
 const login=await context.request.post(base+'/api/access/login',{data:{role:'participant',password:'Review-Quiz-Alex9!'}});
 assert(login.ok());
 // The disposable fixture uses HTTP; production always retains Secure cookies.
 await context.addCookies((await context.cookies()).map(c=>({...c,secure:false})));
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/participant');await page.locator('#team-navigation').waitFor({state:'visible'});
 assert.equal(await page.locator('#team-navigation button').count(),4);
 for(const panel of ['today','pitch','results','more']) {
 await page.locator('#team-navigation [data-panel='+panel+']').click();
 await page.locator('[data-team-panel='+panel+']').waitFor({state:'visible'});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),name+' '+panel+' overflow');
 const nav=await page.locator('#team-navigation').boundingBox();assert(nav.y+nav.height<=height+1);
 await page.screenshot({path:out+'/'+name+'-'+panel+'.png',fullPage:true});
 }
 await page.getByRole('button',{name:'Agenda Follow'}).click();
 await page.locator('#agenda').waitFor({state:'visible'});
 assert.equal(await page.locator('[data-panel=more]').getAttribute('aria-pressed'),'true');
 await page.getByRole('button',{name:'All event tools'}).click();
 await page.getByRole('button',{name:'Updates & notifications Desk'}).click();
 await page.locator('#notification-status').waitFor({state:'visible'});
 await page.locator('[data-panel=pitch]').click();
 await page.locator('#worksheet-speaker').fill('Alex');
 await page.locator('#worksheet-cues').fill('Ask about your customer’s priorities. Agree the next step together.');
 await page.locator('#next-pitch-step').click();
 await page.locator('#previous-pitch-step').click();
 assert((await page.locator('#worksheet-cues').inputValue()).startsWith('Ask about'));
 await page.locator('#worksheet-save').click();
 await page.waitForFunction(()=>document.querySelector('#worksheet-status').textContent==='Saved for the team');
 await page.locator('#toggle-pitch-focus').click();
 await page.locator('#pitch-speaking').waitFor({state:'visible'});
 assert(await page.locator('#team-worksheet').isHidden());
 await page.screenshot({path:out+'/'+name+'-pitch-mode.png',fullPage:true});
 await page.locator('#toggle-pitch-focus').click();
 await context.close();
 console.log(name+': navigation, deep pages, draft preservation, save, Pitch Mode and layout passed');
 }
 const admin=await browser.newContext();
 const auth=await admin.request.post(base+'/api/access/login',{data:{role:'admin',password:'Review-Quiz-Admin9!'}});
 await admin.addCookies((await admin.cookies()).map(c=>({...c,secure:false})));
 const headers={};
 for(const key of ['x-event-context','x-access-context']) if(auth.headers()[key]) headers[key]=auth.headers()[key];
 async function api(route,body) {
  const response=await admin.request.fetch(base+route,{method:body?'POST':'GET',data:body,headers});
  assert.equal(response.status(),200,await response.text());
  for(const key of ['x-event-context','x-access-context']) if(response.headers()[key]) headers[key]=response.headers()[key];
  return response.json();
 }
 const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
 await context.request.post(base+'/api/access/login',{data:{role:'participant',password:'Review-Quiz-Alex9!'}});
 await context.addCookies((await context.cookies()).map(c=>({...c,secure:false})));
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/participant');await page.locator('#team-navigation').waitFor({state:'visible'});
 await api('/api/desk/program',{action:'announce',text:'Coaches are ready. Bring your team to the preparation tables.'});
 await page.waitForFunction(()=>document.querySelector('#announcement-text').textContent.includes('Coaches are ready'));
 assert(await page.locator('#announcement').isVisible());
 assert.equal(await page.locator('[data-team-panel=today] > :first-child').getAttribute('id'),'announcement');
 await api('/api/desk/program',{action:'set',index:1});
 await page.waitForFunction(()=>document.querySelector('#now-title').textContent.includes('Welcome'));
 await page.screenshot({path:out+'/phone-desk-update.png',fullPage:true});
 let quiz=await api('/api/desk/quiz');
 await api('/api/desk/quiz',{action:'settings',enabled:true,rev:quiz.rev});
 await page.locator('[data-panel=pitch]').click();
 await page.locator('#worksheet-cues').fill('Keep this draft while answering the quiz.');
 await page.locator('#worksheet-title').click(); // Prompts wait until the participant stops typing.
 await api('/api/desk/program',{action:'set',index:0});
 quiz=await api('/api/desk/quiz');await api('/api/desk/quiz',{action:'invite',rev:quiz.rev});
 const dialog=page.getByRole('dialog',{name:'Event quiz'});
 await dialog.waitFor({state:'visible',timeout:15000});
 await dialog.getByRole('button',{name:'Yes, count me in'}).click();
 await dialog.getByRole('button',{name:'Close',exact:true}).click();
 assert.equal(await page.locator('#worksheet-cues').inputValue(),'Keep this draft while answering the quiz.');
 await page.locator('#worksheet-save').click();
 await page.waitForFunction(()=>document.querySelector('#worksheet-status').textContent==='Saved for the team');
 await api('/api/desk/program',{action:'set',index:1});
 quiz=await api('/api/desk/quiz');await api('/api/desk/quiz',{action:'send',qid:'W2',rev:quiz.rev});
 await dialog.waitFor({state:'visible',timeout:15000});
 await page.screenshot({path:out+'/phone-quiz-popup.png'});
 await dialog.getByRole('button',{name:'Close',exact:true}).click();
 await page.locator('[data-panel=today]').click();
 assert(await page.locator('.quiz-badge').isVisible());
 await page.screenshot({path:out+'/phone-now-quiz.png',fullPage:true});
 await context.close();await admin.close();
 console.log('Desk announcement and agenda update visible on Today; quiz auto-opens over My Pitch and retains unsaved cues.');
 assert.deepEqual(errors,[]);console.log('No browser errors');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
