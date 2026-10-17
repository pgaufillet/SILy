"""Carrier sense bookkeeping of the SX1262 adapter (board.sense, MESH_DESIGN §8)."""

import unittest
from time import ticks_add

from board.sense import NoiseFloor, Reception

# Close to the wrap of ticks_ms (period 2**30 on MicroPython).
_NEAR_WRAP = (1 << 30) - 5


class ReceptionWindow(unittest.TestCase):
    def setUp(self):
        self.r = Reception(preamble_ms=25, frame_ms=400)

    def test_idle_at_start(self):
        self.assertFalse(self.r.active(1000))
        self.assertEqual(self.r.stale, 0)

    def test_preamble_opens_a_short_window(self):
        self.r.preamble(1000)
        self.assertTrue(self.r.active(1000))
        self.assertTrue(self.r.active(1024))
        self.assertFalse(self.r.active(1025))
        self.assertEqual(self.r.stale, 1)

    def test_header_extends_to_a_maximum_frame(self):
        self.r.preamble(1000)
        self.r.header(1020)
        self.assertTrue(self.r.active(1100))
        self.assertTrue(self.r.active(1419))
        self.assertFalse(self.r.active(1420))

    def test_end_closes_without_stale_count(self):
        self.r.preamble(1000)
        self.r.header(1020)
        self.r.end()
        self.assertFalse(self.r.active(1021))
        self.assertEqual(self.r.stale, 0)

    def test_expiry_is_counted_once(self):
        self.r.preamble(1000)
        self.assertFalse(self.r.active(2000))
        self.assertFalse(self.r.active(3000))
        self.assertEqual(self.r.stale, 1)

    def test_window_across_ticks_wrap(self):
        self.r.preamble(_NEAR_WRAP)
        self.assertTrue(self.r.active(ticks_add(_NEAR_WRAP, 10)))
        self.assertFalse(self.r.active(ticks_add(_NEAR_WRAP, 30)))


class NoiseFloorEstimate(unittest.TestCase):
    def test_no_floor_never_busy(self):
        n = NoiseFloor()
        self.assertIsNone(n.floor)
        self.assertFalse(n.busy(-20))

    def test_calibrate_takes_the_median(self):
        n = NoiseFloor()
        n.calibrate([-110, -112, -60, -111, -113])
        self.assertEqual(n.floor, -111)

    def test_busy_above_floor_plus_threshold(self):
        n = NoiseFloor()
        n.calibrate([-112])
        self.assertFalse(n.busy(-102))
        self.assertTrue(n.busy(-101))

    def test_window_keeps_recent_samples(self):
        n = NoiseFloor(window=3)
        n.calibrate([-112, -112, -112])
        for rssi in (-108, -108, -108):
            n.add(rssi)
        self.assertEqual(n.floor, -108)

    def test_activity_samples_left_out(self):
        n = NoiseFloor(window=5)
        n.calibrate([-112] * 5)
        for _ in range(5):
            n.add(-60)  # a neighbour transmitting
        self.assertEqual(n.floor, -112)
        self.assertEqual(n.rejected, 5)

    def test_persistent_rise_moves_the_floor(self):
        n = NoiseFloor(window=5, adapt_after=4)
        n.calibrate([-112] * 5)
        for _ in range(3):
            n.add(-95)
        self.assertEqual(n.floor, -112)
        n.add(-95)
        self.assertEqual(n.floor, -95)

    def test_quiet_sample_resets_the_run_of_high_ones(self):
        n = NoiseFloor(window=5, adapt_after=3)
        n.calibrate([-112] * 5)
        n.add(-95)
        n.add(-95)
        n.add(-112)
        n.add(-95)
        n.add(-95)
        self.assertEqual(n.floor, -112)
        self.assertEqual(n.rejected, 4)

    def test_floor_follows_a_lower_noise(self):
        n = NoiseFloor(window=3)
        n.calibrate([-100, -100, -100])  # measured while a neighbour sent
        for _ in range(3):
            n.add(-112)
        self.assertEqual(n.floor, -112)
