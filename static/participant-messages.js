/* One inbox and one sheet for participant-facing event activity. */
(() => {
  const sheet = document.getElementById('participant-message-sheet');
  const trigger = document.getElementById('participant-messages-button');
  const stash = document.getElementById('participant-message-stash');
  if (!sheet || !trigger || !stash) return;
  const list = sheet.querySelector('[data-message-list]');
  const detail = sheet.querySelector('[data-message-detail]');
  const heading = sheet.querySelector('h2');
  const back = sheet.querySelector('[data-message-back]');
  const providers = new Map();
  const firstSeen = new Map();
  const dismissedThisPage = new Set();
  const pitching = new Set(['pitch','paused','feedback','final_ready','final_pitch','final_paused','final_feedback']);
  let scope = '';
  let read = new Set();
  let selection;
  let mounted;
  let returnFocus;
  let currentAction;
  let pendingClose;
  function linkedDestination() {
    const anchor = location.hash.slice(1);
    if (['help-reply', 'event-agenda'].includes(anchor)) return anchor;
    return anchor.startsWith('messages') ? 'messages' : '';
  }
  let waitingForLink = linkedDestination();
  let countText = '';

  function entries() {
    const values = [...providers.values()].flat();
    return values.sort((a,b)=>
      (b.at || firstSeen.get(b.id)) - (a.at || firstSeen.get(a.id)));
  }

  function remember(id) {
    read.add(id);
    try { sessionStorage.setItem('kevenos:messages:'+scope,JSON.stringify([...read].slice(-100))); } catch (_) {}
  }

  function restoreRoot() {
    if (mounted) stash.append(mounted);
    mounted = undefined;
  }

  function showList() {
    clearTimeout(pendingClose);
    pendingClose = undefined;
    restoreRoot();
    selection = undefined;
    detail.replaceChildren();
    detail.hidden = true;
    list.hidden = back.hidden = false;
    back.hidden = true;
    heading.textContent = 'Messages';
    draw();
    heading.focus({preventScroll:true});
  }

  function draw() {
    const all = entries();
    const count = all.filter(item=>!read.has(item.id)).length;
    const label = count ? 'Messages · '+count : 'Messages';
    if (label !== countText) trigger.textContent = countText = label;
    if (!sheet.open || selection) return;
    const focused = document.activeElement?.dataset.messageId;
    list.replaceChildren();
    if (!all.length) {
      const empty = document.createElement('p');
      empty.textContent = 'Event questions, Desk updates and replies will appear here.';
      list.append(empty);
    }
    for (const item of all) {
      const row = document.createElement('button');
      const name = document.createElement('strong');
      const meta = document.createElement('small');
      row.type = 'button';
      row.className = 'message-row';
      row.dataset.messageId = item.id;
      name.textContent = item.title;
      meta.textContent = [!read.has(item.id) && 'New', item.label, item.state==='open' ? 'Open' :
        item.state==='answered' ? 'Answered' : item.state==='closed' ? 'Closed' : ''].filter(Boolean).join(' · ');
      row.append(name, meta);
      row.addEventListener('click',()=>openItem(item));
      list.append(row);
      if (item.id === focused) row.focus({preventScroll:true});
    }
  }

  function openSheet() {
    if (sheet.open) return true;
    if (!scope || document.querySelector('dialog[open]')) return false;
    returnFocus = document.activeElement;
    sheet.showModal();
    return true;
  }

  function openItem(item) {
    if (!openSheet()) return;
    clearTimeout(pendingClose);
    pendingClose = undefined;
    restoreRoot();
    selection = item;
    remember(item.id);
    heading.textContent = item.label;
    list.hidden = true;
    detail.hidden = back.hidden = false;
    detail.replaceChildren();
    if (item.root && item.state==='open') {
      mounted = document.getElementById(item.root);
      if (mounted) detail.append(mounted);
    } else {
      const title = document.createElement('h3');
      const text = document.createElement('p');
      title.textContent = item.source==='desk' ? 'From the Event Desk' : item.title;
      text.textContent = item.text || (item.state==='answered' ? 'Answer saved.' : 'This question is no longer accepting answers.');
      detail.append(title,text);
      if (item.results) {
        const result = document.createElement('button');
        result.type='button';
        result.className='btn';
        result.textContent='View results';
        result.onclick=()=>{sheet.close(); document.getElementById('open-team-results')?.click();};
        detail.append(result);
      }
    }
    heading.focus({preventScroll:true});
    draw();
  }

  function maybeOpen() {
    if (!scope || trigger.closest('[hidden]') || document.hidden || sheet.open || document.querySelector('dialog[open]')) return;
    if (waitingForLink === 'messages') {
      waitingForLink = '';
      openSheet();
      showList();
      return;
    }
    if (waitingForLink) {
      const navigation = document.getElementById('team-navigation');
      if (!navigation || navigation.hidden) return;
      const help = waitingForLink === 'help-reply';
      const control = help ? navigation.querySelector('[data-panel="today"]') :
        document.querySelector('[data-open-panel="agenda"]');
      const target = document.getElementById(help ? 'participant-operations-help' : 'full-agenda-title');
      if (!control || !target || (help && !target.querySelector('h2'))) return;
      control.click();
      if (target.closest('[hidden]')) return;
      waitingForLink = '';
      const heading = help ? target.querySelector('h2') : target;
      heading.tabIndex = -1;
      heading.focus({preventScroll:true});
      heading.scrollIntoView();
      return;
    }
    if (pitching.has(currentAction) ||
        document.activeElement?.closest('textarea,input,select,[contenteditable]:not([contenteditable="false"])')) return;
    const next = entries().find(item=>item.auto && !read.has(item.id) && !dismissedThisPage.has(item.id));
    if (next) openItem(next);
  }

  function replace(source, items) {
    if (!scope) return;
    items = items.map(item=>({...item,source}));
    const incoming = new Set(items.map(item=>item.id));
    const previous = providers.get(source) || [];
    for (const old of previous) {
      if (!incoming.has(old.id) && ['open','closed'].includes(old.state)) {
        items.push({...old,state:'closed',auto:false,root:undefined});
      }
    }
    items = items.slice(0,40);
    const identical = JSON.stringify(previous) === JSON.stringify(items);
    if (!identical) {
      providers.set(source,items);
      for (const item of items) if (!firstSeen.has(item.id)) firstSeen.set(item.id,Date.now()/1000);
      if (sheet.open && selection?.root && selection.source===source) {
        const updated = items.find(item=>item.id===selection.id);
        if ((!updated || updated.state!=='open') && !pendingClose) {
          // Leave the renderer in place briefly so its saved-answer status is readable.
          pendingClose = setTimeout(()=>sheet.close(), 1200);
        } else if (updated?.state==='open' && pendingClose) {
          clearTimeout(pendingClose);
          pendingClose=undefined;
        }
      }
      draw();
    }
    maybeOpen();
  }

  function clear() {
    scope='';
    providers.clear();
    firstSeen.clear();
    dismissedThisPage.clear();
    read.clear();
    if (sheet.open) sheet.close();
    restoreRoot();
    selection=undefined;
    clearTimeout(pendingClose);
    pendingClose=undefined;
    draw();
  }

  function receive(state) {
    const nextScope = [KEV.context,state.message_scope || state.participant?.pair,state.message_scope ? '' : state.participant?.name].join(':');
    if (nextScope !== scope) {
      clear();
      scope = nextScope;
      try { read = new Set(JSON.parse(sessionStorage.getItem('kevenos:messages:'+scope) || '[]')); } catch (_) {}
    }
    currentAction=state.action?.code;
    const messages=state.messages || [];
    const latest=messages.at(-1);
    replace('desk',messages.map(item=>({id:'desk:'+item.id,title:item.text.length>84 ? item.text.slice(0,81)+'…' : item.text,label:item.important?'Important announcement':'Announcement',
      text:item.text,at:item.updated_at,auto:item===latest && item.important && item.updated_at===state.announcement?.updated_at})));
    const result=state.result;
    replace('pitch-results',result?.status==='released' ? [{
      id:'pitch-result:'+result.rank+':'+result.total,title:'Your pitch result is available',
      label:'Pitch results',text:result.label,results:true
    }] : []);
  }

  trigger.onclick=()=>{if (openSheet()) showList();};
  back.onclick=showList;
  sheet.querySelector('[data-message-close]').onclick=()=>sheet.close();
  sheet.addEventListener('close',()=>{
    if (selection) dismissedThisPage.add(selection.id);
    restoreRoot();
    clearTimeout(pendingClose);
    pendingClose=undefined;
    selection=undefined;
    if (returnFocus?.isConnected) returnFocus.focus({preventScroll:true});
    returnFocus=undefined;
  });
  window.addEventListener('hashchange',()=>{
    const destination = linkedDestination();
    if (destination) { waitingForLink=destination; maybeOpen(); }
  });
  navigator.serviceWorker?.addEventListener('message',event=>{
    if (event.data?.type==='kevenos:open-messages') {
      if (sheet.open) showList();
      else { waitingForLink='messages'; maybeOpen(); }
    }
    if (event.data?.type==='kevenos:open-notification' && ['help-reply', 'event-agenda'].includes(event.data.destination)) {
      waitingForLink=event.data.destination;
      if (sheet.open) sheet.close();
      maybeOpen();
    }
  });
  setInterval(maybeOpen,1000);
  KEV.messages={receive,replace,clear};
})();
