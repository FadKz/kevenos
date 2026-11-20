// Shared helpers for KevenOS.
const KEV = {
  context: document.body.dataset.eventContext || '',
  workspaceMode: document.body.dataset.eventMode || '',
  workspaceChanging: false,
  checkContext(response, data) {
    const context = response.headers.get('X-Event-Context') || data?.workspace?.context;
    if (data?.code === 'workspace_changed' || (context && KEV.context && context !== KEV.context)) {
      if (!KEV.workspaceChanging) {
        KEV.workspaceChanging = true;
        location.reload();
      }
    }
    if (KEV.workspaceChanging) {
      const error = new Error('The event dataset changed. Reloading this page.');
      error.status = 409;
      error.code = 'workspace_changed';
      throw error;
    }
    if (context) KEV.context = context;
  },
  tokenKey: (role, st) => 'kevenos:token:' + role + (st ? ':' + st : ''),
  async api(path, opts, token) {
    opts = opts || {};
    const h = {'Content-Type': 'application/json'};
    if (KEV.workspaceChanging) KEV.checkContext({headers: {get: () => null}});
    if (token) h['X-Token'] = token;
    if (opts.body && KEV.context) h['X-Event-Context'] = KEV.context;
    const k = new URLSearchParams(location.search).get('key'); if (k) h['X-Key'] = k;     // shared public links carry a key
    const url = new URL(path, location.origin);
    if (['/api/public','/api/board'].includes(url.pathname) &&
      (KEV.nav.query.get('names')==='0' || (KEV.nav.compact && !KEV.nav.query.has('key')))) url.searchParams.set('names','0');
    const accessRevision = KEV.profile.revision;
    const r = await KEV.profile.fetch(url.pathname + url.search, {method: opts.body ? 'POST' : 'GET', headers: h, body: opts.body ? JSON.stringify(opts.body) : undefined, signal: opts.signal, cache: 'no-store', credentials: 'same-origin'});
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (accessRevision !== KEV.profile.revision && !['/api/access/login','/api/access/logout','/api/access/impersonate','/api/access/impersonate/end'].includes(url.pathname)) throw KEV.profile.error();
    KEV.profile.check(r,j,url.pathname,KEV.profile.context);
    KEV.checkContext(r, j);
    if (!r.ok) {
      const err = new Error(j.error || ('HTTP ' + r.status));
      err.status = r.status;
      err.code = j.code;
      if (j.code === 'preliminary_sheets_missing') err.details = {missing:j.missing, review_token:j.review_token};
      throw err;
    }
    if (url.pathname === '/api/access/login' && KEV.profile.hosted) { KEV.profile.transitioning=true; KEV.profile.clearView('Opening your profile.'); KEV.profile.identity=null; KEV.profile.loaded=false; KEV.profile.clearToken(); KEV.profile.clearLegacy(); KEV.profile.context = r.headers.get('X-Access-Context') || j.context || ''; KEV.profile.signalChange(); }
    return j;
  },
  mmss(s) { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); },
  esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])); },
  memberList(i = {}) { return Array.isArray(i.members) ? i.members.map((m, n) => typeof m === 'string' ? {id: n + 1, name: m, coach: 0} : m) : [1, 2].map(id => ({id, name: i['p' + id] || '', coach: i['coach' + id] || 0})); },
  members(i = {}) { const names = KEV.memberList(i).map(m => m.name).filter(Boolean); return names.length > 2 ? names.slice(0, -1).join(', ') + ' and ' + names.at(-1) : names.join(' and '); },
  finalStatus(f = {}) { return f?.status || (f?.active ? 'pitch' : ''); },
  finalLabel(f = {}) { return ({prepared: 'Finalists selected', ready: 'Ready for the stage', pitch: 'Final pitch', collecting: 'Judges are scoring', review: 'Judges are confirming results', completed: 'Final results published'})[KEV.finalStatus(f)] || 'The Final'; },
  beep(n) {
    try {
      const a = KEV._ac || (KEV._ac = new (window.AudioContext || window.webkitAudioContext)());
      for (let k = 0; k < n; k++) {
        const o = a.createOscillator(), g = a.createGain();
        o.connect(g); g.connect(a.destination); o.frequency.value = 880;
        const t = a.currentTime + k * 0.35; g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.4, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);
        o.start(t); o.stop(t + 0.3);
      }
    } catch (e) {}
  },
  STEPS: ['Discover', 'Solution at Work', 'Solve', 'Close']
};

