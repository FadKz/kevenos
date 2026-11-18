/* Existing dashboards share a role-scoped queue. Polling never submits drafts. */
KEV.operations = (() => {
  const instances = new WeakMap();
  const stops = new Set();
  const esc = KEV.esc;
  function pushHistory(push, expanded) {
    if (!push) return '';
    const kinds = {announcement:'Desk announcement', question:'Quiz question', results:'Results', agenda:'Agenda change', help:'Help reply', audience:'Audience question', test:'Device test'};
    const labels = {accepted:'Accepted', queued:'Queued', sending:'Sending', rehearsal:'Rehearsal only',
      invalid:'Invalid device', failed:'Failed', expired:'Expired', unsubscribed:'Unsubscribed',
      unconfirmed:'Unconfirmed', queue_full:'Queue full'};
    return '<details data-push-history'+(expanded?' open':'')+'><summary>Participant notifications · '+push.devices+' opted-in devices</summary>'+
      '<p class="small mut">Accepted means the push service received the alert. The phone may still be offline. Rehearsal sends device tests and the next matching alert for an explicitly armed test device.</p>'+
      (push.events.length ? '<ul>'+push.events.map(event => {
        const counts = Object.entries(event.counts).map(([status, count]) => count+' '+labels[status]).join(' · ');
        return '<li>'+esc(kinds[event.kind]+(event.rehearsal_test?' test':'')+(event.provider?' ('+event.provider+')':''))+' · '+esc(new Date(event.created*1000).toLocaleTimeString())+' · '+esc(counts || 'No eligible subscribed devices')+'</li>';
      }).join('')+'</ul>' : '<p>No alerts recorded yet.</p>')+'</details>';
  }
  function mount(host, participant = false, mode = 'all') {
    if (!host || instances.has(host)) return;
    const revision = KEV.profile?.revision;
    const events=new AbortController();
    const listen=(type,fn)=>host.addEventListener(type,fn,{signal:events.signal});
    let data, busy = false, stopped = false, requestId = null, lastBody = null, timer;
    const path = participant ? '/api/participant/help' : '/api/event-operations';
    const headings = {readiness:'Venue readiness', help:'Help inbox', delivery:'Notification delivery'};
    host.classList.add('event-operations');
    host.dataset.operationsMode = mode;
    host.innerHTML = '<section class="card"><div class="row"><h2 class="grow">'+(participant?'Need help?':headings[mode] || 'Needs attention')+'</h2><button class="btn ghost sm" data-refresh type="button">Refresh</button></div><p data-status role="status">Loading…</p><div data-content></div><div data-test-device></div><div data-compose></div><div data-preview></div></section>';
    const instance = {};
    instances.set(host, instance);
    const replies = new Map();
    const q = selector => host.querySelector(selector);
    const live = () => !stopped && instances.get(host) === instance && host.isConnected && revision === KEV.profile?.revision && !KEV.workspaceChanging;
    function stop() { if(stopped)return;stopped=true;events.abort();clearTimeout(timer);if(instances.get(host)===instance){host.replaceChildren();instances.delete(host);}stops.delete(stop); }
    stops.add(stop);
    function status(text) { q('[data-status]').textContent=text; }
    const button = (action, label, extra='') => '<button type="button" class="btn ghost sm" data-action="'+action+'" '+extra+'>'+label+'</button>';
    function rehearsalTester(push, permitted) {
      const root=q('[data-test-device]');
      if (!root) return;
      if (!permitted || push?.workspace!=='rehearsal') { root.replaceChildren(); return; }
      if (root.firstChild) return;
      root.innerHTML='<details><summary>Test event notifications</summary>'+
        '<p>Choose one test device. The next send of the selected type will send a real push to just that device.</p>'+
        '<label>Test device<select name="device" aria-label="Test device"></select></label>'+
        '<label>Alert type<select name="kind" aria-label="Alert type"><option value="question">Quiz question</option>'+
        '<option value="audience">Audience question</option><option value="announcement">Desk announcement</option><option value="agenda">Agenda change</option><option value="help">Private help reply</option></select></label>'+
        '<div class="row"><button type="button" class="btn" data-arm-device>Arm next send</button>'+
        '<button type="button" class="btn ghost" data-disarm-device>Cancel test</button></div><p role="status" data-test-status></p></details>';
      const details=root.querySelector('details');
      const device=details.querySelector('[name=device]');
      const type=details.querySelector('[name=kind]');
      const line=details.querySelector('[data-test-status]');
      const arm=details.querySelector('[data-arm-device]');
      async function refreshTest() {
        try {
          const value=await KEV.api('/api/staff/push');
          if (!live()) return;
          const selected=device.value;
          device.replaceChildren();
          for (const item of value.test_devices || []) {
            const option=document.createElement('option');
            option.value=item.id;
            option.textContent=item.label;
            device.append(option);
            if(item.id===selected) option.selected=true;
          }
          arm.disabled=!device.options.length;
          line.textContent=value.test_arm ? 'Test armed for the next '+value.test_arm.kind+' send.' :
            device.options.length ? 'Choose the enrolled test phone before arming.' : 'No enrolled Rehearsal devices.';
        } catch(error) { line.textContent=error.message; }
      }
      async function update(body) {
        arm.disabled=true;
        try { await KEV.api('/api/staff/push',{body}); await refreshTest(); }
        catch(error) { line.textContent=error.message; arm.disabled=!device.options.length; }
      }
      details.ontoggle=()=>{if(details.open)void refreshTest();};
      arm.onclick=()=>update({action:'arm',device:device.value,kind:type.value,confirm:'TEST DEVICE'});
      details.querySelector('[data-disarm-device]').onclick=()=>update({action:'disarm'});
    }
    function readiness(f, checks, expanded) {
      return '<div class="operations-facts"><p><strong>'+f.checked_in+' / '+f.participants+
        '</strong> checked in · '+f.capacity+' places</p><p><strong>'+f.participant_access_used+' / '+f.participant_passwords+
        '</strong> issued participant passwords used</p><p>Judge stations without an assignment: <strong>'+
        esc(f.missing_judge_stations.join(', ') || 'none')+'</strong></p></div>'+
        (f.schedule_warnings.length?'<p class="operations-alert">Agenda has timing warnings. Review them before changing section.</p>':'')+
        '<details class="operations-guidance" data-venue'+(expanded?' open':'')+'><summary>Venue checklist</summary>'+
        Object.entries(checks).map(([key,c])=>'<label class="operations-check"><input type="checkbox" data-check="'+key+'" '+
          (c.done?'checked ':'')+(!data.can_check_venue?'disabled':'')+'>'+esc(c.label)+(c.done?' · confirmed by staff':'')+'</label>').join('')+
        '<p class="small mut">These checks cover assignments, sign-ins and staff confirmations. Check the projector and devices in the room.</p></details>';
    }

    function helpInbox(requests, expanded) {
      const open = requests.filter(request=>['open','claimed'].includes(request.status));
      const closed = requests.filter(request=>!['open','claimed'].includes(request.status)).slice(-10).reverse();
      const cards = open.map(request=>'<article class="operations-request" data-request="'+esc(request.id)+
        '"><div class="row"><strong class="grow">'+esc(request.name)+' · Team '+request.team+'</strong><span class="pill '+
        (request.status==='open'?'gray':'ok')+'">'+(request.status==='open'?'New':'Claimed')+'</span></div><p>'+
        esc(data.categories?.[request.category] || request.category)+'</p><p>'+esc(request.message)+'</p>'+
        '<label>Private reply<textarea rows="2" maxlength="500" data-reply>'+
        esc(replies.has(request.id)?replies.get(request.id):request.reply)+'</textarea></label><div class="row">'+
        (request.status==='open'?button('claim','I’m handling this','data-id="'+esc(request.id)+'"'):'')+
        button('resolve','Reply & resolve','data-id="'+esc(request.id)+'"')+'</div></article>').join('');
      return '<p><strong>'+open.length+' awaiting a reply</strong> · Replies go only to the participant who asked.</p>'+
        (cards || '<p class="mut">No open requests for this desk.</p>')+
        '<details class="operations-guidance" data-closed-help'+(expanded?' open':'')+'><summary>Recent closed requests · '+closed.length+'</summary>'+
        (closed.length ? closed.map(request=>'<p>'+esc(request.name)+' · '+esc(request.status)+' · '+esc(request.message)+'</p>').join('') :
          '<p class="mut">No closed requests yet.</p>')+'</details>';
    }

    function render() {
      if (!data) return;
      if (participant) {
        KEV.messages?.replace('help',data.requests.filter(r=>r.reply).map(r=>({id:'help:'+r.id+':'+r.updated,
          title:'Reply from the Event Desk',label:'Private help reply',text:r.reply,at:r.updated})));
        q('[data-content]').innerHTML='<p>Send a short request to the relevant desk. For urgent help, visit the Event Desk in person.</p>'+data.requests.map(r=>'<article class="operations-request"><strong>'+esc(data.categories[r.category])+' · '+esc(r.status)+'</strong><p>'+esc(r.message)+'</p>'+(r.reply?'<p class="operations-reply"><b>From the desk:</b> '+esc(r.reply)+'</p>':'')+(['open','claimed'].includes(r.status)?button('cancel_help','Cancel request','data-id="'+esc(r.id)+'"'):'')+'</article>').join('');
        if(!q('[data-help-form]'))q('[data-compose]').innerHTML='<details><summary>Ask for help</summary><form data-help-form><label>What do you need?<select name="category">'+Object.entries(data.categories).map(([k,v])=>'<option value="'+esc(k)+'">'+esc(v)+'</option>').join('')+'</select></label><label>Tell the desk briefly<textarea name="message" required maxlength="500" rows="3" placeholder="For example: I cannot find my teammate"></textarea></label><button class="btn" type="submit">Send help request</button></form></details>';
      } else {
        if (mode === 'delivery') {
          q('[data-content]').innerHTML = (data.push ?
            pushHistory(data.push, q('[data-push-history]')?.open ?? true) :
            '<p class="mut">Notification delivery information is unavailable for this workspace.</p>') +
            ('mail' in data ? '<p class="small mut">Email service status · '+esc(Object.entries(data.mail).map(([key,value])=>key+': '+value).join(' · ') || 'No email history')+'</p>' : '');
          rehearsalTester(data.push,data.can_check_venue);
          return;
        }
        if (mode === 'help') { q('[data-content]').innerHTML = helpInbox(data.requests,q('[data-closed-help]')?.open); return; }
        if (mode === 'all') rehearsalTester(data.push,data.can_check_venue);
        const handoverOpen = q('[data-handover]')?.open;
        q('[data-content]').innerHTML = readiness(data.facts,data.checks,q('[data-venue]')?.open) +
          (mode === 'all' ? helpInbox(data.requests,q('[data-closed-help]')?.open)+
            (data.push ? pushHistory(data.push,q('[data-push-history]')?.open) : '')+
            ('mail' in data ? '<p class="small mut">Email service status · '+esc(Object.entries(data.mail).map(([key,value])=>key+': '+value).join(' · ') || 'No email history')+'</p>' : '') : '') +
          '<details class="operations-guidance" data-handover'+(handoverOpen?' open':'')+'><summary>Recent staff handover notes</summary>'+
          (data.notes.length ? data.notes.slice(-5).reverse().map(note=>'<article class="operations-note"><p>'+esc(note.note)+
            '</p><small>'+esc(note.author)+'</small></article>').join('') : '<p class="mut">No handover notes yet.</p>')+'</details>';
        if(!q('[data-note-form]'))q('[data-compose]').innerHTML='<form data-note-form><label>Leave a useful note for the next staff member<textarea name="note" rows="2" maxlength="800" required placeholder="What is pending, who is handling it, and what should happen next?"></textarea></label><button type="submit" class="btn alt">Save handover note</button></form><details class="operations-guidance"><summary>When something goes wrong</summary><ul><li>Late arrival or missing teammate: Registration checks the roster before changing a team.</li><li>Missing judge or score sheet: Scoring Desk checks assignments and saved submissions before advancing.</li><li>Programme running late: Organizer previews an agenda change before updating the room.</li><li>Phone offline: keep the page open, reconnect and check the saved confirmation before retrying.</li></ul></details>'+(data.can_change_phase?'<form data-phase-form><h3>Move to another section</h3><label>Choose section<select name="index">'+data.agenda.map(a=>'<option value="'+a.index+'" '+(a.index===data.program.index?'selected':'')+'>'+esc((a.time || '')+' '+a.title)+'</option>').join('')+'</select></label><button class="btn alt" type="submit">Preview section change</button></form>':'')+(KEV.assistant?.available?.() && !location.pathname.includes('registration')?'<div class="operations-ai"><h3>Ask the Assistant</h3>'+button('ai-readiness','Explain what needs attention')+button('ai-update','Draft a room update')+button('ai-handover','Draft a shift handover')+'<p class="small mut">Opens a prompt for review. Nothing is sent automatically.</p></div>':'');
      }
    }
    async function refresh() {
      if(!live()){stop();return;}
      if(busy || host.hidden || document.hidden || host.contains(document.activeElement) && document.activeElement.matches('input,textarea,select'))return;
      try { const next=await KEV.api(path,{},KEV.staff?.token);if(!live()){stop();return;}data=next;render();status('Updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})); }
      catch(e){if(!live()){stop();return;}if([401,403].includes(e.status)){stop();return;}status('Updates unavailable. Your draft is kept; refresh when connected.');}
    }
    async function send(body) {
      if(!live() || busy)return;
      busy=true;host.querySelectorAll('button').forEach(b=>b.disabled=true);
      try {const next=await KEV.api(path,{body},KEV.staff?.token);if(!live()){stop();return;}if(body.action==='preview_phase'){showPreview(next);status('Review the section change below.');return next;}data=next;render();status('Saved.');return next;}
      catch(e){if(live())status((e.status===409?'The event changed. Refresh, review and try again. ': '')+(e.message || 'Could not save. Your draft is kept.'));return null;}
      finally{busy=false;if(live())host.querySelectorAll('button').forEach(b=>b.disabled=false);}
    }
    function showPreview(plan) {
      const box=q('[data-preview]');box.innerHTML='<section class="operations-preview" aria-label="Section change preview"><h3>'+esc(plan.target)+'</h3>'+plan.effects.map(x=>'<p>'+esc(x)+'</p>').join('')+plan.blockers.map(x=>'<p class="operations-alert">'+esc(x)+'</p>').join('')+'<button type="button" class="btn" data-confirm-phase '+(plan.blockers.length?'disabled':'')+'>Confirm section change</button> '+button('dismiss','Cancel')+'</section>';
      box.querySelector('[data-confirm-phase]').onclick=async()=>{if(plan.blockers.length)return;const result=await send({action:'apply_phase',index:plan.index,plan_digest:plan.plan_digest,confirm:'CHANGE SECTION'});if(result)box.replaceChildren();};
    }
    listen('input',e=>{if(e.target.matches('[data-reply]'))replies.set(e.target.closest('[data-request]').dataset.request,e.target.value);});
    listen('submit',async e=>{
      e.preventDefault();const form=e.target;if(!form.checkValidity())return;
      if(form.matches('[data-help-form]')){const body={action:'request_help',category:form.elements.category.value,message:form.elements.message.value};const signature=JSON.stringify(body);if(signature!==lastBody){requestId=crypto.randomUUID();lastBody=signature;}if(await send({...body,request_id:requestId})){form.reset();requestId=null;lastBody=null;}}
      if(form.matches('[data-note-form]')){if(await send({action:'handover',revision:data.revision,note:form.elements.note.value}))form.reset();}
      if(form.matches('[data-phase-form]'))await send({action:'preview_phase',index:Number(form.elements.index.value)});
    });
    listen('change',async e=>{if(e.target.matches('[data-check]')){const result=await send({action:'check',revision:data.revision,key:e.target.dataset.check,done:e.target.checked});if(!result)e.target.checked=!e.target.checked;}});
    listen('click',async e=>{
      const b=e.target.closest('button');if(!b || busy)return;
      if(b.hasAttribute('data-refresh'))return refresh();
      const a=b.dataset.action;
      if(a==='dismiss')q('[data-preview]').replaceChildren();
      if(a==='cancel_help')await send({action:a,id:b.dataset.id});
      if (a==='claim' || a==='resolve') {
        const request = data.requests.find(item=>item.id===b.dataset.id);
        if (!request) return;
        const reply = a==='claim' ? request.reply : b.closest('[data-request]').querySelector('[data-reply]').value;
        await send({action:'help_update',revision:data.revision,id:request.id,status:a==='claim'?'claimed':'resolved',reply});
      }
      const prompts={'ai-readiness':'What needs attention?','ai-update':'Draft a short room update based on the current programme and verified readiness. Do not send it.','ai-handover':'Draft a concise staff handover from the current operations facts. Highlight unknowns and what staff should check. Do not save or send it.'};
      if(prompts[a])KEV.assistant.openWithPrompt(prompts[a]);
    });
    async function poll(){await refresh();if(live())timer=setTimeout(poll,15000);else stop();}
    poll();
  }
  return {mount, clear: () => { for(const stop of [...stops])stop(); }};
})();
