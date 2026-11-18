"""One disposable event day, through authenticated agent and attendee HTTP routes.

No socket, email, push, production credentials or real attendee data is permitted.
The separate Hermes runtime rehearsal qualifies model/tool choice; this qualifies
what the chosen operations actually expose to participants and the room.
"""
import copy
import os
import unittest
from unittest.mock import patch

import agent_api
import event_commands
import test_agent_api as machine

S = machine.S


class EventDayRehearsal(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket.connect'):
            guard = patch(target, side_effect=AssertionError('Rehearsal attempted outbound networking'))
            guard.start()
            self.addCleanup(guard.stop)
        rates = patch.dict(agent_api.RATE, {}, clear=True)
        rates.start()
        self.addCleanup(rates.stop)
        clock = patch('time.time', return_value=1800000000)
        clock.start()
        self.addCleanup(clock.stop)
        self.f = machine.AgentAPI('test_machine_api_requires_bearer_not_browser_cookie')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        # The scenario registers its own roster and produces its own scoring receipts.
        with patch.object(self.f.ws, '_seed'):
            self.f.ws.switch('rehearsal','REHEARSAL')
        env = patch.dict(os.environ, {'KEVENOS_SECRETS_DIR': S.DATA, 'KEVENOS_ASSISTANT_ENABLED': '0'})
        env.start()
        self.addCleanup(env.stop)
        scopes = set()
        for group in event_commands.catalog().values():
            if isinstance(group, list):
                for entry in group:
                    scope = entry['scope']
                    scopes.update([scope] if isinstance(scope, str) else scope)
        self.f.principal['scopes'] = sorted(scopes)
        self.f.save_grant()
        self.admin = self.login('admin', S.CFG['password'])
        self.viewer = {'X-Key': S.anonymous_view_key()}

    def browser(self, path, body=None, cookie='', headers=None, expected=200):
        headers = dict(headers or {})
        if body is not None:
            headers['X-Event-Context'] = S.WORKSPACE.context
        code, value, _ = machine.access.request('POST' if body is not None else 'GET', path,
                                               body, cookie=cookie, headers=headers)
        self.assertEqual(code, expected, (path, value))
        return value

    def login(self, role, password):
        code, result, headers = machine.access.request('POST', '/api/access/login',
                                                       {'role': role, 'password': password})
        self.assertEqual(code, 200, result)
        return next(v.split(';')[0] for v in headers['set-cookie'] if v.startswith('pp_access='))

    def account(self, role, **fields):
        issued = self.browser('/api/admin/credentials',
                              {'action': 'create', 'role': role, 'label': 'Synthetic rehearsal', **fields},
                              cookie=self.admin)
        return self.login(role, issued['password'])

    def read(self, resource, **arguments):
        code, result = self.f.req('read', {'resource': resource, 'arguments': arguments})
        self.assertEqual(code, 200, result)
        return result['data']

    def execute(self, name, expected=200, **arguments):
        body = self.f.prepared(name, arguments)
        code, result = self.f.req('execute', body)
        self.assertEqual(code, expected, (name, result))
        return result.get('data', result)

    def public(self):
        return self.browser('/api/public', headers=self.viewer)

    def test_event_day_from_registration_to_published_final(self):
        self.execute('event.configure', participant_count=6, team_size=2, judge_count=2)
        self.execute('roster.replace', rows=[{'pair': pair, 'partner': 'Synthetic Company',
                     'members': [{'id': member, 'name': f'Synthetic {pair}-{member}', 'coach': False}
                                 for member in (1, 2)]} for pair in (1, 2, 3)])
        participant = self.account('participant', pair=1, member=1)
        judge = self.account('judge', station=1)
        self.assertTrue(self.execute('attendees.update_participant', pair=1, member=1,
                                     checked_in=True)['checked_in'])
        original_cases = {pair: copy.deepcopy(S.scenario_for(pair)) for pair in (1, 2, 3)}
        before = self.read('assignments.list')['assignments']
        self.execute('assignments.swap', pairs=[1, 2])
        after = self.read('assignments.list')['assignments']
        self.assertEqual(after[0]['station'], before[1]['station'])
        self.assertEqual(after[0]['scenario'], before[0]['scenario'])

        # A running judging timer must survive programme edits and their reversal.
        self.execute('event.mode', mode='desk')
        self.execute('event.clock', action='start')
        agenda = self.read('schedule.items')['items']
        index = next(i for i, item in enumerate(agenda) if item['title'] == 'Solution at Work')
        mac = agenda[index]
        self.execute('program.select', action='set', id=mac['id'])
        clock = copy.deepcopy(S.kv_get('clock'))
        for duration in (mac['min'] + 10, mac['min']):
            args = {'change': {'id': mac['id'], 'duration_minutes': duration}, 'shift_following': True}
            plan = self.read('schedule.retime_preview', **args)
            self.assertTrue(plan['can_apply'], plan)
            self.execute('schedule.retime', **args, plan_digest=plan['plan_digest'])
            self.assertEqual(S.kv_get('clock'), clock)
            self.assertEqual(self.read('program.state')['active_item_id'], mac['id'])
            updated = self.read('schedule.items')['items']
            self.assertEqual(updated[:index], agenda[:index])
            self.assertEqual([x['id'] for x in updated], [x['id'] for x in agenda])
            if duration != mac['min']:
                def minute(value):
                    h, m = map(int, value.split(':'))
                    return 60 * h + m
                self.assertEqual([minute(x['time']) - minute(y['time'])
                                  for x, y in zip(updated[index + 1:], agenda[index + 1:])],
                                 [10] * (len(agenda) - index - 1))
        self.assertEqual(self.read('schedule.items')['items'], agenda)
        self.execute('event.clock', action='pause')

        self.execute('display.mode', mode='live')
        for scene in S.SCENES:
            self.execute('program.scene', scene=scene)
            view = self.public()
            self.assertEqual((view['scene_override'], view['scene']), (scene, scene))
        self.execute('program.scene', scene='auto')
        stale = self.f.prepared('program.announce', {'text': 'Stale rehearsal message'})
        body = self.f.prepared('program.announce', {'text': 'Please return to your tables.'})
        code, sent = self.f.req('execute', body)
        self.assertEqual(code, 200, sent)
        code, retry = self.f.req('execute', body)
        self.assertEqual(code, 200, retry)
        self.assertTrue(retry['replayed'])
        code, conflict = self.f.req('execute', stale)
        self.assertEqual((code, conflict['error']['code']), (409, 'revision_changed'))
        self.assertEqual(self.public()['program']['announce'], body['arguments']['text'])
        mobile = self.browser('/api/participant/state', cookie=participant)
        self.assertEqual(mobile['announcement']['text'], body['arguments']['text'])
        self.execute('display.mode', mode='off')
        self.assertTrue(self.public()['off'])
        self.assertEqual(self.public()['rows'], [])
        self.execute('program.announce', text='')
        self.assertIsNone(self.browser('/api/participant/state', cookie=participant)['announcement'])
        self.execute('display.mode', mode='live')

        # One scored question, then one unscored sentiment pack, using the same phone.
        self.execute('program.select', action='set', id=agenda[1]['id'])
        self.execute('quiz.control', action='settings', enabled=True)
        quiz = '/api/participant/quiz'
        self.browser(quiz, {'action': 'join'}, cookie=participant)
        run, = self.execute('quiz.control', action='send', qid='W2')['new_run_ids']
        self.assertIsNone(self.public()['quiz'], 'Sending a quiz to phones must leave the room on the programme.')
        self.execute('quiz.control', action='show', id=run)
        self.assertEqual(self.public()['room']['source'], 'quiz')
        question, = self.browser(quiz, cookie=participant)['questions']
        self.assertNotIn('correct', question)
        answer = {'action': 'answer', 'id': run, 'key': 'B'}
        self.browser(quiz, answer, cookie=participant)
        self.browser(quiz, answer, cookie=participant)
        self.browser(quiz, {**answer, 'key': 'A'}, cookie=participant, expected=409)
        self.assertEqual(self.browser(quiz, cookie=participant)['score']['earned'], 0)
        self.execute('quiz.control', action='close', id=run)
        scored = self.browser(quiz, cookie=participant)['score']
        self.assertEqual(scored['earned'], 1)
        self.assertEqual(self.browser(quiz, cookie=participant)['history'][0]['result_state'], 'ready')
        pack = self.execute('audience.control', action='save', pack={'title': 'Rehearsal sentiment',
                  'questions': [{'type': 'rating', 'prompt': 'How useful was this session?'}]})['pack']
        self.execute('program.return_to_agenda')
        self.assertIsNone(self.public()['quiz'])
        self.execute('audience.control', action='send', packId=pack['id'])
        self.assertIsNone(self.public()['audience'])
        self.execute('audience.control', action='show', packId=pack['id'], questionId=pack['questions'][0]['id'])
        self.assertEqual(self.public()['room']['source'], 'audience')
        self.assertNotIn('counts', self.public()['audience'])
        self.browser('/api/participant/audience', {'packId': pack['id'],
                     'questionId': pack['questions'][0]['id'], 'value': 5}, cookie=participant)
        self.assertNotIn('results', self.browser('/api/participant/audience', cookie=participant)['packs'][0])
        self.execute('audience.control', action='close', packId=pack['id'])
        self.execute('audience.control', action='release', packId=pack['id'])
        released = self.browser('/api/participant/audience', cookie=participant)['packs'][0]
        self.assertEqual(released['results'][pack['questions'][0]['id']]['counts']['5'], 1)
        self.assertNotIn('counts', self.public()['audience'])
        self.execute('audience.control', action='show', packId=pack['id'], questionId=pack['questions'][0]['id'], results=True)
        self.assertEqual(self.public()['audience']['counts']['5'], 1)
        self.execute('program.announce', text='Room only test', channel='room')
        self.assertEqual(self.public()['program']['announce'], 'Room only test')
        self.assertIsNone(self.browser('/api/participant/state', cookie=participant)['announcement'])
        self.execute('program.announce', text='Phone only test', channel='phones', important=True)
        self.assertEqual(self.public()['program']['announce'], 'Room only test')
        self.assertEqual(self.browser('/api/participant/state', cookie=participant)['announcement']['text'], 'Phone only test')
        self.assertEqual(self.browser(quiz, cookie=participant)['score'], scored)

        # Final keeps original scenarios, requires panel receipts, then freezes results.
        self.execute('final.setup', pairs=[1, 2, 3], panel=[1])
        self.execute('display.reveal', expected=409, mode='winners')
        missing = self.execute('final.start', expected=409)['error']
        self.assertEqual(missing['code'], 'preliminary_sheets_missing')
        final = self.execute('final.start', override_reason='Rehearsal deliberately exercises a missing-sheet exception.',
                             review_token=missing['review_token'])['state']
        final_run = final['run_id']
        self.assertIsNone(self.public()['audience'])
        self.assertEqual(self.public()['room']['source'], 'final')
        self.execute('audience.control', expected=409, action='show', packId=pack['id'], questionId=pack['questions'][0]['id'])
        self.execute('final.publish', expected=409)
        for pair in (1, 2, 3):
            state = self.browser('/api/state', cookie=judge)
            self.assertEqual(state['assignment']['pair'], pair)
            self.assertEqual(state['scenario_snapshot'], original_cases[pair])
            self.assertNotIn('podium', self.public())
            self.execute('event.clock', action='finish')
            if pair == 1:
                self.execute('final.advance', expected=409, action='next')
            sheet = {'kind': 'final', 'pair': pair, 'run_id': final_run,
                     'ticks': [1] * 5 + [0] * 5, 'ai': 0, 'spoke': 1,
                     'feedback': 'Synthetic feedback', 'notes': 'Private synthetic evidence',
                     'final_rating': 6 - pair}
            self.browser('/api/submit', sheet, cookie=judge)
            if pair < 3:
                self.execute('final.advance', action='next')
        self.assertTrue(self.execute('final.review')['state']['can_publish'])
        self.assertTrue(self.execute('final.publish')['state']['published'])
        for endpoint in ('/api/public', '/api/board'):
            view = self.browser(endpoint, headers=self.viewer)
            self.assertEqual((view['scene'], view['reveal'], view['podium'][0]['pair']),
                             ('awards', 'winners', 1))
            self.assertNotIn('Private synthetic evidence', str(view))
        self.browser('/api/submit', sheet, cookie=judge, expected=409)
        self.execute('program.scene', scene='message')
        self.assertEqual(self.public()['scene'], 'message')
        self.assertEqual(self.read('final.state')['state']['status'], 'completed')
        self.execute('program.scene', scene='auto')
        self.assertEqual(self.public()['scene'], 'awards')

        self.browser('/api/desk/program', {'action':'next'}, cookie=self.admin)
        view = self.public()
        self.assertEqual(view['room']['scene'], 'message')
        self.assertEqual(view['program']['items'][view['program']['index']]['title'], 'Lunch')
        self.assertEqual(view['podium'][0]['pair'], 1)

    def test_desk_clock_takeover_advances_from_visible_cue(self):
        import time
        self.browser('/api/desk/program', {'action':'mode','mode':'clock'}, cookie=self.admin)
        with patch('time.localtime', return_value=time.struct_time((2026,11,17,10,11,0,1,321,-1))):
            self.assertEqual(self.public()['program']['index'], 2)
            self.browser('/api/desk/program', {'action':'next'}, cookie=self.admin)
        view = self.public()
        self.assertEqual(view['program']['index'], 3)
        self.assertEqual(view['program']['mode'], 'manual')
        self.assertEqual(view['room']['source'], 'agenda')
