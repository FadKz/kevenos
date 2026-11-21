"""Agent domain workflow using a temporary workspace and synthetic delivery."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import server as S
import event_commands as E
import event_mail as M
from jsonschema import Draft202012Validator


class EventCommands(unittest.TestCase):
    def setUp(self):
        previous = {key: getattr(S, key) for key in ('CFG', 'DB', 'DATA', 'CONTENT', 'WORKSPACE', 'AUTH_DB', 'LIVE')}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        S.DATA = temp.name
        S.CFG = S.load_config(str(Path(S.DATA, 'config.json')))
        S.DB = S.open_db(str(Path(S.DATA, 'judging.db')))
        S.CONTENT = S.bundled_content()
        S.LIVE = {}
        S.WORKSPACE = S.event_workspace.initialize(S, Path(S.DATA, 'judging.db'), S.DATA)
        workspace = S.WORKSPACE
        self.addCleanup(lambda: [setattr(S, key, value) for key, value in previous.items()])
        self.addCleanup(workspace.close)
        self.principal = {'id': 'synthetic-agent', 'label': 'Synthetic Agent'}
        address = patch.dict(os.environ, {'KEVENOS_PUBLIC_URL': 'https://event.example.com'})
        address.start()
        self.addCleanup(address.stop)

    def read(self, name, **args):
        schema = E.RESOURCES[name][0]['input_schema']
        Draft202012Validator(schema).validate(args)
        with S.WORKSPACE.read(), S.LOCK:
            return E.read(S, name, args, self.principal)

    def call(self, name, /, key=None, **args):
        schema = E.COMMANDS[name][0]['input_schema']
        Draft202012Validator(schema).validate(args)
        with S.WORKSPACE.write(), S.LOCK:
            return E.execute(S, name, args, dict(self.principal, idempotency_key=key or str(uuid.uuid4())))

    def test_schema_catalog_and_bundled_content(self):
        for entry in E.catalog()['commands'] + E.catalog()['resources']:
            Draft202012Validator.check_schema(entry['input_schema'])
        for section in E.SECTIONS:
            schema = E.COMMANDS['content.update_section'][0]['input_schema']
            self.assertTrue(Draft202012Validator(schema).is_valid({'section': section, 'value': S.CONTENT[section]}), section)
        tests = [
            ('event.clock', {'action': 'seek', 'elapsed': True}),
            ('event.clock', {'action': 'next', 'round': 2}),
            ('final.publish', {'confirm': 'PUBLISH'}),
            ('final.setup', {'pairs': [1, 1], 'panel': [1]}),
            ('credentials.issue', {'role': 'admin', 'label': 'Admin', 'recipient': 'a@example.com'}),
            ('attendees.update_participant', {'pair': 1, 'member': 1, 'email': 'a@example.com', 'notes': 'private'}),
        ]
        for name, args in tests:
            with self.subTest(name=name):
                schema = E.COMMANDS[name][0]['input_schema']
                self.assertFalse(Draft202012Validator(schema).is_valid(args))

    def test_full_event_workflow(self):
        self.call('event.configure', participant_count=6, team_size=2, judge_count=2)
        roster = [{'pair': pair, 'partner': 'Company', 'members': [
            {'id': 1, 'name': f'Synthetic Participant {pair}', 'coach': False}]} for pair in (1, 2, 3)]
        self.call('roster.replace', rows=roster)
        checkin = self.call('attendees.update_participant', pair=1, member=1, checked_in=True,
                            email='participant@example.com')
        self.assertTrue(checkin['checked_in'])
        guest = self.call('attendees.create', name='Synthetic Guest', category='guest', checked_in=True)
        self.assertEqual(self.read('attendees.checkins', category='guest')['attendees'], [guest])
        before = self.read('assignments.list')['assignments']
        self.call('assignments.swap', pairs=[1, 2])
        after = self.read('assignments.list')['assignments']
        self.assertEqual(after[0]['station'], before[1]['station'])
        self.assertEqual(after[0]['scenario'], before[0]['scenario'])

        schedule = self.read('schedule.items')
        items = schedule['items']
        self.call('program.select', action='set', id=items[1]['id'])
        first, second = items[0], items[1]
        items[:2] = [dict(second, time=first['time'], min=first['min']),
                     dict(first, time=second['time'], min=second['min'])]
        updated = self.call('schedule.replace', items=items)
        self.assertEqual(updated['active_item_id'], second['id'])
        self.assertEqual(self.read('program.state')['index'], 0)
        self.call('schedule.update_item', id=second['id'], changes={'title': 'Synthetic opening'})
        self.assertEqual(self.read('schedule.items')['items'][0]['id'], second['id'])
        with self.assertRaises(E.CommandError):
            self.call('schedule.update_item', id=second['id'], changes={'min': 90})
        with self.assertRaisesRegex(E.CommandError, 'current item'):
            self.call('schedule.replace', items=items[1:])

        metadata = self.read('content.section', section='event')['value']
        self.call('content.update_section', section='event', value=dict(metadata, name='Synthetic Event'))
        self.assertEqual(self.read('content.section', section='event', version=1)['value'], metadata)
        self.call('content.restore', version=updated['version'])
        self.assertEqual(self.read('content.section', section='event')['value'], metadata)

        self.call('event.mode', mode='desk')
        self.assertTrue(self.call('event.clock', action='start')['clocks'][0]['running'])
        self.call('event.clock', action='pause')
        self.call('program.announce', text='Synthetic event update')
        self.assertEqual(self.read('program.state')['announcement'], 'Synthetic event update')
        self.call('calibration.set', active=True)

        self.call('final.setup', pairs=[1, 2, 3], panel=[1])
        with self.assertRaisesRegex(E.CommandError, 'final.publish'):
            self.call('display.reveal', mode='winners')
        with self.assertRaises(E.CommandError) as missing:
            self.call('final.start')
        self.assertEqual(len(missing.exception.details['missing']), 3)
        run = self.call('final.start', override_reason='Rehearsal is exercising the Final only.',
                        review_token=missing.exception.details['review_token'])['state']['run_id']
        self.assertFalse(self.read('calibration.status')['active'])
        self.call('event.clock', action='finish')
        with self.assertRaises(E.CommandError) as missing:
            self.call('final.advance', action='next')
        self.assertEqual(missing.exception.status, 409)

        for pair in (1, 2, 3):
            result, code = S.save_sheet(1, {'kind': 'final', 'pair': pair, 'run_id': run,
                                            'ticks': [True] * 5, 'spoke': True, 'final_rating':3}, 'judge1')
            self.assertEqual(code, 200, result)
            if pair != 3:
                self.call('final.advance', action='next')
                self.call('event.clock', action='finish')
        review = self.call('final.review')
        self.assertFalse(review['state']['can_publish'])
        self.call('final.resolve_tie', order=[2, 1, 3], reason='Panel agreed on remaining tie.')
        result = self.call('final.publish')
        self.assertEqual(result['rankings'][0]['pair'], 2)
        with self.assertRaisesRegex(E.CommandError, 'Published Final'):
            self.call('scoring.void_sheet', id=1, reason='Synthetic correction')
        self.call('final.reopen', reason='Panel correction requested.')
        self.call('scoring.correct_sheet', id=1, ticks=[True] * 6 + [False] * 4, ai=False, final_rating=5,
                  reason='Synthetic correction')
        self.assertEqual(self.read('scoring.sheets', id=1)['sheets'][0]['ticks'], [1] * 6 + [0] * 4)
        self.call('final.resolve_tie', order=[1,2,3], reason='Panel retained the tied lower places after correction.')
        self.assertEqual(self.call('final.publish')['rankings'][0]['pair'], 1)

    def test_roster_metadata_follows_person_and_workspace(self):
        row = {'pair': 1, 'partner': 'Company', 'members': [{'id': 1, 'name': 'Previous', 'coach': False}]}
        self.call('roster.update_team', **row)
        self.call('attendees.update_participant', pair=1, member=1, email='a@example.com', checked_in=True)
        self.call('roster.update_team', **dict(row, members=[{'id': 1, 'name': 'Replacement', 'coach': False}]))
        self.call('roster.update_team', **row)
        attendee = self.read('attendees.checkins')['attendees'][0]
        self.assertEqual((attendee['email'], attendee['checked_in']), ('', False))
        self.call('attendees.create', name='Live guest', category='guest')
        context = S.WORKSPACE.context
        self.call('workspace.switch', mode='rehearsal')
        self.assertNotEqual(S.WORKSPACE.context, context)
        self.assertEqual(self.read('attendees.checkins', category='guest')['attendees'], [])
        with patch.object(M, 'smtp_password') as secret:
            with self.assertRaisesRegex(E.CommandError, 'rehearsal'):
                self.call('notifications.send', recipient='a@example.com', subject='Test', message='Test')
            with self.assertRaisesRegex(E.CommandError, 'rehearsal'):
                self.call('credentials.issue', role='judge', label='Test', station=1, recipient='a@example.com')
            secret.assert_not_called()
        practice_context = S.WORKSPACE.context
        self.call('workspace.reset_rehearsal')
        self.assertNotEqual(S.WORKSPACE.context, practice_context)
        self.call('workspace.switch', mode='live')
        self.assertEqual(self.read('attendees.checkins', category='guest')['attendees'][0]['name'], 'Live guest')

    def test_credential_email_is_declined_without_an_event_address_and_issues_nothing(self):
        self.call('roster.update_team', pair=1, partner='Company',
                  members=[{'id': 1, 'name': 'Synthetic Participant', 'coach': False}])
        credentials = lambda: S.AUTH_DB.execute('SELECT count(*) FROM access_credentials').fetchone()[0]
        before = credentials()
        with patch.object(M, 'smtp_password', return_value='synthetic-smtp'), \
                patch.object(M, 'deliver', return_value=('sent', '')) as delivery, patch.dict(os.environ):
            os.environ.pop('KEVENOS_PUBLIC_URL')
            with self.assertRaises(E.CommandError) as refused:
                self.call('credentials.issue', role='participant', pair=1, member=1, recipient='a@example.com')
        self.assertEqual((refused.exception.code, refused.exception.status), ('state_conflict', 409))
        self.assertIn('KEVENOS_PUBLIC_URL', str(refused.exception))
        delivery.assert_not_called()
        self.assertEqual(credentials(), before)
        self.assertEqual(S.DB.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_receipts_metadata_without_password_and_revision_stability(self):
        self.call('roster.update_team', pair=1, partner='Company',
                  members=[{'id': 1, 'name': 'Synthetic Participant', 'coach': False}])
        before = json.dumps(E.snapshot(S), sort_keys=True)
        with patch.object(M, 'smtp_password', return_value='synthetic-smtp'),                 patch.object(M, 'deliver', return_value=('sent', '')) as delivery:
            self.call('notifications.send', recipient='a@example.com', subject='Test', message='Test')
            self.assertEqual(json.dumps(E.snapshot(S), sort_keys=True), before)
            issued = self.call('credentials.issue', role='participant', pair=1, member=1, recipient='a@example.com')
            issued_password = delivery.call_args.args[3].splitlines()[-3]
            self.assertTrue(issued_password)
            self.assertNotIn(issued_password, json.dumps(issued))
            self.assertNotIn(issued_password, '\n'.join(S.DB.iterdump()))
            self.assertNotIn(issued_password, Path(S.DATA, 'log.jsonl').read_text())
            self.assertNotEqual(json.dumps(E.snapshot(S), sort_keys=True), before)
            cid = issued['credential']['id']
            reset = self.call('credentials.reset', id=cid, recipient='a@example.com')
            self.assertEqual(reset['credential']['revision'], 2)
            self.assertEqual(reset['credential']['id'], cid)
            self.call('credentials.revoke', id=cid)
        self.assertFalse(self.read('credentials.list', role='participant')['credentials'][0]['active'])
        self.assertEqual(self.read('notifications.deliveries')['total'], 3)
        status = json.dumps(self.read('event.status'))
        for field in ('secret', 'password', 'view_key', 'anonymous_view_key'):
            self.assertNotIn(field, status)

    def test_smtp_unavailable_does_not_issue_or_reset_a_password(self):
        with patch.object(M, 'smtp_password', side_effect=OSError), patch.object(M, 'deliver') as delivery:
            with self.assertRaises(E.CommandError) as failure:
                self.call('credentials.issue', role='judge', station=1, label='Synthetic judge',
                          recipient='judge@example.com')
            self.assertEqual(failure.exception.status, 503)
            delivery.assert_not_called()
        self.assertEqual(self.read('credentials.list', role='judge')['total'], 0)
        self.assertEqual(self.read('notifications.deliveries')['total'], 0)