// One effective identity per tab. A child profile token never enters a URL or localStorage.
KEV.profile = {
  identity: null, context: '', loaded: false, locked: false, transitioning: false, revision: 0,
  pending: null, hooks: {}, controllers: new Set(), polling: false,
  get hosted() { return document.body.dataset.scoped === '1'; },
  get token() { return this.hosted ? sessionStorage.getItem('kevenos:profile-token') || '' : ''; },
  get isPreview() { return this.identity?.impersonation?.mode === 'preview'; },
  get active() { return !this.locked && !this.transitioning; },
  label(role) { return ({guest:'Guest',participant:'Participant',judge:'Judge',desk:'Organizer',organizer:'Organizer',operator:'Engagement Operator',scoring:'Scoring Desk',admin:'Admin',registration:'Registration',home:'Home'})[role] || 'Profile'; },
  error(message='Your profile changed. Sign in again.', code='access_changed') { const error=new Error(message);error.code=code;error.status=code==='preview_read_only'?403:code==='profile_expired'?401:409;return error; },
  clearLegacy() {
    for (const key of Object.keys(localStorage)) if (key.startsWith('kevenos:token:') || key.startsWith('kevenos:judge-identity:') || key === 'kevenos:judgeName') localStorage.removeItem(key);
  },
  signalChange() { try { localStorage.setItem('kevenos:access-event',Date.now()+':'+Math.random()); } catch (error) {} },
  clearToken() { sessionStorage.removeItem('kevenos:profile-token');sessionStorage.removeItem('kevenos:profile-context'); },
  clearView(message) {
    this.revision++;
    KEV.operations?.clear();
    for (const controller of this.controllers) controller.abort();
    KEV.closeHelp?.();
    if(KEV.nav.draftDialog?.open)KEV.nav.draftDialog.close('stay');
    KEV.nav.render();
    this.hooks.onEnd?.(message);
    document.querySelectorAll('input[type="password"]').forEach(input=>{input.value='';});
  },
  invalidate(message='Your profile changed in another tab. Sign in again.') {
    if (this.locked) return;
    this.locked=true;this.lockMessage=message;this.transitioning=false;this.clearView(message);this.identity=null;
    this.render(message);
  },
  check(response, data, path, expected) {
    if (!this.hosted) return;
    if (data?.code==='access_changed' || data?.code==='profile_expired') {
      this.invalidate(data.code==='profile_expired'?'This profile has ended. Return to Admin or sign in again.':'Your access has changed. Sign in again or ask the Event Desk for help.');
      throw this.error(data.error, data.code);
    }
    if(data?.code==='dashboard_forbidden' && this.identity?.authenticated){
      this.invalidate('Your access has changed. Open Home to check your assigned dashboards.');
      throw this.error(data.error);
    }
    const context=response.headers.get('X-Access-Context') || (path.startsWith('/api/access/') ? data?.context : '') || '';
    const transition=['/api/access/login','/api/access/logout','/api/access/impersonate','/api/access/impersonate/end'].includes(path);
    if (!transition && expected && context && expected!==context) { this.invalidate();throw this.error(); }
    if (response.status===401 && expected && path!=='/api/access/login') { this.invalidate('Your sign-in has ended. Sign in again.');throw this.error('Your sign-in has ended.','profile_expired'); }
  },
  async session(refresh=false, dashboard=KEV.nav.dashboard) {
    if (!this.hosted) return KEV.staff.session();
    if (this.locked || this.transitioning) throw this.error();
    if (this.loaded && !refresh) return this.identity;
    if (this.pending) return this.pending;
    this.pending=(async()=>{
      const revision=this.revision, headers={};
      const token=this.token, expected=this.context || (token ? sessionStorage.getItem('kevenos:profile-context') || '' : '');
      if (token) headers['X-Profile-Token']=token;
      else if (dashboard) headers['X-Dashboard']=dashboard;
      if (expected) headers['X-Access-Context']=expected;
      const response=await fetch('/api/access/me',{headers,credentials:'same-origin',cache:'no-store'});
      const data=await response.json();
      if (revision!==this.revision) throw this.error();
      this.check(response,data,'/api/access/me',expected);KEV.checkContext(response,data);
      if (!response.ok) { const error=new Error(data.error || 'Cannot check your profile.');error.status=response.status;throw error; }
      if (this.identity?.authenticated && !data.authenticated) {this.invalidate('Your sign-in has ended. Sign in again.');throw this.error();}
      this.identity=data;this.context=response.headers.get('X-Access-Context') || data.context || '';this.loaded=true;
      this.render();return data;
    })();
    try {return await this.pending;} finally {this.pending=null;}
  },
  async fetch(path,opts={}) {
    const url=new URL(path,location.origin);
    if(url.origin!==location.origin) throw new Error('Use an event resource from this site.');
    const pathname=url.pathname, login=pathname==='/api/access/login';
    const exit=['/api/access/logout','/api/access/impersonate/end'].includes(pathname);
    const publicPath=['/api/info','/api/public','/api/board','/api/stations'].includes(pathname);
    if (this.hosted && !login && !exit && !publicPath) await this.session();
    if (this.hosted && !this.active && !login && !exit) throw this.error();
    if (this.hosted && this.isPreview && (opts.method || 'GET')!=='GET' && !['/api/sync','/api/access/login','/api/access/logout','/api/access/impersonate/end','/api/access/impersonate','/api/assistant/message','/api/assistant/cancel','/api/assistant/reset'].includes(pathname)) throw this.error('Preview is read-only. Return to Admin to make changes.','preview_read_only');
    const headers={...(opts.headers || {})};
    if(this.hosted) {
      for(const name of Object.keys(headers)) {
        if(['x-token','x-profile-token','x-access-context','x-dashboard'].includes(name.toLowerCase())) delete headers[name];
      }
      if(!login){
        if(this.token)headers['X-Profile-Token']=this.token;
        else if(KEV.nav.dashboard)headers['X-Dashboard']=KEV.nav.dashboard;
        if(this.context)headers['X-Access-Context']=this.context;
      }
    }
    if(KEV.context)headers['X-Event-Context']=KEV.context;
    const controller=new AbortController(), revision=this.revision, expected=login?'':this.context;
    const abort=()=>controller.abort();if(opts.signal){if(opts.signal.aborted)abort();else opts.signal.addEventListener('abort',abort,{once:true});}
    this.controllers.add(controller);
    try {
      const response=await fetch(url.pathname+url.search,{...opts,headers,signal:controller.signal,credentials:'same-origin',cache:'no-store'});
      if(revision!==this.revision && !login && !exit)throw this.error();
      this.check(response,null,pathname,expected);KEV.checkContext(response);
      return response;
    } finally {this.controllers.delete(controller);opts.signal?.removeEventListener('abort',abort);}
  },
  async mount(options={}) {
    this.hooks={...this.hooks,...options};
    if(!this.hosted || !KEV.nav.trackProfile)return null;
    if(!this.polling){this.polling=true;setInterval(()=>{if(!document.hidden && this.active && this.loaded)this.session(true).catch(()=>{});},5000);}
    try {const identity=await this.session();this.render();return identity;} catch(error){this.render(error.message);return null;}
  },
  render(message='') {
    KEV.nav.render(message);
  },
  async allowChange() {return !this.hooks.beforeChange || (await this.hooks.beforeChange())!==false;},
  async start(target) {
    if(!await this.allowChange())return false;
    const result=await KEV.api('/api/access/impersonate',{body:target});
    this.transitioning=true;this.clearView('Opening the selected profile.');
    sessionStorage.setItem('kevenos:profile-token',result.profile_token);sessionStorage.setItem('kevenos:profile-context',result.context);
    this.context=result.context;this.identity=result.identity;location.assign(result.next);return result;
  },
  async end() {
    if(!await this.allowChange())return false;
    if(!this.token){location.assign('/admin');return;}
    try {
      const result=await KEV.api('/api/access/impersonate/end',{body:{}});
      this.transitioning=true;this.clearView('Returning to Admin.');this.clearToken();this.context=result.context;this.identity=result.identity;location.assign('/admin');
    } catch(error) {
      if(error.code!=='profile_expired' && !this.locked)throw error;
      this.clearView('Returning to Admin.');this.clearToken();this.context='';this.identity=null;this.loaded=false;this.locked=false;this.lockMessage='';this.transitioning=false;
      const root=await this.session(false,'home');
      location.assign(root.authenticated && (root.role==='admin' || root.dashboards?.some(d=>d.role==='admin'))?'/admin':'/home');
    }
  },
  async recoverSavedWork() {
    const me=await this.session(true);
    if(me.role!=='admin' || me.impersonation || me.actor)throw new Error('Return to your Admin profile to recover saved work.');
    const saved=Object.keys(localStorage).filter(key=>/^kevenos:(?:workspace-draft|workspace-outbox|draft|outbox):/.test(key)).flatMap(key=>{try{return [{key,data:JSON.parse(localStorage.getItem(key))}];}catch{return [];}});
    if(!saved.length)throw new Error('There is no saved judge work on this device.');
    KEV.saveFile(new Blob([JSON.stringify({export_only:true,saved},null,2)],{type:'application/json'}),'kevenos-device-recovery.json');
    return saved.length;
  },
  async signOut({switching=false}={}) {
    if(!await this.allowChange())return false;
    this.transitioning=true;this.clearView('Signing out.');
    try {
      if(this.locked && this.token){this.clearToken();this.context='';}
      await KEV.api('/api/access/logout',{body:{}});
      this.clearToken();this.clearLegacy();this.identity=null;this.context='';this.loaded=false;this.signalChange();location.replace(switching?'/?switch=1':'/');
    } catch(error){this.locked=true;this.transitioning=false;this.lockMessage='Sign-out was not confirmed. Your private information is hidden. Retry Sign out.';this.render();throw error;}
  },
  previewControls(container, selector) {
    if(!this.isPreview || !container)return;
    const apply=()=>container.querySelectorAll(selector).forEach(control=>{if(!control.disabled)control.disabled=true;});apply();
    new MutationObserver(apply).observe(container,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled']});
  }
};
window.addEventListener('storage',event=>{if(event.key==='kevenos:access-event' && KEV.profile.hosted && KEV.profile.loaded && (KEV.profile.identity?.authenticated || KEV.profile.token))KEV.profile.invalidate();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden && KEV.profile.hosted && KEV.profile.loaded && KEV.profile.active)KEV.profile.session(true).catch(()=>{});});

