"""Core <-> runner protocol, radio contract helpers and neighbour filter."""

import unittest

from mesh.core import Core
from mesh.platform import randbelow
from mesh.radio import (
    CHANNEL,
    RX,
    SENSE,
    SENSE_ACTION,
    TX,
    TX_DONE,
    NeighbourFilter,
    Radio,
    execute,
)

from simcores import source_of


class _Counter(Core):
    """Emits one TX per event and wants a step every 10 ms."""

    def __init__(self):
        super().__init__()
        self.handled = []

    def handle(self, now, event):
        self.handled.append((now, event))
        if event is not None:
            self.emit((TX, b"x"))
        self.set_deadline(now + 10)


class CoreProtocol(unittest.TestCase):
    def test_actions_drained_per_step(self):
        core = _Counter()
        self.assertEqual(list(core.step(0, None)), [])
        self.assertEqual(core.deadline(), 10)
        actions = core.step(3, (CHANNEL, False))
        self.assertEqual(list(actions), [(TX, b"x")])
        self.assertEqual(list(core.step(3, None)), [])
        self.assertEqual(core.handled, [(0, None), (3, (CHANNEL, False)), (3, None)])

    def test_time_must_not_go_backwards(self):
        core = _Counter()
        core.step(5, None)
        self.assertRaises(ValueError, core.step, 4, None)
        core.step(5, None)  # equal time is fine

    def test_base_requires_handle(self):
        self.assertRaises(NotImplementedError, Core().step, 0, None)


class _RecordingRadio(Radio):
    def __init__(self):
        self.calls = []

    def transmit(self, frame):
        self.calls.append(("tx", frame))

    def sense(self):
        self.calls.append(("sense",))


class Execute(unittest.TestCase):
    def test_dispatch(self):
        radio = _RecordingRadio()
        self.assertTrue(execute(radio, (TX, b"ab")))
        self.assertTrue(execute(radio, SENSE_ACTION))
        self.assertFalse(execute(radio, (99,)))
        self.assertEqual(radio.calls, [("tx", b"ab"), ("sense",)])

    def test_actions_and_events_are_disjoint(self):
        self.assertFalse(set((TX, SENSE)) & set((RX, TX_DONE, CHANNEL)))


class _Scripted:
    """Random source returning scripted values."""

    def __init__(self, values):
        self.values = list(values)
        self.bits = []

    def getrandbits(self, bits):
        self.bits.append(bits)
        return self.values.pop(0)


class RandBelow(unittest.TestCase):
    def test_rejection_and_width(self):
        rng = _Scripted([6, 7, 5, 2])
        self.assertEqual(randbelow(rng, 6), 5)  # 6 and 7 rejected
        self.assertEqual(rng.bits, [3, 3, 3])

    def test_power_of_two(self):
        rng = _Scripted([3])
        self.assertEqual(randbelow(rng, 4), 3)
        self.assertEqual(rng.bits, [2])

    def test_bounds(self):
        rng = _Scripted([])
        self.assertEqual(randbelow(rng, 1), 0)
        self.assertRaises(ValueError, randbelow, rng, 0)
        self.assertRaises(ValueError, randbelow, rng, (1 << 32) + 1)


class Filter(unittest.TestCase):
    def setUp(self):
        self.inner = _RecordingRadio()
        self.filter = NeighbourFilter(self.inner, set((1, 2)), source_of)
        self.events = []
        self.filter.sink = self.events.append

    def test_rx_filtered_by_source(self):
        self.inner.sink((RX, b"\x01a", -70, 5))
        self.inner.sink((RX, b"\x03a", -70, 5))
        self.inner.sink((RX, b"\x02b", -71, 4))
        self.assertEqual(self.events, [(RX, b"\x01a", -70, 5), (RX, b"\x02b", -71, 4)])
        self.assertEqual(self.filter.filtered, 1)

    def test_unreadable_source_ignored(self):
        self.inner.sink((RX, b"", -70, 5))
        self.assertEqual(self.events, [])
        self.assertEqual(self.filter.filtered, 1)

    def test_other_events_pass(self):
        self.inner.sink((TX_DONE, 40))
        self.inner.sink((CHANNEL, True))
        self.assertEqual(self.events, [(TX_DONE, 40), (CHANNEL, True)])

    def test_actions_pass_through(self):
        self.filter.transmit(b"\x09")
        self.filter.sense()
        self.assertEqual(self.inner.calls, [("tx", b"\x09"), ("sense",)])

    def test_allowed_set_changed_at_run_time(self):
        self.filter.allowed.add(3)
        self.inner.sink((RX, b"\x03a", -70, 5))
        self.assertEqual(len(self.events), 1)
