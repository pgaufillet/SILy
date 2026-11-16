"""Start decision at boot (sily.guard) on the simulator's NVS fake."""

import unittest

from sily.guard import MAX_CRASHES, SAFE, SKIP, START, Guard
from sim.platform import FakeFlash


class Decisions(unittest.TestCase):
    def setUp(self):
        self.flash = FakeFlash()
        self.guard = Guard(self.flash.namespace("sily"))

    def _crash(self, n):
        for _ in range(n):
            self.guard.crashed()

    def test_fresh_board_starts_without_writing(self):
        self.assertEqual(self.guard.decide(False), START)
        self.assertEqual(self.guard.decide(True), START)
        self.assertEqual(self.flash.commits, 0)

    def test_three_crashes_give_safe_mode(self):
        self._crash(MAX_CRASHES - 1)
        self.assertEqual(self.guard.decide(False), START)
        self._crash(1)
        self.assertEqual(self.guard.crashes, MAX_CRASHES)
        self.assertEqual(self.guard.decide(False), SAFE)
        self.assertEqual(self.guard.decide(False), SAFE)

    def test_crash_count_survives_a_reset(self):
        self._crash(2)
        self.flash.drop_uncommitted()
        self.assertEqual(Guard(self.flash.namespace("sily")).crashes, 2)

    def test_power_on_clears_the_count(self):
        self._crash(MAX_CRASHES)
        self.assertEqual(self.guard.decide(True), START)
        self.assertEqual(self.guard.crashes, 0)

    def test_stable_run_clears_the_count(self):
        self._crash(2)
        self.guard.stable()
        self.assertEqual(self.guard.crashes, 0)
        self._crash(MAX_CRASHES - 1)
        self.assertEqual(self.guard.decide(False), START)

    def test_stable_run_without_crash_writes_nothing(self):
        self.guard.stable()
        self.assertEqual(self.flash.commits, 0)

    def test_skip_flag_once(self):
        self.guard.skip_next()
        self.assertEqual(self.guard.decide(False), SKIP)
        self.assertEqual(self.guard.decide(False), START)

    def test_skip_flag_before_safe_mode(self):
        self._crash(MAX_CRASHES)
        self.guard.skip_next()
        self.assertEqual(self.guard.decide(False), SKIP)
        self.assertEqual(self.guard.decide(False), SAFE)

    def test_power_on_clears_the_skip_flag(self):
        self.guard.skip_next()
        self.assertEqual(self.guard.decide(True), START)
        self.assertEqual(self.guard.decide(False), START)

    def test_clear(self):
        self._crash(MAX_CRASHES)
        self.guard.skip_next()
        self.guard.clear()
        self.assertEqual(self.guard.decide(False), START)
