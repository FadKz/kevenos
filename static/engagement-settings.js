/* Admin-only event availability; the deployment feature flag is separate. */
(function () {
  'use strict';
  const path = '/api/admin/engagement-settings';
  const node = (tag, text) => { const item = document.createElement(tag); if (text != null) item.textContent = text; return item; };
  function requestId() {
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    return [...bytes].map(value => value.toString(16).padStart(2,'0')).join('');
  }
  async function mount(root, read, write) {
    let state = await read(path), pending = null, busy = false;
    if (!root.isConnected) return;
    function draw(message = '') {
      root.replaceChildren(node('h2','Quiz availability'));
      const enabled = state.quiz_enabled !== false;
      root.append(node('p', enabled ? 'Quizzes are enabled for this event.' : 'Quizzes are disabled for this event.'));
      root.append(node('p','Polls and audience questions remain available. Close any active quiz question before disabling.'));
      const button = node('button', enabled ? 'Disable quizzes' : 'Enable quizzes'); button.type = 'button'; button.className = 'btn';
      const refresh = node('button', 'Refresh status'); refresh.type = 'button'; refresh.className = 'btn ghost';
      const notice = node('p',message); notice.setAttribute('role','status');
      root.append(button,refresh,notice);
      refresh.addEventListener('click', async () => {
        if (busy || pending) { notice.textContent = 'Retry the pending action before refreshing.'; return; }
        busy = true; refresh.disabled = true;
        try { const result = await read(path); if (root.isConnected) { state = result; draw('Status refreshed.'); } }
        catch (error) { if (root.isConnected) notice.textContent = error?.message || 'Could not refresh status.'; }
        finally { busy = false; if (refresh.isConnected) refresh.disabled = false; }
      });
      button.addEventListener('click', async () => {
        if (busy) return;
        if (!pending && !confirm(enabled ? 'Disable new quiz launches for this event?' : 'Enable quiz launches for this event?')) return;
        pending ||= {action:'set_quiz_enabled',payload:{enabled:!enabled},revision:state.revision,request_id:requestId()};
        busy = true; button.disabled = true; notice.textContent = 'Saving…';
        try {
          const result = await write(path,pending);
          if (!root.isConnected) return;
          state = result; pending = null; draw('Saved.');
        } catch (error) {
          if (!root.isConnected) return;
          if (error?.status >= 400 && error?.status < 500) pending = null;
          notice.textContent = error?.message || 'Connection interrupted. Retry the same action.';
        } finally { busy = false; if (root.isConnected && button.isConnected) button.disabled = false; }
      });
    }
    draw();
  }
  window.EngagementSettings = {mount};
})();
