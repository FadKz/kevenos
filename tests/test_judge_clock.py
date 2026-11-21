#!/usr/bin/env python3
"""Clock commands must never discard time that has already run."""
import unittest
from unittest import mock

import server as S


def fresh():
    return {'round': 1, 'running': False, 'elapsed': 0.0, 'started': None, 'ended': False}


class JudgeClock(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(S, 'CFG', {'rounds': 4})
        patcher.start()
        self.addCleanup(patcher.stop)

    def at(self, clock, now, action, body=None):
        with mock.patch.object(S.time, 'time', return_value=now):
            return S.updated_clock(clock, S.clock_elapsed(clock, now), action, body or {})

    def test_repeated_start_keeps_the_running_time(self):
        clock = self.at(fresh(), 1000.0, 'start')
        clock = self.at(clock, 1005.0, 'start')      # double tap, retry or a second device
        self.assertTrue(clock['running'])
        self.assertAlmostEqual(S.clock_elapsed(clock, 1005.0), 5.0)
        self.assertAlmostEqual(S.clock_elapsed(clock, 1012.0), 12.0)

    def test_start_after_pause_resumes_from_the_banked_time(self):
        clock = self.at(fresh(), 1000.0, 'start')
        clock = self.at(clock, 1030.0, 'pause')
        clock = self.at(clock, 1100.0, 'start')
        self.assertTrue(clock['running'])
        self.assertAlmostEqual(S.clock_elapsed(clock, 1110.0), 40.0)

    def test_start_after_finish_reopens_the_session(self):
        clock = self.at(fresh(), 1000.0, 'start')
        clock = self.at(clock, 1020.0, 'finish')
        self.assertTrue(clock['ended'])
        clock = self.at(clock, 1050.0, 'start')
        self.assertTrue(clock['running'])
        self.assertFalse(clock['ended'])
        self.assertAlmostEqual(S.clock_elapsed(clock, 1055.0), 25.0)


if __name__ == '__main__':
    unittest.main()
