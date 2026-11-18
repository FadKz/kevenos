/* Participant controls keep unsaved edits in this signed-in page. */
window.ParticipantWorkspace = (() => {
  const $ = id => document.getElementById(id);
  const endpoint = '/api/participant/workspace';
  const steps = [
    ['discover', 'Discover', '2 min', 'Ask who uses laptops, what changed, what slows them down, and who decides.',
      'Try “What changed that made this a priority?” and “Who decides how laptops are managed?” Type A usually has small or outsourced IT. Type B usually has a dedicated IT team.'],
    ['mac', 'Solution at Work', '3 min', 'Pick one benefit each for users, IT and the business. Recommend a solution device for two roles.',
      'Try “You said your team works on the road. Battery life could help them finish a site visit without looking for a charger.” Use the solution device comparison in Resources to choose a model for the role.'],
    ['solve', 'Solve', '3 min', 'Prepare one IT concern and one business concern.',
      'Try “That’s a fair concern. Let’s test your must-have apps with implementation partner Technical Services. Would a pilot help you decide?”'],
    ['close', 'Close', '2 min', 'Ask for an IT Discovery meeting. Explain why it helps, who should join, and when.',
      'Try “Could we book a free 45-minute Discovery meeting with your IT team and implementation partner Technical Services to check your apps? Who should join, and what day works?”']
  ];
  const fmt = new Intl.DateTimeFormat('en-GB', {timeZone: 'UTC', hour: '2-digit', minute: '2-digit'});
  const clockLabel = {pitch: 'Pitch', feedback: 'Feedback', change: 'Changeover'};
  const pitchActions = new Set(['ready','pitch','paused','feedback','final_ready','final_pitch','final_paused','final_feedback']);
  const pitchEntryActions = new Set(['ready','pitch','paused','final_ready','final_pitch','final_paused']);

  function todayPitchAction(update) {
    const action = update?.action;
    if (pitchEntryActions.has(action?.code) && action.clock && !action.clock.ended) return 'focus';
    if (action?.code === 'program' && update.program?.scene === 'prep') return 'prepare';
    return null;
  }
  const writeDisabled = () => KEV.profile.isPreview || KEV.workspaceChanging;
  let data, state, selected = 0, panel = 'today', focus = false, enteredLive = false;
  let connected = false, clockReceived = 0, worksheetBusy = false, nominationBusy = false;
  let stepDrafts = {}, nominationDraft, nominationConflict = false, worksheetError = '', nominationError = '';
  let generation = 0, follow = true;

  function text(id, value) {
    if ($(id).textContent !== value) $(id).textContent = value;
  }

  function html(id, markup) {
    if ($(id).innerHTML !== markup) $(id).innerHTML = markup;
  }

  function time(value) {
    return Number.isFinite(value) ? fmt.format(new Date(value * 1000)) + ' event time' : '';
  }

  function go(next, navigate = false) {
    panel = next;
    document.body.dataset.participantPanel = panel;
    const titles = {today:'Your day, at a glance',pitch:'Make your pitch count',results:'Your team’s results',more:'Your event toolkit',agenda:'Today’s agenda',resources:'Guides & references',nominations:'Your next customers',updates:'Updates & notifications'};
    text('participant-page-title', titles[panel] || titles.today);
    document.body.classList.toggle('pitch-focus-active', focus && panel === 'pitch');
    document.querySelectorAll('[data-team-panel]').forEach(node => { node.hidden = node.dataset.teamPanel !== panel; });
    $('team-navigation').querySelectorAll('button').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.panel === (['agenda','resources','nominations','updates'].includes(panel) ? 'more' : panel)));
    });
    if (navigate) {
      const heading = [...document.querySelectorAll('[data-team-panel="' + panel + '"] h2')]
        .find(el => !el.closest('[hidden]'));
      if (heading) {
        heading.tabIndex = -1;
        heading.focus({preventScroll: true});
      }
      window.scrollTo(0, 0);
    }
  }

  function activeStep() {
    const clock = state?.action?.clock;
    return connected && clock?.kind === 'round' && clock.phase === 'pitch' && clock.started_any &&
      !clock.ended && Number.isInteger(clock.step) && clock.step >= 0 && clock.step < steps.length ? clock.step : null;
  }

  function followStep() {
    const step = activeStep();
    if (focus && follow && step !== null) selected = step;
  }

  function renderMode() {
    document.body.classList.toggle('pitch-focus-active', focus && panel === 'pitch');
    $('pitch-focus').hidden = !focus;
    $('team-worksheet').hidden = focus;
    $('prep-intro').hidden = focus;
    $('worksheet-progress').hidden = focus;
    $('worksheet-brief').hidden = focus;
    $('pitch-speaking').hidden = !focus;
    $('toggle-pitch-focus').setAttribute('aria-pressed', String(focus));
    text('toggle-pitch-focus', focus ? 'Back to worksheet' : 'Open Pitch Mode');
    text('worksheet-title', focus ? 'Your team’s speaking cues' : 'Build your team’s pitch');
    const action = state?.action || {};
    const entry = todayPitchAction(state);
    $('open-team-pitch').hidden = !entry;
    text('open-team-pitch', entry === 'focus' ? 'Open Pitch Mode' : entry === 'prepare' ? 'Prepare my pitch' : '');
    const liveStep = activeStep();
    $('focus-live-step').hidden = !focus || liveStep === null;
    text('focus-live-step', liveStep === null ? '' : 'Judge is on ' + steps[liveStep][1]);
    $('follow-pitch').hidden = !focus || follow || liveStep === null;
    const clock = action.clock;
    const current = clock && !clock.ended && clock.phase !== 'over' && Number.isFinite(clock.phase_left);
    let remaining = current ? clock.phase_left : null;
    if (current && clock.running) remaining = Math.max(0, remaining - (performance.now() - clockReceived) / 1000);
    const label = current ? clockLabel[clock.phase] || 'Pitch' : '';
    const finished = ['finished', 'submitted', 'final_submitted', 'final_review', 'final_complete'].includes(action.code);
    const finalFeedback = action.code === 'final_feedback';
    const countdown = connected && current ? KEV.mmss(Math.ceil(remaining)) : '';
    text('focus-clock', countdown || '–:––');
    text('focus-phase', current ? (clock.kind === 'final' ? 'Final · ' : '') + label
      : finalFeedback ? 'Final · Feedback' : finished ? 'Pitch complete' : 'Waiting for the start signal');
    text('focus-status', !connected ? 'Connection lost. Follow your judge or the Desk. Your cues are still here.'
      : finalFeedback || finished ? action.instruction
      : !current || !clock.started_any ? 'Follow your judge or the Desk for the start signal.'
      : !clock.running ? 'Paused. Wait for the judge or the Desk to resume.'
      : clock.kind === 'final' ? 'Synced to the stage clock.' : 'Synced to your judging session.');
    const todayClock = $('now-clock');
    todayClock.hidden = !countdown || !clock.started_any;
    todayClock.classList.add('today-countdown');
    if (countdown && clock.started_any) text('now-clock', label + ' · ' + countdown + (clock.running ? ' left' : ' · paused'));
  }

  function renderWorksheet() {
    const [key, name, duration, prompt, reference] = steps[selected];
    const saved = data?.steps[key];
    const draft = stepDrafts[key];
    const value = draft || saved || {};
    const prepared = steps.filter(([id]) => data?.steps[id]?.cues?.trim()).length;
    text('worksheet-progress', prepared === 4 ? 'Ready to rehearse · All four steps saved' : prepared + ' of 4 steps ready · Save cues for each part');
    $('prep-progress').value = prepared;
    $('prep-progress').hidden = focus;
    text('step-counter', 'Step ' + (selected + 1) + ' of 4');
    $('previous-pitch-step').disabled = selected === 0;
    $('next-pitch-step').disabled = selected === 3;
    $('team-step-tabs').querySelectorAll('button').forEach((button, i) => {
      button.setAttribute('aria-pressed', String(i === selected));
      button.dataset.saved = String(!!data?.steps[steps[i][0]]?.cues?.trim());
      if (focus && i === activeStep()) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    text('worksheet-step', name + (state?.action?.clock?.kind === 'final' ? '' : ' · ' + duration + ' in the preliminary pitch'));
    text('worksheet-prompt', prompt);
    $('worksheet-prompt').hidden = focus;
    $('worksheet-reference').hidden = focus;
    text('worksheet-reference-text', reference);
    for (const [id, field] of [['worksheet-cues','cues'],['worksheet-speaker','speaker']]) {
      if ($(id).value !== (value[field] || '')) $(id).value = value[field] || '';
      $(id).readOnly = writeDisabled();
    }
    text('pitch-cues', value.cues || 'No speaking cues yet.');
    text('pitch-speaker', value.speaker ? 'Speaker: ' + value.speaker : '');
    const conflict = draft && draft.revision !== (saved?.revision || 0);
    $('worksheet-reload').hidden = !conflict;
    $('worksheet-save').disabled = worksheetBusy || writeDisabled() || !draft || Boolean(conflict);
    text('worksheet-status', conflict ? 'A teammate saved this step. Copy any cues you want to keep, then load their version.'
      : worksheetError || (worksheetBusy ? 'Saving…' : draft ? (connected ? 'Unsaved changes' : 'Unsaved on this phone. Reconnect and save.')
      : saved ? 'Saved for the team' : ''));
  }

  function renderNominations() {
    if (!data) return;
    const saved = data.nomination;
    $('team-nomination').hidden = !saved.open && !saved.submitted_at && !nominationDraft;
    $('nomination-availability').hidden = !$('team-nomination').hidden;
    const entries = nominationDraft?.companies || saved.companies;
    const conflict = nominationConflict || (nominationDraft && nominationDraft.revision !== saved.revision);
    $('team-nomination-fields').querySelectorAll('input,textarea').forEach(el => {
      const value = entries[Number(el.dataset.index)]?.[el.dataset.field] || '';
      if (el.value !== value) el.value = value;
      el.readOnly = !saved.open || writeDisabled();
    });
    $('nomination-draft').hidden = Boolean(saved.submitted_at);
    for (const id of ['nomination-submit', 'nomination-draft']) {
      const maySubmitSaved = id === 'nomination-submit' && !saved.submitted_at && saved.companies.length === 3;
      $(id).disabled = !saved.open || nominationBusy || writeDisabled() || Boolean(conflict) || (!nominationDraft && !maySubmitSaved);
    }
    $('nomination-reload').hidden = !conflict;
    text('nomination-status', conflict ? 'A teammate saved changes. Copy any text you want to keep, then load the team’s version.'
      : nominationError || (nominationBusy ? 'Saving…' : !saved.open ? 'Nominations are closed.'
      : nominationDraft ? (connected ? 'Unsaved changes' : 'Unsaved on this phone. Reconnect and save.')
      : saved.submitted_at ? 'Submitted for your team at ' + time(saved.submitted_at) : saved.revision ? 'Team draft saved.' : ''));
  }

  function renderResults() {
    const result = data.preliminary;
    $('private-preliminary').hidden = false;
    if (state.result?.kind === 'round') $('result-card').hidden = true;
    if (result?.status !== 'released') {
      const waiting = state.result?.kind === 'round' && state.result?.status === 'awaiting';
      const status = result?.status === 'corrected' ? 'Being updated' : waiting ? 'Awaiting your pitch' : 'Under review';
      const message = result?.status === 'corrected' ? 'The Desk will release your corrected score here.'
        : waiting ? 'After your pitch, the Desk reviews the score and judge feedback before sharing them with your team.'
        : 'Your score and judge feedback are under review. Check back here after the Desk releases them.';
      html('private-preliminary-body', '<p class="participant-result-status">' + status + '</p><p>' + message + '</p>');
      return;
    }
    const caps = [30, 20, 30, 20];
    html('private-preliminary-body', '<p class="participant-result-status">Released to your team</p><p class="participant-points">' +
      KEV.esc(result.total) + ' points</p><div class="participant-feedback"><h3>From your judge</h3><p>' + KEV.esc(result.feedback) +
      '</p></div><h3>Score breakdown</h3><dl class="participant-breakdown">' +
      steps.map((step, index) => '<div><dt>' + step[1] + '</dt><dd>' + KEV.esc(result.groups[index]) + ' / ' + caps[index] + '</dd>' +
        '<dd class="participant-score-bar"><progress aria-label="' + step[1] + '" max="' + caps[index] +
        '" value="' + KEV.esc(result.groups[index]) + '"></progress></dd></div>').join('') +
      '<div><dt>Bonus</dt><dd>+' + KEV.esc(result.bonus) + '</dd></div></dl><p class="small mut">Released ' +
      KEV.esc(time(result.released_at)) + '</p>');
  }

  function receive(update) {
    if (data && data.team !== update.workspace.team) clear();
    const initial = !data;
    const previous = data;
    data = update.workspace;
    if (previous) {
      for (const [key, step] of Object.entries(previous.steps)) {
        if (step.revision > (data.steps[key]?.revision || 0)) data.steps[key] = step;
      }
      if (previous.nomination.revision > data.nomination.revision) {
        data.nomination = {...previous.nomination, open: data.nomination.open};
      }
    }
    state = update;
    const code = state.action?.code || '';
    const scene = state.program?.scene || '';
    const phase = code.startsWith('final_') ? 'The Final' : ['pitch','paused','feedback','ready'].includes(code) ? 'Your judging session' : ['submitted','finished'].includes(code) ? 'Pitch complete' : /prep/.test(scene) ? 'Prepare together' : 'Your next move';
    text('participant-phase', phase);
    document.querySelector('.participant-now').dataset.phase = code.startsWith('final_') ? 'final' : ['pitch','paused','feedback'].includes(code) ? 'pitch' : /prep/.test(scene) ? 'prep' : 'event';
    clockReceived = performance.now();
    if (initial) {
      go('today');
      $('team-navigation').hidden = false;
      $('quickstart').open = false;
    }
    const matching = ['pitch','paused','feedback','final_pitch','final_paused','final_feedback'].includes(state.action?.code);
    if (panel === 'pitch' && matching && !enteredLive && !Object.keys(stepDrafts).length && !focus && !worksheetBusy && !document.activeElement?.closest('#team-worksheet')) {
      focus = true;
      enteredLive = true;
    }
    followStep();
    text('worksheet-customer', [$('scenario-title').textContent, $('scenario-summary').textContent].filter(Boolean).join(' — '));
    const pack = $('documents').querySelector('[data-resource^="participant-pack"]');
    html('worksheet-pack', pack ? pack.outerHTML : '');
    $('team-news-card').hidden = data.news.length === 0;
    const resultsAvailable = data.preliminary?.status === 'released' || state.result?.status === 'released';
    $('open-team-results').hidden = !resultsAvailable;
    $('open-team-nominations').hidden = !data.nomination.open;
    $('team-actions').hidden = !resultsAvailable && !data.nomination.open;
    text('open-team-nominations', data.nomination.submitted_at ? 'Review your customer nominations' : 'Nominate your three customers');
    html('team-news', data.news.map(item => '<li><time datetime="' + new Date(item.at * 1000).toISOString() + '">' +
      KEV.esc(time(item.at)) + '</time>' + (item.pinned ? '<strong> · Pinned</strong>' : '') +
      '<p>' + KEV.esc(item.text) + '</p></li>').join(''));
    $('result-card').hidden = state.result?.kind !== 'final';
    renderResults();
    renderWorksheet();
    renderNominations();
    renderMode();
  }

  function connection(fresh) {
    if (connected === fresh) return;
    connected = fresh;
    if (data) {
      followStep();
      renderMode();
      renderWorksheet();
      renderNominations();
    }
  }

  function clear() {
    generation++;
    data = state = nominationDraft = null;
    connected = focus = enteredLive = worksheetBusy = nominationBusy = nominationConflict = false;
    follow = true;
    worksheetError = nominationError = '';
    selected = 0;
    stepDrafts = {};
    document.body.classList.remove('pitch-focus-active');
    $('team-worksheet').reset();
    $('team-nomination-form').reset();
    for (const id of ['team-news', 'private-preliminary-body', 'worksheet-customer', 'worksheet-pack', 'pitch-cues', 'pitch-speaker', 'focus-live-step']) $(id).replaceChildren();
    for (const id of ['team-navigation', 'open-team-pitch', 'team-nomination', 'private-preliminary', 'team-news-card', 'team-actions']) $(id).hidden = true;
  }

  $('team-navigation').addEventListener('click', event => {
    const button = event.target.closest('[data-panel]');
    if (button && data) go(button.dataset.panel, true);
  });
  document.querySelectorAll('[data-open-panel]').forEach(button => {
    button.onclick = () => { if (data) go(button.dataset.openPanel, true); };
  });
  $('open-team-pitch').onclick = () => {
    const entry = todayPitchAction(state);
    if (!data || !entry) return;
    focus = entry === 'focus';
    if (focus) {
      enteredLive = follow = true;
      followStep();
    }
    renderWorksheet();
    renderMode();
    go('pitch', true);
  };
  $('open-team-results').onclick = () => go('results', true);
  $('open-team-nominations').onclick = () => {
    go('nominations', true);
    $('team-nomination').scrollIntoView({block: 'start'});
    $('team-nomination-fields').querySelector('input').focus({preventScroll: true});
  };
  $('toggle-pitch-focus').onclick = () => {
    focus = !focus;
    if (state?.action?.clock?.started_any) enteredLive = true;
    if (focus) { follow = true; followStep(); }
    renderWorksheet(); renderMode();
  };
  $('follow-pitch').onclick = () => { follow = true; followStep(); renderWorksheet(); renderMode(); };
  $('team-step-tabs').innerHTML = steps.map((step, i) => '<button type="button" data-step="' + i + '"><span aria-hidden="true">' + (i + 1) + '</span>' + step[1] + '</button>').join('');
  $('team-step-tabs').addEventListener('click', event => {
    const button = event.target.closest('[data-step]');
    if (button) {
      selected = Number(button.dataset.step);
      if (focus && activeStep() !== null) follow = selected === activeStep();
      worksheetError = '';
      renderWorksheet(); renderMode();
    }
  });
  for (const [id, delta] of [['previous-pitch-step', -1], ['next-pitch-step', 1]]) {
    $(id).onclick = () => {
      const button = $('team-step-tabs').querySelector('[data-step="' + (selected + delta) + '"]');
      button?.click();
      $('worksheet-step').tabIndex = -1;
      $('worksheet-step').focus({preventScroll:true});
    };
  }
  window.addEventListener('beforeunload', event => {
    if (!Object.keys(stepDrafts).length && !nominationDraft) return;
    event.preventDefault(); event.returnValue = '';
  });
  $('team-worksheet').oninput = () => {
    if (!data) return;
    const key = steps[selected][0];
    const prior = stepDrafts[key];
    stepDrafts[key] = {revision: prior?.revision ?? data.steps[key]?.revision ?? 0,
      cues: $('worksheet-cues').value, speaker: $('worksheet-speaker').value};
    worksheetError = '';
    renderWorksheet();
  };
  $('worksheet-reload').onclick = () => { delete stepDrafts[steps[selected][0]]; worksheetError = ''; renderWorksheet(); };
  $('team-worksheet').onsubmit = async event => {
    event.preventDefault();
    const key = steps[selected][0], draft = stepDrafts[key];
    if (!data || !draft || worksheetBusy || writeDisabled()) return;
    const guard = generation;
    worksheetBusy = true; worksheetError = ''; renderWorksheet();
    try {
      const saved = await KEV.api(endpoint, {body: {action: 'save_step', team: data.team, step: key, ...draft}});
      if (guard !== generation) return;
      if ((data.steps[key]?.revision || 0) <= saved.step.revision) data.steps[key] = saved.step;
      if (stepDrafts[key] === draft) delete stepDrafts[key];
      else if (stepDrafts[key]) stepDrafts[key].revision = saved.step.revision;
    } catch (error) {
      if (guard === generation) worksheetError = error.message + ' Your text is still on this phone.';
    } finally {
      if (guard === generation) { worksheetBusy = false; renderWorksheet(); }
    }
  };

  $('team-nomination-fields').innerHTML = [0, 1, 2].map(i =>
    '<fieldset><legend>Customer ' + (i + 1) + '</legend><label class="participant-field">Company name<input data-index="' +
    i + '" data-field="name" maxlength="120" required></label><label class="participant-field">Who are they and why would you pitch to them?' +
    '<textarea data-index="' + i + '" data-field="why" maxlength="1200" rows="3" required></textarea></label></fieldset>').join('');
  $('team-nomination-form').oninput = () => {
    if (!data) return;
    const companies = [0, 1, 2].map(i => {
      const field = name => $('team-nomination-fields').querySelector('[data-index="' + i + '"][data-field="' + name + '"]').value;
      return {name: field('name'), why: field('why')};
    });
    nominationDraft = {revision: nominationDraft?.revision ?? data.nomination.revision, companies};
    nominationError = ''; renderNominations();
  };
  async function saveNomination(action) {
    if (!data || nominationBusy || writeDisabled() || !data.nomination.open) return;
    const draft = nominationDraft || (action === 'submit_nominations' && !data.nomination.submitted_at ? data.nomination : null);
    if (!draft || draft.revision !== data.nomination.revision) return;
    const guard = generation;
    nominationBusy = true; nominationError = ''; renderNominations();
    try {
      const saved = await KEV.api(endpoint, {body: {action, team: data.team, revision: draft.revision, companies: draft.companies}});
      if (guard !== generation) return;
      if (data.nomination.revision <= saved.nomination.revision) data.nomination = saved.nomination;
      if (nominationDraft === draft) nominationDraft = null;
      else if (nominationDraft) nominationDraft.revision = saved.nomination.revision;
      nominationConflict = false;
    } catch (error) {
      if (guard === generation) {
        nominationConflict = error.status === 409 && nominationDraft?.revision !== data.nomination.revision;
        nominationError = error.message + ' Your text is still on this phone.';
      }
    } finally {
      if (guard === generation) { nominationBusy = false; renderNominations(); }
    }
  }
  $('team-nomination-form').onsubmit = event => { event.preventDefault(); saveNomination('submit_nominations'); };
  $('nomination-draft').onclick = () => saveNomination('draft_nominations');
  $('nomination-reload').onclick = () => { nominationDraft = null; nominationConflict = false; nominationError = ''; renderNominations(); };
  setInterval(() => { if (data) renderMode(); }, 250);
  function openPrepTarget(target) {
    if (!data) return;
    if (target === 'resources') { go('resources', true); return; }
    if (pitchActions.has(state.action?.code) && state.action.clock && !state.action.clock.ended) {
      focus = enteredLive = follow = true;
      followStep();
      renderWorksheet();
      renderMode();
      go('pitch', true);
      return;
    }
    const index = steps.findIndex(([id]) => id === target);
    if (index !== -1) selected = index;
    if (focus) focus = false;
    follow = true;
    worksheetError = '';
    renderWorksheet();
    renderMode();
    go('pitch', true);
  }

  return {receive, connection, clear, openPrepTarget};
})();
