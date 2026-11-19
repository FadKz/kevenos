/* Experimental Organizer controls. All commands go through the scoped server API. */
(function () {
  'use strict';
  const path = '/api/desk/engagement';
  let root, state, read, write, pending, updateRuns, capabilities = {}, answerDrafts = {}, generation = 0, busy = false, reading = false, dirty = false, selected = null;
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
  function clear() { generation++; busy = false; reading = false; dirty = false; selected = null; pending = null; updateRuns = null; answerDrafts = {}; if (root) root.replaceChildren(); root = null; }
  function canLeave(includeAnswers = false) {
    if (pending || busy) {
      if (root) root.querySelector('[role=status]').textContent = 'The previous result is uncertain. Retry the original action before leaving.';
      return false;
    }
    if ((dirty || (includeAnswers && Object.values(answerDrafts).some(value => value.trim()))) && !confirm('Discard your unsaved work?')) return false;
    dirty = false;
    return true;
  }
  function analyticsText(question, metrics) {
    const {respondents, distribution, denominator, suppressed_below:threshold, correct_count:correct, median_seconds:median} = metrics;
    const labels = question?.type === 'rating' ? ['1','2','3','4','5'] : question?.options || [];
    const counts = distribution?.map((count,index) => `${labels[index] || `Option ${index + 1}`}: ${count}`).join(' · ');
    return `${respondents} ${respondents === 1 ? 'response' : 'responses'}. ` +
      (distribution ? `Answer counts: ${counts} (out of ${denominator} respondents).` :
        `Answer breakdown is hidden for groups smaller than ${threshold}, or for free text.`) +
      (correct == null ? '' : ` ${correct} of ${denominator} respondents answered correctly.`) +
      (median == null ? '' : ` Median response time: ${median} seconds.`);
  }
  function updateAnalytics(run) {
    const target = [...root.querySelectorAll('[data-engagement-run]')].find(item => item.dataset.engagementRun === run.id);
    if (!target || !run.analytics) return;
    const question = run.snapshot.questions.find(item => item.id === run.active_question_id);
    target.textContent = analyticsText(question, run.analytics);
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
      const runList = root.querySelector('[data-run-list]');
      const changed = latest.runs.some(run => {
        const card = [...(runList?.querySelectorAll('[data-run-card]') || [])].find(item => item.dataset.runCard === run.id);
        return run.status === 'open' && card?.dataset.deliveryStatus !== (run.delivery?.status || 'none');
      });
      state.runs = latest.runs;
      if (changed && updateRuns && !runList?.contains(document.activeElement)) updateRuns();
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
      if (action === 'archive_draft' && payload.archive && selected === payload.activity_id) selected = null;
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
    const points = field('Points for a correct answer', 'points', String(item.points ?? 1));
    const pointsInput = points.querySelector('input'); pointsInput.type = 'number'; pointsInput.min = '1'; pointsInput.max = '100'; pointsInput.step = '1';
    const update = () => {
      options.hidden = !['single', 'multi'].includes(type.value);
      correct.hidden = kind.value !== 'quiz';
      points.hidden = kind.value !== 'quiz';
    };
    type.addEventListener('change', update); kind.addEventListener('change', update); update();
    box.append(options, correct, points, field('Explanation shown after reveal (optional)', 'explanation', item.explanation, true));
    if (item.evidence) box.append(field('Source excerpt — verify this supports the question', 'evidence', item.evidence, true));
    box.append(button('Move earlier', () => {
      const previous = box.previousElementSibling;
      if (previous) { box.parentElement.insertBefore(box, previous); dirty = true; }
    }));
    box.append(button('Move later', () => {
      const next = box.nextElementSibling;
      if (next) { box.parentElement.insertBefore(next, box); dirty = true; }
    }));
    box.append(button('Duplicate question', () => {
      const evidence = box.querySelector('[name=evidence]')?.value || '';
      const copy = questionEditor({evidence}, kind);
      for (const name of ['prompt', 'type', 'options', 'correct', 'points', 'explanation', 'evidence']) {
        const source = box.querySelector(`[name=${name}]`), target = copy.querySelector(`[name=${name}]`);
        if (source && target) target.value = source.value;
      }
      copy.querySelector('[name=type]').dispatchEvent(new Event('change', {bubbles:true}));
      box.after(copy); dirty = true;
    }));
    box.append(button('Remove question', () => { if (box.parentElement.querySelectorAll('fieldset').length > 1) { dirty = true; box.remove(); } }));
    return box;
  }
  function drawQuestions(list) {
    list.replaceChildren();
    const filter = root.querySelector('[name=qa-filter]')?.value || 'all';
    const search = (root.querySelector('[name=qa-search]')?.value || '').trim().toLocaleLowerCase();
    const questions = (state.qa || []).filter(item => (filter === 'all' || item.status === filter) &&
      (!search || item.text.toLocaleLowerCase().includes(search)));
    if (!questions.length) list.append(element('p', state.qa?.length ? 'No questions match these filters.' : 'No audience questions yet.'));
    questions.slice().sort((a, b) => (b.votes || 0) - (a.votes || 0)).forEach(question => {
      const card = element('article', null, 'engagement-run');
      card.append(element('p', question.text), element('p', `${question.status} · ${question.audience} · ${question.votes || 0} supports`));
      if (question.answer) card.append(element('p', question.answer));
      if (question.status === 'pending') card.append(button('Approve question', () => {
        if (confirm('Publish this question to the audience?')) command('moderate_question', {id:question.id, decision:'approve'});
      }));
      if (question.status === 'pending') {
        const targets = (state.qa || []).filter(item => item.id !== question.id && ['approved','answered'].includes(item.status));
        if (targets.length) {
          const label = element('label', 'Combine with a published question', 'engagement-field');
          const select = element('select'); select.name = 'merge-target';
          const placeholder = element('option', 'Choose a published question'); placeholder.value = ''; select.append(placeholder);
          targets.forEach(item => { const option = element('option', item.text); option.value = item.id; select.append(option); });
          label.append(select); card.append(label, button('Combine questions', () => {
            if (!select.value) { root.querySelector('[role=status]').textContent = 'Choose a published question first.'; return; }
            if (confirm('Combine this pending question with the selected published question? No votes will be transferred.'))
              command('merge_question', {id:question.id, target_id:select.value});
          }));
        }
      }
      if (question.status === 'merged') card.append(button('Restore for review', () => {
        if (confirm('Restore this question to the review queue?')) command('moderate_question', {id:question.id, decision:'restore'});
      }));
      if (['pending','approved','answered'].includes(question.status)) card.append(button('Remove question from audience', () => {
        if (confirm('Remove this question from the audience?')) command('moderate_question', {id:question.id, decision:'reject'});
      }));
      if (question.status === 'approved') {
        const form = element('form'); const answer = field('Public answer', 'answer', answerDrafts[question.id] || '', true);
        answer.querySelector('textarea').placeholder = 'Write an answer to share';
        answer.querySelector('textarea').addEventListener('input', event => { answerDrafts[question.id] = event.target.value; });
        const save = element('button', 'Publish answer', 'btn'); save.type = 'submit'; form.append(answer, save);
        form.addEventListener('submit', event => {
          event.preventDefault(); const text = answer.querySelector('textarea').value.trim();
          if (!text) { root.querySelector('[role=status]').textContent = 'Enter an answer before publishing.'; return; }
          if (confirm('Publish this answer to the audience?')) command('moderate_question', {id:question.id, decision:'answer', answer:text}).then(saved => { if (saved) delete answerDrafts[question.id]; });
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
    if (state.quiz_enabled === false) intro.append(element('p', 'Quizzes are disabled by the event Admin. Polls and audience questions remain available.'));
    const notice = element('p'); notice.setAttribute('role', 'status'); intro.append(notice); root.append(intro);
    const legacy = element('details', null, 'engagement-desk-card');
    legacy.append(element('summary', 'Previous quiz and audience history'),
      element('p', 'Existing results remain in the previous Quiz and Audience sections. Inspect counts before planning a migration.'));
    const legacyResult = element('p'); legacyResult.setAttribute('role', 'status');
    const legacyDrafts = element('div');
    legacy.append(button('Check legacy history', async () => {
      const stamp = generation; legacyResult.textContent = 'Checking…';
      try {
        const result = await read('/api/desk/engagement/legacy-preview');
        if (stamp === generation) {
          legacyResult.textContent = `${result.quiz.runs} legacy quiz runs and ${result.audience.packs} audience packs (${result.audience.draft_packs} drafts). No changes were made.`;
          if (result.quiz.cases) {
            const c = result.quiz.cases;
            legacyResult.textContent += ` ${result.quiz.answer_entries} saved answers; ${c.voided} voided, ${c.unscored} unscored, ${c.excluded_audiences} with exclusions, ${c.predictions} predictions, ${c.warmup_or_post} warm-up or post, ${c.catchup_or_waiting} waiting or catch-up, and ${c.open} marked open. Counts can overlap.`;
          }
          legacyDrafts.replaceChildren();
          (result.audience.drafts || []).forEach(item => {
            const row = element('div', null, 'engagement-library-item');
            row.append(element('span', `${item.title} · ${item.question_count} questions`));
            row.append(button('Copy draft', () => {
              if (confirm(`Copy “${item.title}” into a new Engagement Hub draft? The original and historical responses remain unchanged.`))
                command('copy_legacy_pack', {pack_id:item.id, source_digest:item.digest});
            }));
            legacyDrafts.append(row);
          });
        }
      } catch (error) { if (stamp === generation) legacyResult.textContent = error?.message || 'Legacy history is unavailable.'; }
    }), legacyResult, legacyDrafts);
    root.append(legacy);
    const layout = element('div', null, 'engagement-desk-layout');
    const library = element('section', null, 'engagement-desk-card'); library.append(element('h3', 'Activities'));
    library.append(button('New activity', () => { if (canLeave()) { selected = null; dirty = false; draw(); } }));
    if (!state.activities.length) library.append(element('p', 'No activities yet. Create a draft to get started.'));
    state.activities.forEach(activity => {
      const item = element('div', null, 'engagement-library-item');
      item.append(button(activity.draft.title, () => { if (canLeave()) { selected = activity.id; dirty = false; draw(); } }, !!activity.archived));
      item.append(element('span', activity.archived ? 'Archived draft' : `${activity.versions.length} published version(s)`));
      if (!activity.archived) item.append(button('Publish draft', () => { if (confirm(`Publish “${activity.draft.title}” as a new version?`)) command('publish', {activity_id:activity.id}); }));
      if (!activity.versions.length) item.append(button(activity.archived ? 'Restore draft' : 'Archive draft', () => {
        const archive = !activity.archived;
        if (confirm(`${archive ? 'Archive' : 'Restore'} “${activity.draft.title}”? The draft is retained.`))
          command('archive_draft', {activity_id:activity.id, archive});
      }));
      if (activity.versions.length) item.append(button('Start run', () => { if (confirm(`Start the latest published version of “${activity.draft.title}”?`)) command('open_run', {activity_id:activity.id, version:activity.versions.length}); },
        state.quiz_enabled === false && activity.versions[activity.versions.length-1].snapshot?.kind === 'quiz'));
      library.append(item);
    });
    const activity = state.activities.find(item => item.id === selected);
    const draft = activity?.draft;
    const editor = element('section', null, 'engagement-desk-card'); editor.append(element('h3', draft ? 'Edit draft' : 'Create a draft'));
    const form = element('form'); form.noValidate = true;
    form.addEventListener('input', () => { dirty = true; });
    form.addEventListener('change', () => { dirty = true; });
    form.append(field('Activity title', 'title', draft?.title));
    form.append(field('Description (optional)', 'description', draft?.description, true));
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
        if (kind.value === 'quiz') {
          result.correct = correct ? correct.split(',').map(v => Number(v.trim()) - 1) : [];
          result.points = Number(box.querySelector('[name=points]').value);
        }
        const explanation = box.querySelector('[name=explanation]').value.trim();
        if (explanation) result.explanation = explanation;
        const evidence = box.querySelector('[name=evidence]')?.value.trim();
        if (evidence) result.evidence = evidence;
        return result;
      });
      if (items.some(item => !item.prompt)) { notice.textContent = 'Enter a question for each item.'; return; }
      command('save', {title, description:form.elements.description.value.trim(), kind:kind.value, audiences:allowed, questions:items, ...(draft ? {id:draft.id} : {})});
    });
    layout.append(library, editor); root.append(layout);
    if (capabilities.ai) {
      const ai = element('section', null, 'engagement-desk-card'); ai.dataset.aiDraft = '';
      ai.append(element('h3', 'Draft with AI'), element('p', 'Provide source text you are allowed to share. Review every suggestion and source excerpt before saving.'));
      const aiForm = element('form');
      const sourceLabel = field('Source text', 'source', '', true), source = sourceLabel.querySelector('textarea'); source.maxLength = 8000;
      const countLabel = field('Number of questions', 'count', '3'), count = countLabel.querySelector('input'); count.type = 'number'; count.min = '1'; count.max = '5';
      const consentLabel = element('label', 'I can send this source text to the configured AI gateway.');
      const consent = element('input'); consent.type = 'checkbox'; consentLabel.prepend(consent);
      const generate = element('button', 'Generate suggestions', 'btn'); generate.type = 'submit';
      const aiNotice = element('p'); aiNotice.setAttribute('role', 'status');
      const preview = element('div'); aiForm.append(sourceLabel, countLabel, consentLabel, generate, aiNotice); ai.append(aiForm, preview); root.append(ai);
      aiForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy) return;
        if (!consent.checked) { aiNotice.textContent = 'Confirm that the source may be sent to the AI gateway.'; return; }
        const number = Number(count.value), text = source.value.trim();
        if (!text || !Number.isInteger(number) || number < 1 || number > 5) { aiNotice.textContent = 'Enter source text and choose one to five questions.'; return; }
        busy = true; generate.disabled = true; aiNotice.textContent = 'Generating suggestions…'; const stamp = generation;
        try {
          const suggestion = await write('/api/desk/engagement-ai', {source:text, count:number, kind:kind.value, disclose:true});
          if (stamp !== generation) return;
          preview.replaceChildren(element('h4', suggestion.title), element('p', 'Suggested questions — review before saving.'));
          suggestion.questions.forEach(question => preview.append(element('p', question.prompt), element('blockquote', question.evidence)));
          preview.append(button('Use suggestions', () => {
            if (!canLeave()) return;
            form.elements.title.value = suggestion.title; kind.value = suggestion.kind;
            questions.replaceChildren(...suggestion.questions.map(item => questionEditor(item, kind)));
            dirty = true; aiNotice.textContent = 'Suggestions added to the unsaved draft. Review the answers and source excerpts.';
          }));
          aiNotice.textContent = 'Suggestions are ready for review.';
        } catch (error) { if (stamp === generation) aiNotice.textContent = error?.message || 'AI drafting is unavailable.'; }
        finally { if (stamp === generation) { busy = false; generate.disabled = false; } }
      });
    }
    const runs = element('section', null, 'engagement-desk-card'); runs.dataset.runList = '';
    updateRuns = () => {
    const settings = new Map([...runs.querySelectorAll('[data-run-card]')].map(card => [card.dataset.runCard, {
      duration:card.querySelector('[name=duration]')?.value, auto:card.querySelector('[name=auto-reveal]')?.checked}]));
    runs.replaceChildren(element('h3', 'Run show'));
    if (!state.runs.length) runs.append(element('p', 'Published activities can be started from the library.'));
    state.runs.filter(run => run.status === 'open').forEach(run => {
      const card = element('div', null, 'engagement-run');
      card.dataset.runCard = run.id; card.dataset.deliveryStatus = run.delivery?.status || 'none';
      card.append(element('h4', `${run.snapshot.title} · version ${run.version}`));
      card.append(button('Export run analytics', () => {
        if (!window.KEV?.download) { notice.textContent = 'Downloads are unavailable in this session.'; return; }
        window.KEV.download('/api/desk/engagement/analytics.csv?run_id=' + encodeURIComponent(run.id)).catch(error => {
          notice.textContent = error?.message || 'Could not download analytics.';
        });
      }));
      const stats = element('p'); stats.dataset.engagementRun = run.id; card.append(stats);
      const delivery = run.delivery;
      card.append(element('p', delivery ? `Current question: ${delivery.status}` : 'Ready to open the first question.'));
      const durationLabel = field('Question duration in seconds (leave blank for no timer)', 'duration', '15');
      const duration = durationLabel.querySelector('input'); duration.type = 'number'; duration.min = '1'; duration.max = '3600'; duration.step = '1';
      if (settings.has(run.id)) duration.value = settings.get(run.id).duration;
      card.append(durationLabel);
      const autoLabel = element('label', 'Reveal answers automatically when the timer ends', 'engagement-checkbox');
      const autoReveal = element('input'); autoReveal.type = 'checkbox'; autoReveal.name = 'auto-reveal'; autoLabel.prepend(autoReveal); card.append(autoLabel);
      if (settings.has(run.id)) autoReveal.checked = settings.get(run.id).auto;
      if (delivery) card.append(button(run.hall_shared ? 'Stop sharing on hall' : 'Share on hall', () => {
        const share = !run.hall_shared;
        if (confirm(share ? 'Show this question on the hall display?' : 'Stop sharing this question and restore the previous hall scene?'))
          command('share_hall', {run_id:run.id, share});
      }));
      if (delivery?.status === 'open') {
        card.append(button('Pause question', () => command('pause_question', {run_id:run.id})));
        if (delivery.ends_at != null) card.append(button('Add 15 seconds', () => command('extend_question', {run_id:run.id, seconds:15})));
      }
      if (delivery?.status === 'paused') card.append(button('Resume question', () => command('resume_question', {run_id:run.id})));
      if (['open','paused'].includes(delivery?.status)) card.append(button('Close question', () => command('close_question', {run_id:run.id})));
      if (!['open','paused'].includes(delivery?.status)) card.append(button('Finish run', () => {
        const warning = delivery?.status === 'closed' ? ' The current results have not been revealed and cannot be revealed after finishing.' : '';
        if (confirm('Finish this run? Its answers and results will be retained, and hall sharing will stop.' + warning))
          command('finish_run', {run_id:run.id});
      }));
      if (delivery?.status === 'closed') {
        card.append(button('Reveal answer', () => { if (confirm('Reveal the answer to the audience?')) command('reveal_question', {run_id:run.id}); }));
        card.append(button('Resend question', () => {
          const seconds = duration.value === '' ? null : Number(duration.value);
          if (seconds !== null && (!Number.isInteger(seconds) || seconds < 1 || seconds > 3600)) {
            notice.textContent = 'Choose a duration from 1 to 3600 seconds, or leave it blank.'; duration.focus(); return;
          }
          if (seconds === null && autoReveal.checked) { notice.textContent = 'Automatic reveal requires a timer.'; duration.focus(); return; }
          if (confirm('Resend this question to people who have not answered? Existing answers will be kept.'))
            command('resend_question', {run_id:run.id, duration:seconds, auto_reveal:autoReveal.checked});
        }, state.quiz_enabled === false && run.snapshot.kind === 'quiz'));
      }
      run.snapshot.questions.forEach(question => {
        const used = (run.delivered_question_ids || []).includes(question.id);
        const row = element('div', null, 'engagement-run-question'); row.append(element('span', question.prompt));
        row.append(button(used ? 'Sent' : 'Open question', () => {
          const seconds = duration.value === '' ? null : Number(duration.value);
          if (seconds !== null && (!Number.isInteger(seconds) || seconds < 1 || seconds > 3600)) {
            notice.textContent = 'Choose a duration from 1 to 3600 seconds, or leave it blank.'; duration.focus(); return;
          }
          if (seconds === null && autoReveal.checked) { notice.textContent = 'Automatic reveal requires a timer.'; duration.focus(); return; }
          const timing = seconds === null ? 'until closed' : `for ${seconds} seconds`;
          const reveal = autoReveal.checked ? ' Answers will be revealed when the timer ends.' : ' Answers stay hidden until you reveal them.';
          if (confirm(`Send “${question.prompt}” to the selected audience ${timing}?${reveal}`)) command('open_question', {run_id:run.id, question_id:question.id, duration:seconds, auto_reveal:autoReveal.checked});
        }, used || ['open','paused'].includes(delivery?.status) || (state.quiz_enabled === false && run.snapshot.kind === 'quiz')));
        card.append(row);
      });
      runs.append(card);
    });
    const completed = state.runs.filter(run => run.status === 'completed');
    if (completed.length) {
      const history = element('details'); history.append(element('summary', `Completed runs (${completed.length})`));
      completed.forEach(run => {
        const row = element('div', null, 'engagement-run');
        row.append(element('h4', `${run.snapshot.title} · version ${run.version}`));
        row.append(button('Export run analytics', () => {
          if (!window.KEV?.download) { notice.textContent = 'Downloads are unavailable in this session.'; return; }
          window.KEV.download('/api/desk/engagement/analytics.csv?run_id=' + encodeURIComponent(run.id)).catch(error => {
            notice.textContent = error?.message || 'Could not download analytics.';
          });
        }));
        (run.question_analytics || []).forEach(metrics => {
          const question = run.snapshot.questions.find(item => item.id === metrics.question_id);
          if (question) row.append(element('h5', question.prompt), element('p', analyticsText(question, metrics)));
        });
        history.append(row);
      });
      runs.append(history);
    }
    };
    updateRuns();
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
    const filterLabel = element('label', 'Question status', 'engagement-field');
    const filter = element('select'); filter.name = 'qa-filter';
    for (const [value, title] of [['all','All questions'],['pending','Awaiting review'],['approved','Approved'],['answered','Answered'],['merged','Combined'],['rejected','Removed']]) {
      const option = element('option', title); option.value = value; filter.append(option);
    }
    filterLabel.append(filter); qa.append(filterLabel);
    const searchLabel = field('Search audience questions', 'qa-search', ''); qa.append(searchLabel);
    const list = element('div'); list.dataset.qaList = ''; qa.append(list); root.append(qa); drawQuestions(list);
    filter.addEventListener('change', () => drawQuestions(list));
    searchLabel.querySelector('input').addEventListener('input', () => drawQuestions(list));
    state.runs.forEach(updateAnalytics);
  }
  async function mount(target, reader, writer, options = {}) {
    if (root === target) return refresh();
    clear(); root = target; read = reader; write = writer; capabilities = options; const stamp = generation;
    root.replaceChildren(element('p', 'Loading Engagement Hub…'));
    try { const result = await reader(path); if (stamp === generation) { state = result; draw(); } }
    catch (error) { if (stamp === generation) root.replaceChildren(element('p', error?.message || 'Engagement Hub is unavailable.')); }
  }
  window.EngagementDesk = {mount, clear, canLeave};
})();
