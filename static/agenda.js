// The preview response binds the exact edit to a workspace and event revision.
KEV.agenda = (() => {
  const esc = value => KEV.esc(value);
  const scenes = {info: 'Information', prep: 'Preparation', pitch: 'Preliminary pitches', scores: 'Scores',
    final: 'Final', awards: 'Awards', message: 'Message'};
  const timing = value => value ? `${value.start}–${value.end} · ${value.duration_minutes} min` : '—';
  const warnings = rows => rows.length ? '<ul class="agenda-warnings">' + rows.map(row => '<li>' + esc(row) + '</li>').join('') + '</ul>' : '';
  const detailLabels = {title: 'Title', line: 'Description', who: 'Who', do: 'Instructions', scene: 'Display scene'};
  const detailValue = (field, value) => field === 'scene' ? scenes[value] || value || '—' : Array.isArray(value) ? value.join('\n') || '—' : value || '—';
  const requestKey = () => {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  };
  const stale = error => error.status === 409 && error.code !== 'workspace_changed';
  let mounted;

  function mount(root, {request, mode, onError}) {
    unmount();
    const controller = new AbortController(), profileRevision = KEV.profile.revision;
    const view = {root, controller, busy: false, draft: null, attempt: null};
    mounted = view;
    const current = () => mounted === view && profileRevision === KEV.profile.revision && KEV.profile.active;
    const send = body => request(body, controller.signal);
    let schedule, plan, retryAllowed = false;

    root.innerHTML = `<div class="agenda">
      <section class="agenda-hero" aria-label="Agenda overview">
        <div><p class="agenda-mode mut" data-agenda-mode></p><h2 data-agenda-current>Event schedule</h2><p class="mut" data-agenda-mode-help></p></div>
        <div class="agenda-actions">
          <button class="btn" type="button" data-agenda="break">Add break</button>
          <button class="btn ghost" type="button" data-agenda="insert">Add section</button>
          <button class="btn ghost" type="button" data-agenda="refresh">Refresh agenda</button>
        </div>
      </section>
      <p class="agenda-status" data-agenda-status role="status" aria-atomic="true"></p>
      <div class="agenda-layout">
        <section class="card agenda-editor" aria-labelledby="agenda-editor-heading" data-agenda-editor hidden></section>
        <section class="card agenda-list" aria-labelledby="agenda-list-heading">
          <h2 id="agenda-list-heading" tabindex="-1">Scheduled sections</h2>
          <div data-agenda-items></div>
        </section>
      </div>
    </div>`;
    const find = selector => root.querySelector(selector);
    const editor = find('[data-agenda-editor]');
    const button = action => find(`[data-agenda="${action}"]`);
    function status(message, error = false) {
      const node = find('[data-agenda-status]');
      node.textContent = message;
      node.classList.toggle('error', error);
    }
    function controls() {
      for (const action of ['break', 'insert']) button(action).disabled = view.busy || !!view.attempt || !schedule?.items?.length;
      button('refresh').disabled = view.busy || !!view.attempt;
      root.querySelectorAll('[data-agenda-row]').forEach(node => { node.disabled = view.busy || !!view.attempt; });
      const fields = find('fieldset');
      if (fields) fields.disabled = view.busy || !!view.attempt;
      root.querySelectorAll('[data-agenda="apply"], [data-agenda="receipt"], [data-agenda="retry"]').forEach(node => { node.disabled = view.busy; });
    }
    async function busy(work, message) {
      if (!current() || view.busy) return;
      view.busy = true;
      controls();
      status(message);
      try { await work(); }
      catch (error) {
        if (!current() || error.code === 'workspace_changed') return;
        if (error.status === 401 && onError) onError(error);
        else status(error.message || 'The agenda request failed. Try again.', true);
      } finally {
        if (current()) {
          view.busy = false;
          controls();
          const focus = view.focus;
          view.focus = null;
          focus?.focus();
        }
      }
    }
    function renderSchedule() {
      const {items, active_item_id, clock_timezone} = schedule;
      const currentItem = items.find(row => row.id === active_item_id);
      const clock = (schedule.program_mode || mode) === 'clock';
      find('[data-agenda-mode]').textContent = `${clock ? 'Clock mode' : 'Manual mode'} · ${clock_timezone || 'Event time'}`;
      find('[data-agenda-current]').textContent = currentItem ? 'Current section: ' + currentItem.title : 'Event schedule';
      find('[data-agenda-mode-help]').textContent = clock ? 'The display follows the event clock and switches when a section starts.' :
        'Choose which section appears on the live display in Event Operations.';
      find('[data-agenda-items]').innerHTML = warnings(schedule.warnings || []) + (items.length ? '<ol class="agenda-timeline">' + items.map((row, index) => {
        const current = row.id === active_item_id;
        return `<li${current ? ' class="agenda-active"' : ''}>
          <div class="agenda-time"><time datetime="${esc(row.time)}">${esc(row.time)}</time><span class="mut small">${esc(row.min)} min</span></div>
          <div class="agenda-section"><h3>${esc(row.title)}</h3>
            ${current ? '<span class="pill ok">Current section</span>' : ''}
            <span class="small mut">${esc(scenes[row.scene] || row.scene)}</span>
            ${row.line ? `<p class="mut">${esc(row.line)}</p>` : ''}
          </div>
          <div class="agenda-row-actions">
            <button class="btn ghost sm" type="button" data-agenda="update" data-agenda-row="${index}" aria-label="Edit ${esc(row.title)}">Edit</button>
            <button class="btn ghost sm" type="button" data-agenda="move" data-agenda-row="${index}" aria-label="Move ${esc(row.title)}">Move</button>
            <button class="btn ghost sm" type="button" data-agenda="remove" data-agenda-row="${index}" aria-label="Remove ${esc(row.title)}">Remove</button>
          </div>
        </li>`;
      }).join('') + '</ol>' : '<p class="mut">No sections are scheduled.</p>');
      controls();
    }
    async function load() {
      const result = await send({action: 'read', resource: 'schedule.items', arguments: {}});
      if (!current()) return false;
      if (!Array.isArray(result.data?.items)) throw new Error('The agenda could not be loaded. Refresh to try again.');
      schedule = result.data;
      renderSchedule();
      return true;
    }
    function renderEditor(focus = true) {
      const draft = view.draft;
      editor.hidden = !draft;
      if (!draft) { editor.replaceChildren(); return; }
      const row = draft.row;
      const adding = draft.action === 'insert', updating = draft.action === 'update';
      const options = schedule.items.filter(item => item.id !== row?.id).map(item =>
        `<option value="${esc(item.id)}">Before ${esc(item.time)} · ${esc(item.title)}</option>`).join('');
      const position = `<label class="field">Position<select name="before_id"><option value="">End of agenda</option>${options}</select></label>`;
      const min = adding ? 1 : 0;
      const input = (label, name, value, attributes = '') => `<label class="field">${label}<input name="${name}" value="${esc(value)}" ${attributes}></label>`;
      const area = (label, name, value, attrs) => `<label class="field">${label}<textarea name="${name}" ${attrs}>\n${esc(value)}</textarea></label>`;
      const fields = adding || updating ? `${input('Title', 'title', row?.title, 'required maxlength="160"')}
        <div class="agenda-fields">${adding ? position : input('Start time', 'time', row.time, 'type="time" required')}
          ${input('Duration (minutes)', 'min', row?.min, `type="number" min="${min}" max="1440" step="1" required`)}
        </div>
        <label class="field">Display scene<select name="scene">${Object.entries(scenes).map(([key, label]) =>
          `<option value="${key}"${key === row?.scene ? ' selected' : ''}>${label}</option>`).join('')}</select></label>
        ${area('Description (optional)', 'line', row?.line, 'rows="2" maxlength="600"')}
        ${input('Who (optional)', 'who', row?.who, 'maxlength="160"')}
        ${area('Instructions (one per line)', 'do', row?.do?.join('\n'), 'rows="3"')}` : draft.action === 'move' ? position : '';
      const heading = adding ? (draft.break ? 'Add break' : 'Add section') : `${({update: 'Edit', move: 'Move', remove: 'Remove'})[draft.action]} ${row.title}`;
      editor.innerHTML = `<h2 id="agenda-editor-heading" tabindex="-1">${esc(heading)}</h2>
        ${draft.action === 'remove' ? '<p class="mut">Review how removing this section changes the rest of the day.</p>' : ''}
        <form data-agenda-form><fieldset>${fields}
          ${input('Change note (optional)', 'note', '', 'maxlength="300"')}
          <div class="agenda-actions"><button class="btn alt" type="submit">Preview changes</button>
            <button class="btn ghost" type="button" data-agenda="cancel">Cancel edit</button></div>
        </fieldset></form>
        <section class="agenda-preview" data-agenda-preview aria-label="Change preview" hidden></section>`;
      if (draft.action === 'move') find('[name="before_id"]').value = schedule.items[schedule.items.indexOf(row) + 1]?.id || '';
      controls();
      if (focus) find('#agenda-editor-heading').focus();
    }
    function clearPlan() {
      plan = null;
      const node = find('[data-agenda-preview]');
      if (node) { node.hidden = true; node.replaceChildren(); }
    }
    function argumentsFor(form) {
      const {action, row} = view.draft;
      const value = name => form.elements.namedItem(name)?.value;
      const note = value('note').trim();
      const edit = {action};
      if (action !== 'insert') edit.id = row.id;
      if (value('before_id')) edit.before_id = value('before_id');
      if (action === 'insert' || action === 'update') {
        const fields = {title: value('title').trim(), min: Number(value('min')), scene: value('scene'),
          line: value('line'), who: value('who'), do: value('do').split('\n').map(line => line.trim()).filter(Boolean)};
        if (!fields.title) throw new Error('Enter a section title.');
        if (fields.do.length > 20 || fields.do.some(line => line.length > 600)) throw new Error('Use up to 20 instructions, with up to 600 characters per line.');
        if (action === 'insert') edit.item = fields;
        else {
          fields.time = value('time');
          const original = {...row, who: row.who || '', line: row.line || '', do: row.do || []};
          edit.changes = Object.fromEntries(Object.entries(fields).filter(([key, next]) => JSON.stringify(next) !== JSON.stringify(original[key])));
          if (!Object.keys(edit.changes).length) throw new Error('Change a field before previewing.');
        }
      }
      return note ? {edit, note} : {edit};
    }
    function renderPreview() {
      const {data} = plan;
      const node = find('[data-agenda-preview]');
      const clock = data.program?.mode === 'clock';
      const active = data.program?.active_item_after?.title;
      const canApply = data.changed && data.can_apply;
      node.hidden = false;
      node.innerHTML = `<h3 tabindex="-1">Change preview</h3>
        <div class="agenda-finish"><div><span class="small mut">Event finish${data.clock_timezone ? ' · ' + esc(data.clock_timezone) : ''}</span>
          <strong>${esc(data.old_event_end || '—')} <span aria-hidden="true">→</span><span class="agenda-sr-only"> to </span> ${esc(data.new_event_end || '—')}</strong></div>
          ${active ? `<div><span class="small mut">${clock && data.program.selection_changes ? 'Current section after applying' : 'Current section'}</span><strong>${esc(active)}</strong></div>` : ''}
        </div>
        ${data.blockers.length ? '<ul class="agenda-blockers">' + data.blockers.map(row => `<li>${esc(row.message)}</li>`).join('') + '</ul>' : ''}
        ${data.changes.length ? '<ul class="agenda-changes">' + data.changes.map(change => `<li>
          <div class="agenda-change-title"><strong>${esc(change.title)}</strong><span class="pill gray">${esc(({added: 'Added', removed: 'Removed', updated: 'Updated'})[change.kind] || change.kind)}</span></div>
          <dl><div><dt>Before</dt><dd>${esc(timing(change.before))}</dd></div><div><dt>After</dt><dd>${esc(timing(change.after))}</dd></div></dl>
          ${Object.entries(change.details || {}).map(([field, {before, after}]) => `<div class="agenda-change-detail">
            <strong>${esc(detailLabels[field] || field)}</strong>
            <dl><div><dt>Before</dt><dd>${esc(detailValue(field, before))}</dd></div>
              <div><dt>After</dt><dd>${esc(detailValue(field, after))}</dd></div></dl></div>`).join('')}
        </li>`).join('') + '</ul>' : ''}
        ${warnings(data.warnings || [])}
        ${canApply ? '<div class="agenda-actions"><button class="btn" type="button" data-agenda="apply">Apply changes</button></div>' : ''}`;
      controls();
      view.focus = node.querySelector('h3');
    }
    async function preview(args) {
      clearPlan();
      const result = await send({action: 'read', resource: 'schedule.edit_preview', arguments: args});
      if (!current()) return;
      if (!result.data?.plan_digest || !result.context || !result.revision) throw new Error('The preview did not load. Preview the changes again.');
      plan = {args, data: result.data, context: result.context, revision: result.revision};
      renderPreview();
      status(result.data.can_apply ? 'Preview ready for review.' : 'Resolve the issues shown in the preview before applying.', !result.data.can_apply);
    }
    async function refreshPreview(args) {
      clearPlan();
      if (!await load()) return;
      await preview(args);
      if (current()) status('The event changed. Review the refreshed preview before applying.');
    }
    function renderAttempt(message, allowRetry = false) {
      retryAllowed = allowRetry;
      const node = find('[data-agenda-preview]');
      node.hidden = false;
      node.innerHTML = `<h3 tabindex="-1">Apply outcome</h3><p>${esc(message)}</p>
        <div class="agenda-actions"><button class="btn alt" type="button" data-agenda="receipt">Check outcome</button>
          ${allowRetry ? '<button class="btn ghost" type="button" data-agenda="retry">Retry same request</button>' : ''}</div>`;
      status('Agenda update needs verification.', true);
      controls();
      view.focus = node.querySelector('h3');
    }
    async function finishApplied(data) {
      view.attempt = view.draft = null;
      clearPlan();
      renderEditor();
      const message = data.applied ? 'Agenda updated.' : 'The agenda is already up to date.';
      if (Array.isArray(data.schedule?.items)) { schedule = data.schedule; renderSchedule(); }
      else {
        try { await load(); }
        catch { if (current()) status(message + ' Refresh the agenda to see the latest schedule.', true); return; }
      }
      if (current()) {
        status(message);
        view.focus = find('#agenda-list-heading');
      }
    }
    async function executeAttempt() {
      const {body, args} = view.attempt;
      let result;
      try {
        result = await send(body);
        if (!result.data || typeof result.data.applied !== 'boolean') throw new Error('Incomplete agenda response');
      } catch (error) {
        if (!current() || error.code === 'workspace_changed') return;
        if (!error.status || error.status >= 500 || error.code === 'outcome_uncertain') {
          renderAttempt('The update may have been applied. Check its outcome before sending another change.');
          return;
        }
        view.attempt = null;
        if (stale(error)) await refreshPreview(args);
        else { clearPlan(); throw error; }
        return;
      }
      if (current()) await finishApplied(result.data);
    }
    async function apply() {
      if (!plan?.data.can_apply || !plan.data.changed || view.attempt) return;
      const {args, data, context, revision} = plan;
      const payload = {command: 'schedule.edit', arguments: {...args, plan_digest: data.plan_digest}, context, revision};
      try {
        const prepared = await send({action: 'prepare', ...payload});
        if (!current()) return;
        if (!prepared.data?.confirmation_id || prepared.context !== context || prepared.revision !== revision) {
          throw new Error('Confirmation could not be verified. Preview the changes again.');
        }
        const body = {action: 'execute', ...payload, confirmation_id: prepared.data.confirmation_id, confirm: 'EXECUTE',
          idempotency_key: requestKey()};
        view.attempt = {body, args};
      } catch (error) {
        if (!current() || error.code === 'workspace_changed') return;
        if (stale(error)) await refreshPreview(args);
        else { clearPlan(); throw error; }
        return;
      }
      await executeAttempt();
    }
    async function checkReceipt() {
      const {body, args} = view.attempt;
      let result;
      try { result = await send({action: 'receipts', idempotency_key: body.idempotency_key}); }
      catch (error) {
        if (!current() || error.code === 'workspace_changed') return;
        if (error.code === 'receipt_not_found') {
          renderAttempt('No receipt was found. You can retry this exact request with the original confirmation and request key.', true);
          return;
        }
        throw error;
      }
      if (!current()) return;
      const {state, response} = result.data || {};
      if (state === 'completed' && response?.context === body.context && typeof response.data?.applied === 'boolean') {
        await finishApplied(response.data);
      } else if (state === 'failed' && response?.context === body.context) {
        view.attempt = null;
        await refreshPreview(args);
        if (current()) status(response.error?.message || 'The update failed. Review the refreshed preview.', true);
      } else {
        await load();
        if (current()) renderAttempt('The outcome is still unresolved. The latest schedule is shown. Check again before sending another change.');
      }
    }
    function closeDraft() {
      view.draft = null;
      clearPlan();
      renderEditor();
      status('');
    }
    function discardDraft() { return !view.draft || window.confirm('Discard this agenda edit?'); }
    function openDraft(action, index, trigger) {
      if (!discardDraft()) return;
      const row = schedule.items[index];
      view.draft = action === 'break' || action === 'insert' ? {action: 'insert', break: action === 'break', trigger,
        row: {title: action === 'break' ? 'Break' : '', min: action === 'break' ? 10 : 5, scene: 'info'}} : {action, row, trigger};
      clearPlan();
      status('');
      renderEditor();
    }
    root.addEventListener('input', event => {
      if (!current() || !event.target.closest('[data-agenda-form]') || !plan || view.attempt) return;
      clearPlan();
      status('Preview the updated edit before applying.');
    }, {signal: controller.signal});
    root.addEventListener('submit', event => {
      if (!event.target.matches('[data-agenda-form]')) return;
      event.preventDefault();
      if (!current() || view.busy || view.attempt || !event.target.reportValidity()) return;
      try {
        const args = argumentsFor(event.target);
        busy(() => preview(args), 'Building preview…');
      } catch (error) { status(error.message, true); }
    }, {signal: controller.signal});
    root.addEventListener('click', event => {
      const control = event.target.closest('[data-agenda]');
      if (!control || !current() || view.busy) return;
      const action = control.dataset.agenda;
      if (action === 'receipt' && view.attempt) busy(checkReceipt, 'Checking outcome…');
      if (action === 'retry' && view.attempt && retryAllowed) busy(executeAttempt, 'Retrying the same update…');
      if (view.attempt) return;
      if (action === 'apply') busy(apply, 'Applying reviewed changes…');
      if (action === 'cancel') {
        const trigger = view.draft?.trigger;
        closeDraft();
        (trigger?.isConnected ? trigger : find('#agenda-list-heading'))?.focus();
      }
      if (action === 'refresh' && discardDraft()) {
        closeDraft();
        busy(async () => { if (await load()) status('Agenda refreshed.'); }, 'Loading agenda…');
      }
      if (['break', 'insert', 'update', 'move', 'remove'].includes(action)) openDraft(action, Number(control.dataset.agendaRow), control);
    }, {signal: controller.signal});
    controls();
    return busy(async () => { if (await load()) status(''); }, 'Loading agenda…');
  }

  function unmount() {
    if (!mounted) return;
    const {root, controller} = mounted;
    mounted = null;
    controller.abort();
    root.replaceChildren();
  }

  return {mount, unmount, get busy() { return !!mounted?.busy; }, get dirty() { return !!mounted?.draft; },
    get unresolved() { return !!mounted?.attempt; }};
})();
