// Runs the actual Admin credential-issuance dialog functions with mocked APIs.
// No SMTP, server, or external network. NODE_PATH=/tmp/pitch-rehearsal-20261112/js/node_modules node tests/test_admin_credential_email_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {JSDOM, VirtualConsole} = require('jsdom');
const admin = fs.readFileSync(path.join(__dirname,'../static/admin.html'),'utf8');
const dialogStart = admin.indexOf('<dialog id="issue-dialog"');
assert(dialogStart >= 0, 'Admin credential dialog must exist');
const dialog = admin.slice(dialogStart,admin.indexOf('</dialog>',dialogStart)+9);
const functionStart = admin.indexOf('let issueGeneration =');
const functionEnd = admin.indexOf('async function vCredentials(',functionStart);
assert(functionStart >= 0 && functionEnd > functionStart, 'Run the actual issuance functions, not a copied implementation');
const source = admin.slice(functionStart,functionEnd);
const fixture = {id:'fixture-credential',role:'participant',label:'Alex Example',pair:3,member:1,password:'Fixture-password-1',issuance_grant:'fixture-issuance-grant'};
const clone = value => JSON.parse(JSON.stringify(value));
const turn = () => new Promise(resolve=>setTimeout(resolve,0));
const deferred = () => {let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function create({mode='live',availability={enabled:true},send}={}) {
  const calls=[],gets=[],errors=[];let uuids=0;
  const vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error.message));
  const dom=new JSDOM('<!doctype html><body>'+dialog+'</body>',{url:'https://example.test/admin',runScripts:'outside-only',virtualConsole:vc});
  const w=dom.window;
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  Object.defineProperty(w.crypto,'randomUUID',{value:()=> '00000000-0000-4000-8000-'+String(++uuids).padStart(12,'0')});
  w.KEV={workspaceMode:mode,esc:value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),copyInput:()=>{},saveFile:()=>{}};
  w.get=async url=>{gets.push(url);return await availability;};
  w.post=async(url,payload)=>{calls.push({url,payload:clone(payload)});return send?await send(calls.length,payload):{message:{status:'sent',recipient:payload.recipient}};};
  w.eval('const $=id=>document.getElementById(id);\n'+source);
  return {dom,calls,gets,errors,$:id=>w.document.getElementById(id),form:()=>w.document.querySelector('[data-email]'),button:()=>w.document.querySelector('[data-email] button'),status:()=>w.document.querySelector('[data-email-status]'),uuids:()=>uuids,
    async show(row=fixture){w.showIssued([clone(row)]);await turn();},async submit(email='alex@example.test'){const form=w.document.querySelector('[data-email]');if(!form.elements.recipient.disabled)form.elements.recipient.value=email;form.querySelector('button').click();await turn();},close:()=>w.close()};
}
let failures=0;
async function test(name,run){try{await run();console.log('PASS '+name);}catch(error){failures++;console.error('FAIL '+name+': '+error.stack);}}
(async()=>{
  await test('email is optional: checking availability never sends a password',async()=>{
    const waiting=deferred(),page=create({availability:waiting.promise});
    try{await page.show();assert(page.$('issue-dialog').open);assert.equal(page.$('issue-0').value,fixture.password);assert(page.button().disabled);assert.deepEqual(page.gets,['/api/admin/notifications']);assert.equal(page.calls.length,0);waiting.resolve({enabled:true});await turn();assert(!page.button().disabled);assert.match(page.status().textContent,/Optional/);assert.equal(page.calls.length,0);assert.deepEqual(page.errors,[]);}finally{page.close();}
  });
  await test('rehearsal keeps email disabled even if the transport reports availability',async()=>{
    const page=create({mode:'rehearsal'});
    try{await page.show();assert(page.button().disabled);await page.submit();assert.equal(page.calls.length,0);assert.equal(page.$('issue-0').value,fixture.password);assert.equal(typeof page.$('issue-download').onclick,'function');}finally{page.close();}
  });
  await test('unavailable transport or missing issuance grant keeps copy available and never sends',async()=>{
    for(const variant of [{availability:{enabled:false,disabled_reason:'Email is disabled.'},row:fixture},{availability:{enabled:true},row:{...fixture,issuance_grant:null}}]){
      const page=create(variant);
      try{await page.show(variant.row);assert(page.button().disabled);await page.submit();assert.equal(page.calls.length,0);assert(page.dom.window.document.querySelector('[data-copy]'));assert.equal(page.$('issue-0').value,fixture.password);}finally{page.close();}
    }
  });
  await test('Live sends exactly the issued credential, grant, password, recipient and request ID',async()=>{
    const page=create();
    try{await page.show();page.$('issue-0').value='An edited display must not alter the issued credential';await page.submit('alex@example.test');assert.equal(page.calls.length,1);assert.equal(page.calls[0].url,'/api/admin/credentials/share');assert.deepEqual(page.calls[0].payload,{credential_id:fixture.id,issuance_grant:fixture.issuance_grant,password:fixture.password,recipient:'alex@example.test',idempotency_key:'00000000-0000-4000-8000-000000000001'});assert.equal(page.uuids(),1);assert.match(page.status().textContent,/Accepted by the mail service/);}finally{page.close();}
  });
  await test('network failure checks the same delivery with the same UUID and payload',async()=>{
    const page=create({send:async attempt=>{if(attempt===1)throw new Error('Synthetic connection failure');return {message:{status:'sent',recipient:'alex@example.test'}};}});
    try{await page.show();await page.submit();assert.equal(page.calls.length,1);assert.equal(page.button().textContent,'Check delivery');assert(!page.button().disabled);assert(page.form().elements.recipient.disabled);await page.submit();assert.equal(page.calls.length,2);assert.deepEqual(page.calls[1],page.calls[0]);assert.equal(page.uuids(),1);assert(page.button().disabled);assert.equal(page.button().textContent,'Sent');await page.submit();assert.equal(page.calls.length,2);}finally{page.close();}
  });
  await test('accepted, uncertain and sending receipts stop further button sends',async()=>{
    for(const status of ['sent','uncertain','sending']){
      const page=create({send:async()=>({message:{status,recipient:'alex@example.test'}})});
      try{await page.show();await page.submit();assert(page.button().disabled);assert(page.form().elements.recipient.disabled);if(status!=='sent')assert.match(page.status().textContent,/Check Notifications history/);await page.submit();assert.equal(page.calls.length,1);assert.equal(page.uuids(),1);}finally{page.close();}
    }
  });
  await test('closing the dialog removes visible secrets and download handlers without delivery',async()=>{
    const waiting=deferred(),page=create({availability:waiting.promise});
    try{await page.show();page.$('issue-done').click();assert(!page.$('issue-dialog').open);assert.equal(page.$('issue-rows').childElementCount,0);assert.equal(page.$('issue-0'),null);assert.equal(page.$('issue-download').onclick,null);assert.equal(page.$('issue-status').textContent,'');waiting.resolve({enabled:true});await turn();assert.equal(page.$('issue-rows').childElementCount,0);assert.equal(page.calls.length,0);assert(!page.dom.serialize().includes(fixture.password));assert.deepEqual(page.errors,[]);}finally{page.close();}
  });
  await test('a late response from a closed dialog cannot overwrite a newly issued password dialog',async()=>{
    const first=deferred(),page=create({send:async()=>first.promise});
    try{await page.show();await page.submit();assert.equal(page.calls.length,1);page.$('issue-done').click();await page.show({...fixture,id:'second-credential',password:'Fixture-password-2',issuance_grant:'second-grant'});assert.equal(page.$('issue-0').value,'Fixture-password-2');const before=page.status().textContent;first.resolve({message:{status:'sent',recipient:'alex@example.test'}});await turn();assert.equal(page.status().textContent,before);assert.equal(page.$('issue-0').value,'Fixture-password-2');assert.equal(page.button().textContent,'Send password');assert(!page.button().disabled);assert.equal(page.calls.length,1);assert.deepEqual(page.errors,[]);}finally{page.close();}
  });
  if(failures)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
