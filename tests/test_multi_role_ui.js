const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const root = path.resolve(__dirname,'../static');
const common = fs.readFileSync(path.join(root,'common.js'),'utf8');
const accountsScript = fs.readFileSync(path.join(root,'accounts.js'),'utf8');
const admin = fs.readFileSync(path.join(root,'admin.html'),'utf8');
const dataset = mode => {
  const judge = {id:'judge-alex',label:'Alex',revision:2,credentials:[{active:1,last_used:1800000000}],grants:[{role:'judge',station:3}]};
  const participant = {id:'participant-alex',label:'Alex',revision:5,credentials:[{active:1}],grants:[{role:'participant',pair:2,member:1}]};
  return {
    accounts:[judge,participant],
    roles:[{id:'judge',label:'Judging'},{id:'participant',label:'My Event'},{id:'registration',label:'Registration Desk'},{id:'organizer',label:'Event Operations'}],
    roster:[{pair:2,members:[{id:1,name:'Alex'}, {id:2,name:'Sam'}]}],
    stations:[{station:3},{station:4}],
    mode
  };
};
const wait = () => new Promise(resolve=>setImmediate(resolve));

async function open(mode, verify) {
  const data = dataset(mode), mutations = [];
  const html = admin.replace('<body','<body data-scoped="1" data-event-mode="'+mode+'" data-event-context="event-a"')
    .replace('<script src="/static/common.js"></script>',()=>'<script>'+common+'</script>')
    .replace('<script src="/static/accounts.js"></script>',()=>'<script>'+accountsScript+'</script>');
  const dom = new JSDOM(html,{url:'https://example.test/admin#accounts',runScripts:'dangerously',beforeParse(window){
    window.setInterval=()=>1;
    window.fetch=async(endpoint, options)=>{
      let json={};
      if (endpoint==='/api/info') json={workspace:{mode}};
      if (endpoint==='/api/access/me') json={authenticated:true,role:'admin',dashboard:'admin',account_id:'owner',context:'admin-a',dashboards:[{role:'admin',label:'Event Administration',url:'/admin'}]};
      if (endpoint==='/api/desk/state') json={stations:data.stations};
      if (endpoint==='/api/desk/roster') json={roster:data.roster};
      if (endpoint==='/api/admin/accounts') {
        if (options.body) {
          assert.equal(options.headers['X-Dashboard'],'admin');
          mutations.push(JSON.parse(options.body));
          json={ok:true};
        } else json={accounts:data.accounts,roles:data.roles};
      }
      return {ok:true,status:200,headers:{get:()=>null},json:async()=>json};
    };
  }});
  try {
    await wait();
    const doc=dom.window.document;
    assert.equal(doc.querySelectorAll('#main tbody tr').length,2);
    const types=doc.getElementById('kevenos-account-type');
    types.value='participant';types.dispatchEvent(new dom.window.Event('change'));
    assert.equal(doc.querySelectorAll('#main tbody tr:not([hidden])').length,1);
    assert.equal(doc.querySelector('#main tbody tr:not([hidden])').dataset.accountType,'participant');
    types.value='';types.dispatchEvent(new dom.window.Event('change'));
    doc.querySelector('[data-account-index="0"]').click();
    await verify(dom.window,doc,mutations);
  } finally {dom.window.close();}
}

(async()=>{
  await open('live',async(window,doc,mutations)=>{
    const roles = doc.getElementById('kevenos-account-grants');
    const station = roles.elements.namedItem('judge');
    assert.equal(station.value,'3');
    station.value='4';
    roles.querySelector('[value="registration"]').click();
    assert.match(roles.querySelector('[data-role-preview]').textContent,/Judging · Station 4, Registration Desk/);
    roles.elements.namedItem('reason').value='Assign during arrival cover';
    roles.requestSubmit();
    await wait();
    assert.deepEqual(mutations,[{action:'roles',id:'judge-alex',revision:2,reason:'Assign during arrival cover',grants:[{role:'judge',station:4},{role:'registration'}]}]);

    doc.querySelector('[data-account-index="0"]').click();
    const link = doc.getElementById('kevenos-account-link');
    assert.match(link.closest('details').textContent,/source password is retired/);
    link.elements.namedItem('source').value='0';
    link.dispatchEvent(new window.Event('change'));
    assert.match(link.querySelector('[data-link-preview]').textContent,/Judging, My Event/);
    link.elements.namedItem('reason').value='Verified by event lead';
    link.elements.namedItem('confirm').value='LINK AND RETIRE SOURCE PASSWORD';
    link.requestSubmit();
    await wait();
    assert.deepEqual(mutations[1],{action:'link',id:'judge-alex',revision:2,source_id:'participant-alex',source_revision:5,reason:'Verified by event lead',confirm:'LINK AND RETIRE SOURCE PASSWORD'});
  });
  await open('rehearsal',async(window,doc,mutations)=>{
    const roles=doc.getElementById('kevenos-account-grants');
    const registration=roles.querySelector('[value="registration"]');
    const organizer=roles.querySelector('[value="organizer"]');
    assert.equal(registration.disabled,false);assert.equal(organizer.disabled,false);
    registration.click();organizer.click();
    assert.equal(roles.elements.namedItem('judge').value,'3');
    assert.match(roles.textContent,/Live and Rehearsal/);
    roles.elements.namedItem('reason').value='Cover registration and event operations';
    roles.dispatchEvent(new window.Event('submit',{cancelable:true}));await wait();
    assert.equal(mutations.length,0);
    assert.match(roles.querySelector('[data-error]').textContent,/Confirm/);
    roles.elements.namedItem('confirm_shared_roles').click();
    roles.requestSubmit();await wait();
    assert.deepEqual(mutations[0],{action:'roles',id:'judge-alex',revision:2,reason:'Cover registration and event operations',
      grants:[{role:'judge',station:3},{role:'registration'},{role:'organizer'}],confirm_shared_roles:true});
    assert(!doc.getElementById('kevenos-account-link'));
  });
  console.log('Admin role assignments, explicit account linking and Rehearsal shared-role confirmation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
