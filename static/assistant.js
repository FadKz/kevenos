// Event Assistant for signed-in Admin and Organizer profiles.
KEV.assistant = (() => {
  const API = '/api/assistant';
  const PENDING = new Set(['queued', 'submitting', 'running', 'cancel_requested']);
  const PROPOSED = new Set(['pending', 'ready', 'prepared', 'proposed']);
  const STATUS = {
    queued: 'Waiting for the assistant…',
    submitting: 'Connecting to the assistant…',
    running: 'The assistant is working…',
    cancel_requested: 'Cancelling the request…',
    failed: 'The assistant could not finish. You can send another message.',
    cancelled: 'Request cancelled.',
    unknown: 'The outcome is unknown. Check event state before trying an action again.'
  };
  const SUGGESTIONS = [
    'What is happening in the event right now?',
    'Which judges still need to submit?',
    'What should the Desk check before the next round?'
  ];
  let current = null;

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  function button(text, className, click) {
    const node = element('button', className, text);
    node.type = 'button';
    node.addEventListener('click', click);
    return node;
  }

  function label(value) {
    const words = String(value || '').replace(/[_.-]+/g, ' ');
    return words ? words[0].toUpperCase() + words.slice(1) : '';
  }

  function marker() {
    const icon = element('span', 'ppa-mark', '✦');
    icon.setAttribute('aria-hidden', 'true');
    return icon;
  }

  function mount(host, role, onChange) {
    unmount();
    if (!host) return;
    const state = {
      data: null, uncertain: null, epoch: 0, error: '', reset: false, staleProposal: null,
      timer: 0, clockTimer: 0, reading: false, writing: false, digest: '',
      token: KEV.staff.token, revision: KEV.profile.revision
    };
    const launcher = button('Assistant', 'btn ppa-launch', open);
    launcher.prepend(marker());
    launcher.hidden = true;
    launcher.setAttribute('aria-haspopup', 'dialog');
    launcher.setAttribute('aria-controls', 'kevenos-assistant');
    const dialog = element('dialog', 'ppa-dialog');
    dialog.id = 'kevenos-assistant';
    dialog.setAttribute('aria-labelledby', 'ppa-title');
    const panel = element('div', 'ppa-panel');
    const header = element('header', 'ppa-header');
    const heading = element('div', 'ppa-heading');
    const title = element('h2', '', 'Event Assistant');
    title.id = 'ppa-title';
    const scope = element('p', 'ppa-scope');
    heading.append(title, scope);
    const close = button('Close', 'btn ghost sm', () => dialog.close());
    header.append(marker(), heading, close);

    const log = element('div', 'ppa-log');
    log.setAttribute('role', 'log');
    log.setAttribute('aria-label', 'Conversation');
    log.setAttribute('aria-relevant', 'additions text');
    const footer = element('footer', 'ppa-footer');
    const notice = element('p', 'ppa-notice');
    notice.setAttribute('role', 'status');
    const elapsed = element('p', 'ppa-elapsed');
    elapsed.setAttribute('role', 'timer');
    elapsed.setAttribute('aria-live', 'off');
    elapsed.hidden = true;
    const recovery = element('div', 'ppa-recovery');
    const refresh = button('Check again', 'btn alt sm', () => read(true));
    const stop = button('Cancel request', 'btn ghost sm', () => {
      const id = job()?.job_id;
      if (id) change('/cancel', {job_id: id});
    });
    const restart = button('New conversation', 'btn ghost sm', () => {
      state.reset = true;
      render();
      resetButton.focus();
    });
    recovery.append(refresh, stop, restart);
    const reset = element('div', 'ppa-reset');
    reset.append(element('p', '', 'Start a new conversation? Your current conversation will close.'));
    const resetButton = button('Start new', 'btn sm', () => change('/reset', {}));
    const keep = button('Keep conversation', 'btn ghost sm', () => {
      state.reset = false;
      render();
      restart.focus();
    });
    reset.append(resetButton, keep);
    const form = element('form', 'ppa-form');
    const inputLabel = element('label', 'ppa-visually-hidden', 'Ask about the event');
    inputLabel.htmlFor = 'ppa-message';
    const input = element('textarea');
    input.id = 'ppa-message';
    input.rows = 2;
    input.maxLength = 2000;
    input.required = true;
    input.placeholder = 'Ask about the event';
    const send = element('button', 'btn ppa-send', 'Send');
    send.type = 'submit';
    form.append(inputLabel, input, send);
    form.addEventListener('submit', event => {
      event.preventDefault();
      const message = input.value.trim();
      if (message && !send.disabled) change('/message', {message, operation_id: crypto.randomUUID()});
    });
    footer.append(notice, elapsed, recovery, reset, form, element('p', 'ppa-privacy', 'Private to this sign-in. Keep passwords and access keys out of chat.'));
    panel.append(header, log, footer);
    dialog.append(panel);
    dialog.addEventListener('close', () => {
      clearTimeout(state.timer);
      clearTimeout(state.clockTimer);
      state.reset = false;
      if (live()) launcher.focus();
    });
    host.append(launcher, dialog);
    const instance = {dialog, launcher, state, prefill: text => { open(); input.value = text; input.focus(); }};
    current = instance;
    read();
    return instance;

    function live() {
      return current === instance && KEV.profile.revision === state.revision && !KEV.workspaceChanging;
    }

    function messages() {
      return Array.isArray(state.data?.thread?.messages) ? state.data.thread.messages : [];
    }

    function job() {
      const jobs = messages().filter(row => row.job_id).reverse();
      return jobs.find(row => PENDING.has(row.status)) || jobs[0];
    }

    function proposalCard(proposal) {
      const id = proposal.id;
      const status = state.staleProposal === id ? 'stale' : proposal.status;
      const card = element('section', 'ppa-proposal');
      card.append(element('h3', '', proposal.title || 'Proposed action'));
      if (proposal.summary) card.append(element('p', '', proposal.summary));
      if (proposal.arguments && Object.keys(proposal.arguments).length) {
        const rows = element('dl', 'ppa-arguments');
        for (const [name, value] of Object.entries(proposal.arguments)) {
          if (name === 'plan_digest') continue;
          const retiming = proposal.command === 'schedule.retime';
          if (retiming && name === 'change') continue;
          const row = element('div', 'ppa-argument');
          const following = retiming && name === 'shift_following';
          const text = following ? (value ? 'Shift with this change' : 'Keep their current times') :
            typeof value === 'object' && value !== null ? JSON.stringify(value, null, 2) : String(value ?? '');
          row.append(element('dt', '', following ? 'Following sections' : label(name)), element('dd', '', text));
          rows.append(row);
        }
        card.append(rows);
      }
      const effects = Array.isArray(proposal.effects) ? proposal.effects : proposal.effects ? [proposal.effects] : [];
      if (effects.length) {
        const list = element('ul', 'ppa-effects');
        for (const effect of effects) list.append(element('li', '', typeof effect === 'string' ? effect : effect.message || effect.description || JSON.stringify(effect)));
        card.append(list);
      }
      if (!PROPOSED.has(status)) {
        card.append(element('p', 'ppa-result', 'Action status: ' + label(status)));
        if (proposal.result?.message) card.append(element('p', 'ppa-result', proposal.result.message));
        if (status === 'stale') card.append(element('p', 'ppa-result', 'Check current event state and ask for a new proposal.'));
      } else if (KEV.profile.isPreview || state.data?.can_apply === false) {
        card.append(element('p', 'ppa-result', 'Preview is read-only. Return to your own sign-in to apply changes.'));
      } else {
        const actions = element('div', 'ppa-actions');
        const apply = button('Apply action', 'btn sm', () => change('/confirm', {proposal_id: id}));
        const cancel = button('Cancel', 'btn ghost sm', () => change('/cancel', {proposal_id: id}));
        apply.dataset.ppaAction = 'confirm:' + id;
        cancel.dataset.ppaAction = 'cancel:' + id;
        actions.append(apply, cancel);
        card.append(actions);
      }
      return card;
    }

    function render() {
      if (!live()) return;
      const data = state.data;
      const active = job();
      const working = PENDING.has(active?.status);
      const enabled = data?.enabled === true;
      const workspace = typeof data?.workspace === 'string' ? data.workspace : data?.workspace?.mode || KEV.workspaceMode;
      launcher.hidden = !enabled && !state.error;
      scope.replaceChildren(element('span', 'pill', KEV.profile.label(data?.role || role)));
      if (workspace) scope.append(element('span', 'pill ppa-workspace', label(workspace)));
      if (KEV.profile.isPreview) scope.append(element('span', 'pill', 'Preview'));
      const started = active?.created_at;
      const seconds = working && typeof started === 'number' && Number.isFinite(started) && started > 0 ?
        Math.max(0, Math.floor(Date.now() / 1000 - started)) : null;
      const waiting = seconds >= 10 && ['queued', 'submitting', 'running'].includes(active?.status) ?
        (active.status === 'queued' ? 'Your request is waiting. You can cancel it.' : 'Preparing your answer. You can cancel while it finishes.') :
        STATUS[active?.status] || '';
      const status = state.error || (state.uncertain ? 'The connection was interrupted. Check the conversation for an update before sending again.' : waiting);
      elapsed.hidden = seconds === null;
      elapsed.textContent = seconds === null ? '' : seconds + 's elapsed';
      if (notice.textContent !== status) notice.textContent = status;
      notice.classList.toggle('ppa-problem', Boolean(state.error || state.uncertain || ['failed', 'unknown'].includes(active?.status)));
      refresh.hidden = !state.error && !state.uncertain;
      stop.hidden = !working || !active.job_id || active.status === 'cancel_requested';
      restart.hidden = !enabled || (!messages().length && !state.uncertain);
      reset.hidden = !state.reset;
      form.hidden = !enabled;
      input.disabled = send.disabled = state.writing || working || Boolean(state.uncertain);
      refresh.disabled = state.reading || state.writing;
      stop.disabled = restart.disabled = resetButton.disabled = keep.disabled = state.writing;
      const rows = messages();
      const suggestions = data?.suggestions?.length ? data.suggestions : SUGGESTIONS;
      const digest = JSON.stringify([rows, suggestions, enabled, KEV.profile.isPreview, data?.can_apply, state.staleProposal]);
      if (state.digest !== digest) {
        const previousFocus = document.activeElement?.dataset?.ppaAction;
        const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 36;
        const oldCount = log.childElementCount;
        state.digest = digest;
        log.replaceChildren();
        if (enabled && !rows.length) {
          const welcome = element('section', 'ppa-welcome');
          welcome.append(element('h3', '', 'What do you need to check?'));
          const chips = element('div', 'ppa-suggestions');
          for (const entry of suggestions) {
            const text = typeof entry === 'string' ? entry : entry?.text || entry?.label;
            if (!text) continue;
            const suggestion = button(text, 'btn ghost sm', () => {
              input.value = text;
              input.focus();
            });
            suggestion.dataset.ppaEntry = 'true';
            chips.append(suggestion);
          }
          welcome.append(chips);
          log.append(welcome);
        }
        for (const row of rows) {
          if (row.text) {
            const own = row.role === 'user';
            const article = element('article', 'ppa-message' + (own ? ' ppa-user' : ''));
            article.append(element('h3', '', own ? 'You' : 'Assistant'), element('p', '', row.text));
            if (!own && row.status === 'completed' && row.source === 'event') {
              article.append(element('small', 'ppa-source', 'From current event data'));
            }
            log.append(article);
          }
          if (row.proposal) log.append(proposalCard(row.proposal));
        }
        if (previousFocus) {
          const next = [...log.querySelectorAll('[data-ppa-action]')].find(node => node.dataset.ppaAction === previousFocus);
          if (next) next.focus();
          else if (dialog.open) input.focus();
        }
        if (atBottom || !oldCount) log.scrollTop = log.scrollHeight;
      }
      for (const action of log.querySelectorAll('[data-ppa-action], [data-ppa-entry]')) {
        action.disabled = state.writing || Boolean(state.uncertain);
      }
      clearTimeout(state.clockTimer);
      if (dialog.open && working && seconds !== null) state.clockTimer = setTimeout(render, 1000);
    }

    function schedule() {
      clearTimeout(state.timer);
      if (live() && dialog.open && (state.uncertain || PENDING.has(job()?.status))) {
        state.timer = setTimeout(() => read(), 1000);
      }
    }

    function accept(response) {
      if (typeof response?.enabled !== 'boolean') throw new Error('The assistant returned an unexpected response.');
      state.data = response;
    }

    function reconciled() {
      const uncertain = state.uncertain;
      if (!uncertain) return;
      if (uncertain.path === '/message') {
        if (messages().some(row => row.operation_id === uncertain.body.operation_id)) {
          state.uncertain = null;
          input.value = '';
        }
      } else if (uncertain.path === '/reset' && state.data?.thread?.id !== uncertain.threadId) {
        state.uncertain = null;
        state.reset = false;
      } else if (uncertain.body.proposal_id) {
        const id = uncertain.body.proposal_id;
        const found = messages().find(row => row.proposal?.id === id)?.proposal;
        if (!found || (found.status && !PROPOSED.has(found.status))) state.uncertain = null;
      } else if (uncertain.path === '/cancel') {
        const cancelled = messages().find(row => row.job_id === uncertain.body.job_id);
        if (cancelled && (cancelled.status === 'cancel_requested' || !PENDING.has(cancelled.status))) state.uncertain = null;
      }
    }

    async function read(manual = false) {
      if (!live() || state.reading || state.writing) return;
      const epoch = state.epoch;
      state.reading = true;
      if (manual) render();
      try {
        const response = await KEV.api(API, null, state.token);
        if (!live() || epoch !== state.epoch) return;
        accept(response);
        state.error = '';
        reconciled();
      } catch (error) {
        if (!live() || epoch !== state.epoch) return;
        if ([401, 403, 404].includes(error.status) && !dialog.open) {
          state.data = {enabled: false};
          state.error = '';
        } else {
          state.error = 'The conversation could not be loaded. Check again to reconnect.';
        }
      } finally {
        if (live()) {
          state.reading = false;
          render();
          schedule();
        }
      }
    }

    async function change(path, body) {
      if (!live() || state.writing || (state.uncertain && path !== '/reset') ||
          (path === '/confirm' && (KEV.profile.isPreview || state.data?.can_apply === false))) return;
      state.epoch++;
      state.writing = true;
      state.error = '';
      const prior = {path, body, threadId: state.data?.thread?.id};
      render();
      let check = false;
      try {
        const response = await KEV.api(API + path, {body}, state.token);
        if (!live()) return;
        accept(response);
        state.uncertain = null;
        if (path === '/message' || path === '/reset') input.value = '';
        if (path === '/reset') state.reset = false;
        if (path === '/confirm' && messages().some(row => row.proposal?.id === body.proposal_id && row.proposal.result?.ok)) onChange?.();
      } catch (error) {
        if (!live()) return;
        if (path === '/confirm' && error.status === 409) {
          state.staleProposal = body.proposal_id;
          state.error = 'The event state changed. Check the event and ask for a new proposal.';
          check = true;
        } else if (!error.status || error.status >= 500) {
          state.uncertain = prior;
          check = true;
        } else {
          state.error = error.message || 'The request was rejected.';
        }
      } finally {
        if (live()) {
          state.writing = false;
          render();
          if (check) read();
          schedule();
        }
      }
    }

    function open() {
      if (!live() || dialog.open) return;
      dialog.showModal();
      render();
      (input.disabled || form.hidden ? close : input).focus();
      read();
    }
  }

  function unmount() {
    if (!current) return;
    const {dialog, launcher, state} = current;
    current = null;
    clearTimeout(state.timer);
    clearTimeout(state.clockTimer);
    if (dialog.open) dialog.close();
    dialog.remove();
    launcher.remove();
  }

  return {mount, unmount, available: () => !!current?.state?.data?.enabled, openWithPrompt: text => { if (current && current.state.revision === KEV.profile.revision && !KEV.workspaceChanging) current.prefill(String(text).slice(0,2000)); }};
})();
