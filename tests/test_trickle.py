"""Trickle timer (RFC 6206; MESH_DESIGN §9 Trickle and control traffic)."""

import unittest

from mesh.trickle import PERIODIC, URGENT, K, Trickle
from sim.prng import XorShift32

IMIN = 1600
IMAX = 30000


def _trickle(beacon=False, seed=1, imin=IMIN, imax=IMAX):
    return Trickle(imin, imax, XorShift32(seed), beacon=beacon)


def _intervals(t, count):
    """Poll t to the end of count intervals; return [(start, interval, send_at, due)]."""
    out = []
    for _ in range(count):
        start, interval, send_at = t.start_at, t.interval, t.send_at
        due = t.poll(send_at)
        t.poll(t.end_at)
        out.append((start, interval, send_at, due))
    return out


class Intervals(unittest.TestCase):
    def test_stopped_until_reset(self):
        t = _trickle()
        self.assertFalse(t.running)
        self.assertIsNone(t.timer())
        self.assertEqual(t.poll(100000), 0)
        self.assertTrue(t.reset(500))
        self.assertTrue(t.running)
        self.assertEqual((t.interval, t.start_at, t.end_at), (IMIN, 500, 500 + IMIN))

    def test_doubling_up_to_imax(self):
        t = _trickle()
        t.reset(0)
        seen = _intervals(t, 8)
        self.assertEqual(
            [i for _, i, _, _ in seen], [1600, 3200, 6400, 12800, 25600, 30000, 30000, 30000]
        )
        # Contiguous intervals.
        for (s0, i0, _, _), (s1, _, _, _) in zip(seen, seen[1:]):
            self.assertEqual(s1, s0 + i0)

    def test_send_time_in_second_half(self):
        t = _trickle(seed=7)
        lows = []
        for n in range(200):
            t.reset(n * IMIN * 4)
            lows.append(t.send_at - t.start_at)
            t.poll(t.end_at)  # decided: the next reset restarts
        self.assertTrue(min(lows) >= IMIN // 2)
        self.assertTrue(max(lows) < IMIN)
        # The draw spreads over the second half, not a fixed point.
        self.assertTrue(min(lows) < IMIN // 2 + IMIN // 8)
        self.assertTrue(max(lows) > IMIN - IMIN // 8)

    def test_timer_is_send_time_then_interval_end(self):
        t = _trickle()
        t.reset(0)
        self.assertEqual(t.timer(), t.send_at)
        self.assertEqual(t.poll(t.send_at - 1), 0)
        self.assertEqual(t.poll(t.send_at), URGENT)
        self.assertEqual(t.timer(), t.end_at)

    def test_late_poll_sends_once_and_catches_up(self):
        t = _trickle()
        t.reset(0)
        self.assertEqual(t.poll(IMIN + 2 * IMIN + 1), URGENT)  # two intervals passed
        self.assertEqual((t.start_at, t.interval), (3 * IMIN, 4 * IMIN))
        self.assertEqual(t.sends, 1)

    def test_imax_below_imin_is_imin(self):
        t = _trickle(imin=5000, imax=1000)
        self.assertEqual(t.imax, 5000)
        self.assertRaises(ValueError, t.configure, 0, 1000)

    def test_configure_applies_from_the_next_reset(self):
        t = _trickle()
        t.reset(0)
        t.configure(1000, 4000)
        self.assertEqual(t.interval, IMIN)
        t.poll(t.end_at)
        self.assertEqual(t.interval, 3200)
        t.reset(t.start_at + 1)
        self.assertEqual(t.interval, 1000)
        t.poll(t.end_at)
        t.poll(t.end_at)
        self.assertEqual(t.interval, 4000)


class Suppression(unittest.TestCase):
    def setUp(self):
        self.t = _trickle()
        self.t.reset(0)

    def test_k_is_two(self):
        self.assertEqual(K, 2)

    def test_sends_without_redundancy(self):
        self.assertEqual([d for _, _, _, d in _intervals(self.t, 5)], [URGENT] * 5)
        self.assertEqual(self.t.sends, 5)

    def test_one_frame_heard_does_not_suppress(self):
        self.t.heard()
        self.assertEqual(self.t.poll(self.t.send_at), URGENT)

    def test_two_frames_heard_suppress(self):
        self.t.heard()
        self.t.heard()
        self.assertEqual(self.t.poll(self.t.send_at), 0)
        self.assertEqual((self.t.sends, self.t.suppressed), (0, 1))

    def test_count_restarts_each_interval(self):
        self.t.heard()
        self.t.heard()
        self.t.poll(self.t.end_at)
        self.t.heard()
        self.assertEqual(self.t.poll(self.t.send_at), URGENT)

    def test_frames_after_the_decision_count_for_nothing(self):
        self.assertEqual(self.t.poll(self.t.send_at), URGENT)
        self.t.heard()
        self.t.heard()
        self.t.poll(self.t.end_at)
        self.assertEqual(self.t.poll(self.t.send_at), URGENT)

    def test_own_frame_suppresses(self):
        self.t.transmitted()
        self.assertEqual(self.t.poll(self.t.send_at), 0)
        self.assertEqual(self.t.suppressed, 1)

    def test_information_needed_counts_port_9_only(self):
        t = _trickle()
        t.reset(0, need_info=True)
        t.heard()
        t.heard()
        t.heard()
        t.transmitted()  # a frame without DODAG information
        self.assertEqual(t.poll(t.send_at), URGENT)
        self.assertFalse(t.need_info)
        t.poll(t.end_at)
        t.heard()
        t.heard()
        self.assertEqual(t.poll(t.send_at), 0)  # back to any frame

    def test_information_needed_suppressed_by_two_port_9_messages(self):
        t = _trickle()
        t.reset(0, need_info=True)
        t.heard()
        t.heard(True)
        self.assertEqual(t.poll(t.send_at - 1), 0)
        t.heard()
        t.heard(True)
        self.assertEqual(t.poll(t.send_at), 0)
        self.assertFalse(t.need_info)

    def test_information_needed_suppressed_by_own_advertisement(self):
        t = _trickle()
        t.reset(0, need_info=True)
        t.transmitted(True)
        self.assertEqual(t.poll(t.send_at), 0)


class AtImax(unittest.TestCase):
    def _at_imax(self, t):
        t.reset(0)
        for _ in range(20):
            if t.at_imax():
                return
            t.poll(t.end_at)
        raise AssertionError("Imax not reached")

    def test_node_quiet_at_imax(self):
        t = _trickle()
        self._at_imax(t)
        sends = t.sends
        self.assertEqual([d for _, _, _, d in _intervals(t, 5)], [0] * 5)
        self.assertEqual(t.sends, sends)

    def test_root_beacons_at_imax_unsuppressed(self):
        t = _trickle(beacon=True)
        self._at_imax(t)
        t.heard()
        t.heard()
        t.transmitted()
        self.assertEqual(t.poll(t.send_at), PERIODIC)
        t.poll(t.end_at)
        self.assertEqual([d for _, _, _, d in _intervals(t, 3)], [PERIODIC] * 3)

    def test_root_below_imax_is_urgent_and_suppressible(self):
        t = _trickle(beacon=True)
        t.reset(0)
        self.assertEqual(t.poll(t.send_at), URGENT)
        t.poll(t.end_at)
        t.heard()
        t.heard()
        self.assertEqual(t.poll(t.send_at), 0)

    def test_imin_equal_to_imax_is_not_steady_state(self):
        t = _trickle(imin=IMAX, imax=IMAX)
        t.reset(0)
        self.assertFalse(t.at_imax())
        self.assertEqual(t.poll(t.send_at), URGENT)
        t.poll(t.end_at)
        self.assertTrue(t.at_imax())
        self.assertEqual(t.poll(t.send_at), 0)

    def test_information_needed_sent_at_imax(self):
        t = _trickle(imin=IMAX, imax=IMAX)
        t.reset(0)
        t.poll(t.end_at)
        t.need_info = True
        self.assertEqual(t.poll(t.send_at), PERIODIC)


class Reset(unittest.TestCase):
    def test_reset_from_a_longer_interval_restarts_at_imin(self):
        t = _trickle()
        t.reset(0)
        for _ in range(2):
            t.poll(t.end_at)
        self.assertEqual(t.interval, 4 * IMIN)
        now = t.start_at + 100
        self.assertTrue(t.reset(now))
        self.assertEqual((t.interval, t.start_at), (IMIN, now))
        self.assertEqual(t.poll(t.send_at), URGENT)

    def test_reset_keeps_a_pending_advertisement_at_imin(self):
        t = _trickle()
        t.reset(0)
        send_at = t.send_at
        self.assertFalse(t.reset(send_at - 1, need_info=True))
        self.assertEqual((t.start_at, t.send_at), (0, send_at))
        self.assertTrue(t.need_info)
        self.assertEqual(t.resets, 2)

    def test_reset_after_the_decision_restarts(self):
        t = _trickle()
        t.reset(0)
        t.poll(t.send_at)
        now = t.send_at + 1
        self.assertTrue(t.reset(now))
        self.assertEqual(t.start_at, now)
        self.assertEqual(t.poll(t.send_at), URGENT)

    def test_reset_clears_counts(self):
        t = _trickle()
        t.reset(0)
        t.poll(t.end_at)
        t.heard()
        t.heard()
        t.transmitted()
        t.reset(t.start_at + 1)
        self.assertEqual(t.poll(t.send_at), URGENT)

    def test_stop(self):
        t = _trickle()
        t.reset(0, need_info=True)
        t.stop()
        self.assertFalse(t.running)
        self.assertFalse(t.need_info)
        self.assertIsNone(t.timer())
        self.assertEqual(t.poll(10 * IMAX), 0)