// Shared site navigation keeps the route and profile disclosures independent of content polling.
KEV.nav = {
  path: location.pathname.replace(/\/$/, '') || '/',
  query: new URLSearchParams(location.search),
  localAccess: false,
  get compact() {return this.query.get('embed')==='1' || this.query.get('hdr')==='0' || window.top!==window;},
  get privateDisplay() {return this.compact || (['/live','/board'].includes(this.path) && this.query.has('key'));},
  get trackProfile() {return !this.privateDisplay || (this.compact && !this.query.has('key') && !!KEV.profile.token);},
  get dashboard() {
    if (this.privateDisplay && ['/live','/board'].includes(this.path)) return '';
    if (/^\/judge(?:\/\d+)?$/.test(this.path)) return 'judge';
    return ({'/':'home','/index.html':'home','/home':'home','/event':'guest','/guest':'guest','/participant':'participant',
      '/admin':'admin','/desk':'home','/organizer':'organizer','/operator':'operator','/scoring':'scoring','/registration':'registration',
      '/board':'home','/live':'home'})[this.path] || '';
  },
  get page() {
    const labels={'/':'Sign in','/index.html':'Sign in','/home':'Home','/event':'Event information','/guest':'Event information','/participant':'My Event',
      '/admin':'Event Administration','/desk':'Scoring Desk','/organizer':'Event Operations','/operator':'Engagement Operator','/scoring':'Judging Operations',
      '/registration':'Registration Desk','/judge':'Judge stations','/live':'Live View','/board':'Scoreboard'};
    return document.body.dataset.navPage || labels[this.path] || (/^\/judge\/\d+$/.test(this.path) ? 'Station '+this.path.split('/').pop() : 'Event');
  },
  mount() {
    if(!document.getElementById('kevenos-profile-css')){
      const link=document.createElement('link');
      link.id='kevenos-profile-css';link.rel='stylesheet';link.href='/static/profile.css';document.head.append(link);
      const roleStyles=document.createElement('link');
      roleStyles.rel='stylesheet';roleStyles.href='/static/roles.css';document.head.append(roleStyles);
    }
    const nav=document.createElement('header');
    nav.id='kevenos-nav';nav.className='kevenos-nav';
    nav.dataset.display=this.compact?'compact':this.privateDisplay?'presenter':'site';
    if(['/live','/board'].includes(this.path))nav.dataset.screen=this.path.slice(1);
    nav.innerHTML='<div class="kevenos-nav-inner">'+
      (this.compact?'':'<div class="kevenos-nav-brand"><span class="kevenos-nav-mark" aria-hidden="true">KEV</span><strong>KevenOS</strong></div>')+
      '<span id="workspace-badge" class="kevenos-nav-rehearsal" aria-label="Rehearsal — sample data" title="Rehearsal — sample data" hidden>Rehearsal</span>'+
      (this.compact?'':'<nav class="kevenos-nav-current" aria-label="Site navigation"></nav><div class="kevenos-nav-account"></div>')+
      '</div>';
    document.body.prepend(nav);
    this.element=nav;
    this.brand=nav.querySelector('.kevenos-nav-brand');
    this.current=nav.querySelector('.kevenos-nav-current');
    this.account=nav.querySelector('.kevenos-nav-account');
    document.addEventListener('click',event=>{
      if(this.open && !this.open.contains(event.target))this.closeMenu();
    });
    document.addEventListener('focusin',event=>{
      if(this.open && !this.open.contains(event.target))this.closeMenu();
    });
    nav.addEventListener('keydown',event=>{
      if(event.key==='Escape' && this.open){
        event.preventDefault();event.stopPropagation();this.closeMenu(true);
      }
    });
    const measure=()=>document.documentElement.style.setProperty('--kevenos-nav-height',nav.getBoundingClientRect().height+'px');
    if(typeof ResizeObserver!=='undefined')new ResizeObserver(measure).observe(nav);
    else window.addEventListener('resize',measure);
    this.measure=measure;
    this.render();
    if(KEV.profile.hosted && this.trackProfile)KEV.profile.mount();
  },
  closeMenu(focus=false) {
    if(!this.open)return;
    const menu=this.open;
    this.open=null;
    const toggle=menu.querySelector('[aria-expanded]');
    toggle.setAttribute('aria-expanded','false');
    menu.querySelector('.kevenos-nav-panel').hidden=true;
    if(focus)toggle.focus({preventScroll:true});
  },
  bindMenu(menu) {
    if(!menu)return;
    const toggle=menu.querySelector('[aria-expanded]');
    toggle.onclick=()=>{
      const wasOpen=this.open===menu;
      this.closeMenu();
      if(wasOpen)return;
      this.open=menu;
      toggle.setAttribute('aria-expanded','true');
      menu.querySelector('.kevenos-nav-panel').hidden=false;
    };
  },
  menu(panel, toggle, items, label='') {
    return '<div class="kevenos-nav-disclosure"><button type="button" class="kevenos-nav-toggle" aria-expanded="false" aria-controls="'+panel+'"'+label+'>'+
      toggle+'<span class="kevenos-nav-chevron" aria-hidden="true"></span></button><div id="'+panel+'" class="kevenos-nav-panel" hidden>'+items+'</div></div>';
  },
  link(href, label, current=false) {
    return '<a data-nav-link href="'+KEV.esc(href)+'"'+(current?' aria-current="page"':'')+'>'+KEV.esc(label)+'</a>';
  },
  dashboards(me) {
    if (!Array.isArray(me?.dashboards)) return [];
    return me.dashboards.filter(d => d && typeof d.label==='string' &&
      (d.role==='judge' ? /^\/judge\/\d+$/.test(d.url) : ({
        guest:['/event','/guest'],participant:['/participant'],admin:['/admin'],registration:['/registration'],
        organizer:['/organizer'],operator:['/operator'],scoring:['/scoring']
      })[d.role]?.includes(d.url)));
  },
  taskKey(me) {
    if (!me?.account_id || me.impersonation || KEV.profile.token) return '';
    return 'kevenos:last-task:'+me.account_id+':'+me.account_revision+':'+KEV.context;
  },
  remember(me) {
    const key=this.taskKey(me);
    if (!key || !this.dashboards(me).some(d=>d.url===this.path || (d.role==='guest' && this.dashboard==='guest'))) return;
    try {sessionStorage.setItem(key,this.path+location.hash);} catch (error) {}
  },
  resume(me) {
    const key=this.taskKey(me);
    if (!key) return null;
    try {
      const href=sessionStorage.getItem(key);
      return this.dashboards(me).find(d=>d.url===href?.split('#')[0]) ? href : null;
    } catch (error) {return null;}
  },
  home(me) {
    const homes={guest:['/guest','Guest'],participant:['/participant','Participant'],desk:['/desk','Scoring Desk'],admin:['/admin','Admin'],registration:['/registration','Registration Desk']};
    if(me?.role==='judge' && Number.isInteger(me.station) && me.station>0)return ['/judge/'+me.station,'Station '+me.station];
    return homes[me?.role];
  },
  entries(me, locked) {
    if(locked || this.privateDisplay)return '';
    const links=[];
    const views=[['/live','Live View'],['/board','Scoreboard']];
    if(!KEV.profile.hosted && this.localAccess)links.push(['/','Home'],['/desk','Scoring Desk'],['/judge','Judge stations'],['/admin','Admin']);
    const home=this.home(me);
    if(home)links.push(home);
    if(me?.role==='admin')links.push(['/desk','Scoring Desk'],['/registration','Registration Desk']);
    let items=links.map(([href,label])=>this.link(href,label,href===this.path)).join('');
    if((me && me.role!=='registration') || this.localAccess)items+=views.map(([href,label])=>this.link(href+'?names=0',label,href===this.path)).join('');
    const help=this.help(me);
    if(help)items+='<button type="button" data-kevenos-help="'+help[0]+'" aria-haspopup="dialog">'+help[1]+'</button>';
    return items;
  },
  help(me) {
    const help={
      '/admin':['admin-help','Admin guide',['admin']],
      '/desk':['desk-help','Desk guide',['desk','admin']],
      '/guest':['guest-help','Guest guide',['guest']],
      '/participant':['participant-help','App guide',['participant']],
      judge:['judge-help','How to judge',['judge']]
    }[/^\/judge\/\d+$/.test(this.path)?'judge':
      ['/organizer','/scoring'].includes(this.path)?'/desk':this.path==='/event'?'/guest':this.path];
    if(!help || !me || !help[2].includes(me.role))return null;
    const trigger=document.getElementById(help[0]);
    return trigger && !trigger.closest('[hidden]') ? help : null;
  },
  update(node, html) {
    if(node.dataset.signature===html)return false;
    if(this.open && node.contains(this.open))this.closeMenu();
    node.dataset.signature=html;
    node.innerHTML=html;
    return true;
  },
  async action(control, run) {
    if(control.tagName==='A')control.setAttribute('aria-busy','true');
    else control.disabled=true;
    const error=this.account.querySelector('.kevenos-profile-error');
    if(error)error.textContent='';
    try {await run();}
    catch(problem){
      const currentError=this.account.querySelector('.kevenos-profile-error');
      if(currentError && !currentError.textContent)currentError.textContent=problem.message || 'Could not open this page. Try again.';
    } finally {
      if(control.tagName==='A')control.removeAttribute('aria-busy');
      else control.disabled=false;
    }
  },
  helpButton(help) {
    return help ? '<button type="button" data-kevenos-help="'+help[0]+'" aria-haspopup="dialog">'+help[1]+'</button>' : '';
  },
  async follow(href, guardedDashboard) {
    if (guardedDashboard && KEV.profile.hooks.navigationBusy?.()) return;
    if (guardedDashboard && KEV.profile.hooks.hasPendingWork?.()) {
      if (!this.draftDialog) {
        const dialog=document.createElement('dialog');
        dialog.className='card';
        dialog.setAttribute('aria-labelledby','kevenos-draft-title');
        dialog.innerHTML='<h2 id="kevenos-draft-title">You have saved judge work</h2>'+
          '<p>Your draft stays on this device until your sheet is accepted.</p>'+
          '<form method="dialog" class="row"><button class="btn ghost" value="stay" autofocus>Stay</button>'+
          '<button class="btn" value="switch">Keep draft and switch</button>'+
          '<a class="btn ghost" target="_blank" rel="noopener">Open other dashboard in a new tab</a></form>';
        dialog.querySelector('a').onclick=()=>dialog.close('stay');
        document.body.append(dialog);this.draftDialog=dialog;
      }
      const dialog=this.draftDialog;
      if (dialog.open) return;
      dialog.returnValue='stay';
      dialog.querySelector('a').href=href;
      const choice=await new Promise(resolve=>{
        dialog.addEventListener('close',()=>resolve(dialog.returnValue),{once:true});
        dialog.showModal();
      });
      if(choice!=='switch') return;
    } else if (!await KEV.profile.allowChange()) return;
    this.closeMenu();
    this.remember(KEV.profile.identity);
    location.assign(href);
  },
  render(message='') {
    if(!this.element)return;
    const nav=this.element;
    const badge=nav.querySelector('#workspace-badge');
    badge.hidden=KEV.workspaceMode!=='rehearsal';
    nav.hidden=this.compact && badge.hidden;
    if(this.compact){this.measure();return;}
    const profile=KEV.profile;
    const me=profile.identity?.authenticated && profile.active && !this.privateDisplay ? profile.identity : null;
    const locked=profile.hosted && profile.locked && !this.privateDisplay;
    const child=!!profile.token;
    const unified=profile.hosted && (Array.isArray(me?.dashboards) || child);
    const dashboards=child?[]:this.dashboards(me);
    const help=this.help(me);
    const entries=unified?dashboards.map(d=>this.link(d.url,d.label,d.role===me?.dashboard)).join(''):this.entries(me,locked);
    const brand='<span class="kevenos-nav-mark" aria-hidden="true">KEV</span><strong>KevenOS</strong>';
    const home=this.home(me);
    const homeUrl=profile.hosted?'/home':home?.[0] || '/';
    this.update(this.brand,this.privateDisplay || profile.transitioning ? brand :
      '<a data-nav-link href="'+homeUrl+'"'+(this.path==='/home'?' aria-current="page"':'')+'>'+brand+'</a>');
    const landing=this.path==='/' || this.path==='/index.html';
    const title='<span class="kevenos-nav-label">'+KEV.esc(this.page)+'</span>';
    const switcher=entries && (!unified || dashboards.length>1) ?
      this.menu('kevenos-nav-links','<span>'+KEV.esc(this.page)+'</span>',entries,
        unified?' aria-label="Switch dashboard from '+KEV.esc(this.page)+'"':' aria-label="'+KEV.esc(this.page)+' navigation"') :
      landing && !locked ? '' : title;
    this.update(this.current,switcher+(unified?this.helpButton(help):''));
    this.bindMenu(this.current.querySelector('.kevenos-nav-disclosure'));
    this.current.querySelectorAll('[data-kevenos-help]').forEach(button=>button.onclick=()=>{
      const trigger=document.getElementById(button.dataset.kevenosHelp);
      if(!trigger)return;
      this.helpReturn=button.closest('.kevenos-nav-disclosure')?.querySelector('[aria-expanded]') || button;
      this.closeMenu(true);
      try {trigger.click();} finally {this.helpReturn=null;}
    });
    document.body.classList.toggle('kevenos-nav-shared-help',!!help);
    let account='';
    if(me || locked){
      const mode=me?.impersonation?.mode;
      const role=profile.label(me?.dashboard || me?.role);
      const roles=dashboards.map(d=>profile.label(d.role)).join(', ');
      const name=locked?'Access changed':(me.account_label || me.label || role);
      const detail=(locked ? profile.lockMessage : message) || (mode==='preview'?'Preview — read-only. Changes are disabled.':mode==='support'?'Support mode — changes are recorded as Admin assistance.':'');
      const admin=!child && (dashboards.some(d=>d.role==='admin') || !unified && me?.role==='admin');
      const items=(roles?'<p class="kevenos-nav-roles">'+KEV.esc(roles)+'</p>':'')+
        (admin?this.link('/admin#profiles','View as a person'):'')+
        (locked?this.link('/','Sign in again'):'<p class="kevenos-nav-roles">For password help, ask the Event Desk.</p><button type="button" data-profile="switch">Sign in as someone else</button>')+
        '<button type="button" data-profile="out">Sign out</button>';
      const initial=locked?'!':Array.from(name.trim())[0]?.toUpperCase() || '?';
      const identity='<span class="kevenos-nav-avatar" aria-hidden="true">'+KEV.esc(initial)+'</span><span class="kevenos-profile-name"><strong>'+
        KEV.esc(name)+'</strong>'+(locked?'':'<span>'+KEV.esc(dashboards.length===1?profile.label(dashboards[0].role):role)+'</span>')+'</span>';
      account='<section id="kevenos-profile" class="kevenos-profile" data-mode="'+(mode==='preview'||mode==='support'?mode:'')+'" aria-label="Your profile">'+
        this.menu('kevenos-profile-actions',identity,items,' aria-label="'+KEV.esc(locked?name:name+' · '+role)+'"')+
        (detail || child?'<div class="kevenos-profile-status">'+(detail?'<span role="status">'+KEV.esc(detail)+'</span>':'')+
          (child?'<button type="button" data-profile="end">Return to Admin</button>':'')+'</div>':'')+
        '<p class="kevenos-profile-error" role="alert"></p></section>';
    }else if(!this.privateDisplay && !profile.transitioning){
      if(!profile.hosted && this.localAccess){
        account='<button class="kevenos-nav-signin" type="button" data-kevenos-local-signout>Sign out</button><p class="kevenos-profile-error" role="alert"></p>';
      }else if(landing){
        account='<a class="kevenos-nav-signin" href="#password" data-nav-link>Sign in</a>';
      }else account='<a class="kevenos-nav-signin" href="/" data-nav-link>Sign in</a>';
    }
    this.update(this.account,account);
    this.bindMenu(this.account.querySelector('.kevenos-nav-disclosure'));
    this.account.querySelectorAll('[data-profile]').forEach(button=>button.onclick=()=>this.action(button,()=>button.dataset.profile==='end' ?
      profile.end() : profile.signOut({switching:button.dataset.profile==='switch'})));
    const localSignOut=this.account.querySelector('[data-kevenos-local-signout]');
    if(localSignOut)localSignOut.onclick=()=>{
      const trigger=document.querySelector('[data-nav-signout]:not([hidden])');
      return this.action(localSignOut,async()=>{
        if(!await profile.allowChange())return;
        if(trigger)trigger.click();
        else {await KEV.api('/api/site-logout',{body:{}});location.reload();}
      });
    };
    nav.querySelectorAll('[data-nav-link]').forEach(link=>{
      const follow=async event=>{
        if(event.type==='auxclick' && event.button!==1)return;
        if(event.metaKey || event.ctrlKey || event.shiftKey || event.type==='auxclick'){
          if(!profile.token) return;
          // A fresh tab has the parent cookie. Keep a preview or support navigation in this tab.
          event.preventDefault();
          return;
        }
        if(link.getAttribute('aria-busy')==='true'){event.preventDefault();return;}
        event.preventDefault();
        const href=link.getAttribute('href');
        await this.action(link,()=>this.follow(href,href==='/home' || dashboards.some(d=>d.url===href)));
      };
      link.onclick=follow;
      link.onauxclick=follow;
    });
    if(me)this.remember(me);
    this.measure();
  }
};
KEV.nav.mount();
window.addEventListener('hashchange',()=>KEV.nav.remember(KEV.profile.identity));

