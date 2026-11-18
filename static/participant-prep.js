/* The agenda selects when Prep appears. Each participant chooses when to play it. */
window.ParticipantPrep = (() => {
  const card = document.getElementById('pitch-prep-card');
  const worksheetButton = document.getElementById('worksheet-prep-guide');
  const dialog = document.getElementById('pitch-prep-dialog');
  const container = document.getElementById('pitch-prep-player');
  const status = document.getElementById('pitch-prep-status');
  let loadPromise, unmount, opener;

  function loadPlayer() {
    if (!loadPromise) {
      loadPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = '/static/participant-prep-player.js';
        script.onload = () => {
          if (window.PitchPrepPlayer) resolve(window.PitchPrepPlayer);
          else { script.remove(); reject(); }
        };
        script.onerror = () => { script.remove(); reject(); };
        document.head.append(script);
      }).catch(error => { loadPromise = null; throw error; });
    }
    return loadPromise;
  }

  function close(restoreFocus = true) {
    if (!dialog.open) return;
    const previous = opener;
    opener = null;
    dialog.close();
    unmount?.();
    unmount = null;
    if (restoreFocus && previous?.getClientRects().length) previous.focus();
  }

  function openTarget(target) {
    close(false);
    window.ParticipantWorkspace?.openPrepTarget(target);
  }

  async function open(button) {
    opener = button;
    status.textContent = 'Loading the walkthrough…';
    dialog.showModal();
    try {
      const player = await loadPlayer();
      if (dialog.open && !unmount) {
        status.textContent = '';
        unmount = player.mount(container, openTarget);
      }
    } catch {
      if (dialog.open) status.textContent = 'The video is unavailable. Use the quick prep checklist below, or close and try again.';
    }
  }

  document.querySelectorAll('[data-watch-prep]').forEach(button => {
    button.addEventListener('click', () => open(button));
  });
  document.getElementById('pitch-prep-close').onclick = () => close();
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.querySelector('[data-prep-worksheet]').onclick = () => openTarget('worksheet');

  return {
    receive(state) {
      const active = Boolean(state.workspace) && state.program?.scene === 'prep' && state.action?.code === 'program';
      if (!active && !card.hidden && (card.contains(opener) || opener === worksheetButton)) {
        close(false);
        const next = document.querySelector('[data-team-panel]:not([hidden]) h2');
        if (next?.getClientRects().length) {
          next.tabIndex = -1;
          next.focus({preventScroll: true});
        }
      }
      card.hidden = worksheetButton.hidden = !active;
    },
    clear() { close(false); card.hidden = worksheetButton.hidden = true; }
  };
})();
