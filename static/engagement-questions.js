(function () {
  'use strict';
  const path = '/api/engagement/questions';
  let root, api, generation = 0, reading = false, sending = false, pending;
  const node = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
  function clear() { generation++; reading = false; sending = false; pending = null; if (root) { root.replaceChildren(); root.hidden = true; } root = null; }
  function makeId() { const bytes = new Uint8Array(16); crypto.getRandomValues(bytes); return [...bytes].map(b => b.toString(16).padStart(2, '0')).join(''); }
  function create() {
    const details = node('details'); details.className = 'card event-section engagement-questions';
    details.append(node('summary', 'Questions for the event team'));
    details.append(node('p', 'Questions are reviewed by the event team before everyone can see them. Avoid sharing private information.'));
    const form = node('form');
    const label = node('label', 'Your question'); const input = node('textarea');
    input.maxLength = 500; input.rows = 3; input.required = true; label.append(input); form.append(label);
    const submit = node('button', 'Send question'); submit.type = 'submit'; submit.className = 'btn'; form.append(submit);
    const notice = node('p'); notice.setAttribute('role', 'status'); form.append(notice);
    const own = node('section'); own.dataset.own = ''; const approved = node('section'); approved.dataset.approved = '';
    details.append(form, own, approved); root.replaceChildren(details);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (sending) return;
      const text = input.value.trim();
      if (!text) { notice.textContent = 'Enter your question first.'; input.focus(); return; }
      if (pending && pending.text !== text) { notice.textContent = 'Retry the original question before sending a different one.'; return; }
      pending ||= {text, request_id:makeId()};
      const stamp = generation; sending = true; submit.disabled = true; notice.textContent = 'Sending…';
      try {
        await api(path, {body:pending}); if (stamp !== generation) return;
        pending = null; input.value = ''; notice.textContent = 'Question received. Check its status below.';
      } catch (error) {
        if (stamp !== generation) return;
        if (error?.status >= 400 && error?.status < 500) pending = null;
        notice.textContent = error?.message || 'Connection interrupted. Retry the same question.';
      } finally { if (stamp === generation) { sending = false; submit.disabled = false; } }
      if (stamp === generation) refresh();
    });
  }
  function show(data) {
    if (!root.firstElementChild) create();
    root.hidden = !data.enabled && !data.questions.length && !data.own.length;
    const form = root.querySelector('form'); form.hidden = !data.enabled;
    const own = root.querySelector('[data-own]'); own.replaceChildren();
    if (data.own.length) {
      own.append(node('h3', 'Your questions'));
      const list = node('ul');
      data.own.forEach(item => { const li = node('li', `${item.text} — ${item.status === 'pending' ? 'Awaiting review' : item.status === 'rejected' ? 'Not published' : item.status === 'answered' ? 'Answered' : 'Published'}`); if (item.answer) li.append(node('p', item.answer)); list.append(li); });
      own.append(list);
    }
    const approved = root.querySelector('[data-approved]'); approved.replaceChildren();
    if (data.questions.length) {
      approved.append(node('h3', 'Questions shared by the event team'));
      const list = node('ul');
      data.questions.forEach(item => {
        const li = node('li', item.text); if (item.answer) li.append(node('p', item.answer));
        const vote = node('button', `${item.upvoted ? 'Remove support' : 'Support'} · ${item.votes || 0} ${(item.votes || 0) === 1 ? 'support' : 'supports'}`);
        vote.type = 'button'; vote.className = 'btn ghost'; vote.disabled = !data.enabled;
        vote.setAttribute('aria-pressed', String(!!item.upvoted));
        vote.addEventListener('click', async () => {
          if (sending) return;
          const stamp = generation; sending = true; vote.disabled = true;
          try { await api(path, {body:{question_id:item.id, upvote:!item.upvoted}}); }
          catch (error) { if (stamp === generation) root.querySelector('[role=status]').textContent = error?.message || 'Could not update support.'; }
          finally { if (stamp === generation) { sending = false; vote.disabled = false; refresh(); } }
        });
        li.append(vote); list.append(li);
      });
      approved.append(list);
    }
  }
  async function refresh() {
    if (!root || reading || sending || document.hidden) return;
    const stamp = generation; reading = true;
    try { const data = await api(path); if (stamp === generation) show(data); }
    catch (error) { if (stamp === generation && error?.status === 404) clear(); }
    finally { if (stamp === generation) reading = false; }
  }
  async function mount(target, request) {
    if (root !== target) { clear(); root = target; api = request; }
    await refresh();
  }
  window.EngagementQuestions = {mount, clear};
})();
