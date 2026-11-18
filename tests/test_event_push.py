"""Participant notification authorization and workspace isolation."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import event_push
import test_workspace_routes as workspace


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


class PushRoutes(unittest.TestCase):
    def setUp(self):
        self.f = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = ec.generate_private_key(ec.SECP256R1())
        der = self.private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        Path(temporary.name, 'KEVENOS_WEB_PUSH_VAPID_PRIVATE_KEY').write_text(b64(der))
        env = patch.dict('os.environ', {'KEVENOS_SECRETS_DIR': temporary.name})
        env.start()
        self.addCleanup(env.stop)
        self.password = self.f.issue('participant', pair=1, member=1)
        self.player = self.f.login(self.password)
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.subscription = {'endpoint': 'https://web.push.apple.com/example',
                             'keys': {'p256dh': b64(public), 'auth': b64(bytes(16))}}

    def test_subscription_is_participant_only_and_workspace_scoped(self):
        route = event_push.PATH
        self.f.get(route, status=401)
        self.f.get(route, status=403, cookie=self.f.admin)
        config = self.f.get(route, cookie=self.player)[0]
        actual = base64.urlsafe_b64decode(config['public_key']+'='*(-len(config['public_key'])%4))
        expected = self.private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.assertEqual(actual, expected)
        self.f.post(route, {'action': 'subscribe', 'subscription': self.subscription}, cookie=self.player)
        self.assertEqual(len(workspace.S.kv_get(event_push.KEY)), 1)
        bad = self.subscription | {'endpoint': 'https://localhost/internal'}
        self.f.post(route, {'action': 'subscribe', 'subscription': bad}, cookie=self.player, status=400)
        self.f.post(route, {'action': 'unsubscribe', 'subscription': self.subscription}, cookie=self.player)
        self.assertEqual(workspace.S.kv_get(event_push.KEY), {})
        self.f.post(route, {'action': 'subscribe', 'subscription': self.subscription}, cookie=self.player)
        self.f.switch('rehearsal')
        self.assertIsNone(workspace.S.kv_get(event_push.KEY))

    def test_android_subscription_accepts_fcm_but_rejects_spoofed_hosts(self):
        route = event_push.PATH
        fcm = self.subscription | {'endpoint': 'https://fcm.googleapis.com/fcm/send/example'}
        self.f.post(route, {'action': 'subscribe', 'subscription': fcm}, cookie=self.player)
        spoof = self.subscription | {'endpoint': 'https://fcm.googleapis.com.attacker.example/send'}
        self.f.post(route, {'action': 'subscribe', 'subscription': spoof}, cookie=self.player, status=400)

    def subscribe(self, cookie=None, endpoint=None):
        value = self.subscription | ({'endpoint': endpoint} if endpoint else {})
        self.f.post(event_push.PATH, {'action': 'subscribe', 'subscription': value}, cookie=cookie or self.player)
        return value

    def test_announcement_delivers_encrypted_to_apple_and_android(self):
        import requests
        self.subscribe()
        self.subscribe(endpoint='https://fcm.googleapis.com/fcm/send/example')
        self.f.post('/api/desk/program', {'action': 'announce', 'text': 'Private event detail'}, cookie=self.f.admin)
        self.f.get(event_push.STAFF_PATH, cookie=self.player, status=403)
        before = self.f.get(event_push.STAFF_PATH, cookie=self.f.admin)[0]
        self.assertEqual(before['events'][0]['counts'], {'queued': 2})
        self.assertNotIn('Private event detail', str(before))
        self.assertNotIn('fcm.googleapis.com', str(before))
        response = requests.Response()
        response.status_code = 201
        response._content = b''
        with patch('requests.Session.post', return_value=response) as send:
            for job in event_push.jobs(workspace.S):
                event_push.deliver_one(workspace.S, *job)
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['headers']['content-encoding'], 'aes128gcm')
            self.assertNotIn(b'Private event detail', call.kwargs['data'])
        after = self.f.get(event_push.STAFF_PATH, cookie=self.f.admin)[0]
        self.assertEqual(after['events'][0]['counts'], {'accepted': 2})

    def test_retries_and_removes_invalid_devices(self):
        self.subscribe()
        event_push.enqueue(workspace.S, 'agenda')
        job, = event_push.jobs(workspace.S)
        with patch('event_push.send', return_value=503):
            event_push.deliver_one(workspace.S, *job)
        self.assertEqual(event_push.report(workspace.S)['events'][0]['counts'], {'queued': 1})
        self.assertEqual(event_push.jobs(workspace.S), [])
        import time
        with patch('event_push.time.time', return_value=time.time() + 3):
            with patch('event_push.send', return_value=410):
                event_push.deliver_one(workspace.S, *job)
        report = event_push.report(workspace.S)
        self.assertEqual(report['events'][0]['counts'], {'invalid': 1})
        self.assertEqual(report['devices'], 0)

    def test_help_reply_targets_current_request_owner_only(self):
        self.f.post('/api/desk/roster', {'rows': [
            {'pair': 1, 'partner': 'Company', 'p1': 'Live Test Person', 'p2': 'Two'}]}, cookie=self.f.admin)
        self.player = self.f.login(self.password)
        two = self.f.login(self.f.issue('participant', pair=1, member=2))
        self.subscribe()
        self.subscribe(two, 'https://fcm.googleapis.com/fcm/send/example')
        result, _ = self.f.post('/api/participant/help', {
            'action':'request_help','category':'arrival','message':'Where should I sit?',
            'request_id':'12345678-1234-4234-8234-123456789abc'}, cookie=self.player)
        state, _ = self.f.get('/api/event-operations', cookie=self.f.admin)
        body = {'action':'help_update', 'id': result['requests'][0]['id'], 'revision':state['revision'],
                'status':'resolved', 'reply':'Please visit the Event Desk.'}
        reply, _ = self.f.post('/api/event-operations', body, cookie=self.f.admin)
        self.f.post('/api/event-operations', body | {'revision': reply['revision']}, cookie=self.f.admin)
        job, = event_push.jobs(workspace.S)
        with patch('event_push.send', return_value=201) as send:
            event_push.deliver_one(workspace.S, *job)
        self.assertEqual(send.call_args.args[0]['endpoint'], self.subscription['endpoint'])
        self.assertEqual(send.call_args.args[1], 'help')

    def test_quiz_send_and_result_follow_real_quiz_actions(self):
        import event_quiz
        self.subscribe()
        def act(action, **fields):
            state, _ = self.f.get(event_quiz.DESK, cookie=self.f.admin)
            return self.f.post(event_quiz.DESK, {'action':action, 'rev':state['rev'], **fields},
                               cookie=self.f.admin)[0]
        act('settings', enabled=True)
        self.f.post(event_quiz.PLAYER, {'action':'join'}, cookie=self.player)
        self.f.post('/api/desk/program', {'action':'set','index':1}, cookie=self.f.admin)
        run, = act('send', qid='W2')['runs']
        self.f.post(event_quiz.PLAYER, {'action':'answer','id':run['id'],'key':'B'}, cookie=self.player)
        act('close', id=run['id'])
        rows = event_push.report(workspace.S)['events']
        self.assertEqual([(r['kind'], r['counts']) for r in rows],
                         [('results', {'queued': 1}), ('question', {'queued': 1})])

    def test_agenda_only_notifies_after_a_real_saved_change(self):
        from copy import deepcopy
        from event_commands import _agenda_items, _save_agenda
        self.subscribe()
        server = workspace.S
        with server.LOCK:
            items = _agenda_items(server.CONTENT)
            _save_agenda(server, deepcopy(items), 'No timing change', 'admin')
            self.assertEqual(event_push.report(server)['events'], [])
            modified = deepcopy(items)
            modified[0]['title'] += ' updated'
            _save_agenda(server, modified, 'Update the agenda', 'admin')
        self.assertEqual(event_push.report(server)['events'][0]['kind'], 'agenda')

    def test_rehearsal_tracks_without_dispatching(self):
        self.f.switch('rehearsal')
        player = self.f.login(self.f.issue('participant', pair=1, member=1))
        self.subscribe(player)
        event_push.enqueue(workspace.S, 'agenda')
        self.assertEqual(event_push.report(workspace.S)['events'][0]['counts'], {'rehearsal': 1})
        self.assertEqual(event_push.jobs(workspace.S), [])
        self.f.switch('live')
        self.assertEqual(event_push.report(workspace.S)['events'], [])

    def test_rehearsal_test_sends_only_to_confirmed_device_after_delay(self):
        import time
        self.f.post(event_push.PATH, {'action':'test','subscription':self.subscription},
                    cookie=self.player, status=409)
        self.f.switch('rehearsal')
        player = self.f.login(self.f.issue('participant', pair=1, member=1))
        apple = self.subscribe(player)
        google = self.subscribe(player, 'https://fcm.googleapis.com/fcm/send/second-device')
        unknown = self.subscription | {'endpoint':'https://web.push.apple.com/unknown'}
        self.f.post(event_push.PATH, {'action':'test','subscription':unknown}, cookie=player, status=403)
        result, _ = self.f.post(event_push.PATH, {'action':'test','subscription':apple}, cookie=player)
        self.assertEqual(result['delay_seconds'], 15)
        self.f.post(event_push.PATH, {'action':'test','subscription':google}, cookie=player, status=429)
        event_push.enqueue(workspace.S, 'audience')
        self.assertEqual(event_push.jobs(workspace.S), [])
        with patch('event_push.time.time', return_value=time.time() + 16):
            job, = event_push.jobs(workspace.S)
            with patch('event_push.send', return_value=201) as send:
                event_push.deliver_one(workspace.S, *job)
        self.assertEqual(send.call_args.args[:2], (apple, 'test'))
        events = event_push.report(workspace.S)['events']
        self.assertEqual(events[0]['counts'], {'rehearsal':2})
        self.assertEqual(events[1]['counts'], {'accepted':1})
        self.assertEqual(events[1]['provider'], 'product vendor')

    def test_web_push_uses_relay_with_encrypted_payload(self):
        import requests
        response = requests.Response()
        response.status_code = 201
        response._content = b''
        with patch.dict('os.environ', {'KEVENOS_PUSH_SOCKET':'/run/push/relay.sock'}):
            with patch('event_push.relay_post', return_value=response) as relay:
                self.assertEqual(event_push.send(self.subscription, 'test', 60), 201)
        socket_path, endpoint, payload, headers, timeout = relay.call_args.args
        self.assertEqual(socket_path, '/run/push/relay.sock')
        self.assertEqual(endpoint, self.subscription['endpoint'])
        self.assertEqual(headers['content-encoding'], 'aes128gcm')
        self.assertNotIn(b'KevenOS', payload)

    def test_armed_rehearsal_quiz_uses_normal_delivery_once_for_selected_device(self):
        import event_quiz
        self.f.switch('rehearsal')
        player = self.f.login(self.f.issue('participant', pair=1, member=1))
        apple = self.subscribe(player)
        self.subscribe(player, 'https://fcm.googleapis.com/fcm/send/example')
        route = event_push.STAFF_PATH
        devices = self.f.get(route, cookie=self.f.admin)[0]['test_devices']
        device = next(item['id'] for item in devices if 'product vendor' in item['label'])
        request = {'action':'arm','device':device,'kind':'question','confirm':'TEST DEVICE'}
        self.f.post(route, request, cookie=player, status=403)
        self.f.post(route, request, cookie=self.f.admin)
        event_push.enqueue(workspace.S,'announcement')
        self.assertEqual(event_push.jobs(workspace.S), [])
        self.assertIsNotNone(self.f.get(route,cookie=self.f.admin)[0]['test_arm'])
        def act(action, **fields):
            state = self.f.get(event_quiz.DESK,cookie=self.f.admin)[0]
            return self.f.post(event_quiz.DESK,{'action':action,'rev':state['rev'],**fields},cookie=self.f.admin)[0]
        act('settings',enabled=True)
        sent = act('send',qid='P01')
        self.assertIsNone(self.f.get(route,cookie=self.f.admin)[0]['test_arm'])
        job, = event_push.jobs(workspace.S)
        with patch('event_push.send',return_value=201) as send:
            event_push.deliver_one(workspace.S,*job)
        self.assertEqual(send.call_args.args[:2],(apple,'question'))
        event = self.f.get(route,cookie=self.f.admin)[0]['events'][0]
        self.assertEqual(event['counts'],{'accepted':1,'rehearsal':1})
        self.assertTrue(event['rehearsal_test'])
        act('resend',id=sent['runs'][0]['id'])
        self.assertEqual(event_push.jobs(workspace.S),[])

    def test_armed_agenda_and_help_use_the_selected_device_and_existing_audience(self):
        self.f.switch('rehearsal')
        first = self.f.login(self.f.issue('participant', pair=1, member=1))
        second = self.f.login(self.f.issue('participant', pair=1, member=2))
        self.subscribe(first)
        android = self.subscribe(second, 'https://fcm.googleapis.com/fcm/send/example')
        route = event_push.STAFF_PATH
        options = self.f.get(route, cookie=self.f.admin)[0]['test_devices']
        selected = next(item['id'] for item in options if 'Google' in item['label'])
        server = workspace.S

        def arm(kind):
            self.f.post(route, {'action':'arm', 'device':selected, 'kind':kind,
                                'confirm':'TEST DEVICE'}, cookie=self.f.admin)

        def deliver(kind):
            job, = event_push.jobs(server)
            with patch('event_push.send', return_value=201) as send:
                event_push.deliver_one(server, *job)
            self.assertEqual(send.call_args.args[:2], (android, kind))
            self.assertIsNone(self.f.get(route, cookie=self.f.admin)[0]['test_arm'])

        arm('agenda')
        from event_commands import _agenda_items, _save_agenda
        items = _agenda_items(server.CONTENT)
        items[0]['title'] += ' updated'
        _save_agenda(server, items, 'Rehearsal update', 'admin')
        deliver('agenda')

        arm('help')
        for index, player in enumerate((first, second), 1):
            value, _ = self.f.post('/api/participant/help', {
                'action':'request_help', 'category':'arrival', 'message':'Where is my station?',
                'request_id':f'12345678-1234-4234-8234-123456789ab{index}'}, cookie=player)
            state, _ = self.f.get('/api/event-operations', cookie=self.f.admin)
            self.f.post('/api/event-operations', {
                'action':'help_update', 'id':value['requests'][0]['id'], 'revision':state['revision'],
                'status':'resolved', 'reply':'Visit the Event Desk.'}, cookie=self.f.admin)
            if index == 1:
                self.assertEqual(event_push.jobs(server), [])
                self.assertIsNotNone(self.f.get(route, cookie=self.f.admin)[0]['test_arm'])
        deliver('help')
