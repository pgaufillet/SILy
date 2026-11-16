"""Turnaround of the gateway's immediate replies (sily.turn): matching, figures, bound.

The wrapper is driven alone: a scripted inner radio whose transmissions set
the adapter's SetTx timestamp, the RxDone timestamp set before each frame
is reported, the link engine's reply counter moved by hand. The wiring in
the application is in ``test_sily_app``.
"""

import unittest
from time import ticks_add

from mesh.frame import BROADCAST, DEST_OFFSET, HEADER_SIZE, ROOT, SOURCE_OFFSET
from mesh.radio import CHANNEL, RX, RX_ERROR, TX_DONE
from sily.turn import SIZE, Turn


class Adapter:
    """The two timestamps of ``board.sx1262.Sx1262`` the wrapper reads (µs)."""

    def __init__(self):
        self._irq_us = 0
        self._tx_us = 0


class Counters:
    replies = 0


class Inner:
    """A radio recording its actions; a transmission stamps the adapter's SetTx."""

    sink = None

    def __init__(self, adapter):
        self.adapter = adapter
        self.sent = []
        self.senses = 0
        self.start_us = 0  # the SetTx timestamp of the next transmission

    def transmit(self, frame):
        self.sent.append(frame)
        self.adapter._tx_us = self.start_us

    def sense(self):
        self.senses += 1


def frame(dest, source, size=HEADER_SIZE + 6):
    f = bytearray(size)
    f[DEST_OFFSET] = dest & 0xFF
    f[DEST_OFFSET + 1] = dest >> 8
    f[SOURCE_OFFSET] = source & 0xFF
    f[SOURCE_OFFSET + 1] = source >> 8
    return bytes(f)


class Turns(unittest.TestCase):
    def setUp(self, size=SIZE):
        self.adapter = Adapter()
        self.inner = Inner(self.adapter)
        self.counters = Counters()
        self.turn = Turn(self.inner, self.adapter, self.counters, size)
        self.events = []
        self.turn.sink = self.events.append

    def received(self, data, irq_us):
        self.adapter._irq_us = irq_us
        self.inner.sink((RX, data, -80, 5.0))

    def sent(self, dest, start_us, reply=False):
        if reply:
            self.counters.replies += 1
        self.inner.start_us = start_us
        self.turn.transmit(frame(dest, ROOT))

    def answer(self, source, irq_us, start_us, dest=None):
        """A frame of source to the root at irq_us, its reply started at start_us."""
        self.received(frame(ROOT, source), irq_us)
        self.sent(source if dest is None else dest, start_us, True)

    def test_events_and_actions_pass_unchanged(self):
        events = [
            (RX, frame(ROOT, 3), -80, 7.25),
            (RX_ERROR, -120, -11.5),
            (TX_DONE, 41),
            (CHANNEL, True),
            (RX, b"\x01", -90, 1.0),  # too short for a header
        ]
        for e in events:
            self.inner.sink(e)
        self.assertEqual(len(self.events), len(events))
        for got, sent in zip(self.events, events):
            self.assertIs(got, sent)
        f = frame(3, ROOT)
        self.turn.transmit(f)
        self.turn.sense()
        self.assertEqual((self.inner.sent, self.inner.senses), ([f], 1))

    def test_reply_timed_from_rxdone_to_settx(self):
        self.answer(3, 1000, 22300)
        self.assertEqual((self.turn.n, self.turn.samples[0]), (1, 21300))
        self.assertEqual(self.turn.minute(), [1, 213, 213, 213])

    def test_timestamps_across_the_wrap(self):
        start = 5000
        self.answer(3, ticks_add(start, -21000), start)
        self.assertEqual(self.turn.minute(), [1, 210, 210, 210])

    def test_frames_to_others_and_short_frames_start_nothing(self):
        self.received(frame(5, 3), 1000)  # overheard: to node 5
        self.received(frame(ROOT, 3)[: HEADER_SIZE - 1], 1000)
        self.sent(3, 20000, True)
        self.sent(BROADCAST, 21000, True)
        self.assertEqual(self.turn.n, 0)
        self.assertIsNone(self.turn.minute())

    def test_other_frames_leave_the_reply_pending(self):
        self.received(frame(ROOT, 3), 1000)
        self.sent(3, 4000)  # to the source, no reply counted: not the reply
        self.sent(7, 5000, True)  # another destination
        self.assertEqual(self.turn.n, 0)
        self.sent(3, 24000)  # the reply counted before the frame to 7
        self.assertEqual((self.turn.n, self.turn.samples[0]), (1, 23000))

    def test_reply_to_every_neighbour(self):
        self.answer(3, 1000, 20000, BROADCAST)  # entries of several sources
        self.assertEqual((self.turn.n, self.turn.samples[0]), (1, 19000))

    def test_one_sample_per_reply(self):
        self.answer(3, 1000, 20000)
        self.sent(3, 90000, True)
        self.assertEqual(self.turn.n, 1)

    def test_an_unanswered_frame_gives_way_to_the_next(self):
        self.received(frame(ROOT, 3), 1000)
        self.answer(4, 9000, 30000)
        self.sent(3, 40000, True)
        self.assertEqual((self.turn.n, self.turn.samples[0]), (1, 21000))

    def test_minute_figures_nearest_rank_in_tenths(self):
        # 1 to 10 ms, out of order; the 10th rounds 10.0049 ms down.
        for i, ms in enumerate((7, 2, 9, 1, 10, 4, 6, 3, 8, 5)):
            us = ms * 1000 + (49 if ms == 10 else 0)
            self.answer(3, 100000 * i, 100000 * i + us)
        self.assertEqual(self.turn.minute(), [10, 50, 90, 100])
        self.answer(3, 0, 21350)  # rounds up
        self.answer(3, 0, 21349)
        self.assertEqual(self.turn.minute(), [2, 213, 214, 214])

    def test_minute_restarts(self):
        self.assertIsNone(self.turn.minute())
        self.answer(3, 0, 30000)
        self.assertEqual(self.turn.minute(), [1, 300, 300, 300])
        self.assertIsNone(self.turn.minute())
        self.assertIsNone(self.turn.last)
        self.answer(3, 0, 20000)  # a lower maximum than the previous period's
        self.assertEqual(self.turn.minute(), [1, 200, 200, 200])

    def test_samples_beyond_the_bound(self):
        self.setUp(size=4)
        for ms in (20, 21, 22, 23, 50, 19):  # the 5th and 6th beyond the buffer
            self.answer(3, 0, ms * 1000)
        self.assertEqual(len(self.turn.samples), 4)
        self.assertEqual(list(self.turn.samples), [20000, 21000, 22000, 23000])
        # n and the maximum count every reply, the ranks the first four only.
        self.assertEqual(self.turn.minute(), [6, 210, 230, 500])
        self.answer(3, 0, 18000)  # the buffer free again
        self.assertEqual(self.turn.minute(), [1, 180, 180, 180])

    def test_figures_of_the_last_minute(self):
        none = {"n": 0, "median_ms": None, "p90_ms": None, "max_ms": None}
        self.assertEqual(self.turn.figures(), none)
        self.answer(3, 0, 21300)
        self.answer(3, 0, 38000)
        self.assertEqual(self.turn.figures(), none)  # until the minute record
        self.turn.minute()
        self.assertEqual(
            self.turn.figures(), {"n": 2, "median_ms": 21.3, "p90_ms": 38.0, "max_ms": 38.0}
        )
        self.turn.minute()
        self.assertEqual(self.turn.figures(), none)


if __name__ == "__main__":
    unittest.main()
