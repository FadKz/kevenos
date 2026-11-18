/* Admin-only cutover UI. The host supplies its normal authenticated JSON API. */
(() => {
  'use strict';
  window.PitchLiveTransition = {
    mount(root, request) {
      let plan = null;
      let busy = false;
      root.classList.add('live-transition');
      root.innerHTML = `<h3>Prepare the real event</h3>
        <p>Keep Staff and judges. Start Live with 40 participant places, teams of 2 and 8 judges. Previous participant passwords will stop working. Applying this reset switches to Live and signs everyone out; sign in again with your Staff password.</p>
        <label>Shared Guest access<select data-guest><option value="revoke">Revoke previous Guest access</option><option value="retain">Keep the current Guest password</option></select></label>
        <button type="button" data-preview>Review the transition</button>
        <div data-status role="status" aria-live="polite"></div><div data-plan></div>`;
      const status = root.querySelector('[data-status]');
      const box = root.querySelector('[data-plan]');
      const policy = root.querySelector('[data-guest]');
      const previewButton = root.querySelector('[data-preview]');
      const text = (tag, value) => { const node = document.createElement(tag); node.textContent = value; return node; };
      const clear = () => { plan = null; box.replaceChildren(); };
      policy.addEventListener('change', clear);
      previewButton.addEventListener('click', async () => {
        if (busy) return;
        busy = true; previewButton.disabled = true; policy.disabled = true; clear(); status.textContent = 'Checking both workspaces and account permissions…';
        try {
          plan = await request('/api/admin/live-transition', {action:'preview', guest_policy:policy.value});
          box.append(text('p', `${plan.retained_staff_accounts} Staff accounts kept · ${plan.retired_participant_accounts} Participant accounts retired · ${plan.live_judge_assignments} Live judge assignments`));
          const list = document.createElement('ul');
          for (const effect of plan.effects) list.append(text('li', effect));
          box.append(list);
          if (!plan.ready) {
            const issues = document.createElement('ul');
            for (const blocker of plan.blockers) issues.append(text('li', blocker));
            box.append(text('strong', 'Resolve these items first:'), issues);
            status.textContent = 'Live is not ready. No changes have been made.';
            return;
          }
          const label = text('label', 'Type PREPARE LIVE to confirm this reset and switch.');
          const confirmation = document.createElement('input'); confirmation.type = 'text'; confirmation.autocomplete = 'off'; confirmation.spellcheck = false;
          label.append(confirmation);
          const apply = text('button', 'Prepare and activate Live'); apply.type = 'button'; apply.disabled = true;
          confirmation.addEventListener('input', () => { apply.disabled = confirmation.value !== 'PREPARE LIVE' || busy; });
          box.append(label, apply);
          const operation = crypto.randomUUID();
          apply.addEventListener('click', async () => {
            if (busy || confirmation.value !== 'PREPARE LIVE') return;
            busy = true; apply.disabled = true; previewButton.disabled = true; policy.disabled = true;
            status.textContent = 'Creating verified backups and preparing Live. Keep this page open…';
            try {
              await request('/api/admin/live-transition', {action:'apply', plan_digest:plan.plan_digest, guest_policy:plan.guest_policy, confirm:confirmation.value, operation_id:operation});
              box.replaceChildren(text('p', 'Live is ready. Staff and judges can sign in with their existing passwords. Add the new roster, then issue new participant access.'));
              status.textContent = 'Transition complete. All old sessions have ended.';
              const login = text('a', 'Sign in again'); login.href = '/'; box.append(login);
            } catch (error) {
              clear(); status.textContent = error.message || 'Could not confirm completion. Sign in again and check the event workspace before retrying.';
              previewButton.disabled = false; policy.disabled = false;
            } finally { busy = false; }
          });
          status.textContent = 'Preview only. No data or accounts have changed.';
        } catch (error) { status.textContent = error.message || 'Could not load the preview.'; }
        finally { busy = false; previewButton.disabled = false; policy.disabled = false; }
      });
    }
  };
})();
