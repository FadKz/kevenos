// Independent review reproduction. Expected fixed result: savedAfterIdentityEnded=0.
const {JSDOM}=require('jsdom'),fs=require('fs');
const src=fs.readFileSync(require('path').join(__dirname,'../static/admin.html'),'utf8');
const handler=src.slice(src.indexOf("$('main').addEventListener('click', async e => {"),src.indexOf('\nfunction vReports()'));
const d=new JSDOM('<main id="main"><button data-export="backup">Download</button></main><p id="download-status"></p>',{url:'https://example.test/admin',runScripts:'outside-only'});
let release,saved=0;
d.window.KEV={staff:{token:null,hosted:true},profile:{active:true,revision:0,fetch:async()=>({ok:true,headers:{get:()=>null},blob:()=>new Promise(r=>release=r)}),error:()=>new Error('Profile ended')},checkContext:()=>{},saveFile:()=>saved++};
d.window.eval("var $=id=>document.getElementById(id);var EXPORTS={backup:'/api/desk/backup.json'};var sectionKey=1;var active=true;function fail(){};"+handler);
d.window.document.querySelector('button').click();
setImmediate(()=>{
 d.window.sectionKey++;d.window.active=false;d.window.KEV.profile.active=false;d.window.KEV.profile.revision++;
 d.window.document.getElementById('main').replaceChildren();
 release(new d.window.Blob(['private backup']));
 setImmediate(()=>{console.log(JSON.stringify({savedAfterIdentityEnded:saved}));d.window.close();process.exitCode=saved?1:0;});
});
