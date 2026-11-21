"""A judge's sheet is checked before it is stored, and a bad stored sheet cannot break the Desk."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_event_access as access

S = access.S

GOOD = {'kind': 'round', 'pair': 1, 'round': 1, 'wrap_rating': 4,
        'ticks': [1, 0, 1, 1, 0, 0, 1, 0, 0, 1], 'ai': 1, 'spoke': 0,
        'notes': 'Clear opening.', 'feedback': 'Good discovery.', 'judge': 'Synthetic Judge',
        'asked': [{'q': 'Q1', 't': 12}, {'q': 'Q2', 't': 40}],
        'raised': [{'k': 'IT', 't': 310}, {'k': 'Business'}]}


class SheetValidation(unittest.TestCase):
    def setUp(self):
        fixture = access.EventAccess('test_desk_can_run_live_but_cannot_administer_credentials_or_content')
        self.fixture = fixture
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.desk = fixture.login(fixture.create('desk')['password'])
        S.kv_set('mode', 'desk')
        self.judge = fixture.login(fixture.create('judge', station=1)['password'])

    def submit(self, **changes):
        code, result, _ = access.request('POST', '/api/submit', {**GOOD, **changes}, self.judge)
        return code, result

    def scores(self):
        code, result, _ = access.request('GET', '/api/desk/scores', None, self.desk)
        return code, result

    def test_the_sheet_the_judge_page_sends_is_accepted(self):
        code, result = self.submit()
        self.assertEqual(code, 200, result)
        self.assertEqual(result['score'], 52)
        self.assertEqual(self.scores()[0], 200)
        stored = S.active_sheets('round')[0]
        self.assertEqual(stored['asked'], GOOD['asked'])
        self.assertEqual(stored['raised'], GOOD['raised'])

    def test_short_and_boolean_tick_lists_remain_valid(self):
        code, result = self.submit(ticks=[True], ai=False, spoke=True)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['score'], 10)

    def test_malformed_fields_are_refused_and_nothing_is_stored(self):
        bad = {
            'ticks is a number': {'ticks': 5},
            'ticks is text': {'ticks': '1111111111'},
            'a tick is not yes or no': {'ticks': [2]},
            'a tick is text': {'ticks': ['yes']},
            'too many ticks': {'ticks': [1] * 11},
            'the bonus is text': {'ai': 'false'},
            'the bonus is a number other than 0 or 1': {'ai': 7},
            'both spoke is text': {'spoke': 'no'},
            'asked is a number': {'asked': 5},
            'asked holds bare names': {'asked': ['Q1']},
            'an asked entry has no question': {'asked': [{'t': 3}]},
            'an asked question is not text': {'asked': [{'q': 5}]},
            'an asked question name is too long': {'asked': [{'q': 'Q' * 201}]},
            'too many asked entries': {'asked': [{'q': 'Q1'}] * 501},
            'raised is a number': {'raised': 5},
            'raised holds bare names': {'raised': ['IT']},
            'too many raised entries': {'raised': [{'k': 'IT'}] * 101},
        }
        for reason, change in bad.items():
            code, result = self.submit(**change)
            self.assertEqual(code, 400, '%s: %r' % (reason, result))
            self.assertIn('error', result, reason)
        self.assertEqual(S.active_sheets('round'), [])
        code, result = self.submit()
        self.assertEqual(code, 200, result)

    def test_any_question_the_organizer_can_configure_can_be_logged(self):
        long_names = [{'q': 'Question-%03d-' % index + 'x' * 40, 't': index} for index in range(61)]
        code, result = self.submit(asked=long_names)
        self.assertEqual(code, 200, result)
        self.assertEqual(S.active_sheets('round')[0]['asked'], long_names)
        self.assertEqual(self.scores()[0], 200)

    def test_content_cannot_define_questions_a_sheet_would_refuse(self):
        def errors(change):
            content = copy.deepcopy(S.CONTENT)
            change(content['questions'][0]['items'])
            return [error for error in S.validate_content(content) if error.startswith('Questions')]
        self.assertEqual(errors(lambda items: items.append(dict(items[0], id='Q' * 200))), [])
        self.assertTrue(errors(lambda items: items.append(dict(items[0], id='Q' * 201))))
        self.assertTrue(errors(lambda items: items.append(dict(items[0], id=''))))
        # A numeric id reaches a sheet as its text form, so content saved with one stays valid.
        self.assertEqual(errors(lambda items: items.append(dict(items[0], id=7))), [])
        self.assertTrue(errors(lambda items: items.append(dict(items[0], id=True))))
        self.assertTrue(errors(lambda items: items.extend(dict(items[0], id='Extra-%d' % index) for index in range(500))))

    def test_a_sheet_stored_by_an_earlier_release_cannot_break_the_scores_view(self):
        shapes = (['Q1'], [{'q': ['Q1']}, {'q': None}, {'q': {'a': 1}}, 5, None, 'Q1', {'t': 3}], 'Q1', [], [{'q': 'Q1'}])
        with S.LOCK:
            for index, asked in enumerate(shapes):
                S.DB.execute("INSERT INTO sheets(kind,pair,station,round,judge,ticks,ai,spoke,notes,feedback,asked,raised,created,wrap_rating) "
                             "VALUES('round',?,1,1,'Synthetic Judge',?,1,1,'','',?,?,?,4)",
                             (index + 1, json.dumps([1] + [0] * 9), json.dumps(asked), json.dumps(['IT']), time.time()))
            S.DB.commit()
        code, result = self.scores()
        self.assertEqual(code, 200, result)
        self.assertTrue(result['leaderboard'])

    def test_a_refused_correction_changes_nothing_about_the_final(self):
        with S.LOCK:
            cursor = S.DB.execute("INSERT INTO sheets(kind,pair,station,round,judge,ticks,ai,spoke,notes,feedback,asked,raised,created,final_run,final_rating) "
                                  "VALUES('final',1,1,0,'Synthetic Judge',?,0,1,'','','[]','[]',?,'earlier-run',4)",
                                  (json.dumps([1] * 10), time.time()))
            S.DB.commit()
            sheet_id = cursor.lastrowid
        body = {'id': sheet_id, 'action': 'ticks', 'final_rating': 4, 'why': 'Synthetic correction reason'}
        with patch.object(S.final_event, 'invalidate_decision') as invalidate:
            code, result, _ = access.request('POST', '/api/desk/sheet', dict(body, ticks=5), self.desk)
            self.assertEqual(code, 400, result)
            code, result, _ = access.request('POST', '/api/desk/sheet', dict(body, ticks=[1] * 10, final_rating=9), self.desk)
            self.assertEqual(code, 400, result)
            code, result, _ = access.request('POST', '/api/desk/sheet', dict(body, action='something-else'), self.desk)
            self.assertEqual(code, 400, result)
            invalidate.assert_not_called()
            code, result, _ = access.request('POST', '/api/desk/sheet', dict(body, ticks=[1] * 9), self.desk)
            self.assertEqual(code, 200, result)
            invalidate.assert_called_once()

    def test_a_desk_correction_with_malformed_ticks_is_refused(self):
        self.assertEqual(self.submit()[0], 200)
        sheet = S.active_sheets('round')[0]
        for ticks in (5, 'x', [2], [1] * 11):
            code, result, _ = access.request('POST', '/api/desk/sheet',
                                             {'id': sheet['id'], 'action': 'ticks', 'ticks': ticks, 'wrap_rating': 4,
                                              'why': 'Synthetic correction reason'}, self.desk)
            self.assertEqual(code, 400, '%r: %r' % (ticks, result))
        self.assertEqual(S.active_sheets('round')[0]['ticks'], GOOD['ticks'])

    def test_a_desk_correction_for_an_unknown_sheet_is_refused(self):
        for sheet_id, expected in ((999999, 404), ('x', 400), (None, 400), (1.5, 400)):
            for action in ('ticks', 'void'):
                code, result, _ = access.request('POST', '/api/desk/sheet',
                                                 {'id': sheet_id, 'action': action, 'ticks': [1], 'wrap_rating': 4,
                                                  'why': 'Synthetic correction reason'}, self.desk)
                self.assertEqual(code, expected, '%r %s: %r' % (sheet_id, action, result))
                self.assertIn('error', result)


if __name__ == '__main__':
    unittest.main()
