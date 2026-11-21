(() => {
  const byId = id => document.getElementById(id);
  const button = byId('enable-notifications');
  const disable = byId('disable-notifications');
  const testButton = byId('test-notifications');
  const status = byId('notification-status');
  const invitation = byId('notification-invitation');
  const inviteButton = byId('notification-invitation-enable');
  if (!button || !invitation) return;

  const path = '/api/participant/push';
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  const installation = 'On an iPhone or iPad, use Share → Add to Home Screen, then open KevenOS from its icon.';
  const dismissal = 'kevenos-notification-invitation-dismissed-v1';
  let config;
  let busy = false;

  function message(value) {
    status.textContent = value;
    byId('notification-invitation-status').textContent = value;
  }
  function render(enabled) {
    button.hidden = enabled || !supported || !config?.available;
    disable.hidden = !enabled;
    testButton.hidden = !enabled || !config?.test_available;
    invitation.hidden = enabled || sessionStorage.getItem(dismissal) === 'yes';
    if (enabled) message('Alerts are on for this device.');
  }
  async function subscription() {
    const registration = await navigator.serviceWorker.getRegistration('/');
    return registration?.pushManager.getSubscription();
  }
  function update(action, value) {
    return KEV.api(path, {body: {action, subscription: value.toJSON()}});
  }
  async function enable() {
    if (busy) return;
    busy = button.disabled = inviteButton.disabled = true;
    try {
      if (await Notification.requestPermission() !== 'granted') {
        throw new Error('Allow notifications in your browser or device settings to receive alerts.');
      }
      const registration = await navigator.serviceWorker.register('/participant-sw.js', {scope: '/'});
      await navigator.serviceWorker.ready;
      const key = config.public_key.replace(/-/g, '+').replace(/_/g, '/');
      const raw = atob(key + '='.repeat((4 - key.length % 4) % 4));
      const publicKey = Uint8Array.from(raw, character => character.charCodeAt(0));
      const value = await registration.pushManager.getSubscription() ||
        await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: publicKey});
      await update('subscribe', value);
      render(true);
    } catch (error) {
      message(error.message || 'Could not enable notifications. Try again.');
    } finally {
      busy = button.disabled = inviteButton.disabled = false;
    }
  }
  button.addEventListener('click', enable);
  inviteButton.addEventListener('click', enable);
  byId('notification-later').addEventListener('click', () => {
    sessionStorage.setItem(dismissal, 'yes');
    invitation.hidden = true;
  });
  testButton.addEventListener('click', async () => {
    testButton.disabled = true;
    try {
      const value = await subscription();
      if (!value) throw new Error('Enable alerts on this device first.');
      const result = await update('test', value);
      message('Close KevenOS or lock your phone now. The test sends in '+result.delay_seconds+' seconds.');
    } catch (error) {
      message(error.message || 'Could not schedule the test alert.');
    } finally {
      testButton.disabled = false;
    }
  });
  disable.addEventListener('click', async () => {
    disable.disabled = true;
    try {
      const value = await subscription();
      if (value) {
        await update('unsubscribe', value);
        await value.unsubscribe();
      }
      sessionStorage.setItem(dismissal, 'yes');
      render(false);
      message('Alerts are off for this device.');
    } catch (error) {
      message(error.message || 'Could not turn off notifications. Try again.');
    } finally {
      disable.disabled = false;
    }
  });
  (async () => {
    try {
      config = await KEV.api(path);
      if (!config.available) {
        message('Device alerts are currently unavailable. Check Today for event updates.');
        return;
      }
      if (!supported) {
        message(ios ? installation : 'This browser does not support device alerts. Check Today for updates.');
        if (ios) {
          inviteButton.hidden = true;
          render(false);
        }
        return;
      }
      const value = await subscription();
      const state = value && await update('status', value);
      render(!!state?.subscribed && Notification.permission === 'granted');
      if (Notification.permission === 'denied') {
        message('Enable notifications in your browser or device settings to receive alerts.');
      }
    } catch (_) {
      message('Check Today for event updates.');
    }
  })();
})();
