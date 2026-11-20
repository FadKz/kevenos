/* Operator controls consume only the published, restricted projection. */
(function () {
  'use strict';
  const path = '/api/desk/engagement', root = document.getElementById('operator-content');
  const status = document.getElementById('operator-status'), retry = document.getElementById('operator-retry');
  let state = null, pending = null, busy = false, generation = 0, clockOffset = 0;
  const node = (tag, text, className) => {
    const value = document.createElement(tag); if (text != null) value.textContent = text;
    if (className) value.className = className; return value;
  };
  const button = (title, click, disabled = false) => {
    const item = node('button', title, 'btn ghost'); item.type = 'button'; item.disabled = disabled;
    item.addEventListener('click', click); return item;
  };
  function clear() { generation++; state = null; pending = null; root.replaceChildren(); retry.hidden = true; }
  function id() { const bytes = new Uint8Array(16); crypto.getRandomValues(bytes); return [...bytes].map(n => n.toString(16).padStart(2, '0')).join(''); }
  async function send() {
    if (!pending || busy) return;
    const stamp = generation; busy = true; status.textContent = 'Updating…';
    try {
      const result = await KEV.api(path, {body:pending});
      if (stamp !== generation) return;
      state = result; clockOffset = (result.now || Date.now()/1000) - Date.now()/1000;
      pending = null; retry.hidden = true; draw(); status.textContent = 'Updated.';
    } catch (error) {
      if (stamp !== generation) return;
      if (error.status >= 400 && error.status < 500) { pending = null; retry.hidden = true; }
      else retry.hidden = false;
      status.textContent = pending ? 'The result is uncertain. Retry the same action before continuing.' : error.message;
    } finally { busy = false; }
  }
  function action(name, payload, message) {
    if (pending || busy || !state || (message && !confirm(message))) return;
    pending = {action:name, payload, revision:state.revision, request_id:id()}; send();
  }
  function draw() {
    root.replaceChildren();
    const active = state.runs.filter(run => run.status === 'open');
    if (!active.length) root.append(node('p', 'No activity is running. Choose a published activity below.'));
    for (const run of active) {
      const card = node('section', null, 'card'), title = run.snapshot.title, delivery = run.delivery || {};
      card.append(node('h2', title), node('p', `${delivery.status || 'Ready'} · ${run.respondents} responses`));
      if (delivery.status === 'open' && delivery.ends_at != null) {
        const timer = node('p'); timer.dataset.endsAt = String(delivery.ends_at); card.append(timer);
      } else if (delivery.status === 'paused') card.append(node('p', delivery.remaining_seconds == null ? 'Paused · no timer' : `Paused · ${Math.ceil(delivery.remaining_seconds)} seconds remaining`));
      const question = run.snapshot.questions.find(q => q.id === run.active_question_id);
      if (question) card.append(node('h3', question.prompt));
      const timing = node('label', 'Question duration in seconds (blank for no timer) ', 'field');
      const duration = node('input'); duration.type = 'number'; duration.min = '1'; duration.max = '3600'; duration.value = '15'; timing.append(duration); card.append(timing);
      const open = ['open','paused'].includes(delivery.status);
      const common = {run_id:run.id};
      const timed = (name, payload, message) => {
        const seconds = duration.value === '' ? null : Number(duration.value);
        if (seconds !== null && (!Number.isInteger(seconds) || seconds < 1 || seconds > 3600)) { status.textContent = 'Choose 1–3600 seconds, or leave the duration blank.'; duration.focus(); return; }
        action(name, {...common,...payload,duration:seconds,auto_reveal:false}, message);
      };
      for (const item of run.snapshot.questions) {
        const used = [delivery,...(run.deliveries || [])].some(d => d.question_id === item.id);
        card.append(button('Send: ' + item.prompt, () => timed('open_question', {question_id:item.id}, `Send “${item.prompt}” to the selected audience?`), open || used || (!state.quiz_enabled && run.snapshot.kind === 'quiz')));
      }
      if (delivery.status === 'open') card.append(button('Pause', () => action('pause_question', common)),
        button('Add 15 seconds', () => action('extend_question', {...common,seconds:15}), delivery.ends_at == null));
      if (delivery.status === 'paused') card.append(button('Resume', () => action('resume_question', common)));
      if (open) card.append(button('Close question', () => action('close_question', common)));
      if (delivery.status === 'closed') card.append(button('Resend question', () => timed('resend_question', {}, 'Resend this question? Previous answers remain saved.')),
        button('Reveal answer', () => action('reveal_question', common, 'Reveal the result to the audience?')));
      if (delivery.id) card.append(button(run.hall_shared ? 'Stop hall sharing' : 'Share on hall',
        () => action('share_hall', {...common,share:!run.hall_shared}, run.hall_shared ? 'Stop sharing on the hall screen?' : 'Show this question on the hall screen?')));
      if (!open) card.append(button('Finish activity', () => action('finish_run', common,
        'Finish this activity? It cannot be resumed.' + (delivery.status === 'closed' ? ' Unrevealed results cannot be revealed afterwards.' : ''))));
      root.append(card);
    }
    const published = node('section', null, 'card'); published.append(node('h2', 'Published activities'));
    for (const activity of state.activities) for (const version of activity.versions) {
      const title = `${version.snapshot.title} · version ${version.version}`;
      published.append(button('Start ' + title, () => action('open_run', {activity_id:activity.id,version:version.version}, `Start ${title}?`),
        !state.quiz_enabled && version.snapshot.kind === 'quiz'));
    }
    if (!state.activities.length) published.append(node('p', 'An Organizer must publish an activity first.'));
    root.append(published); tick();
  }
  function tick() {
    root.querySelectorAll('[data-ends-at]').forEach(timer => {
      const seconds = Math.max(0, Math.ceil(Number(timer.dataset.endsAt) - (Date.now()/1000 + clockOffset)));
      timer.textContent = seconds ? `${seconds} seconds remaining` : 'Time elapsed · waiting for the latest status';
    });
  }
  async function refresh() {
    if (busy || pending || document.hidden || root.contains(document.activeElement)) return;
    const stamp = generation;
    try { const latest = await KEV.api(path); if (stamp === generation) { state = latest; clockOffset = (latest.now || Date.now()/1000) - Date.now()/1000; draw(); status.textContent = 'Connected.'; } }
    catch (error) { if (stamp === generation) status.textContent = error.message; }
  }
  retry.addEventListener('click', send);
  KEV.profile.mount({onEnd:clear}); refresh(); setInterval(refresh, 5000); setInterval(tick, 1000);
})();
