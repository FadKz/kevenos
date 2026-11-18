/* Organizer controls for private team results and participant updates. */
window.ParticipantDesk = (() => {
  const esc = KEV.esc;
  const endpoint = '/api/desk/participant-workspace';
  const format = new Intl.DateTimeFormat('en-GB', {timeZone: 'UTC', hour: '2-digit', minute: '2-digit'});
  let state, container, busy = false, editing = null, generation = 0;
  let message = '', latestSignature = '';
  const timestamp = at => format.format(new Date(at * 1000)) + ' event time';

  function clear() {
    generation++;
    container = state = editing = null;
    busy = false;
    message = latestSignature = '';
  }

  function exportNominations() {
    const quote = value => {
      let cell = String(value ?? '');
      if (/^\s*[=+\-@]|^[\t\r\n]/.test(cell)) cell = "'" + cell;
      return '"' + cell.replace(/"/g, '""') + '"';
    };
    const rows = [['Team', 'Partner', 'Company', 'Why the team nominated them']];
    for (const team of state.nominations) {
      if (team.submitted_at) team.companies.forEach(item => rows.push([team.pair, team.partner, item.name, item.why]));
    }
    const url = URL.createObjectURL(new Blob([rows.map(row => row.map(quote).join(',')).join('\r\n')], {type:'text/csv;charset=utf-8'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'kevenos-customer-nominations.csv';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function change(body) {
    if (busy || KEV.profile.isPreview || !container) return;
    busy = true;
    const guard = generation;
    const status = container.querySelector('#participant-desk-status');
    status.textContent = 'Saving…';
    try {
      const result = await KEV.api(endpoint, {body}, KEV.staff.token);
      if (guard !== generation || !container.isConnected) return;
      if (body.action === 'post_news' || body.action === 'edit_news') {
        editing = null;
        container.querySelector('#participant-news-form').reset();
      }
      message = result.released ? 'Released feedback for ' + result.released + ' team(s).' : 'Saved.';
      const updated = await KEV.api(endpoint, null, KEV.staff.token);
      if (guard === generation && container.isConnected) render(container, updated, true);
    } catch (error) {
      if (guard === generation) status.textContent = error.message;
    } finally {
      if (guard === generation) busy = false;
    }
  }

  function render(target, next, force = false) {
    const scoped=KEV.profile.identity?.dashboard;
    next={review:{entries:[],can_release:false},news:[],nominations:[],nominations_open:false,...next};
    state = next;
    container = target;
    const signature = JSON.stringify(next);
    const form = target.querySelector('#participant-news-form');
    if (!force && (signature === latestSignature ||
        (form && (form.elements.text.value || form.contains(document.activeElement))))) return;
    latestSignature = signature;
    const draft = form?.elements.text.value || '';
    const pin = form?.elements.pinned.checked;
    const expanded = new Set([...target.querySelectorAll('details[open][data-expand]')].map(node => node.dataset.expand));
    const statuses = {awaiting:'Awaiting score', needs_approval:'Judge approval needed', duplicate:'Check duplicate score sheets', ready:'Ready for release', released:'Released'};
    const ready = next.review.entries.filter(entry => entry.status === 'ready').length;
    const released = next.review.entries.filter(entry => entry.status === 'released').length;
    const submitted = next.nominations.filter(team => team.submitted_at).length;
    const locked = KEV.profile.isPreview ? ' disabled' : '';
    target.innerHTML =
      '<p id="participant-desk-status" role="status">' + esc(message) + '</p>' +
      '<section class="card participant-news" data-participant-panel="updates"><div class="ops-participant-grid"><div><h2>Post an update</h2><p class="small mut">Adds a message to the participant page. Use Live View for time-sensitive phone alerts or a message on the room screen.</p>' +
      '<form id="participant-news-form"><label class="field">Update<textarea name="text" maxlength="500" rows="3" required></textarea></label>' +
      '<div class="row"><label><input type="checkbox" name="pinned"> Pin this update</label><button class="btn" type="submit"' + locked + '>' +
      (editing ? 'Save correction' : 'Post update') + '</button><button class="btn ghost" type="button" id="participant-news-cancel"' +
      (editing ? '' : ' hidden') + '>Cancel edit</button></div></form>' +
      '</div><div><h2>Posted updates</h2>' + (next.news.length ? '' : '<p class="mut">No updates posted.</p>') +
      '<ul class="event-list">' + next.news.slice().reverse().map(item => '<li><p style="white-space:pre-wrap">' + esc(item.text) +
      '</p><p class="small mut">' + esc(timestamp(item.at)) + (item.pinned ? ' · Pinned' : '') + '</p><div class="row"><button class="btn ghost sm" data-edit="' +
      esc(item.id) + '"' + locked + '>Correct</button><button class="btn ghost sm" data-expire="' + esc(item.id) + '"' + locked + '>Expire</button></div></li>').join('') +
      '</ul></div></div></section><section class="card"><h2>Preliminary feedback release</h2><p>' + ready + ' ready · ' + released + ' released · ' +
      (next.review.entries.length - ready - released) + ' awaiting review</p>' +
      (next.review.can_release ? '' : '<p class="small mut">Finish preliminary judging and advance the programme to release results.</p>') +
      '<div>' + next.review.entries.map(entry => '<details data-expand="score-' + entry.pair + '"' +
      (expanded.has('score-' + entry.pair) ? ' open' : '') + '><summary>Team ' + entry.pair + ' · ' + esc(entry.partner) + ' · ' + statuses[entry.status] +
      '</summary>' + (entry.result ? '<p>' + entry.result.total + ' points · Discover ' + entry.result.groups[0] + ' · Solution at Work ' +
      entry.result.groups[1] + ' · Solve ' + entry.result.groups[2] + ' · Close ' + entry.result.groups[3] + ' · Bonus +' + entry.result.bonus +
      '</p><p style="white-space:pre-wrap">' + esc(entry.result.feedback) + '</p>' : '') + '</details>').join('') +
      '</div><p><button id="participant-release" class="btn"' + (ready && next.review.can_release ? locked : ' disabled') +
      '>Release ' + ready + ' ready team result' + (ready === 1 ? '' : 's') + '</button></p></section>' +
      '<section class="card" data-participant-panel="nominations"><div class="row"><h2 class="grow">Customer nominations</h2><span class="pill '+(next.nominations_open ? 'ok' : 'gray')+'">'+
      (next.nominations_open ? 'Accepting nominations' : 'Nominations closed')+'</span></div><p>' + submitted + ' of ' + next.nominations.length +
      ' teams submitted</p><progress aria-label="Teams that submitted nominations" value="' + submitted + '" max="' + Math.max(next.nominations.length,1) + '"></progress><div class="row section-tools"><button class="btn alt" id="participant-nom-window"' + locked + '>' +
      (next.nominations_open ? 'Close nominations' : 'Open nominations now') + '</button><button class="btn ghost" id="participant-nom-export"' +
      (!submitted || KEV.profile.isPreview ? ' disabled' : '') + '>Export submitted companies</button></div>' +
      (next.nominations.length ? '<h3>Team submissions</h3>' : '<p class="mut">Team nominations appear here once the roster is available.</p>') +
      next.nominations.map(team => '<details data-expand="nom-' + team.pair + '"' + (expanded.has('nom-' + team.pair) ? ' open' : '') +
      '><summary>Team ' + team.pair + ' · ' + esc(team.partner) + ' · ' + (team.submitted_at ? 'Submitted' : 'Awaiting submission') + '</summary>' +
      (team.submitted_at ? '<ul>' + team.companies.map(item => '<li><strong>' + esc(item.name) +
      '</strong><p style="white-space:pre-wrap">' + esc(item.why) + '</p></li>').join('') + '</ul>' : '') + '</details>').join('') + '</section>';
    const editor = target.querySelector('#participant-news-form');
    editor.elements.text.value = draft;
    editor.elements.pinned.checked = Boolean(pin);
    editor.elements.text.readOnly = Boolean(locked);
    editor.elements.pinned.disabled = Boolean(locked);
    editor.onsubmit = event => {
      event.preventDefault();
      change({action: editing ? 'edit_news' : 'post_news', id: editing, text: editor.elements.text.value, pinned: editor.elements.pinned.checked});
    };
    target.querySelector('#participant-news-cancel').onclick = () => { editing = null; editor.reset(); render(target, state, true); };
    target.querySelectorAll('[data-edit]').forEach(button => button.onclick = () => {
      const item = state.news.find(value => value.id === button.dataset.edit);
      editing = item.id;
      editor.elements.text.value = item.text;
      editor.elements.pinned.checked = item.pinned;
      render(target, state, true);
      target.querySelector('#participant-news-form').elements.text.focus();
    });
    target.querySelectorAll('[data-expire]').forEach(button => button.onclick = () => change({action: 'expire_news', id: button.dataset.expire}));
    target.querySelector('#participant-release').onclick = () => {
      if (confirm('Release the reviewed score and feedback privately to ' + ready + ' team(s)?')) change({action: 'release', digest: next.review.digest});
    };
    target.querySelector('#participant-nom-window').onclick = () => change({action: 'nominations', open: !next.nominations_open});
    target.querySelector('#participant-nom-export').onclick = exportNominations;
    if (scoped === 'organizer' || target.dataset.view) {
      target.querySelector('#participant-release').closest('section').remove();
      const current = target.dataset.view || 'updates';
      target.querySelectorAll('[data-participant-panel]').forEach(panel => panel.hidden = panel.dataset.participantPanel !== current);
    }
    if(scoped==='scoring'){editor.closest('section').remove();target.querySelector('#participant-nom-export').closest('section').remove();}
  }

  return {render, clear};
})();
