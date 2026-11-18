(() => {
  const $ = id => document.getElementById(id);
  const esc = KEV.esc;
  const api = '/api/registration';
  let state, editor, handoff, selected, receipt, busy = false, generation = 0, request = 0;

  function status(message) { $('registration-status').textContent = message; }
  function teamRef(team) { return 'Team ' + team.pair + ' · Station ' + team.station + ' · Round ' + team.round; }
  function openSlots() {
    return state.teams.flatMap(team => team.members.filter(member => !member.name).map(member => ({team, member})));
  }

  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();
  function timeLabel(value) { return value ? new Date(value * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : ''; }
  function resolve(key) {
    const [pair, member] = (key || '').split(':').map(Number);
    const team = state?.teams.find(item => item.pair === pair);
    const person = team?.members.find(item => item.id === member);
    return person?.name ? {team,person} : null;
  }
  function control(team, person, action, label, style='ghost') {
    const locked = state.roster_locked && ['move','swap','correct'].includes(action);
    return '<button type="button" class="btn sm '+style+'" data-action="'+action+'" data-slot="'+team.pair+':'+person.id+'"'+
      (locked ? ' disabled' : '')+' aria-label="'+esc(label+' — '+person.name)+'">'+esc(label)+'</button>';
  }
  function working(value) {
    $('registration-list').setAttribute('aria-busy',String(value));
    document.querySelectorAll('.registration [data-action], #registration-detail [data-action], #registration-form button[type=submit], #add-participant, #refresh-registration, #registration-home-add, #registration-next').forEach(button => {
      if (value) { if (!Object.hasOwn(button.dataset,'wasDisabled')) button.dataset.wasDisabled=String(button.disabled); button.disabled=true; }
      else if (Object.hasOwn(button.dataset,'wasDisabled')) { button.disabled=button.dataset.wasDisabled==='true';delete button.dataset.wasDisabled; }
    });
  }
  function renderDetail() {
    const item=resolve(selected);if(!item){if($('registration-detail').open)$('registration-detail').close();return;}
    const {team,person}=item;
    $('registration-detail-error').textContent='';
    $('registration-detail-content').innerHTML='<h2 id="registration-detail-title">'+esc(person.name)+'</h2><p>'+esc(team.partner)+'</p><p class="registration-assignment">'+esc(teamRef(team))+'</p><p class="mut">'+esc(team.scenario)+'</p><p><span class="arrival-status '+(person.checked_in?'present':'')+'">'+(person.checked_in?'Checked in'+(person.checked_in_at?' · '+timeLabel(person.checked_in_at):''):'Awaiting arrival')+'</span></p><div class="row">'+
      control(team,person,'checkin',person.checked_in?'Undo check-in':'Check in',person.checked_in?'ghost':'alt')+control(team,person,'qr','Show sign-in QR','')+'</div><h3>Your team</h3><ul class="registration-teammates">'+team.members.map(member=>'<li><strong>'+esc(member.name || 'Open slot')+'</strong><span>'+(!member.name?'Available':member.checked_in?'Checked in':'Awaiting arrival')+'</span></li>').join('')+'</ul><p class="small mut">Confirm identity before sharing access. A QR is private, single-use and expires after five minutes.</p><details><summary>Manage team &amp; access</summary><p class="small mut">'+(state.roster_locked?'Team changes are locked while judging is underway. Contact the Scoring Desk.':'Name or team changes require fresh participant access.')+'</p><div class="registration-secondary">'+
      control(team,person,'correct','Correct name')+control(team,person,'move','Move to an open slot')+control(team,person,'swap','Swap participants')+
      (person.has_password?control(team,person,'reset','Reset password')+control(team,person,'revoke','Revoke access'):control(team,person,'issue','Issue typed password'))+'</div></details>';
  }
  function showReceipt(key) {
    const item=resolve(key);if(!item || !item.person.checked_in)return;
    const {team,person}=item;receipt={key,name:person.name};
    $('registration-receipt-title').textContent=person.name+' is checked in';
    $('registration-receipt-assignment').textContent=teamRef(team)+' · '+team.partner;
    $('registration-receipt-actions').innerHTML=control(team,person,'qr','Show sign-in QR','')+control(team,person,'details','View team');
    $('registration-receipt').hidden=false;
    $('registration-receipt-title').focus();
  }
  function render() {
    const named=state.teams.flatMap(team=>team.members.filter(person=>person.name).map(person=>({team,person})));
    const present=named.filter(({person})=>person.checked_in).length;
    $('total-present').textContent=present;
    $('total-expected').textContent=named.length-present;
    $('total-open').textContent=openSlots().length;
    $('add-participant').disabled=state.roster_locked || !openSlots().length;
    $('registration-home-add').disabled=$('add-participant').disabled;
    $('registration-home-status').textContent=state.roster_locked?'Team changes are locked. Ask the Scoring Desk about changes to teams already judging.':'Confirm identity before showing a QR, changing a password or moving a team member.';
    const query=normalize($('registration-search').value.trim()),filter=$('registration-filter').value;
    const rows=named.filter(({team,person})=>{
      if(filter==='waiting' && person.checked_in || filter==='present' && !person.checked_in || filter==='access' && person.has_password || filter==='incomplete' && !team.members.some(member=>!member.name))return false;
      if(/^\d+$/.test(query))return team.pair===Number(query);
      const text=normalize([person.name,team.partner,teamRef(team)].join(' '));
      return query.split(/\s+/).every(word=>text.includes(word));
    }).sort((a,b)=>a.person.name.localeCompare(b.person.name,undefined,{sensitivity:'base'}) || a.team.pair-b.team.pair || a.person.id-b.person.id);
    $('registration-list').innerHTML=rows.length?'<table class="registration-table" aria-label="Participant arrivals"><thead><tr><th scope="col">Participant</th><th scope="col">Team / assignment</th><th scope="col">Arrival</th><th scope="col">Actions</th></tr></thead><tbody>'+rows.map(({team,person})=>'<tr><th scope="row"><strong>'+esc(person.name)+'</strong><span class="registration-company">'+esc(team.partner || 'Company not set')+'</span></th><td data-label="Assignment"><strong>Team '+team.pair+'</strong><span class="registration-sub">Station '+team.station+' · Round '+team.round+'</span></td><td data-label="Arrival"><span class="arrival-status '+(person.checked_in?'present':'')+'">'+(person.checked_in?'Checked in':'Awaiting arrival')+'</span><span class="registration-sub">'+(person.checked_in?timeLabel(person.checked_in_at):person.has_password?'Access ready':'Access needed')+'</span></td><td class="registration-row-actions">'+
      (person.checked_in?'':control(team,person,'checkin','Check in','alt'))+control(team,person,'details','Details')+'</td></tr>').join('')+'</tbody></table>':'<div class="registration-empty"><h3>No participants found</h3><p>Try a name, company or team number, or clear the filters.</p></div>';
    $('registration-count').textContent=rows.length+' of '+named.length+' participants · '+present+' checked in · '+(named.length-present)+' awaiting arrival';
    $('registration-app').hidden=false;
    KEV.operations?.mount($('registration-operations'));
    if(receipt && (!resolve(receipt.key)?.person.checked_in || resolve(receipt.key)?.person.name!==receipt.name)){receipt=null;$('registration-receipt').hidden=true;}
    if($('registration-detail').open)renderDetail();
    if(busy)working(true);
  }

  function clearAccess() {
    handoff = null;
    $('registration-password').value = '';
    $('registration-qr').removeAttribute('src');
    $('registration-expiry').textContent = '';
    $('registration-copy-status').textContent = '';
    $('registration-copy-link').disabled = false;
  }

  function clear(message) {
    dismissReport();
    generation++;
    $('open-registration-report').disabled = false;
    busy = false;
    state = editor = selected = receipt = null;
    $('registration-receipt').hidden=true;
    if($('registration-detail').open)$('registration-detail').close();
    $('registration-app').hidden = true;
    $('registration-list').replaceChildren();
    if ($('registration-edit').open) $('registration-edit').close();
    if ($('registration-access').open) $('registration-access').close();
    clearAccess();
    status(message);
    $('registration-signin').hidden = false;
  }

  async function refresh(conflict=false) {
    if(busy && conflict !== true)return;
    const current = generation, serial=++request;
    try {
      const value = await KEV.api(api);
      if (generation !== current || serial!==request) return;
      state = value;
      $('registration-updated').textContent='Updated '+timeLabel(Date.now()/1000);
      $('registration-signin').hidden = true;
      status('');
      render();
    } catch (error) {
      if (generation !== current || serial!==request) return;
      if ([401,403].includes(error.status)) clear(error.message);
      else status(error.message);
    }
  }

  const reportDate = new Intl.DateTimeFormat('en-GB', {
    timeZone:'UTC', day:'numeric', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit'
  });
  const reportTime = new Intl.DateTimeFormat('en-GB', {timeZone:'UTC',hour:'2-digit',minute:'2-digit'});
  const deskTitle = document.title;

  function dismissReport() {
    document.body.classList.remove('registration-report-active');
    $('registration-report').hidden = true;
    $('registration-report-content').replaceChildren();
    document.title = deskTitle;
  }

  function reportMarkup(snapshot) {
    const teams = [...snapshot.teams].sort((a,b)=>a.pair-b.pair);
    const roster = teams.flatMap(team=>team.members.filter(member=>member.name).map(member=>({team,member})));
    roster.sort((a,b)=>a.member.name.localeCompare(b.member.name,undefined,{sensitivity:'base'}) ||
      a.team.pair-b.team.pair || a.member.id-b.member.id);
    const present = roster.filter(({member})=>member.checked_in).length;
    const vacant = teams.reduce((sum,team)=>sum+team.members.filter(member=>!member.name).length,0);
    const stamp = esc(reportDate.format(new Date())) + ' event time';
    const mode = snapshot.rehearsal ? 'Rehearsal' : 'Live';
    const meta = '<p class="report-meta">' + mode + ' · Organizer copy · ' + stamp + '</p>';
    const registrationRows = roster.map(({team,member})=>{
      const arrival = member.checked_in ? 'Checked in' : 'Awaiting arrival';
      const time = member.checked_in && member.checked_in_at ? ' · ' + esc(reportTime.format(member.checked_in_at*1000)) : '';
      return '<tr><th scope="row" dir="auto">'+esc(member.name)+'</th><td dir="auto">'+esc(team.partner || '—')+
        '</td><td>'+esc(team.pair)+'</td><td>'+arrival+time+'</td></tr>';
    }).join('');
    const assignmentRows = teams.map(team=>{
      const members = team.members.map(member=>'<li><bdi>'+esc(member.name || 'Open slot')+'</bdi></li>').join('');
      return '<tr><th scope="row">'+esc(team.pair)+'</th><td dir="auto">'+esc(team.partner || '—')+
        '</td><td><ul>'+members+'</ul></td><td>Station '+esc(team.station)+'<br>Round '+esc(team.round)+
        '</td><td dir="auto">'+esc(team.scenario || '—')+'</td></tr>';
    }).join('');
    return '<h1 id="registration-report-title" tabindex="-1">KevenOS Registration</h1>' + meta +
      '<p class="report-totals">'+present+' checked in · '+(roster.length-present)+' awaiting arrival · '+vacant+(vacant===1?' open slot':' open slots')+'</p>'+
      '<h2>Check-in list</h2><table aria-label="Alphabetical check-in list" class="report-check-in"><thead><tr>'+
      '<th scope="col">Participant</th><th scope="col">Company</th><th scope="col">Team</th><th scope="col">Arrival</th>'+
      '</tr></thead><tbody>'+(registrationRows || '<tr><td colspan="4">No participants registered.</td></tr>')+'</tbody></table>'+
      '<section class="report-assignment-page"><h2>Team assignments</h2>'+meta+
      '<table aria-label="Team assignments" class="report-assignments"><thead><tr>'+
      '<th scope="col">Team</th><th scope="col">Company</th><th scope="col">Participants</th>'+
      '<th scope="col">Assignment</th><th scope="col">Scenario</th></tr></thead><tbody>'+
      (assignmentRows || '<tr><td colspan="5">No team slots configured.</td></tr>')+'</tbody></table></section>';
  }

  async function openReport() {
    if (busy || !state || $('open-registration-report').disabled) return;
    const current = generation;
    $('open-registration-report').disabled = true;
    try {
      const snapshot = await KEV.api(api);
      if (generation !== current || KEV.workspaceChanging) return;
      $('registration-report-content').innerHTML = reportMarkup(snapshot);
      $('registration-report').hidden = false;
      document.body.classList.add('registration-report-active');
      document.title = 'KevenOS Registration and Team Assignments' + (snapshot.rehearsal ? ' - Rehearsal' : '');
      window.scrollTo(0,0);
      $('registration-report-title').focus();
    } catch (error) {
      if (generation !== current) return;
      if ([401,403].includes(error.status)) clear(error.message);
      else status(error.message);
    } finally {
      if (generation === current) $('open-registration-report').disabled = false;
    }
  }

  $('open-registration-report').onclick = openReport;
  $('close-registration-report').onclick = () => {
    dismissReport();
    $('open-registration-report').focus();
  };
  $('print-registration-report').onclick = () => {
    if (!$('registration-report').hidden && !KEV.workspaceChanging) window.print();
  };

  async function change(body, errorNode) {
    if (busy || !state) return false;
    busy = true;
    ++request;working(true);
    const current = generation;
    status('Saving…');
    try {
      const result = await KEV.api(api, {body: {revision: editor?.revision || state.revision, ...body}});
      if (generation !== current) return false;
      state = result.state;
      $('registration-updated').textContent='Updated '+timeLabel(Date.now()/1000);
      render();
      const item=resolve(body.pair+':'+body.member);
      status(body.action === 'checkin' ? (item?.person.name || 'Participant')+(body.checked_in ? ' checked in.' : ': check-in cleared.') : 'Saved.');
      if ((body.action==='checkin' || body.action==='register') && body.checked_in) { if($('registration-edit').open)$('registration-edit').close(); if($('registration-detail').open)$('registration-detail').close(); showReceipt(body.pair+':'+body.member); }
      if (result.password || result.qr_image) { if($('registration-detail').open)$('registration-detail').close(); showAccess(result,body); }
      return true;
    } catch (error) {
      if (generation !== current) return false;
      if ([401,403].includes(error.status)) clear(error.message);
      else {
        if (error.status === 409) await refresh(true);
        status(error.message);
        if (errorNode) errorNode.textContent = error.message;
      }
      return false;
    } finally {
      if (generation === current) {busy = false;working(false);}
    }
  }

  function showAccess(result, body) {
    clearAccess();
    handoff = result;
    const item=resolve(body.pair+':'+body.member);
    $('registration-access-name').textContent = result.name+(item?' · '+teamRef(item.team):'');
    $('registration-qr-box').hidden = !result.qr_image;
    $('registration-password-box').hidden = !result.password;
    if (result.qr_image) {
      $('registration-qr').src = result.qr_image;
      $('registration-expiry').textContent = 'Valid for one sign-in within five minutes.';
    }
    if (result.password) $('registration-password').value = result.password;
    $('registration-access').showModal();
  }

  function edit(action, team, person) {
    if (!state || busy) return;
    const slots = action === 'swap' ? state.teams.flatMap(other => other.members
      .filter(member => member.name && (other.pair !== team.pair || member.id !== person.id))
      .map(member => ({team: other, member}))) : openSlots();
    if (action !== 'correct' && !slots.length) {
      const message=action==='swap'?'There is no other participant to swap with.':'There are no open slots. Ask Admin about capacity.';
      status(message);if($('registration-detail').open)$('registration-detail-error').textContent=message;return;
    }
    if($('registration-detail').open)$('registration-detail').close();
    editor = {action, team, person, revision: state.revision};
    const form = $('registration-form');
    form.reset();
    $('registration-edit-title').textContent = {register:'Register walk-in',correct:'Correct participant name',move:'Move participant',swap:'Swap participants'}[action];
    $('registration-edit-help').textContent = action === 'move' ? 'Move ' + person.name + '. Check-in follows them. Issue fresh access after the move.'
      : action === 'swap' ? 'Swap ' + person.name + ' with another participant. Issue fresh access for both after the swap.'
      : action === 'correct' ? 'Correct ' + person.name + '’s name. Issue fresh access if the name changes.' : 'Choose an open spot and confirm the participant’s name.';
    $('registration-edit-error').textContent = '';
    $('registration-slot-field').hidden = action === 'correct';
    $('registration-name-field').hidden = ['move','swap'].includes(action);
    $('registration-company-field').hidden = ['correct','swap'].includes(action);
    $('registration-checkin-field').hidden = action !== 'register';
    form.elements.participant.required = ['register','correct'].includes(action);
    form.elements.participant.value = action === 'correct' ? person.name : '';
    form.elements.checked_in.checked = action === 'register';
    form.elements.slot.innerHTML = slots.map(({team,member}) => '<option value="' + team.pair + ':' + member.id + '">' +
      esc(teamRef(team)) + ' · Slot ' + member.id + ' · ' + esc(member.name || team.partner) + '</option>').join('');
    $('registration-edit').showModal();
  }

  $('registration-form').onsubmit = async event => {
    event.preventDefault();
    const draft = editor;
    if (!draft) return;
    const form = event.currentTarget;
    const [pair, member] = form.elements.slot.value.split(':').map(Number);
    const body = {action: draft.action};
    if (draft.action === 'register') Object.assign(body, {pair, member, name: form.elements.participant.value,
      company: form.elements.company.value, checked_in: form.elements.checked_in.checked});
    else {
      Object.assign(body, {pair: draft.team.pair, member: draft.person.id});
      if (['move','swap'].includes(draft.action)) Object.assign(body, {to_pair: pair, to_member: member, company: form.elements.company.value});
      else body.name = form.elements.participant.value;
    }
    if (await change(body, $('registration-edit-error'))) $('registration-edit').close();
  };

  function actionClick(event) {
    const button=event.target.closest('[data-action]');
    if(!button || !state || busy)return;
    const item=resolve(button.dataset.slot);if(!item)return;
    const {team,person}=item,action=button.dataset.action;
    if(action==='details'){selected=button.dataset.slot;renderDetail();$('registration-detail').showModal();return;}
    if(['correct','move','swap'].includes(action))return edit(action,team,person);
    if(action==='checkin' && person.checked_in && !confirm('Undo check-in for '+person.name+'? Only do this if their arrival was recorded by mistake.'))return;
    if(['reset','revoke'].includes(action) && !confirm((action==='reset'?'Reset password for ':'Revoke access for ')+person.name+'? Their current password and sessions will stop working.'))return;
    change({action,pair:team.pair,member:person.id,checked_in:!person.checked_in},$('registration-detail').open?$('registration-detail-error'):null);
  }
  for(const id of ['registration-list','registration-detail-content','registration-receipt-actions'])$(id).onclick=actionClick;
  $('registration-detail-close').onclick=()=>$('registration-detail').close();
  $('registration-detail').addEventListener('close',()=>{selected=null;});
  $('registration-next').onclick=()=>{receipt=null;$('registration-receipt').hidden=true;$('registration-search').value='';render();$('registration-search').focus();};
  $('registration-clear').onclick=()=>{$('registration-search').value='';$('registration-filter').value='all';if(state)render();$('registration-search').focus();};
  function destination(people){history.replaceState(null,'',people?'#participants':'#today');$('registration-home').hidden=people;$('registration-workspace').hidden=!people;$('registration-home-tab').setAttribute('aria-pressed',String(!people));$('registration-people-tab').setAttribute('aria-pressed',String(people));window.scrollTo(0,0);if(people)$('registration-search').focus();}
  $('registration-home-tab').onclick=()=>destination(false);
  $('registration-people-tab').onclick=()=>destination(true);
  document.querySelectorAll('[data-registration-action]').forEach(button=>button.onclick=()=>{destination(true);$('registration-filter').value=button.dataset.registrationAction;if(state)render();});
  $('registration-home-add').onclick=()=>{destination(true);$('add-participant').click();};
  $('registration-copy-link').onclick = async () => {
    if (!handoff?.link) return;
    try { await navigator.clipboard.writeText(handoff.link); $('registration-copy-status').textContent = 'Link copied.'; }
    catch { $('registration-copy-status').textContent = 'Use the QR on the participant’s phone.'; }
  };
  setInterval(() => {
    if (handoff?.expires_at && handoff.expires_at <= Date.now() / 1000) {
      $('registration-qr').removeAttribute('src');
      $('registration-copy-link').disabled = true;
      $('registration-expiry').textContent = 'Expired. Close this card and generate a fresh QR.';
      handoff.link = '';
    }
  }, 1000);
  $('registration-access').addEventListener('close', clearAccess);
  $('registration-edit').addEventListener('close', () => { editor = null; });
  $('registration-edit-close').onclick = () => $('registration-edit').close();
  $('registration-access-close').onclick = () => $('registration-access').close();
  $('add-participant').onclick = () => edit('register');
  $('refresh-registration').onclick = refresh;
  $('registration-search').oninput = $('registration-filter').onchange = () => { if (state) render(); };
  setInterval(()=>{if(!document.hidden && state && !busy && $('registration-report').hidden && !document.querySelector('dialog[open]') && !$('registration-list').contains(document.activeElement))refresh();},30000);
  KEV.profile.mount({onEnd:clear});
  refresh();
  if(location.hash==='#participants')destination(true);
})();
