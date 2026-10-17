"""Frame counter persistence by blocks of 256 (MESH_DESIGN §11, §14)."""

import unittest

from mesh.counter import BLOCK, COUNTER_LIMIT, RANDOM_RANGE, RECORD_KEY, FrameCounter
from sim.platform import FakePlatform
from sim.prng import XorShift32


def _record(storage):
    return int.from_bytes(storage.get(RECORD_KEY), "little")


def _platform(seed=1, start=None):
    """A board whose record holds start (None: no record, as after an erase)."""
    platform = FakePlatform(XorShift32(seed))
    if start is not None:
        platform.storage.set(RECORD_KEY, start.to_bytes(4, "little"))
        platform.storage.commit()
    return platform


def _counter(platform):
    return FrameCounter(platform.storage, platform.random)


class Blocks(unittest.TestCase):
    def test_boot_starts_at_record_and_records_next_block(self):
        platform = _platform(start=5 * BLOCK)
        commits = platform.flash.commits
        counter = _counter(platform)
        self.assertFalse(counter.random_restart)
        self.assertEqual(platform.flash.commits, commits + 1)
        self.assertEqual(_record(platform.storage), 6 * BLOCK)
        self.assertEqual(counter.peek(), 5 * BLOCK)
        self.assertEqual([counter.next() for _ in range(3)], [1280, 1281, 1282])

    def test_next_block_recorded_at_half_block(self):
        platform = _platform(start=0)
        counter = _counter(platform)
        commits = platform.flash.commits
        for _ in range(BLOCK // 2 - 1):
            counter.next()
        self.assertEqual(platform.flash.commits, commits)
        self.assertEqual(_record(platform.storage), BLOCK)
        # Using the 128th counter of the block records the following block,
        # 128 frames before it is needed.
        self.assertEqual(counter.next(), BLOCK // 2 - 1)
        self.assertEqual(platform.flash.commits, commits + 1)
        self.assertEqual(_record(platform.storage), 2 * BLOCK)
        for _ in range(BLOCK):
            counter.next()
        self.assertEqual(platform.flash.commits, commits + 2)
        self.assertEqual(_record(platform.storage), 3 * BLOCK)
        # One write per block of 256 frames.
        for _ in range(10 * BLOCK):
            counter.next()
        self.assertEqual(platform.flash.commits, commits + 12)

    def test_used_counters_always_below_record(self):
        platform = _platform(start=0)
        counter = _counter(platform)
        for _ in range(3 * BLOCK):
            c = counter.next()
            platform.flash.drop_uncommitted()
            self.assertTrue(c < _record(platform.storage))


class Restarts(unittest.TestCase):
    def test_power_loss_skips_a_block(self):
        platform = _platform(start=0)
        counter = _counter(platform)
        used = [counter.next() for _ in range(100)]
        platform.power_loss()
        counter = _counter(platform)
        self.assertEqual(counter.next(), BLOCK)  # block 0's rest skipped
        self.assertTrue(BLOCK > used[-1])

    def test_never_repeats_across_resets_and_power_losses(self):
        platform = _platform(start=0)
        rng = XorShift32(7)
        counter = _counter(platform)
        last = -1
        for cycle in range(60):
            for _ in range(rng.getrandbits(10)):  # 0-1023 frames
                c = counter.next()
                self.assertTrue(c > last, "cycle %d: %d after %d" % (cycle, c, last))
                last = c
            if rng.getrandbits(1):
                platform.power_loss()
            else:
                platform.reset()
            counter = _counter(platform)
        # Each boot costs at most two blocks beyond the frames sent.
        self.assertTrue(last < 60 * 2 * BLOCK + 60 * 1024)

    def test_full_flash_erase_restarts_at_random_block(self):
        starts = set()
        for seed in range(1, 6):
            platform = _platform(seed=seed, start=9 * BLOCK)
            _counter(platform).next()
            platform.flash.full_erase()
            counter = _counter(platform)
            self.assertTrue(counter.random_restart)
            c = counter.next()
            self.assertEqual(c % BLOCK, 0)
            self.assertTrue(0 <= c < RANDOM_RANGE)
            self.assertEqual(_record(platform.storage), c + BLOCK)
            starts.add(c)
        self.assertTrue(len(starts) >= 4)

    def test_unreadable_record_restarts_at_random_block(self):
        platform = _platform()
        platform.storage.set(RECORD_KEY, b"\x01\x02")
        platform.storage.commit()
        self.assertTrue(_counter(platform).random_restart)


class Exhaustion(unittest.TestCase):
    def test_stops_before_2_24_minus_1(self):
        platform = _platform(start=COUNTER_LIMIT - 3)
        counter = _counter(platform)
        self.assertEqual([counter.next() for _ in range(3)], [0xFFFFFC, 0xFFFFFD, 0xFFFFFE])
        self.assertTrue(counter.exhausted)
        self.assertEqual(counter.next(), None)
        self.assertEqual(counter.peek(), None)
        # Stays exhausted after a reboot.
        platform.reset()
        counter = _counter(platform)
        self.assertTrue(counter.exhausted)
        self.assertEqual(counter.next(), None)