// small version stamp on every page, so everyone can confirm they are on the same app and text
KEV.stamp = function (v) {
  if (!v || typeof document === 'undefined') return;
  let el = document.getElementById('ppver');
  if (!el) { el = document.createElement('div'); el.id = 'ppver'; el.style.cssText = 'position:fixed;right:8px;bottom:2px;font:600 10px ui-monospace,Menlo,monospace;color:#8e8e93;opacity:.8;z-index:5;pointer-events:none'; (document.body || document.documentElement).appendChild(el); }
  el.textContent = 'app v' + v.app + ' · content v' + v.content + (v.content_source === 'edited' ? '*' : '');
};
KEV.showWorkspace = function (mode) {
  if (!mode) return;
  KEV.workspaceMode = mode;
  KEV.nav.render();
};
KEV.showWorkspace(KEV.workspaceMode);
KEV.refreshInfo = () => KEV.api('/api/info').then(i => {
  KEV.nav.localAccess = !KEV.profile.hosted && !!i.authed;
  KEV.showWorkspace(i.workspace?.mode || (i.rehearsal ? 'rehearsal' : 'live'));
  KEV.stamp(i.version);
}).catch(() => {});
KEV.refreshInfo();
if (['/', '/home', '/admin', '/participant', '/event', '/guest'].includes(location.pathname.replace(/\/$/, '') || '/')) {
  setInterval(KEV.refreshInfo, 8000);
}

