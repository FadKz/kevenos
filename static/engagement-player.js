/* Experimental Engagement Hub player. The server feature flag remains off by default. */
(function () {
  'use strict';
  const path = '/api/engagement/player';
  let generation = 0, reading = false, sending = false, currentKey = '', dismissed = '', timer, updateClock;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function clear() {
    generation++; reading = false; sending = false; currentKey = ''; dismissed = '';
    clearInterval(timer); timer = undefined; updateClock = undefined;
    const root = document.getElementById('engagement-player');
    if (root) root.replaceChildren();
    const score = document.getElementById('engagement-score');
    if (score) { score.replaceChildren(); score.hidden = true; }
  }

  function render(data, api, stamp, keepOpen = false) {
    const root = document.getElementById('engagement-player');
    if (!root) return;
    const score = document.getElementById('engagement-score');
    if (score) {
      score.hidden = !data.score;
      score.textContent = data.score ? `Your quiz: ${data.score.earned} of ${data.score.available} points so far` : '';
    }
    const run = data.question, delivery = run?.delivery, question = run?.questions?.[0];
    if (!delivery || !question) { clearInterval(timer); timer = undefined; updateClock = undefined; root.replaceChildren(); currentKey = ''; return; }
    const key = `${delivery.id}:${delivery.status}:${!!data.answered}`;
    if (key === currentKey) { if (updateClock) updateClock(delivery.ends_at, data.now); return; }
    currentKey = key;
    clearInterval(timer); timer = undefined; updateClock = undefined;
    root.replaceChildren();
    const reopen = node('button', 'Open event question', 'engagement-reopen btn');
    reopen.type = 'button';
    const dialog = node('dialog', null, 'engagement-dialog');
    dialog.setAttribute('aria-labelledby', 'engagement-title');
    const panel = node('div', null, 'engagement-panel');
    const header = node('header', null, 'engagement-header');
    const eyebrow = node('p', run.kind === 'quiz' ? 'EVENT QUIZ' : 'EVENT POLL', 'engagement-eyebrow');
    const title = node('h2', run.title); title.id = 'engagement-title';
    const close = node('button', 'Return to event', 'btn ghost'); close.type = 'button';
    close.addEventListener('click', () => { dismissed = delivery.id; dialog.close(); reopen.hidden = false; reopen.focus(); });
    header.append(eyebrow, title, close); panel.append(header);
    if (run.description) panel.append(node('p', run.description, 'engagement-description'));
    const status = node('p', '', 'engagement-status'); status.setAttribute('role', 'status');
    if (data.answered) status.textContent = 'Answer saved.';
    else if (delivery.status === 'paused') status.textContent = 'The event team paused this question. Please wait.';
    else if (delivery.status !== 'open' || (delivery.ends_at != null && data.now >= delivery.ends_at)) status.textContent = 'This question is closed.';
    else status.textContent = delivery.ends_at == null ? 'Open until the event team closes it.' : 'Answer before the timer ends.';
    panel.append(status);
    const prompt = node('h3', question.prompt, 'engagement-prompt'); panel.append(prompt);
    if (run.result) panel.append(node('p', `Your result: ${run.result.earned} of ${run.result.available} points.`, 'engagement-result'));
    if (delivery.status === 'revealed' && question.correct?.length) {
      panel.append(node('p', `Correct answer: ${question.correct.map(index => question.options[index]).join(', ')}`, 'engagement-result'));
    }
    const form = node('form', null, 'engagement-form');
    const choices = node('fieldset', null, 'engagement-choices');
    choices.append(node('legend', question.type === 'multi' ? 'Select all that apply' : 'Your answer'));
    const inputs = [];
    if (['single', 'true_false', 'multi', 'rating'].includes(question.type)) {
      const options = question.type === 'rating' ? ['1', '2', '3', '4', '5'] : question.options;
      options.forEach((option, index) => {
        const label = node('label', null, 'engagement-choice');
        const input = node('input'); input.type = question.type === 'multi' ? 'checkbox' : 'radio';
        input.name = 'engagement-answer'; input.value = String(question.type === 'rating' ? index + 1 : index);
        input.checked = data.answered && (Array.isArray(data.selection) ? data.selection.includes(Number(input.value)) : data.selection === Number(input.value));
        label.append(input, node('span', option)); choices.append(label); inputs.push(input);
      });
    } else if (question.type === 'text') {
      const input = node('textarea'); input.maxLength = 1000; input.rows = 4;
      if (data.answered && typeof data.selection === 'string') input.value = data.selection;
      input.setAttribute('aria-label', 'Your answer'); choices.append(input); inputs.push(input);
    }
    form.append(choices);
    const submit = node('button', 'Submit answer', 'btn engagement-submit'); submit.type = 'submit';
    const closed = data.answered || delivery.status !== 'open' || (delivery.ends_at != null && data.now >= delivery.ends_at);
    inputs.forEach(input => { input.disabled = closed; }); submit.disabled = closed;
    form.append(submit); panel.append(form); dialog.append(panel); root.append(reopen, dialog);
    if (!closed && delivery.ends_at != null) {
      let started = performance.now(), deadline = delivery.ends_at, serverNow = data.now;
      const clock = node('p', '', 'engagement-timer');
      panel.insertBefore(clock, prompt);
      const tick = () => {
        const remaining = Math.max(0, Math.ceil(deadline - serverNow - (performance.now() - started) / 1000));
        clock.textContent = `${remaining} seconds remaining`;
        if (!remaining) {
          clearInterval(timer); timer = undefined;
          inputs.forEach(input => { input.disabled = true; }); submit.disabled = true;
          status.textContent = 'Time is up. Waiting for the event team.';
        } else {
          inputs.forEach(input => { input.disabled = false; }); submit.disabled = sending;
          if (!sending) status.textContent = 'Answer before the timer ends.';
        }
      };
      tick(); timer = setInterval(tick, 500);
      updateClock = (newDeadline, now) => {
        deadline = newDeadline; serverNow = now; started = performance.now(); tick();
        if (!timer && deadline > serverNow) timer = setInterval(tick, 500);
      };
    }
    reopen.hidden = true;
    reopen.addEventListener('click', () => { reopen.hidden = true; dialog.showModal(); });
    dialog.addEventListener('cancel', () => { dismissed = delivery.id; reopen.hidden = false; });
    const editing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if (dismissed !== delivery.id && ((!closed && !data.answered) || keepOpen) && !editing && !document.querySelector('dialog[open]')) dialog.showModal();
    else reopen.hidden = false;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (sending || stamp !== generation) return;
      const selected = inputs.filter(input => input.checked);
      let choice;
      if (question.type === 'text') choice = inputs[0].value.trim();
      else if (question.type === 'multi') choice = selected.map(input => Number(input.value));
      else choice = selected.length ? Number(selected[0].value) : null;
      if (choice === null || choice === '' || (Array.isArray(choice) && !choice.length)) {
        status.textContent = 'Choose an answer before submitting.'; return;
      }
      sending = true; submit.disabled = true; status.textContent = 'Sending your answer…';
      try {
        await api(path, {body:{run_id:run.id, delivery_id:delivery.id, choice}});
        if (stamp !== generation) return;
        data.answered = true; data.selection = choice; currentKey = ''; render(data, api, stamp, true);
      } catch (error) {
        if (stamp !== generation) return;
        status.textContent = error?.message || 'Connection interrupted. You can retry the same answer.';
        submit.disabled = false;
      } finally { if (stamp === generation) sending = false; }
    });
  }

  async function refresh(api) {
    if (reading || sending || document.hidden) return;
    reading = true;
    const stamp = generation;
    try {
      const data = await api(path);
      if (stamp === generation) render(data, api, stamp);
    } catch (error) {
      if (stamp === generation && error?.status === 404) clear();
    } finally { if (stamp === generation) reading = false; }
  }

  window.EngagementPlayer = {refresh, clear};
})();
