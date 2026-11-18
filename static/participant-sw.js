const destinationFor = kind => kind === 'help' ? 'help-reply' : kind === 'agenda' ? 'event-agenda' : 'messages';

self.addEventListener('push', event => {
  let message = {};
  try { message = event.data ? event.data.json() : {}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(message.title || 'KevenOS', {
    body: message.body || 'The Event Desk has an update.',
    data: {destination: destinationFor(message.kind)},
    tag: 'kevenos-' + (['announcement','question','results','agenda','help','audience','test'].includes(message.kind) ? message.kind : 'announcement')
  }));
});
self.addEventListener('notificationclick', event => {
  const destination = event.notification.data?.destination;
  const target = ['help-reply', 'event-agenda'].includes(destination) ? destination : 'messages';
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await clients.matchAll({type: 'window', includeUncontrolled: true});
    const existing = windows.find(client => {
      const url = new URL(client.url);
      return url.origin === self.location.origin && url.pathname === '/participant';
    });
    if (existing) {
      existing.postMessage(target === 'messages' ? {type:'kevenos:open-messages'} : {type:'kevenos:open-notification', destination:target});
      return existing.focus();
    }
    return clients.openWindow('/participant#' + target);
  })());
});