KEV.focusedControl = function (container, el = document.activeElement) {
  if (!el || !container.contains(el)) return () => {};
  const attr = ['id', 'data-s', 'data-i', 'data-info', 'data-m', 'data-t', 'data-q', 'data-raise', 'data-fi', 'data-r', 'data-sa', 'data-a', 'data-k', 'data-sheet-download'].find(name => el.hasAttribute(name));
  if (!attr) return () => {};
  const value = el.getAttribute(attr);
  const station = el.getAttribute('data-st');
  const selection = typeof el.selectionStart === 'number' ? [el.selectionStart, el.selectionEnd] : null;
  return () => {
    const target = [...container.querySelectorAll('[' + attr + ']')].find(node => node.getAttribute(attr) === value && node.getAttribute('data-st') === station);
    if (!target || target.disabled) return;
    target.focus({preventScroll: true});
    if (selection) target.setSelectionRange(...selection);
  };
};

KEV.finalClock = function (clock, secondsSinceUpdate = 0) {
  const seconds = Math.max(0, clock.round_left - (clock.running ? secondsSinceUpdate : 0));
  return {
    time: KEV.mmss(Math.ceil(seconds)),
    label: seconds <= 0 ? 'Time is up' : clock.running ? 'Final pitch' : 'Final clock paused',
    ended: seconds <= 0
  };
};

