const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM, VirtualConsole} = require('jsdom');

const common = fs.readFileSync(process.argv[2] || path.join(__dirname, '../static/common.js'), 'utf8');
const pages = process.argv[3] || path.join(__dirname, '../static');
const role = (name, extra = {}) => ({authenticated: true, role: name, label: 'Example identity', context: 'access-a', ...extra});
const tick = () => new Promise(resolve => setImmediate(resolve));
const dashboard = (role,label,url) => ({role,label,url});

async function withPage(url, identity, verify, {mode = 'rehearsal', child = false, file, local = false, dataFor} = {}) {
  const calls = [], errors = [];
  let current = identity;
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error.message));
  console.on('error', (...values) => errors.push(values.join(' ')));
  const source = file ? fs.readFileSync(path.join(pages, file), 'utf8') :
    '<!doctype html><body><main>Screen</main><script src="/static/common.js"></script></body>';
  const html = source.replace('<body', '<body data-scoped="' + (local ? '0' : '1') + '" data-event-mode="' + mode + '"')
    .replace('<script src="/static/common.js"></script>', () => '<script>' + common + '</script>');
  const dom = new JSDOM(html, {
    url: 'https://example.test' + url, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: console,
    beforeParse(window) {
      window.setInterval = () => 1;
      if (local) window.localStorage.setItem('kevenos:token:desk', 'local-admin');
      window.confirm = prompt => {window.lastConfirm = prompt; return false;};
      if (child) {
        window.sessionStorage.setItem('kevenos:profile-token', 'child-secret');
        window.sessionStorage.setItem('kevenos:profile-context', identity.context);
      }
      window.fetch = async (pathname, options) => {
        calls.push({pathname, options});
        if (pathname === '/api/access/me' && current.expired) {
          return {ok: false, status: 401, headers: {get: () => null},
            json: async () => ({code: 'profile_expired', error: 'Profile expired'})};
        }
        const data = pathname === '/api/access/me' ? current : dataFor?.(pathname) || {workspace: {mode}, authed: local};
        return {ok: true, status: 200, headers: {get: () => null}, json: async () => data};
      };
    }
  });
  try {
    await tick();
    const {window} = dom;
    const doc = window.document;
    const nav = doc.getElementById('kevenos-nav');
    assert(nav, url + ' must mount shared navigation');
    const find = selector => nav.querySelector(selector);
    const links = () => [...nav.querySelectorAll('a')].map(link => link.getAttribute('href'));
    await verify({window, doc, nav, find, links, calls, setIdentity: me => {current = me;}});
    assert.deepEqual(errors, []);
  } finally {
    dom.window.close();
  }
}

