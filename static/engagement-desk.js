/* Experimental Organizer controls. All commands go through the scoped server API. */
(function () {
  'use strict';
  const path = '/api/desk/engagement';
  let root, state, read, write, pending, generation = 0, busy = false, reading = false, dirty = false, selected = null;
  const element = (tag, value, className) => { const n = document.createElement(tag); if (value != null) n.textContent = value; if (className) n.className = className; return n; };
  function button(label, action, disabled = false) {
    const b = element('button', label, 'btn ghost'); b.type = 'button'; b.disabled = disabled; b.addEventListener('click', action); return b;
  }
  function field(label, name, value, multiline = false) {
    const wrap = element('label', label, 'engagement-field');
    const input = element(multiline ? 'textarea' : 'input'); input.name = name; input.value = value || ''; wrap.append(input); return wrap;
  }
  function identifier() {
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
  }
  function clear() { generation++; busy = false; reading = false; dirty = false; selected = null; pending = null; if (root) root.replaceChildren(); root = null; }
  function canLeave() {
    if (pending || busy) {
      if (root) root.querySelector('[role=status]').textContent = 'The previous result is uncertain. Retry the original action before leaving.';
      return false;
    }
    if (dirty && !confirm('Discard the unsaved activity draft?')) return false;
    dirty = false;
    return true;
  }
  function updateAnalytics(run) {
    const target = [...root.querySelectorAll('[data-engagement-run]')].find(item => item.dataset.engagementRun === run.id);
    if (!target || !run.analytics) return;
    const {respondents, distribution, denominator, suppressed_below:threshold} = run.analytics;
    target.textContent = `${respondents} ${respondents === 1 ? 'response' : 'responses'}. ` +
      (distribution ? `Answer counts: ${distribution.join(' · ')} (out of ${denominator} respondents).` :
        `Answer breakdown is hidden for groups smaller than ${threshold}, or for free text.`);
  }
  async function refresh() {
    if (busy || reading || !root) return;
    reading = true; const stamp = generation;
    try {
      const latest = await read(path);
      if (stamp !== generation) return;
      if (latest.revision !== state.revision) {
        root.querySelector('[role=status]').textContent = 'This activity changed elsewhere. Your draft is still here; reload the activity before saving.';
        return;
      }
      latest.runs.forEach(updateAnalytics);
      state.qa = latest.qa || []; state.qa_settings = latest.qa_settings || {};
      const list = root.querySelector('[data-qa-list]');
      if (list && !list.querySelector('textarea:not(:placeholder-shown)') && !list.contains(document.activeElement)) drawQuestions(list);
    } catch (_) { /* Keep the draft and last known counts during a disconnect. */ }
    finally { if (stamp === generation) reading = false; }
  }
  async function command(action, payload, revision = state.revision) {
    if (busy) return false;
    if (action !== 'save' && !pending && !canLeave()) return false;
    const stamp = generation, notice = root.querySelector('[role=status]');
    const signature = JSON.stringify({action, payload, revision});
    if (pending && pending.signature !== signature) {
      notice.textContent = 'The previous result is uncertain. Retry the original action before making another change.';
      return false;
    }
    pending ||= {signature, request_id:identifier()};
    busy = true; notice.textContent = 'Saving…';
    try {
      const result = await write(path, {action, payload, revision, request_id:pending.request_id});
      if (stamp !== generation) return false;
      pending = null; dirty = false;
      state = result; draw(); root.querySelector('[role=status]').textContent = 'Saved.'; return true;
    } catch (error) {
      if (error?.status >= 400 && error?.status < 500) pending = null;
      if (stamp === generation) notice.textContent = error?.message || 'Could not save. Your draft is still here.';
      return false;
    } finally { if (stamp === generation) busy = false; }
  }
  function questionEditor(item = {}, kind) {
    const box = element('fieldset', null, 'engagement-editor-question');
    if (item.id) box.dataset.questionId = item.id;
    box.append(element('legend', 'Question'));
    box.append(field('Question', 'prompt', item.prompt));
    const label = element('label', 'Answer type', 'engagement-field');
    const type = element('select'); type.name = 'type';
    for (const [value, title] of [['single','Single choice'],['multi','Multiple choice'],['true_false','True or false'],['rating','Rating 1–5'],['text','Free text']]) {
      const option = element('option', title); option.value = value; type.append(option);
    }
    type.value = item.type || 'single'; label.append(type); box.append(label);
    const options = field('Options, one per line', 'options', (item.options || []).join('\n'), true);
    const correct = field('Correct option numbers, separated by commas', 'correct', (item.correct || []).map(i => i + 1).join(', '));
    const update = () => {
      options.hidden = !['single', 'multi'].includes(type.value);
      correct.hidden = kind.value !== 'quiz';
    };
    type.addEventListener('change', update); kind.addEventListener('change', update); update();
    box.append(options, correct);
    box.append(button('Remove question', () => { if (box.parentElement.querySelectorAll('fieldset').length > 1) { dirty = true; box.remove(); } }));
    return box;
  }
  function drawQuestions(list) {
    list.replaceChildren();
    const questions = state.qa || [];
    if (!questions.length) list.append(element('p', 'No audience questions yet.'));
    questions.slice().reverse().forEach(question => {
      const card = element('article', null, 'engagement-run');
      card.append(element('p', question.text), element('p', `${question.status} · ${question.audience}`));
      if (question.answer) card.append(element('p', question.answer));
      if (question.status === 'pending') card.append(button('Approve question', () => {
        if (confirm('Publish this question to the audience?')) command('moderate_question', {id:question.id, decision:'approve'});
      }));
      if (['pending','approved','answered'].includes(question.status)) card.append(button('Remove question from audience', () => {
        if (confirm('Remove this question from the audience?')) command('moderate_question', {id:question.id, decision:'reject'});
      }));
      if (question.status === 'approved') {
        const form = element('form'); const answer = field('Public answer', 'answer', '', true);
        answer.querySelector('textarea').placeholder = 'Write an answer to share';
        const save = element('button', 'Publish answer', 'btn'); save.type = 'submit'; form.append(answer, save);
        form.addEventListener('submit', event => {
          event.preventDefault(); const text = answer.querySelector('textarea').value.trim();
          if (!text) { root.querySelector('[role=status]').textContent = 'Enter an answer before publishing.'; return; }
          if (confirm('Publish this answer to the audience?')) command('moderate_question', {id:question.id, decision:'answer', answer:text});
        });
        card.append(form);
      }
      list.append(card);
    });
  }
  function draw() {
    if (!root) return;
    root.replaceChildren();
    const intro = element('section', null, 'engagement-desk-intro');
    intro.append(element('h2', 'Engagement Hub'), element('p', 'Prepare an activity, publish its version, then choose when each question goes live.'));
    const notice = element('p'); notice.setAttribute('role', 'status'); intro.append(notice); root.append(intro);
    const layout = element('div', null, 'engagement-desk-layout');
    const library = element('section', null, 'engagement-desk-card'); library.append(element('h3', 'Activities'));
    library.append(button('New activity', () => { if (canLeave()) { selected = null; dirty = false; draw(); } }));
    if (!state.activities.length) library.append(element('p', 'No activities yet. Create a draft to get started.'));
    state.activities.forEach(activity => {
      const item = element('div', null, 'engagement-library-item');
      item.append(button(activity.draft.title, () => { if (canLeave()) { selected = activity.id; dirty = false; draw(); } }));
      item.append(element('span', `${activity.versions.length} published version(s)`));
      item.append(button('Publish draft', () => { if (confirm(`Publish “${activity.draft.title}” as a new version?`)) command('publish', {activity_id:activity.id}); }));
      if (activity.versions.length) item.append(button('Start run', () => { if (confirm(`Start the latest published version of “${activity.draft.title}”?`)) command('open_run', {activity_id:activity.id, version:activity.versions.length}); }));
      library.append(item);
    });
    const activity = state.activities.find(item => item.id === selected);
    const draft = activity?.draft;
    const editor = element('section', null, 'engagement-desk-card'); editor.append(element('h3', draft ? 'Edit draft' : 'Create a draft'));
    const form = element('form'); form.noValidate = true;
    form.addEventListener('input', () => { dirty = true; });
    form.addEventListener('change', () => { dirty = true; });
    form.append(field('Activity title', 'title', draft?.title));
    const kindLabel = element('label', 'Activity type', 'engagement-field');
    const kind = element('select'); kind.name = 'kind';
    for (const [value, text] of [['poll','Poll — no points'],['quiz','Quiz — scored']]) { const option = element('option', text); option.value = value; kind.append(option); }
    kind.value = draft?.kind || 'poll'; kindLabel.append(kind); form.append(kindLabel);
    const audiences = element('fieldset', null, 'engagement-editor-question'); audiences.append(element('legend', 'Who can take part?'));
    const audienceInput = (name, label, active) => {
      const wrap = element('label', label, 'engagement-checkbox'), input = element('input');
      input.type = 'checkbox'; input.name = `audience-${name}`; input.checked = active; wrap.prepend(input); audiences.append(wrap); return input;
    };
    const participantAudience = audienceInput('participant', 'Participants', (draft?.audiences || ['participant']).includes('participant'));
    const guestAudience = audienceInput('guest', 'Guests', (draft?.audiences || []).includes('guest'));
    audiences.append(element('p', 'Guests use an individual browser session; their identity is not verified.'));
    form.append(audiences);
    const questions = element('div', null, 'engagement-editor-questions');
    (draft?.questions?.length ? draft.questions : [{}]).forEach(item => questions.append(questionEditor(item, kind)));
    form.append(questions, button('Add question', () => { questions.append(questionEditor({}, kind)); dirty = true; }));
    const save = element('button', 'Save draft', 'btn'); save.type = 'submit'; form.append(save); editor.append(form);
    form.addEventListener('submit', event => {
      event.preventDefault();
      const title = form.elements.title.value.trim();
      if (!title) { notice.textContent = 'Enter a title for the activity.'; form.elements.title.focus(); return; }
      const allowed = [...(participantAudience.checked ? ['participant'] : []), ...(guestAudience.checked ? ['guest'] : [])];
      if (!allowed.length) { notice.textContent = 'Choose at least one audience.'; participantAudience.focus(); return; }
      const items = [...questions.querySelectorAll('fieldset')].map(box => {
        const type = box.querySelector('[name=type]').value;
        const correct = box.querySelector('[name=correct]').value.trim();
        const result = {type, prompt:box.querySelector('[name=prompt]').value.trim()};
        if (box.dataset.questionId) result.id = box.dataset.questionId;
        if (['single','multi'].includes(type)) result.options = box.querySelector('[name=options]').value.split('\n').map(v => v.trim()).filter(Boolean);
        if (kind.value === 'quiz') result.correct = correct ? correct.split(',').map(v => Number(v.trim()) - 1) : [];
        return result;
      });
      if (items.some(item => !item.prompt)) { notice.textContent = 'Enter a question for each item.'; return; }
      command('save', {title, kind:kind.value, audiences:allowed, questions:items, ...(draft ? {id:draft.id} : {})});
    });
    layout.append(library, editor); root.append(layout);
    const runs = element('section', null, 'engagement-desk-card'); runs.append(element('h3', 'Run show'));
    if (!state.runs.length) runs.append(element('p', 'Published activities can be started from the library.'));
    state.runs.filter(run => run.status === 'open').forEach(run => {
      const card = element('div', null, 'engagement-run');
      card.append(element('h4', `${run.snapshot.title} · version ${run.version}`));
      const stats = element('p'); stats.dataset.engagementRun = run.id; card.append(stats);
      const delivery = run.delivery;
      card.append(element('p', delivery ? `Current question: ${delivery.status}` : 'Ready to open the first question.'));
      const durationLabel = field('Question duration in seconds (leave blank for no timer)', 'duration', '15');
      const duration = durationLabel.querySelector('input'); duration.type = 'number'; duration.min = '1'; duration.max = '3600'; duration.step = '1';
      card.append(durationLabel);
      if (delivery) card.append(button(run.hall_shared ? 'Stop sharing on hall' : 'Share on hall', () => {
        const share = !run.hall_shared;
        if (confirm(share ? 'Show this question on the hall display?' : 'Return the hall display to the event?'))
          command('share_hall', {run_id:run.id, share});
      }));
      if (delivery?.status === 'open') card.append(button('Close question', () => command('close_question', {run_id:run.id})));
      if (delivery?.status === 'closed') {
        card.append(button('Reveal answer', () => { if (confirm('Reveal the answer to the audience?')) command('reveal_question', {run_id:run.id}); }));
        card.append(button('Resend question', () => {
          const seconds = duration.value === '' ? null : Number(duration.value);
          if (seconds !== null && (!Number.isInteger(seconds) || seconds < 1 || seconds > 3600)) {
            notice.textContent = 'Choose a duration from 1 to 3600 seconds, or leave it blank.'; duration.focus(); return;
          }
          if (confirm('Resend this question to people who have not answered? Existing answers will be kept.'))
            command('resend_question', {run_id:run.id, duration:seconds});
        }));
      }
      run.snapshot.questions.forEach(question => {
        const used = (run.delivered_question_ids || []).includes(question.id);
        const row = element('div', null, 'engagement-run-question'); row.append(element('span', question.prompt));
        row.append(button(used ? 'Sent' : 'Open question', () => {
          const seconds = duration.value === '' ? null : Number(duration.value);
          if (seconds !== null && (!Number.isInteger(seconds) || seconds < 1 || seconds > 3600)) {
            notice.textContent = 'Choose a duration from 1 to 3600 seconds, or leave it blank.'; duration.focus(); return;
          }
          const timing = seconds === null ? 'until closed' : `for ${seconds} seconds`;
          if (confirm(`Send “${question.prompt}” to participants ${timing}?`)) command('open_question', {run_id:run.id, question_id:question.id, duration:seconds});
        }, used || delivery?.status === 'open'));
        card.append(row);
      });
      runs.append(card);
    });
    root.append(runs);
    const qa = element('section', null, 'engagement-desk-card'); qa.append(element('h3', 'Audience questions'));
    qa.append(element('p', 'Choose who may submit. Questions stay private until you approve them.'));
    const config = element('form');
    const check = (name, label, value) => {
      const wrap = element('label', label, 'engagement-checkbox'); const input = element('input');
      input.type = 'checkbox'; input.name = name; input.checked = !!value; wrap.prepend(input); config.append(wrap); return input;
    };
    const participants = check('participants', 'Allow participants', state.qa_settings?.participants);
    const guests = check('guests', 'Allow guests', state.qa_settings?.guests);
    const apply = element('button', 'Save audience settings', 'btn'); apply.type = 'submit'; config.append(apply);
    config.addEventListener('submit', event => { event.preventDefault(); command('configure_questions', {participants:participants.checked, guests:guests.checked}); });
    qa.append(config);
    const list = element('div'); list.dataset.qaList = ''; qa.append(list); root.append(qa); drawQuestions(list);
    state.runs.forEach(updateAnalytics);
  }
  async function mount(target, reader, writer) {
    if (root === target) return refresh();
    clear(); root = target; read = reader; write = writer; const stamp = generation;
    root.replaceChildren(element('p', 'Loading Engagement Hub…'));
    try { const result = await reader(path); if (stamp === generation) { state = result; draw(); } }
    catch (error) { if (stamp === generation) root.replaceChildren(element('p', error?.message || 'Engagement Hub is unavailable.')); }
  }
  window.EngagementDesk = {mount, clear, canLeave};
})();