// The local judging kit still accepts its organiser PIN. Hosted staff sessions
// come from the HttpOnly access cookie and never enter browser storage.
KEV.staff = {
  get hosted() { return document.body.dataset.scoped === '1'; },
  get token() { return this.hosted ? null : localStorage.getItem(KEV.tokenKey('desk')); },
  async session() {
    if (this.hosted) return KEV.profile.session();
    return {authenticated: Boolean(this.token), role: 'admin', label: 'Organiser'};
  },
  async localLogin(pin) {
    if (this.hosted) throw new Error('Use the event sign-in page.');
    const result = await KEV.api('/api/login', {body: {role: 'desk', pin}});
    localStorage.setItem(KEV.tokenKey('desk'), result.token);
  },
  async signOut() {
    if (this.hosted) return KEV.profile.signOut();
    localStorage.removeItem(KEV.tokenKey('desk'));
  }
};

KEV.saveFile = function (blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
};

KEV.loadHelp = async function (panel, {token, path = '/api/help', title} = {}) {
  const esc = KEV.esc;
  const moveFocus = panel.contains(document.activeElement);
  panel.innerHTML = '<p role="status">Loading help…</p>';
  if (moveFocus) panel.focus({preventScroll: true});
  try {
    const guide = await KEV.api(path, null, token);
    if (!panel.isConnected || !panel.closest('dialog')?.open) return;
    if (title) title.textContent = guide.title;
    const downloads = guide.downloads.filter(file => /^[a-z0-9-]+$/.test(file.id) && file.url === '/api/resources/' + file.id);
    panel.innerHTML = '<p class="usage-summary">' + esc(guide.summary) + '</p><h3>Quick start</h3><ol class="usage-steps">' +
      guide.steps.map(step => '<li><strong>' + esc(step.title) + '</strong><p>' + esc(step.body) + '</p></li>').join('') + '</ol>' +
      guide.sections.map(section => '<details class="usage-topic"><summary>' + esc(section.title) + '</summary><ul>' +
        section.items.map(item => '<li>' + esc(item) + '</li>').join('') + '</ul></details>').join('') +
      (downloads.length ? '<h3 class="usage-download-title">Downloads</h3><div class="row usage-downloads">' + downloads.map(file =>
        '<button class="btn alt" type="button" data-help-resource="' + esc(file.id) + '">' + esc(file.label) + '</button>').join('') +
        '</div><p role="alert" class="usage-error"></p>' : '');
    for (const button of panel.querySelectorAll('[data-help-resource]')) {
      const file = downloads.find(entry => entry.id === button.dataset.helpResource);
      button.onclick = async () => {
        const error = panel.querySelector('[role="alert"]');
        button.disabled = true;
        error.textContent = '';
        try {
          if (KEV.workspaceChanging) return;
          const response = await KEV.profile.fetch(file.url, {headers: token ? {'X-Token': token} : {}, cache: 'no-store', credentials: 'same-origin'});
          KEV.checkContext(response);
          if (!response.ok) { let data={};try{data=await response.json();}catch{}KEV.profile.check(response,data,file.url,KEV.profile.context);throw new Error('Resource unavailable'); }
          const blob = await response.blob();
          KEV.checkContext(response);
          if (!panel.isConnected || !panel.closest('dialog')?.open) return;
          const filename = /filename="([^"/\\]+\.pdf)"/i.exec(response.headers.get('Content-Disposition') || '')?.[1];
          KEV.saveFile(blob, filename || file.id + '.pdf');
        } catch {
          error.textContent = 'The PDF could not be downloaded. Try again or ask the scoring desk for a copy.';
        } finally {
          button.disabled = false;
        }
      };
    }
  } catch (error) {
    if (!panel.isConnected || error.code === 'workspace_changed') return;
    panel.innerHTML = '<p role="alert" class="usage-error">Could not load this guide.</p>' +
      '<button type="button" class="btn alt" data-help-retry>Try again</button>';
    panel.querySelector('[data-help-retry]').onclick = () => KEV.loadHelp(panel, {token, path, title});
  }
};