(async () => {
  await withPage('/', {authenticated: false}, ({find, nav}) => {
    assert.match(nav.textContent, /KevenOS.*Sign in/s);
    assert.equal(find('#kevenos-profile'), null);
    assert.equal(find('#workspace-badge').hidden, false);
  });
  for (const name of ['guest', 'participant']) {
    const view = dashboard(name,name,'/'+name);
    await withPage('/' + name, role(name,{dashboard:name,dashboards:[view]}), ({links, find}) => {
      assert.deepEqual(links(), ['/home']);
      assert(!find('[aria-controls="kevenos-nav-links"]'));
      assert.match(find('#kevenos-profile').textContent, /Example identity/);
    });
  }
  const assigned = [dashboard('judge','Judging','/judge/3'),dashboard('registration','Registration Desk','/registration')];
  for (const [url, name] of [['/judge/3','judge'],['/registration','registration']]) {
    await withPage(url, role(name,{dashboard:name,dashboards:assigned,account_label:'Same person'}), async ({window,find,links,calls}) => {
      assert.deepEqual(links(), ['/home','/judge/3','/registration']);
      assert.equal(find('#kevenos-nav-links [aria-current]').getAttribute('href'),url);
      assert.match(find('#kevenos-profile').textContent,/Same person/);
      await window.eval("KEV.api('/api/event')");
      for (const endpoint of ['/api/access/me','/api/event']) assert.equal(calls.find(call=>call.pathname===endpoint).options.headers['X-Dashboard'],name);
    });
  }
  await withPage('/home', role('home',{dashboard:'home',dashboards:assigned,account_id:'alex',account_revision:2}), ({doc,find,calls})=>{
    assert.deepEqual([...doc.querySelectorAll('#home-dashboards a')].map(a=>a.getAttribute('href')),['/judge/3','/registration']);
    assert.match(doc.getElementById('home-title').textContent,/Welcome, Example identity/);
    assert.equal(find('.kevenos-nav-brand a').getAttribute('aria-current'),'page');
    assert.equal(calls.find(call=>call.pathname==='/api/access/me').options.headers['X-Dashboard'],'home');
  },{file:'home.html'});
  await withPage('/home', role('home',{dashboard:'home',dashboards:[]}), ({doc})=>{
    assert.match(doc.getElementById('home-title').textContent,/Your access has changed/);
    assert.equal(doc.getElementById('home-dashboards').querySelector('a'),null);
  },{file:'home.html'});
  await withPage('/home', role('home',{dashboard:'home',dashboards:[assigned[1]]}), ({doc,find})=>{
    assert.match(doc.getElementById('home-dashboards').textContent,/Continue to Registration Desk/);
    assert(!find('[aria-controls="kevenos-nav-links"]'));
  },{file:'home.html'});
  await withPage('/judge/3', role('judge',{dashboard:'judge',dashboards:assigned}), async ({window,doc,find})=>{
    window.HTMLDialogElement.prototype.showModal = function() {this.open = true;};
    window.HTMLDialogElement.prototype.close = function(result) {
      this.returnValue = result;
      this.open = false;
      this.dispatchEvent(new window.Event('close'));
    };
    window.eval('KEV.profile.hooks.hasPendingWork=()=>true');
    find('[aria-controls="kevenos-nav-links"]').click();
    find('#kevenos-nav-links a[href="/registration"]').click();
    const dialog = doc.querySelector('dialog');
    assert(dialog.open);
    assert.deepEqual([...dialog.querySelectorAll('button')].map(button=>button.textContent),['Stay','Keep draft and switch']);
    const link = dialog.querySelector('a');
    assert.equal(link.getAttribute('href'),'/registration');
    assert.equal(link.getAttribute('target'),'_blank');
    assert.equal(link.getAttribute('rel'),'noopener');
    dialog.close('stay');
    await tick();
    assert.equal(window.location.pathname,'/judge/3');
  });
  await withPage('/admin', role('admin',{dashboard:'admin',dashboards:[dashboard('admin','Event Administration','/admin'),dashboard('organizer','Event Operations','/organizer'),dashboard('registration','Registration Desk','/registration')]}), ({links}) => {
    assert.deepEqual(links(), ['/home','/admin','/organizer','/registration','/admin#profiles']);
  });
  await withPage('/judge/2', role('judge', {station: 2, actor: {role: 'admin'}, impersonation: {mode: 'preview'}}),
    async ({window, doc, find, links}) => {
      assert.deepEqual(links(), ['/home']);
      const returnButton = find('[data-profile="end"]');
      assert(returnButton && !returnButton.closest('[hidden]'));
      assert.match(find('#kevenos-profile').textContent, /Preview.*read-only/);
      const navButton = find('[aria-controls="kevenos-profile-actions"]');
      navButton.click();
      const link = find('[data-profile="switch"]');
      link.focus();
      await window.eval('KEV.profile.session(true)');
      assert.equal(doc.activeElement, link);
      assert.equal(navButton.getAttribute('aria-expanded'), 'true');
      window.eval('window.changeRequests=0; KEV.profile.hooks.beforeChange=()=>{window.changeRequests++; return false;}');
      link.click();
      await tick();
      assert.equal(window.changeRequests, 1);
      assert.equal(window.location.pathname, '/judge/2');
      link.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      assert.equal(doc.activeElement, navButton);
      assert.equal(navButton.getAttribute('aria-expanded'), 'false');
      navButton.click();
      const outside = doc.querySelector('main');
      outside.tabIndex = 0;
      outside.focus();
      assert.equal(navButton.getAttribute('aria-expanded'), 'false');
      navButton.click();
      outside.click();
      assert.equal(navButton.getAttribute('aria-expanded'), 'false');
      find('[aria-controls="kevenos-profile-actions"]').click();
      find('[data-profile="switch"]').click();
      find('[data-profile="out"]').click();
      find('.kevenos-nav-brand a').click();
      returnButton.click();
      await tick();
      assert.equal(window.changeRequests, 5);
      window.eval("KEV.profile.invalidate('Profile expired')");
      window.eval("KEV.showWorkspace('live')");
      assert.match(find('#kevenos-profile').textContent, /Profile expired/);
      assert(find('[data-profile="end"]'));
      assert(!navButton.isConnected || navButton.getAttribute('aria-expanded') === 'false');
    }, {child: true});
  await withPage('/participant', role('participant', {impersonation: {mode: 'support'}}),
    ({find, links}) => {
      assert.match(find('#kevenos-profile').textContent, /Support mode/);
      assert(find('[data-profile="end"]'));
      assert(!links().includes('/admin'));
    }, {child: true});
  for (const url of ['/live?key=display-secret', '/board?key=display-secret', '/live?embed=1', '/board?hdr=0']) {
    await withPage(url, role('admin'), async ({window, nav, find, links, calls}) => {
      await window.eval('KEV.profile.mount()');
      assert(!find('#kevenos-profile'));
      assert(!calls.some(call => call.pathname === '/api/access/me'));
      assert(!nav.textContent.includes('Example identity'));
      assert.deepEqual(links(), []);
      assert(!nav.outerHTML.includes('display-secret'));
      assert(!find('#workspace-badge').hidden);
      if (/embed|hdr/.test(url)) {
        assert.equal(nav.textContent, 'Rehearsal');
        await window.eval("KEV.api('/api/public')");
        await window.eval("KEV.api('/api/board')");
        assert(calls.some(call => call.pathname === '/api/public?names=0'));
        assert(calls.some(call => call.pathname === '/api/board?names=0'));
      } else {
        await window.eval("KEV.api('/api/public')");
        const viewer = calls.find(call => call.pathname === '/api/public');
        assert.equal(viewer.options.headers['X-Key'], 'display-secret');
      }
    });
  }
  await withPage('/live?embed=1', role('admin'), ({nav}) => assert(nav.hidden), {mode: 'live'});
  await withPage('/live?embed=1', role('participant', {impersonation: {mode: 'preview'}}),
    async ({window, find, calls}) => {
      assert(!find('#kevenos-profile'));
      await window.eval("KEV.profile.mount({onEnd:()=>{window.displayCleared=true;}})");
      assert(calls.some(call => call.pathname === '/api/access/me' && call.options.headers['X-Profile-Token'] === 'child-secret'));
      window.eval("KEV.profile.invalidate('Expired')");
      assert(window.displayCleared);
      await assert.rejects(window.eval("KEV.api('/api/public')"), /profile changed/i);
    }, {child: true});
  await withPage('/board?names=0', role('guest'), ({find, links}) => {
    assert(find('#kevenos-profile'));
    assert(links().includes('/live?names=0'));
  });
  const displayState = {
    event: 'Fixture event', now: 1800000000, program: {items: [], index: 0, announce: 'Fixture programme update'},
    clock: {elapsed: 0, round: 1, running: false}, final: {}, mode: 'desk'
  };
  const failures = [];
  const integration = async (name, run) => {
    try { await run(); }
    catch(error) { failures.push(name + ': ' + error.message); }
  };
  for (const [file, url, api, content] of [
    ['live.html', '/live?embed=1', '/api/public?presenter=1&names=0', '#ann'],
    ['board.html', '/board?hdr=0', '/api/board?names=0', '#wrap']
  ]) {
    await integration(file + ' clears loaded display after child expiry', () => withPage(url, role('guest'),
      async ({window, doc, find, calls, setIdentity}) => {
        assert(!find('#kevenos-profile'));
        assert(calls.some(call => call.pathname === api));
        assert.match(doc.querySelector(content).textContent, /Fixture/);
        setIdentity({expired: true});
        await assert.rejects(window.eval('KEV.profile.session(true)'), /expired/i);
        assert.equal(doc.querySelector(content).textContent, '');
        assert(window.eval('profileStopped'));
      }, {file, child: true, dataFor: pathname => pathname === api ? displayState : null}));
  }
  await integration('local Admin navbar prompts before discarding unsaved content', () => withPage('/admin#content', {},
    async ({window, doc, find}) => {
      assert(!doc.getElementById('app').hidden);
      window.eval('ctDirty()');
      find('[aria-controls="kevenos-nav-links"]').click();
      find('#kevenos-nav-links a[href="/desk"]').click();
      await tick();
      assert.match(window.lastConfirm || '', /Discard unsaved changes/);
      assert.equal(window.location.pathname, '/admin');
      assert(window.eval('contentDirty'));
    }, {file: 'admin.html', local: true, dataFor: pathname => pathname === '/api/info' ? null : {}}));
  await integration('help header sticks to the dialog top', () => {
    const css = fs.readFileSync(path.join(pages, 'app.css'), 'utf8');
    assert(/\.usage-dialog > header\s*\{[^}]*top:\s*0\s*;/.test(css), 'expected sticky top: 0');
  });
  if (failures.length) throw new Error(failures.join('\n'));
    console.log('Shared navigation role, profile, keyboard, and presenter checks passed.');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
