"""Link measurements per source (sily.tap): pass-through, figures, bound."""

import unittest

from test_sily_stack import KEY, FakeHw, Inbox, peer_frame, run_for

from mesh.frame import SOURCE_OFFSET
from mesh.radio import CHANNEL, RX, RX_ERROR, TX_DONE
from sily.stack import Stack
from sily.tap import CAPACITY, Tap


class Inner:
    """A radio recording the actions it is given."""

    sink = None

    def __init__(self):
        self.sent = []
        self.senses = 0

    def transmit(self, frame):
        self.sent.append(frame)

    def sense(self):
        self.senses += 1


def frame_from(source):
    f = bytearray(SOURCE_OFFSET + 8)
    f[SOURCE_OFFSET] = source & 0xFF
    f[SOURCE_OFFSET + 1] = source >> 8
    return bytes(f)


class Taps(unittest.TestCase):
    def setUp(self):
        self.inner = Inner()
        self.tap = Tap(self.inner)
        self.events = []
        self.tap.sink = self.events.append

    def heard(self, source, rssi, snr):
        self.inner.sink((RX, frame_from(source), rssi, snr))

    def test_events_pass_unchanged(self):
        events = [
            (RX, frame_from(3), -80, 7.25),
            (RX_ERROR, -120, -11.5),
            (TX_DONE, 41),
            (CHANNEL, True),
            (RX, b"\x01", -90, 1.0),  # too short for a source
        ]
        for e in events:
            self.inner.sink(e)
        self.assertEqual(len(self.events), len(events))
        for got, sent in zip(self.events, events):
            self.assertIs(got, sent)
        self.assertEqual(list(self.tap.sources), [3])

    def test_actions_pass_to_the_radio(self):
        self.tap.transmit(b"abc")
        self.tap.sense()
        self.assertEqual((self.inner.sent, self.inner.senses), ([b"abc"], 1))

    def test_figures_per_source(self):
        self.heard(3, -80, 7.25)
        self.heard(3, -85, 6.5)
        self.heard(3, -84, -2.25)
        self.heard(9, -110, -12.0)
        # frames, last RSSI, mean RSSI (-83), mean SNR in 0.1 dB (3.833 dB)
        self.assertEqual(self.tap.figures(3), [3, -84, -83, 38])
        self.assertEqual(self.tap.figures(9), [1, -110, -110, -120])
        self.assertIsNone(self.tap.figures(4))

    def test_restart_keeps_the_frames_and_last_values(self):
        self.heard(3, -80, 7.25)
        self.tap.restart()
        self.assertEqual(self.tap.figures(3), [1, -80, None, None])
        self.heard(3, -90, 1.0)
        self.assertEqual(self.tap.figures(3), [2, -90, -90, 10])

    def test_the_65th_source_overflows(self):
        for source in range(1, CAPACITY + 1):
            self.heard(source, -90, 5.0)
        self.assertEqual((len(self.tap.sources), self.tap.overflow), (CAPACITY, 0))
        self.heard(CAPACITY + 1, -90, 5.0)
        self.heard(CAPACITY + 1, -90, 5.0)
        self.heard(1, -70, 5.0)  # known sources still counted
        self.assertEqual((len(self.tap.sources), self.tap.overflow), (CAPACITY, 2))
        self.assertIsNone(self.tap.figures(CAPACITY + 1))
        self.assertEqual(self.tap.figures(1)[:2], [2, -70])
        self.assertEqual(len(self.events), CAPACITY + 3)  # every event passed on


class InTheStack(unittest.TestCase):
    def test_peer_frame_counted_by_the_tap_and_the_table(self):
        taps = []

        def wrap(stack, radio):
            taps.append(Tap(radio))
            return taps[0]

        s = Stack(FakeHw(), 3, KEY, 7, routing=False, wrap=wrap)
        inbox = Inbox()
        s.engine.bind(20, inbox)
        frame = peer_frame(1, 3, b"punch")
        run_for(s, 30, lambda s: s.adapter.on_receive(frame, -70, 6.5))
        self.assertEqual(inbox.messages, [(1, b"punch")])
        self.assertEqual(taps[0].figures(1), [1, -70, -70, 65])
        self.assertIsNotNone(s.engine.neighbours.get(1))
