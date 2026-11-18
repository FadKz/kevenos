(() => {
  const DESK = '/api/desk/audience';
  const PARTICIPANT = '/api/participant/audience';
  const RATING = [1, 2, 3, 4, 5];
  const TYPES = {choice: 'Multiple choice', rating: 'Rating (1–5)', text: 'Free text'};
  const STATUSES = {draft: 'Draft', live: 'Accepting answers', closed: 'Closed', cancelled: 'Cancelled'};
  const TIME = new Intl.DateTimeFormat('en-GB', {timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit'});
  let view;

  function node(tag, props = {}, ...children) {
    const element = Object.assign(document.createElement(tag), props);
    element.append(...children);
    return element;
  }

  function status(role, className) {
    const element = node('p', {className: 'audience-message ' + className});
    element.setAttribute('role', role);
    return element;
  }

  function button(label, onClick, kind = 'ghost') {
    return node('button', {type: 'button', className: 'btn ' + kind, textContent: label, onclick: onClick});
  }

  function section(...children) {
    return node('section', {className: 'card audience-card audience-stack'}, ...children);
  }

  function field(label, control) {
    return node('label', {}, label, control);
  }

  function textField(value, limit, required = false, multiline = false) {
    return node(multiline ? 'textarea' : 'input', {
      ...(multiline ? {rows: 2} : {type: 'text'}),
      value, maxLength: limit, required
    });
  }

  function options(question) {
    return question.type === 'rating'
      ? RATING.map(value => ({key: value, text: String(value)}))
      : question.options || [];
  }

  function countLabel(count) {
    return count + (count === 1 ? ' response' : ' responses');
  }

  function results(question, result, showText = false) {
    const lines = options(question).map(({key, text}) =>
      node('li', {}, text + ' · ' + (result?.counts?.[key] || 0)));
    const total = result?.count || 0;
    const written = showText ? result?.texts || [] : [];
    for (const text of written) lines.push(node('li', {}, text));
    const limited = showText && question.type === 'text' && written.length < total;
    return node('div', {}, node('p', {}, countLabel(total)),
      ...(limited ? [node('p', {className: 'mut'}, 'Showing ' + written.length + ' of ' + total + ' written responses.')] : []),
      ...(lines.length ? [node('ul', {className: 'audience-results'}, ...lines)] : []));
  }

  function clear() {
    if (!view) return;
    view.request?.abort();
    view.mutation?.abort();
    view.feedback?.abort();
    view.root.replaceChildren();
    if (view.kind === 'participant') {
      view.root.hidden = true;
      view.history.remove();
    }
    view.root.removeAttribute('aria-busy');
    view = null;
  }

  function start(kind, root, get, post) {
    clear();
    view = {kind, root, get, post, serial: 0, lastRead: -Infinity, preview: Boolean(window.KEV?.profile?.isPreview)};
    if (kind === 'desk') {
      view.notice = status('status', 'audience-receipt');
      view.error = status('alert', 'audience-error');
      view.editor = node('div');
      view.filter = 'all';
      view.timers = new Map();
      const create = button('New question pack', () => editPack(view), 'alt');
      create.dataset.audienceFocus = 'new';
      create.disabled = view.preview;
      const filters = [['all','All packs'], ['live','Open'], ['draft','Drafts'], ['closed','History']];
      const controls = filters.map(([key, label]) => {
        const item = button(label, () => { view.filter = key; if (view.data) drawDesk(view); });
        item.dataset.packFilter = key;
        return item;
      });
      const search = node('input', {type:'search', placeholder:'Pack title or question'});
      search.addEventListener('input', () => { if (view.data) drawDesk(view); });
      view.filters = controls;
      view.search = search;
      view.activity = status('status', 'mut');
      view.list = node('div', {className: 'audience-stack'}, node('p', {}, 'Loading question packs…'));
      const toolbar = section(node('div', {className:'row'}, node('h2', {className:'grow'}, 'Question packs'), create),
        view.activity, node('div', {className:'audience-desk-filter row', role:'group', ariaLabel:'Filter question packs'}, ...controls),
        field('Search packs',search));
      toolbar.classList.add('audience-desk-toolbar');
      root.replaceChildren(node('div', {className: 'audience-stack'}, toolbar, view.notice, view.error, view.editor, view.list));
    } else {
      view.error = status('alert', 'audience-error');
      view.live = node('div', {className: 'audience-stack'});
      view.history = node('details', {className: 'card audience-card audience-stack', hidden: true});
      view.receipts = new Map();
      const resultsRoot = document.getElementById('audience-question-results');
      root.replaceChildren(node('div', {className: 'audience-stack'}, view.error, view.live,
        ...(resultsRoot ? [] : [view.history])));
      if (resultsRoot) resultsRoot.replaceChildren(view.history);
    }
    return view;
  }

  function desk(root, get, post) {
    const state = view?.kind === 'desk' && view.root === root ? view : start('desk', root, get, post);
    refresh(state);
  }

  function pause() {
    if (view?.kind !== 'participant') return;
    view.serial++;
    view.request?.abort();
    view.root.hidden = true;
    window.KEV?.messages?.replace('audience', []);
  }

  function participant(root, api) {
    const state = view?.kind === 'participant' && view.root === root ? view
      : start('participant', root, (path, signal) => api(path, {signal}), (path, body, signal) => api(path, {body, signal}));
    refresh(state);
  }

  async function refresh(state, force = false) {
    if (state.reading || state.pending || document.hidden || (!force && performance.now() - state.lastRead < 1500)) return;
    const serial = state.serial;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    state.request = controller;
    state.reading = true;
    state.lastRead = performance.now();
    try {
      const data = await state.get(state.kind === 'desk' ? DESK : PARTICIPANT, controller.signal);
      if (view === state && serial === state.serial) receive(state, data);
    } catch (error) {
      if (view !== state || serial !== state.serial) return;
      if (state.kind === 'desk') {
        state.error.textContent = error.message || 'Audience questions are unavailable.';
        state.refreshFailed = true;
        if (!state.data) state.list.replaceChildren(button('Retry', () => refresh(state, true)));
      } else {
        state.root.hidden = true;
        if ([401, 403].includes(error.status)) clear();
      }
    } finally {
      clearTimeout(timer);
      state.reading = false;
      state.request = null;
    }
  }

  function receive(state, data) {
    state.data = data;
    if (state.kind === 'desk') {
      if (state.refreshFailed) state.error.textContent = '';
      state.refreshFailed = false;
      drawDesk(state);
    } else drawParticipant(state);
  }

  async function write(state, body, onSuccess, onError) {
    if (state.preview || state.pending || view !== state) return;
    state.pending = true;
    const serial = ++state.serial;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    state.mutation = controller;
    state.root.setAttribute('aria-busy', 'true');
    try {
      const data = await state.post(state.kind === 'desk' ? DESK : PARTICIPANT, body, controller.signal);
      if (view === state && serial === state.serial) {
        if (state.kind === 'desk') {
          const destinations = {save:'draft',send:'live',resend:'live',close:'closed',cancel:'closed',release:'closed'};
          if (state.filter !== 'all') state.filter = destinations[body.action] || state.filter;
          if (body.action === 'save' || body.action === 'resend') {
            const existing = new Set(state.data.packs.map(pack => pack.id));
            const added = data.packs.find(pack => !existing.has(pack.id));
            if (added) state.selected = added.id;
          }
        }
        receive(state, data);
        onSuccess();
      }
    } catch (error) {
      if (view !== state || serial !== state.serial) return;
      onError(error.name === 'AbortError' ? 'Could not confirm your change. Check the current state before retrying.'
        : error.message || 'Your change could not be saved.');
      if (error.status === 409 || error.name === 'AbortError') state.lastRead = -Infinity;
    } finally {
      clearTimeout(timer);
      state.mutation = null;
      if (view === state) {
        state.root.removeAttribute('aria-busy');
        state.pending = false;
      }
    }
  }

  function drawDesk(state) {
    const {packs} = state.data;
    const query = state.search.value.trim().toLowerCase();
    const counts = {all:packs.length, live:0, draft:0, closed:0};
    const stage = pack => ['live','draft'].includes(pack.status) ? pack.status : 'closed';
    packs.forEach(pack => counts[stage(pack)]++);
    for (const item of state.filters) {
      const key = item.dataset.packFilter;
      const label = {all:'All packs',live:'Open',draft:'Drafts',closed:'History'}[key];
      const title = label+' · '+counts[key];
      if (item.textContent !== title) item.textContent = title;
      item.setAttribute('aria-pressed', String(state.filter === key));
    }
    const activePack = packs.find(pack => pack.status === 'live');
    const headline = activePack ? 'Open on phones · '+activePack.title :
      'Ready to send when the presenter gives the cue.';
    const roomNote = state.data.hall_blocked_reason;
    const activity = roomNote ? headline+' '+roomNote : headline;
    if (state.activity.textContent !== activity) state.activity.textContent = activity;
    const visible = packs.filter(pack => (state.filter === 'all' || stage(pack) === state.filter) &&
      [pack.title, ...pack.questions.map(question => question.prompt)].some(value => value.toLowerCase().includes(query)));
    const order = ['live', 'draft', 'closed', 'cancelled'];
    const sorted = [...visible].reverse().sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status));
    const selected = sorted.find(pack => pack.id === state.selected) || sorted[0];
    const signature = JSON.stringify([visible.map(({results, ...pack}) => pack), state.data.hall, roomNote, state.filter, query, selected?.id]);
    if (state.signature !== signature) {
      const action = document.activeElement?.dataset.audienceFocus;
      state.cards = new Map();
      if (!selected) {
        state.list.replaceChildren(section(node('p', {className: 'mut'},
          !packs.length ? 'No audience question packs yet.' : 'No packs match this view.')));
      } else {
        const navigate = node('nav', {className:'audience-pack-queue', ariaLabel:'Question pack list'},
          ...sorted.map(pack => {
            const entry = button('', () => { state.selected = pack.id; drawDesk(state); });
            entry.dataset.audienceFocus = 'pack:'+pack.id;
            entry.setAttribute('aria-current', pack.id === selected.id ? 'true' : 'false');
            entry.append(node('strong', {}, pack.title), node('small', {}, STATUSES[pack.status] +
              ' · '+pack.questions.length+(pack.questions.length === 1 ? ' question' : ' questions')));
            return entry;
          }));
        const sidebar = section(node('h2', {}, 'Packs in this view'),navigate);
        const detail = deskPack(state, selected);
        detail.classList.add('audience-pack-detail');
        state.list.replaceChildren(node('div', {className:'audience-pack-workspace'}, sidebar, detail));
      }
      if (action && document.activeElement === document.body) {
        [...state.list.querySelectorAll('[data-audience-focus]')]
          .find(control => control.dataset.audienceFocus === action)?.focus();
      }
      state.signature = signature;
    }
    for (const pack of packs) {
      const card = state.cards?.get(pack.id);
      if (!card || pack.status === 'draft' || pack.status === 'cancelled') continue;
      const signature = JSON.stringify(pack.results);
      if (signature === card.signature) continue;
      card.signature = signature;
      for (const [question, detail] of card.questions) {
        const result = pack.results?.[question.id];
        if (pack.status === 'live') {
          detail.replaceChildren(countLabel(result?.count || 0));
        } else {
          detail.replaceChildren(results(question, result, true));
          if (question.type === 'text' && (result?.count || 0) > (result?.texts?.length || 0)) {
            const show = button('View all written responses', () => showFeedback(state, pack, question, show));
            detail.append(show);
          }
        }
      }
    }
  }

  function showFeedback(state, pack, question, trigger) {
    if (state.feedback) return;
    const pageSize = 50;
    let offset = 0;
    let controller;
    const close = button('Close', () => dialog.close());
    const title = node('h2', {id: 'audience-feedback-title'}, 'Written responses');
    const notice = status('status', 'mut');
    const error = status('alert', 'audience-error');
    const entries = node('ol', {className: 'audience-results'});
    const previous = button('Previous page', () => load(offset - pageSize));
    const next = button('Next page', () => load(offset + pageSize));
    const retry = button('Retry', () => load(offset));
    retry.hidden = true;
    const content = node('div', {className: 'usage-content audience-stack'},
      node('strong', {}, question.prompt), notice, error, entries,
      node('div', {className: 'row'}, previous, next, retry));
    const dialog = node('dialog', {className: 'usage-dialog'},
      node('header', {className: 'row'}, node('div', {className: 'grow'}, title), close), content);
    dialog.setAttribute('aria-labelledby', title.id);
    state.feedback = {abort: () => controller?.abort()};
    dialog.onclose = () => {
      state.feedback.abort();
      state.feedback = null;
      dialog.remove();
      if (trigger.isConnected) trigger.focus();
    };
    state.root.append(dialog);
    dialog.showModal();
    load(0);

    async function load(target) {
      if (controller || target < 0) return;
      controller = new AbortController();
      const timer = setTimeout(() => controller?.abort(), 8000);
      previous.disabled = next.disabled = true;
      retry.hidden = true;
      error.textContent = '';
      notice.textContent = 'Loading written responses…';
      const params = new URLSearchParams({packId: pack.id, questionId: question.id, offset: target, limit: pageSize});
      try {
        const data = await state.get(DESK + '/responses?' + params, controller.signal);
        if (!dialog.open || view !== state) return;
        offset = data.offset;
        entries.start = offset + 1;
        entries.replaceChildren(...data.texts.map(item => node('li', {}, item)));
        notice.textContent = data.texts.length
          ? 'Responses ' + (offset + 1) + '–' + (offset + data.texts.length) + ' of ' + data.total
          : 'No written responses.';
        previous.disabled = offset === 0;
        next.disabled = offset + data.limit >= data.total;
      } catch (problem) {
        if (!dialog.open || view !== state) return;
        entries.replaceChildren();
        notice.textContent = '';
        error.textContent = problem.name === 'AbortError' ? 'Loading written responses timed out.' :
          problem.message || 'Written responses could not be loaded.';
        offset = target;
        retry.hidden = false;
      } finally {
        clearTimeout(timer);
        controller = null;
      }
    }
  }

  function deskPack(state, pack) {
    const questionNodes = new Map();
    const editable = pack.status === 'draft';
    const detail = node('p', {className: 'mut'}, STATUSES[pack.status] +
      (pack.releasedAt ? ' · Results shared' : pack.closesAt && pack.status === 'live' ? ' · Until ' + TIME.format(pack.closesAt) + ' event time' : ''));
    const heading = node('h2', {tabIndex: -1}, pack.title);
    const timer = () => {
      const select = node('select', {}, ...[[0,'Until closed'],[1,'1 minute'],[2,'2 minutes'],[5,'5 minutes'],[10,'10 minutes']]
        .map(([value,label]) => node('option',{value:String(value)},label)));
      select.value = String(state.timers.get(pack.id) || 0);
      select.disabled = state.preview;
      select.addEventListener('change', () => state.timers.set(pack.id,Number(select.value)));
      return field('Answer window',select);
    };
    const duration = () => state.timers.get(pack.id) ? {minutes:state.timers.get(pack.id)} : {};
    state.cards.set(pack.id, {heading, questions: questionNodes, signature: null});
    const head = [heading, detail];
    if (pack.description) head.push(node('p', {}, pack.description));
    const questions = node('ol', {className: 'audience-results'}, ...pack.questions.map(question => {
      const body = node('div', {className: 'mut'});
      questionNodes.set(question, body);
      if (editable) {
        const labels = options(question).map(option => node('li', {}, option.text));
        body.append(TYPES[question.type], ...(labels.length ? [node('ul', {}, ...labels)] : []));
      }
      const controls = node('div', {className:'row'});
      if (['live','closed'].includes(pack.status) && !state.preview) {
        const shown = state.data.hall;
        const onRoom = shown?.packId === pack.id && shown?.questionId === question.id;
        const reason = state.data.hall_blocked_reason;
        if (onRoom) controls.append(button('Hide from room screen', () => act(state, pack, 'hide', 'Room returned to programme.')));
        if (!reason) {
          if (!onRoom || shown.results) controls.append(button('Show on room screen', () =>
            act(state, pack, 'show', 'Question shown on room screen.', {questionId:question.id})));
          if (pack.releasedAt && (!onRoom || !shown.results)) controls.append(button('Show results on room screen', () =>
            act(state, pack, 'show', 'Results shown on room screen.', {questionId:question.id, results:true})));
        } else if (onRoom) controls.append(node('small', {}, reason));
      }
      return node('li', {}, node('strong', {}, question.prompt), body, controls);
    }));
    const card = section(...head);
    card.dataset.packStage = pack.status;
    if (editable) {
      const preview = node('details', {}, node('summary', {}, 'Preview questions'), questions);
      const edit = button('Edit draft', () => editPack(state, pack));
      edit.disabled = state.preview;
      edit.dataset.audienceFocus = pack.id + ':edit';
      const send = button('Send to phones', () => act(state, pack, 'send', 'Questions sent to phones.', duration()), '');
      send.disabled = state.preview || state.data.packs.some(item => item.status === 'live');
      send.dataset.audienceFocus = pack.id + ':send';
      card.append(preview, node('div', {className: 'audience-pack-actions row'}, timer(), edit, send));
      if (send.disabled && !state.preview) card.append(node('p', {className: 'mut'}, 'Close the open pack before sending another.'));
    } else if (pack.status !== 'cancelled') {
      card.append(questions);
    }
    if (['live','closed'].includes(pack.status)) {
      const resend = button(pack.releasedAt ? 'Replay on phones' : 'Resend to phones', () =>
        act(state, pack, 'resend', 'Questions sent to phones.', duration()));
      resend.disabled = state.preview || state.data.packs.some(item => item.id !== pack.id && item.status === 'live');
      if (pack.status === 'closed') card.append(node('details', {}, node('summary', {}, 'Send this pack again'),
        node('div',{className:'audience-pack-actions row'},timer(),resend)));
      else card.append(node('div',{className:'audience-pack-actions row'},timer(),resend));
    }
    if (pack.status === 'live') {
      card.append(confirmAction(state, pack, 'close', 'Stop accepting answers', 'Attendees will no longer be able to answer. Existing responses will be kept.', 'Confirm close', 'Questions closed.'));
    }
    if (pack.status === 'closed' && !pack.releasedAt) {
      card.append(confirmAction(state, pack, 'release', 'Share results to phones', 'Attendees will see choice totals, rating totals and response counts. Written answers stay with the organizers.', 'Confirm sharing', 'Results shared with attendees.'));
    }
    if (pack.status !== 'cancelled' && !pack.releasedAt) {
      const detail = editable ? 'This draft will be cancelled and cannot be sent.' :
        'The pack will be removed from attendee view and its results cannot be shared.';
      card.append(confirmAction(state, pack, 'cancel', editable ? 'Cancel draft' : 'Cancel questions',
        detail, 'Confirm cancellation', 'Questions cancelled.', 'bad'));
    }
    return card;
  }

  function confirmAction(state, pack, action, label, description, confirm, receipt, kind) {
    const open = button(label, () => {
      panel.hidden = false;
      open.hidden = true;
      back.focus();
    }, kind);
    open.disabled = state.preview;
    open.dataset.audienceFocus = pack.id + ':' + action;
    const back = button('Go back', () => {
      panel.hidden = true;
      open.hidden = false;
      open.focus();
    });
    const submit = button(confirm, () => act(state, pack, action, receipt), kind || '');
    const panel = node('div', {hidden: true, className: 'audience-confirm audience-stack'},
      node('p', {}, description), node('div', {className: 'row'}, back, submit));
    return node('div', {}, open, panel);
  }

  function act(state, pack, action, receipt, extra = {}) {
    state.error.textContent = '';
    state.notice.textContent = '';
    write(state, {action, ...(action === 'hide' ? {} : {packId:pack.id}), expectedRev: state.data.rev, ...extra}, () => {
      state.notice.textContent = receipt;
      if (document.activeElement === document.body) state.cards.get(pack.id)?.heading.focus();
    }, message => { state.error.textContent = message; });
  }

  function editPack(state, pack = {title: '', description: '', questions: []}) {
    if (state.pending || state.editor.firstChild) return;
    const baseline = JSON.stringify(pack);
    const title = textField(pack.title, 120, true);
    const description = textField(pack.description, 500, false, true);
    const questions = node('div', {className: 'audience-stack'});
    const error = status('alert', 'audience-error');
    const titleFor = index => 'Question ' + (index + 1);
    const editors = [];
    const add = button('Add question', () => addQuestion({type: 'choice', prompt: ''}, true));
    const discard = button('Cancel editing', () => {
      if (state.pending) return;
      state.editor.replaceChildren();
      [...state.root.querySelectorAll('[data-audience-focus]')].find(item =>
        item.dataset.audienceFocus === (pack.id ? pack.id + ':edit' : 'new'))?.focus();
    });
    const save = node('button', {type: 'submit', className: 'btn'}, 'Save draft');
    const form = node('form', {className: 'audience-stack'}, field('Pack title', title),
      field('Description (optional)', description), questions, add, error, node('div', {className: 'row'}, save, discard));

    function updateQuestions() {
      editors.forEach((editor, index) => {
        editor.legend.textContent = titleFor(index);
        editor.remove.disabled = editors.length === 1;
      });
      add.disabled = editors.length >= 10;
    }

    function addQuestion(question, focus = false) {
      const legend = node('legend');
      const prompt = textField(question.prompt, 500, true, true);
      const type = node('select', {}, ...Object.entries(TYPES).map(([value, label]) => node('option', {value}, label)));
      type.value = question.type;
      const choiceFields = node('div', {className: 'audience-stack', hidden: type.value !== 'choice'});
      const choices = [];
      const addOption = button('Add choice', () => appendChoice({}, true));
      const remove = button('Remove question', () => {
        const index = editors.indexOf(editor);
        editors.splice(index, 1);
        questionField.remove();
        updateQuestions();
        (editors[index] || editors[index - 1]).prompt.focus();
      });
      const questionField = node('fieldset', {className: 'audience-question audience-stack'}, legend,
        field('Prompt', prompt), field('Answer type', type), choiceFields, remove);
      const editor = {legend, prompt, remove, get value() {
        const value = {type: type.value, prompt: prompt.value.trim()};
        if (question.id) value.id = question.id;
        if (type.value === 'choice') {
          value.options = choices.map(choice => ({key: choice.key, text: choice.input.value.trim()}));
        }
        return value;
      }};
      function updateOptions() {
        choices.forEach((choice, index) => {
          choice.label.textContent = 'Choice ' + (index + 1);
          choice.input.disabled = type.value !== 'choice';
          choice.remove.disabled = choices.length === 2;
        });
        addOption.disabled = choices.length >= 8;
        choiceFields.hidden = type.value !== 'choice';
      }
      let next = 0;
      function appendChoice(option, focus = false) {
        let key = option.key;
        if (!key) {
          do { key = 'option-' + (++next); } while (choices.some(choice => choice.key === key));
        }
        const input = textField(option.text || '', 120, true);
        const label = node('span');
        const remove = button('Remove choice', () => {
          const index = choices.indexOf(choice);
          choices.splice(index, 1);
          row.remove();
          updateOptions();
          (choices[index] || choices[index - 1]).input.focus();
        });
        const row = node('div', {className: 'row'}, node('label', {className: 'grow'}, label, input), remove);
        const choice = {key, input, label, remove};
        choices.push(choice);
        choiceFields.insertBefore(row, addOption);
        updateOptions();
        if (focus) input.focus();
      }
      choiceFields.append(addOption);
      for (const option of question.options || [{}, {}]) appendChoice(option);
      type.onchange = updateOptions;
      editors.push(editor);
      questions.append(questionField);
      updateQuestions();
      if (focus) prompt.focus();
    }

    const editorHeading = pack.id ? 'Edit question pack' : 'New question pack';
    const initial = pack.questions.length ? pack.questions : [{type: 'choice', prompt: ''}];
    for (const question of initial) addQuestion(question);
    state.editor.append(section(node('h2', {}, editorHeading), form));
    title.focus();
    form.onsubmit = event => {
      event.preventDefault();
      if (state.pending) return;
      if (pack.id && JSON.stringify(state.data.packs.find(item => item.id === pack.id)) !== baseline) {
        error.textContent = 'This pack changed while you were editing. Copy any changes you want to keep, then exit and reopen the draft.';
        return;
      }
      const questions = editors.map(editor => editor.value);
      const draft = {title: title.value.trim(), description: description.value.trim(), questions};
      if (pack.id) draft.id = pack.id;
      error.textContent = '';
      state.notice.textContent = '';
      write(state, {action: 'save', expectedRev: state.data.rev, pack: draft}, () => {
        state.notice.textContent = 'Draft saved.';
        state.editor.replaceChildren();
        state.list.querySelector('[data-audience-focus="new"]')?.focus();
      }, message => { error.textContent = message; });
    };
  }

  function drawParticipant(state) {
    const {packs} = state.data;
    const current = packs.filter(pack => pack.status === 'live');
    const history = packs.filter(pack => pack.status === 'closed').reverse();
    window.KEV?.messages?.replace('audience', [
      ...current.map(pack=>{
        const next = pack.questions.find(question=>!Object.hasOwn(pack.answers || {},question.id));
        return {id:'audience:'+pack.id,title:next?.prompt || pack.title,label:'Audience question',
          state:next?'open':'answered',auto:!!next,root:next?'audience-questions-player':undefined};
      }),
      ...history.flatMap(pack=>[
        {id:'audience:'+pack.id,title:pack.title,label:'Audience question',
          state:Object.keys(pack.answers || {}).length?'answered':'closed',results:!!pack.releasedAt},
        ...(pack.releasedAt?[{id:'audience-result:'+pack.id,title:pack.title,label:'Audience results',
          results:true,text:'Results are available.'}]:[])
      ])
    ]);
    redraw('liveSignature', state.live, current, []);
    const summary = history.some(pack => pack.releasedAt) ? 'Recent audience question results' : 'Recent audience questions';
    redraw('historySignature', state.history, history, [node('summary', {}, summary)]);
    state.history.hidden = !history.length;
    state.root.hidden = !current.length && !state.error.textContent && (!history.length || !state.history.parentElement?.closest('#audience-questions-player'));
    for (const packId of state.receipts.keys()) {
      if (!packs.some(pack => pack.id === packId)) state.receipts.delete(packId);
    }
    function redraw(key, container, items, heading) {
      const signature = JSON.stringify(items.map(({answers, ...pack}) => pack));
      if (state[key] === signature) return;
      container.replaceChildren(...heading, ...items.map(pack => participantPack(state, pack)));
      state[key] = signature;
    }
    for (const pack of packs) {
      const cards = state.receipts.get(pack.id);
      let advanceFocus = false;
      for (const question of pack.questions) {
        const receipt = cards?.get(question.id);
        if (receipt) advanceFocus = updateReceipt(receipt, question, pack.answers) || advanceFocus;
      }
      cards?.advance?.(pack.answers || {}, advanceFocus);
    }
  }

  function updateReceipt({receipt, editor}, question, answers) {
    if (!Object.hasOwn(answers || {}, question.id)) return;
    const value = answers[question.id];
    const chosen = options(question).find(option => option.key === value);
    const text = value === null ? 'Skipped.' : chosen ? 'Answer saved · ' + chosen.text : 'Answer saved.';
    if (receipt.textContent !== text) receipt.textContent = text;
    if (editor && !editor.hidden) {
      const active = editor.contains(document.activeElement);
      editor.hidden = true;
      return active;
    }
  }

  function participantPack(state, pack) {
    const live = pack.status === 'live';
    const cards = state.receipts;
    cards.set(pack.id, new Map());
    const fields = pack.questions.map((question, index) => {
      const prompt = (index + 1) + '. ' + question.prompt;
      if (!live) {
        const receipt = status('status', 'audience-receipt');
        cards.get(pack.id).set(question.id, {receipt});
        return node('div', {}, node('strong', {}, prompt), receipt,
          ...(pack.releasedAt ? [results(question, pack.results?.[question.id])] : []));
      }
      return answerForm(state, pack, question, prompt, cards.get(pack.id));
    });
    const title = node('h2', {}, pack.title);
    const badge = node('i', {className:'quiz-topic-mark'});
    badge.setAttribute('aria-hidden','true');
    const label = live ? node('div', {className:'quiz-question-top'},
      node('span', {className:'quiz-topic'}, badge, 'Audience question'),
      node('span', {className:'quiz-point-value'}, 'No points')) :
      node('p', {className:'participant-question-meta'}, 'Audience question · No points');
    const timing = node('p', {className: 'mut'}, live
      ? pack.closesAt ? 'Open until ' + TIME.format(pack.closesAt) + ' event time' : ''
      : pack.releasedAt ? 'Results shared by the organizer' : 'Closed');
    const questions = live ? [questionSteps(fields, pack, cards.get(pack.id))] : fields;
    const card = node('section', {className:'card event-section participant-question-card audience-stack'+(live?' quiz-question':'')},

      label, title, ...(pack.description ? [node('p', {}, pack.description)] : []),
      ...(timing.textContent ? [timing] : []), ...questions);
    if (live) card.dataset.quizTheme = 'audience';
    return card;
  }

  function questionSteps(forms, pack, receipts) {
    const progress = node('p', {className: 'small mut'});
    const saved = status('status', 'participant-question-receipt');
    saved.tabIndex = -1;
    receipts.advance = (answers, focus = false) => {
      const index = pack.questions.findIndex(question => !Object.hasOwn(answers, question.id));
      progress.textContent = index >= 0 && forms.length > 1 ? 'Question ' + (index + 1) + ' of ' + forms.length : '';
      forms.forEach((form, i) => { form.hidden = i !== index; });
      saved.textContent = Object.keys(answers).length ? 'Answer saved.' : '';
      if (focus) (index >= 0 ? forms[index].querySelector('legend') : saved).focus({preventScroll:true});
    };
    return node('div', {className: 'audience-stack'}, saved, progress, ...forms);
  }

  function answerForm(state, pack, question, prompt, receipts) {
    const saved = pack.answers?.[question.id];
    const form = node('form');
    form.dataset.openQuestion = '';
    const receipt = status('status', 'audience-receipt');
    receipt.tabIndex = -1;
    const error = status('alert', 'audience-error');
    const submit = node('button', {type: 'submit', className: 'btn quiz-submit'}, 'Submit answer');
    let input;
    let controls;
    if (question.type === 'text') {
      input = textField(saved ?? '', 1000, true, true);
      controls = node('div', {className: 'audience-stack'}, field('Your response', input),
        node('p', {className: 'small mut'}, 'Organizers can read written responses.'));
    } else {
      const choices = options(question).map(({key, text}, index) => {
        const mark = node('span', {className:'quiz-choice-key'}, question.type==='rating' ? String(key) : String.fromCharCode(65+index));
        mark.setAttribute('aria-hidden','true');
        return node('label', {className:'quiz-choice'},
          node('input', {type:'radio', name:'answer', value:key, checked:saved===key, required:true}),
          mark, node('span', {className:question.type==='rating'?'participant-sr-only':''}, text));
      });
      controls = node('div', {className: 'quiz-choices'}, ...choices);
    }
    function save(value) {
      error.textContent = '';
      state.error.textContent = '';
      write(state, {packId: pack.id, questionId: question.id, value}, () => {}, message => {
        state.error.textContent = message;
      });
    }
    form.onsubmit = event => {
      event.preventDefault();
      const value = input ? input.value.trim() : new FormData(form).get('answer');
      if (!value) {
        error.textContent = 'Enter or select an answer, or skip this question.';
        return;
      }
      save(question.type === 'rating' ? Number(value) : value);
    };
    const editor = node('div', {className: 'audience-stack'}, controls,
      node('div', {className: 'row'}, submit, button('Skip question', () => save(null))), error);
    receipts.set(question.id, {receipt, editor});
    form.append(node('fieldset', {className: 'audience-question audience-stack', disabled: state.preview},
      node('legend', {tabIndex: -1}, prompt), editor, receipt));
    return form;
  }

  window.AudienceQuestions = {desk, participant, pause, clear};
})();
