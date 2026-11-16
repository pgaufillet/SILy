"""Drop rates of the box application (sily.drop): frames of chosen sources dropped.

The wrapper is driven alone, on a recording radio, with frames whose link
source is read by ``sily.radio.link_source`` (as on the board) and with
scripted or seeded draws.
"""

import random
import unittest

from mesh.platform import randbelow
from mesh.radio import CHANNEL, RX, RX_ERROR, TX_DONE
from sily.drop import Drop, percent
from sily.radio import link_source
from sim.prng import XorShift32

# Frames per source of the statistical test. The dropped count of a
# source with rate p is binomial (N, p); the bounds are 6 standard
# deviations either side (275 frames at 30 % or 70 %): a correct wrapper
# falls outside with a probability below 1e-8 per source, whatever the
# seed. A rate 5 points off (500 frames) lands outside them but with a
# probability of about 1e-6.
N = 10000
BOUND = 275


class Inner:
    """The wrapped radio: records the actions it receives."""

    sink = None

    def __init__(self):
        self.actions = []

    def transmit(self, frame):
        self.actions.append(("tx", frame))

    def sense(self):
        self.actions.append(("sense",))


def frame(source, n=0):
    """Return a frame of link source source (header bytes 3-4), numbered n."""
    return bytes((0, 0, 0, source & 0xFF, source >> 8)) + n.to_bytes(4, "little")


def rx(source, n=0):
    return (RX, frame(source, n), -80, 5.0)


class Scripted:
    """A draw returning the values of a list, in turn."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        value = self.values[self.calls % len(self.values)]
        self.calls += 1
        return value


def _drop(rates, draw):
    inner = Inner()
    d = Drop(inner, rates, link_source, draw)
    passed = []
    d.sink = passed.append
    return inner, d, passed


class Rates(unittest.TestCase):
    def _statistics(self, draw):
        """Frames of sources 0 (30 %), 2 (70 %) and 5 (no rate) interleaved."""
        _, d, passed = _drop({0: 30, 2: 70}, draw)
        sent = []
        for n in range(N):
            for source in (0, 2, 5):
                event = rx(source, n)
                sent.append(event)
                d._on_event(event)
        return d, sent, passed

    def _check_statistics(self, d, sent, passed):
        missing = {0: 0, 2: 0, 5: 0}
        kept = set(id(e) for e in passed)
        for event in sent:
            if id(event) not in kept:
                missing[link_source(event[1])] += 1
        self.assertEqual(missing[5], 0)  # no rate: every frame passed
        self.assertTrue(abs(missing[0] - 3000) <= BOUND, missing)
        self.assertTrue(abs(missing[2] - 7000) <= BOUND, missing)
        self.assertEqual(d.dropped, {0: missing[0], 2: missing[2]})  # counted per source
        order = [e for e in sent if id(e) in kept]
        self.assertEqual(len(order), len(passed))
        for a, b in zip(order, passed):  # unchanged and in order
            self.assertIs(a, b)

    def test_rates_within_bounds_seeded(self):
        rng = XorShift32(12345)
        self._check_statistics(*self._statistics(lambda: randbelow(rng, 100)))

    def test_rates_within_bounds_with_the_board_draw(self):
        random.seed(4242)
        self._check_statistics(*self._statistics(percent))

    def test_board_draw_covers_0_to_99(self):
        random.seed(7)
        seen = set()
        for _ in range(N):
            v = percent()
            self.assertTrue(0 <= v <= 99, v)
            seen.add(v)
        # Each value is missed by N draws with probability 0.99 ** N, about 2e-44.
        self.assertEqual(len(seen), 100)

    def test_dropped_below_the_rate_only(self):
        _, d, passed = _drop({1: 30}, Scripted((29, 30, 0, 99)))
        events = [rx(1, n) for n in range(4)]
        for event in events:
            d._on_event(event)
        self.assertEqual(passed, [events[1], events[3]])
        self.assertEqual(d.dropped, {1: 2})

    def test_zero_and_hundred_percent_exact(self):
        _, d, passed = _drop({1: 0, 2: 100}, Scripted(range(100)))
        for n in range(300):  # every draw value three times for each source
            d._on_event(rx(1, n))
            d._on_event(rx(2, n))
        self.assertEqual(len(passed), 300)
        self.assertTrue(all(link_source(e[1]) == 1 for e in passed))
        self.assertEqual(d.dropped, {1: 0, 2: 300})

    def test_sources_without_a_rate_untouched(self):
        draw = Scripted((0,))  # drops whatever has a rate
        _, d, passed = _drop({1: 100}, draw)
        events = [rx(s, s) for s in (0, 2, 3, 65534)]
        for event in events:
            d._on_event(event)
        self.assertEqual(passed, events)
        self.assertEqual(draw.calls, 0)  # no draw for them
        self.assertEqual(d.dropped, {1: 0})

    def test_unreadable_source_passed_on(self):
        _, d, passed = _drop({0: 100, 3: 100}, Scripted((0,)))
        short = (RX, b"\x00\x00\x03\x00", -80, 5.0)  # source bytes cut
        d._on_event(short)
        self.assertEqual(passed, [short])
        self.assertEqual(d.dropped, {0: 0, 3: 0})


class Contract(unittest.TestCase):
    def test_other_events_always_passed(self):
        draw = Scripted((0,))
        _, d, passed = _drop({0: 100, 1: 100}, draw)
        events = [(TX_DONE, 40), (CHANNEL, True), (CHANNEL, False), (RX_ERROR, -90, -3.5)]
        for event in events:
            d._on_event(event)
        self.assertEqual(passed, events)
        self.assertEqual(draw.calls, 0)
        self.assertEqual(d.dropped, {0: 0, 1: 0})

    def test_actions_reach_the_radio(self):
        inner, d, _ = _drop({0: 100}, Scripted((0,)))
        f = frame(0)
        d.transmit(f)
        d.sense()
        self.assertEqual(inner.actions, [("tx", f), ("sense",)])
        self.assertEqual(inner.sink, d._on_event)  # the radio reports to the wrapper

    def test_minute_counts_by_address(self):
        _, d, _ = _drop({7: 100, 2: 0, 300: 100}, Scripted((0,)))
        for source, times in ((300, 3), (7, 1), (2, 4)):
            for n in range(times):
                d._on_event(rx(source, n))
        self.assertEqual(d.minute(), [[2, 0], [7, 1], [300, 3]])


if __name__ == "__main__":
    unittest.main()
