const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { JSDOM } = require('jsdom');

test('participant sees an invitation and controls the browser permission prompt', async () => {
  const dom = new JSDOM(readFileSync('static/participant.html', 'utf8'), {
    url:'https://example.test/participant', runScripts:'outside-only'
  });
  const w = dom.window;
  const calls = [];
  w.PushManager = class {};
  w.Notification = {
    permission:'default',
    requestPermission:async () => { calls.push('permission'); return 'granted'; }
  };
  const subscription = {toJSON: () => ({endpoint:'synthetic'}), unsubscribe: async () => true};
  const registration = {pushManager: {
    getSubscription:async () => null,
    subscribe: async () => subscription
  }};
  Object.defineProperty(w.navigator, 'serviceWorker', {value: {
    getRegistration:async () => registration,
    register:async () => registration,
    ready:Promise.resolve(registration)
  }});
  w.KEV = {api:async (_, options) => {
    calls.push(options?.body?.action || 'config');
    return {available:true, public_key:'AQ'};
  }};
  w.eval(readFileSync('static/participant-push.js','utf8'));
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await settle();
  const invitation = w.document.querySelector('#notification-invitation');
  assert.equal(invitation.hidden, false);
  assert.deepEqual(calls, ['config']);
  w.document.querySelector('#notification-invitation-enable').click();
  await settle();
  assert.deepEqual(calls, ['config','permission','subscribe']);
  assert.equal(invitation.hidden, true);
  assert.equal(w.document.querySelector('#disable-notifications').hidden, false);
  w.close();
});

test('agenda and help alerts open the relevant participant view on warm and cold starts', async () => {
  const vm = require('node:vm');
  for (const [kind, destination] of [['agenda','event-agenda'], ['help','help-reply'], ['question','messages']]) {
    for (const warm of [false,true]) {
      const events = {};
      let notification, opened, posted, pending;
      const self = {
        location:{origin:'https://example.test'},
        addEventListener:(event, listener) => { events[event]=listener; },
        registration:{showNotification:async (_, opts) => { notification=opts; }}
      };
      const active = { url:'https://example.test/participant',
        postMessage:value => { posted=value.type === 'kevenos:open-messages' ? 'messages' : value.destination; },
        focus:async () => {} };
      const clients = {
        matchAll:async () => warm ? [active] : [],
        openWindow:async url => { opened=url; }
      };
      vm.runInNewContext(readFileSync('static/participant-sw.js','utf8'), {self,clients,URL});
      const waitUntil = promise => { pending=promise; };
      events.push({data:{json:() => ({kind})},waitUntil});
      await pending;
      events.notificationclick({notification:{data:notification.data,close(){}},waitUntil});
      await pending;
      assert.equal(warm ? posted : opened, warm ? destination : '/participant#'+destination);
    }
  }
});
