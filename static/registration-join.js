(() => {
  const code = location.hash.slice(1);
  const status = document.getElementById('join-status');
  history.replaceState(null, '', '/join');
  if (!/^[A-Za-z0-9_-]{43}$/.test(code)) {
    status.textContent = 'Scan a fresh sign-in QR at the Registration Desk.';
    return;
  }
  fetch('/api/access/registration-link', {
    method: 'POST', cache: 'no-store', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code})
  }).then(async response => {
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.error || 'Ask Registration for a fresh QR.';
      return;
    }
    try {
      sessionStorage.removeItem('kevenos:profile-token');
      sessionStorage.removeItem('kevenos:profile-context');
      if (result.session_changed) localStorage.setItem('kevenos:access-event', Date.now() + ':' + Math.random());
    } catch {}
    const session = await fetch('/api/access/me', {credentials: 'same-origin', cache: 'no-store'});
    if (!session.ok || (await session.json()).role !== 'participant') {
      status.textContent = 'Your browser did not keep the sign-in. Open the link in Safari or Chrome and ask Registration for a fresh QR.';
      return;
    }
    location.replace('/participant');
  }).catch(() => {
    status.textContent = 'Could not reach the event. Reconnect and scan the QR again.';
  });
})();
