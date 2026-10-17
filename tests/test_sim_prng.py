"""Deterministic random generator of the simulator."""

import unittest

from mesh.platform import randbelow
from sim.prng import XorShift32

# First draws of seed 1, identical on the unix port and CPython.
_PINNED = [4020896444, 1346862338, 1124809937]


def _draws(rng, n=8):
    return [rng.getrandbits(32) for _ in range(n)]


class Determinism(unittest.TestCase):
    def test_same_seed_same_sequence(self):
        self.assertEqual(_draws(XorShift32(42)), _draws(XorShift32(42)))

    def test_different_seeds_differ(self):
        self.assertNotEqual(_draws(XorShift32(1)), _draws(XorShift32(2)))

    def test_seed_zero_is_usable(self):
        draws = _draws(XorShift32(0))
        self.assertNotEqual(draws, [0] * 8)

    def test_pinned_sequence(self):
        # Recorded scenarios depend on the algorithm: changing it must be
        # deliberate.
        self.assertEqual(_draws(XorShift32(1), 3), _PINNED)

    def test_derive_depends_on_seed_and_label_only(self):
        a = XorShift32(7)
        a.getrandbits(32)  # consuming the parent does not change a child
        self.assertEqual(_draws(a.derive(3)), _draws(XorShift32(7).derive(3)))
        self.assertNotEqual(_draws(a.derive(3)), _draws(a.derive(4)))
        self.assertNotEqual(_draws(a.derive(3)), _draws(XorShift32(8).derive(3)))


class Ranges(unittest.TestCase):
    def test_getrandbits_bounds(self):
        rng = XorShift32(5)
        values = [rng.getrandbits(3) for _ in range(200)]
        self.assertEqual(sorted(set(values)), list(range(8)))
        self.assertRaises(ValueError, rng.getrandbits, 0)
        self.assertRaises(ValueError, rng.getrandbits, 33)

    def test_randbytes(self):
        rng = XorShift32(5)
        data = rng.randbytes(64)
        self.assertEqual(len(data), 64)
        self.assertTrue(len(set(data)) > 16)

    def test_randbelow_covers_range(self):
        rng = XorShift32(9)
        values = [randbelow(rng, 5) for _ in range(300)]
        self.assertEqual(sorted(set(values)), [0, 1, 2, 3, 4])
        self.assertEqual(randbelow(rng, 1), 0)
        self.assertRaises(ValueError, randbelow, rng, 0)

    def test_chance_extremes_and_rate(self):
        rng = XorShift32(11)
        self.assertFalse(any(rng.chance(0.0) for _ in range(100)))
        self.assertTrue(all(rng.chance(1.0) for _ in range(100)))
        hits = sum(1 for _ in range(4000) if rng.chance(0.25))
        self.assertTrue(900 < hits < 1100, hits)
