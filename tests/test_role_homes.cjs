const {webkit}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs');
const base=process.env.KEVENOS_REVIEW_URL||'http://localhost:8783';
const shots=process.env.KEVENOS_SHOTS||'/tmp/pitch-583-screens';
(async()=>{fs.mkdirSync(shots,{recursive:true});const browser=await webkit.launch();const errors=[];
try {
 const owner=await browser.newContext();let response=await owner.request.post(base+'/api/access/login',{data:{password:'Review-Quiz-Admin9!'}});assert(response.ok());
 await owner.addCookies((await owner.cookies()).map(c=>({...c,secure:false})));
 const dashboardHeaders=async role=>{
  const current=await owner.request.get(base+'/api/access/me',{headers:{'X-Dashboard':role}});assert(current.ok());
  const headers={'X-Dashboard':role};for(const key of ['x-event-context','x-access-context'])if(current.headers()[key])headers[key]=current.headers()[key];
  return headers;
 };
 let headers=await dashboardHeaders('admin');
 const workspace=(await (await owner.request.get(base+'/api/admin/workspace',{headers})).json()).mode;
 if(workspace==='rehearsal') {
  response=await owner.request.post(base+'/api/admin/workspace',{headers,data:{mode:'live',confirm:'LIVE'}});assert(response.ok());
  headers=await dashboardHeaders('admin');
 }
 response=await owner.request.post(base+'/api/admin/credentials',{headers,data:{action:'create',role:'registration',label:'Synthetic Registration',password:'Review-Registration9!'}});assert(response.ok());
 if(workspace==='rehearsal') {
  response=await owner.request.post(base+'/api/admin/workspace',{headers,data:{mode:'rehearsal',confirm:'REHEARSAL'}});assert(response.ok());
 }
 headers=await dashboardHeaders('organizer');
 response=await owner.request.post(base+'/api/desk/program',{headers,data:{action:'announce',text:'Please take your seats. The next session starts shortly.'}});assert(response.ok());
 for(const [device,width,height] of [['phone',390,844],['ipad',820,1180],['desktop',1440,1000]])for(const [role,password,route,ready] of [
 ['guest','Review-Quiz-Guest9!','/event','#overview'],['judge','Review-Quiz-Judge9!','/judge/1','#judge-home'],
 ['organizer','Review-Quiz-Desk9!','/organizer','#desk-home'],['scoring','Review-Quiz-Desk9!','/scoring','#desk-home'],
 ['registration','Review-Registration9!','/registration','#registration-home'],['admin','Review-Quiz-Admin9!','/admin','.role-hero']]){
 const context=await browser.newContext({viewport:{width,height},isMobile:device==='phone',hasTouch:device!=='desktop'});
 response=await context.request.post(base+'/api/access/login',{data:{password}});assert(response.ok(),role);
 await context.addCookies((await context.cookies()).map(c=>({...c,secure:false})));
 const page=await context.newPage();page.on('pageerror',e=>errors.push(role+': '+e.message));page.on('response',r=>{if(r.url().includes('/static/') && r.status()>=400)errors.push(role+' asset '+r.status()+' '+r.url());});
 await page.goto(base+'/home');await page.locator('#home-dashboards a[href="'+route+'"]').click();await page.locator(ready).waitFor({state:'visible'});
 if(role==='guest')await page.waitForFunction(()=>document.querySelector('#guest-announcement-text').textContent.includes('Please take your seats'));
 if(role==='judge')await page.waitForFunction(()=>document.querySelector('#judge-home-team').textContent.startsWith('Team'));
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),role+' '+device+' overflow');
 await page.screenshot({path:shots+'/'+role+'-'+device+'.png',fullPage:device==='phone'});
 if(role==='guest') {await page.locator('[data-guest-tab=agenda]').click();assert(await page.locator('#agenda').isVisible());await page.locator('[data-guest-tab=about]').click();assert(await page.locator('#attend-title').isVisible());}
 if(role==='judge') {assert((await page.locator('#judge-home-announcement').textContent()).includes('Please take your seats'));await page.locator('#judge-home-customer').click();await page.locator('#sheet').waitFor({state:'visible'});await page.evaluate(()=>closeSheet());await page.locator('#judge-home-open').click();assert(await page.locator('#judge-workspace').isVisible());assert(await page.locator('#judge-home').isHidden());
 await page.locator('#bnote').click();await page.locator('#notes').fill('A clear customer-focused pitch.');await page.locator('#sx').click();
 await page.locator('#judge-home-tab').click();const ticks=await page.evaluate(()=>D.ticks.slice());await page.keyboard.press('1');assert.deepEqual(await page.evaluate(()=>D.ticks.slice()),ticks);
 await page.locator('#judge-home-open').click();await page.locator('#bnote').click();assert.equal(await page.locator('#notes').inputValue(),'A clear customer-focused pitch.');await page.locator('#notes').fill('');await page.locator('#sx').click();await page.locator('#judge-home-tab').click();assert(await page.locator('#judge-home').isVisible());await page.reload();await page.locator('#judge-home').waitFor({state:'visible'});await page.evaluate(()=>stopJudge('Session ended'));assert.equal(await page.locator('#judge-home-team').textContent(),'');assert.equal(await page.locator('#judge-home-announcement').textContent(),'');assert(await page.locator('#app').isHidden());}
 if(role==='scoring'){await page.locator('[data-home-tab=run]').first().click();assert.equal(await page.locator('#nav [data-t=quiz]').count(),0);}
 if(role==='organizer'){await page.locator('[data-home-tab=quiz]').click();await page.locator('.quiz-desk').waitFor({state:'visible'});assert.equal(await page.locator('#nav [data-t=scores]').count(),0);}
 if(role==='registration'){await page.locator('[data-registration-action=waiting]').click();assert(await page.locator('#registration-search').isVisible());assert.equal(await page.locator('#registration-filter').inputValue(),'waiting');await page.locator('#registration-home-tab').click();assert(await page.locator('#registration-home').isVisible());}
 if(role==='admin'){await page.locator('.role-action-grid [data-jump=accounts]').click();await page.locator('[data-account-index]').first().waitFor({state:'visible'});await page.locator('#nav [data-t=overview]').click();await page.locator('.role-hero').waitFor({state:'visible'});}
 await context.close();console.log(role+' '+device+' passed');
 }
 await owner.close();assert.deepEqual(errors,[]);console.log('All role landings and navigation passed without browser errors.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
