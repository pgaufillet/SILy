"""Replay table: 128 senders, LRU eviction, retained memory (MESH_DESIGN §14)."""

import unittest

from mesh.dutycycle import DutyCycle
from mesh.dutycycle import retained_size as window_size
from mesh.platform import retained_layout
from mesh.replay import CAPACITY, ReplayTable, retained_size
from sim.platform import FakePlatform, FakeRetained
from sim.prng import XorShift32


class Freshness(unittest.TestCase):
    def test_strictly_greater_counter_required(self):
        table = ReplayTable()
        self.assertTrue(table.check(0x3F2A, 0))  # unknown sender: baseline
        table.update(0x3F2A, 1000)
        self.assertFalse(table.check(0x3F2A, 1000))
        self.assertFalse(table.check(0x3F2A, 999))
        self.assertFalse(table.check(0x3F2A, 0))
        self.assertTrue(table.check(0x3F2A, 1001))
        self.assertTrue(table.check(0x3F2B, 5))  # other senders independent
        self.assertEqual(table.highest(0x3F2A), 1000)

    def test_check_does_not_change_table(self):
        table = ReplayTable()
        table.check(0x0001, 10)
        self.assertEqual(len(table), 0)
        self.assertEqual(table.highest(0x0001), None)
        table.update(0x0001, 10)
        table.check(0x0001, 20)
        self.assertEqual(table.highest(0x0001), 10)

    def test_full_counter_and_address_range(self):
        table = ReplayTable()
        for source in (0x0000, 0x00FF, 0xFF00, 0xFFFE):
            table.update(source, 0xFFFFFE)
        table.update(0x0100, 0x00FFFF)
        self.assertEqual(table.highest(0xFF00), 0xFFFFFE)
        self.assertEqual(table.highest(0x0100), 0x00FFFF)
        self.assertTrue(table.check(0x00FF, 0xFFFFFF))
        self.assertFalse(table.check(0xFFFE, 0xFFFFFE))
        self.assertEqual(table.highest(0x0001), None)  # not confused with 0x0100


class Eviction(unittest.TestCase):
    def _full(self):
        table = ReplayTable()
        for i in range(CAPACITY):
            table.update(0x1000 + i, i)
        return table

    def test_holds_128_senders(self):
        table = self._full()
        self.assertEqual(CAPACITY, 128)
        self.assertEqual(len(table), 128)
        for i in range(CAPACITY):
            self.assertEqual(table.highest(0x1000 + i), i)
        self.assertEqual(table.evictions, 0)

    def test_least_recently_heard_evicted(self):
        table = self._full()
        table.update(0x1000, 500)  # heard again: now the most recent
        table.update(0x2000, 1)  # new sender: evicts 0x1001, the oldest
        self.assertEqual(len(table), 128)
        self.assertEqual(table.evictions, 1)
        self.assertEqual(table.highest(0x1001), None)
        self.assertEqual(table.highest(0x1000), 500)
        self.assertEqual(table.highest(0x2000), 1)
        table.update(0x2001, 1)  # next oldest: 0x1002
        self.assertEqual(table.highest(0x1002), None)
        self.assertEqual(table.highest(0x1003), 3)
        self.assertEqual(table.evictions, 2)

    def test_evicted_sender_starts_a_new_baseline(self):
        # Degradation of §14: an evicted sender's old frame passes once.
        table = self._full()
        table.update(0x2000, 1)
        self.assertTrue(table.check(0x1000, 0))

    def test_lru_order_against_model(self):
        rng = XorShift32(3)
        table = ReplayTable(capacity=16)
        model = []  # (source, counter), least recently heard first
        for _ in range(2000):
            source = rng.getrandbits(5)  # 32 senders for 16 entries
            counter = rng.getrandbits(24)
            for i, (s, _c) in enumerate(model):
                if s == source:
                    del model[i]
                    break
            else:
                if len(model) == 16:
                    del model[0]
            model.append((source, counter))
            table.update(source, counter)
        for source in range(32):
            expected = None
            for s, c in model:
                if s == source:
                    expected = c
            self.assertEqual(table.highest(source), expected)
        self.assertEqual(len(table), 16)


class Retention(unittest.TestCase):
    def test_survives_reset_not_power_loss(self):
        platform = FakePlatform(XorShift32(1))
        table = ReplayTable(platform.retained, 100)
        table.update(0x0042, 7)
        table.update(0x0043, 9)
        platform.reset()
        table = ReplayTable(platform.retained, 100)
        self.assertEqual(table.highest(0x0042), 7)
        self.assertFalse(table.check(0x0043, 9))
        platform.power_loss()
        table = ReplayTable(platform.retained, 100)
        self.assertEqual(len(table), 0)
        self.assertTrue(table.check(0x0043, 9))

    def test_every_update_saved(self):
        retained = FakeRetained(1024)
        table = ReplayTable(retained)
        table.update(0x0042, 7)
        table.update(0x0042, 8)
        retained.reset()
        self.assertEqual(ReplayTable(retained).highest(0x0042), 8)

    def test_invalid_content_gives_empty_table(self):
        retained = FakeRetained(1024)
        retained.data[3] = 5  # count without magic
        self.assertEqual(len(ReplayTable(retained)), 0)
        ReplayTable(retained).update(1, 1)
        retained.data[3] = CAPACITY + 1  # count above capacity
        self.assertEqual(len(ReplayTable(retained)), 0)

    def test_shares_retained_memory_with_duty_cycle(self):
        platform = FakePlatform(XorShift32(1))
        retained = platform.retained
        window_at, table_at = retained_layout(retained, window_size(60), retained_size())
        self.assertEqual(table_at, window_size(60))
        self.assertTrue(table_at + retained_size() <= 2048)
        window = DutyCycle(324000, retained=retained, offset=window_at)
        table = ReplayTable(retained, table_at)
        for i in range(CAPACITY):
            table.update(0x5000 + i, 0xFFFFFE)
        window.start(1000, 1234)
        table.update(0x6000, 0xFFFFFF)  # evicts 0x5000
        platform.reset()
        window = DutyCycle(324000, retained=retained, offset=window_at)
        table = ReplayTable(retained, table_at)
        self.assertEqual(window.used(1000), 1234)
        self.assertEqual(table.highest(0x5001), 0xFFFFFE)
        self.assertEqual(table.highest(0x6000), 0xFFFFFF)
        self.assertEqual(table.highest(0x5000), None)

    def test_layout_checks_size(self):
        retained = FakeRetained(100)
        self.assertEqual(retained_layout(retained, 10, 20, 70), (0, 10, 30))
        self.assertRaises(ValueError, retained_layout, retained, 50, 51)
        self.assertRaises(ValueError, ReplayTable, retained)
        self.assertRaises(ValueError, ReplayTable, FakeRetained(2048), 2048 - 643)
