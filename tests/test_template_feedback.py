#!/usr/bin/env python3
"""The built-in feedback template is what judges see whenever AI is unavailable."""
import json
import os
import unittest
from unittest import mock

import server as S

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TemplateFeedback(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, 'content.json'), encoding='utf-8') as source:
            content = json.load(source)
        patcher = mock.patch.object(S, 'CONTENT', content)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.labels = [tick['short'] for group in content['rubric'] for tick in group['ticks']]

    @staticmethod
    def mid_sentence(label):
        # Labels read as the middle of a sentence; an abbreviation keeps its capitals.
        return label if label.split()[0] in ('IT', 'AI') else label[0].lower() + label[1:]

    def test_missing_tick_is_named_as_one_readable_sentence(self):
        ticks = [True] * len(self.labels)
        ticks[3] = False
        text = S.template_feedback(ticks)
        self.assertTrue(text.endswith('One to add: %s.' % self.mid_sentence(self.labels[3])), text)

    def test_every_single_missing_tick_reads_cleanly(self):
        for index, label in enumerate(self.labels):
            ticks = [True] * len(self.labels)
            ticks[index] = False
            text = S.template_feedback(ticks)
            self.assertIn('One to add: %s.' % self.mid_sentence(label), text)
            self.assertEqual(text.count('One to add:'), 1, text)

    def test_abbreviations_keep_their_capitals_in_both_sentences(self):
        index = next(i for i, label in enumerate(self.labels) if label.startswith('IT '))
        missing = [True] * len(self.labels)
        missing[index] = False
        self.assertIn('One to add: IT worry', S.template_feedback(missing))
        only = [False] * len(self.labels)
        only[index] = True
        self.assertIn('One thing that worked: IT worry', S.template_feedback(only))
        self.assertNotIn('iT', S.template_feedback(missing) + S.template_feedback(only))
        first = S.template_feedback([True] + [False] * (len(self.labels) - 1))
        self.assertTrue(first.startswith('One thing that worked: ' + self.mid_sentence(self.labels[0])), first)

    def test_complete_sheet_keeps_the_encouragement(self):
        text = S.template_feedback([True] * len(self.labels))
        self.assertTrue(text.endswith('One to add: keep the same clear order in your next pitch.'), text)

    def test_only_ordinary_first_words_lose_their_capital(self):
        cases = {'Found why now': 'found why now', 'A point for users': 'a point for users',
                 'IT worry: understood it': 'IT worry: understood it', "UAE's rules were covered": "UAE's rules were covered",
                 'I will follow up': 'I will follow up', 'AI, where it fits': 'AI, where it fits', 'A': 'a', '': ''}
        for label, expected in cases.items():
            self.assertEqual(S._mid_sentence(label), expected)


if __name__ == '__main__':
    unittest.main()
