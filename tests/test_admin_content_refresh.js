const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {JSDOM}=require('jsdom');
const html=readFileSync(require('node:path').join(__dirname,'../static/admin.html'),'utf8');
const source=html.slice(html.indexOf('async function refreshAfterAssistant()'),html.indexOf("$('local-login').onsubmit"));
function fixture(dirty=false) {
 const dom=new JSDOM('',{runScripts:'outside-only'}),w=dom.window;
 w.eval(`let active=true,sectionKey=1,contentDirty=${dirty},RDIRTY=false,staffDirty=false,issuing=false,notificationBusy=false,issueMailBusy=false,tab='content',people='credentials';
 let C={content_version:1}, CT={versions:{content:1}}, CTDOC={draft:'preserved'};
 const KEV={profile:{revision:1}}; let calls=0,message='',resolve;
 const get=()=>new Promise(r=>resolve=r); const select=async()=>calls++;
 const setStatus=text=>message=text;const fail=e=>{throw e};
 ${source}
 window.begin=()=>refreshAfterAssistant();window.reply=value=>resolve(value);
 window.switchProfile=()=>KEV.profile.revision++;
 window.state=()=>({C,CT,CTDOC,calls,message,contentDirty});`);
 return {w,close:()=>dom.window.close()};
}
test('assistant content changes refresh a clean editor',async()=>{
 const f=fixture();try{const done=f.w.begin();f.w.reply({content_version:2});await done;
 const s=f.w.state();assert.equal(s.C.content_version,2);assert.equal(s.CT,null);assert.equal(s.CTDOC,null);assert.equal(s.calls,1);
 }finally{f.close()}
});
test('assistant content changes keep dirty drafts and warn before save',async()=>{
 const f=fixture(true);try{const done=f.w.begin();f.w.reply({content_version:2});await done;
 const s=f.w.state();assert.equal(s.CTDOC.draft,'preserved');assert.equal(s.CT.versions.content,1);assert.equal(s.contentDirty,true);assert.equal(s.calls,0);assert.match(s.message,/draft is kept.*Reload latest/);
 }finally{f.close()}
});
test('a stale assistant refresh cannot cross a profile change',async()=>{
 const f=fixture();try{const done=f.w.begin();f.w.switchProfile();f.w.reply({content_version:2});await done;
 const s=f.w.state();assert.equal(s.C.content_version,1);assert.equal(s.CTDOC.draft,'preserved');assert.equal(s.calls,0);
 }finally{f.close()}
});