KEV.bindHelp = function (trigger, getToken = () => null, path = '/api/help') {
  trigger.setAttribute('aria-haspopup', 'dialog');
  trigger.onclick = () => {
    const returnFocus = KEV.nav.helpReturn || trigger;
    const dialog = document.createElement('dialog');
    dialog.id = 'role-help-dialog';
    dialog.className = 'usage-dialog';
    dialog.setAttribute('aria-labelledby', 'role-help-title');
    dialog.innerHTML = '<header class="row"><h2 class="grow" id="role-help-title">' + KEV.esc(trigger.textContent) +
      '</h2><button type="button" class="btn ghost">Close</button></header><div class="usage-content" tabindex="-1"></div>';
    const close = dialog.querySelector('button');
    close.onclick = () => dialog.close();
    dialog.addEventListener('cancel', event => { event.preventDefault(); dialog.close(); });
    dialog.addEventListener('close', () => {
      dialog.remove();
      if (returnFocus.isConnected && !returnFocus.closest('[hidden]')) returnFocus.focus({preventScroll: true});
    }, {once: true});
    document.body.append(dialog);
    dialog.showModal();
    close.focus({preventScroll: true});
    KEV.loadHelp(dialog.querySelector('.usage-content'), {token: getToken(), path, title: dialog.querySelector('h2')});
  };
};

KEV.closeHelp = function () {
  const dialog = document.getElementById('role-help-dialog');
  if (dialog?.open) dialog.close();
};

KEV.copyInput = async function (input, status, label = 'Link') {
  try {
    await navigator.clipboard.writeText(input.value);
    status.textContent = label + ' copied.';
  } catch {
    input.focus();
    input.select();
    status.textContent = label + ' selected. Copy it from the field.';
  }
};

// Protected downloads always retain the tab's effective profile, including previews.
KEV.download = async function(path, filename) {
  const revision=KEV.profile.revision;
  const response=await KEV.profile.fetch(path);
  if(!response.ok){let data={};try{data=await response.json();}catch{}KEV.profile.check(response,data,new URL(path,location.origin).pathname,KEV.profile.context);throw new Error(data.error || 'This download is unavailable.');}
  const blob=await response.blob();
  if(revision!==KEV.profile.revision || !KEV.profile.active)throw KEV.profile.error();
  KEV.saveFile(blob,filename || /filename="([^"/\\]+)"/i.exec(response.headers.get('Content-Disposition') || '')?.[1] || 'event-download.pdf');
};
document.addEventListener('click',async event=>{
  const link=event.target.closest('[data-resource],a[href^="/api/resources/"]');
  if(!link || (!KEV.profile.hosted && !link.dataset.resource))return;event.preventDefault();
  let status=link.parentElement.querySelector('[data-download-status]');
  if(!status){status=document.createElement('span');status.dataset.downloadStatus='1';status.setAttribute('role','status');link.after(status);}
  status.textContent='Downloading…';
  try{await KEV.download(link.dataset.resource ? '/api/resources/'+encodeURIComponent(link.dataset.resource) : link.getAttribute('href'));status.textContent='Downloaded.';}catch(error){if(status.isConnected)status.textContent=error.message;}
});
