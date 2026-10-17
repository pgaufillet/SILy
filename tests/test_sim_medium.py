"""Scripted medium: delivery, asymmetry, collisions, half duplex, partitions,
carrier sense, airtime statistics and seeded loss.

Nodes are driven through the core protocol by `simcores.ScriptCore`.
"""

import unittest

from mesh.radio import CHANNEL, SENSE_ACTION, TX, TX_DONE
from sim.medium import COLLISION, CUT, HALF_DUPLEX, LOSS, OK, Medium
from sim.node import Node
from sim.prng import XorShift32
from sim.scheduler import Scheduler

from simcores import ScriptCore


def _tx(address, length=5):
    return (TX, bytes([address]) + bytes(length - 1))


class _Bench:
    """n nodes 0..n-1 on one medium; airtime(length) defaults to 100 ms."""

    def __init__(self, n, airtime=None, seed=1, **options):
        self.s = Scheduler()
        self.m = Medium(
            self.s,
            airtime or (lambda length: 100),
            XorShift32(seed),
            trace=True,
            **options,
        )
        self.ports = [self.m.attach(a) for a in range(n)]
        self.cores = [None] * n

    def script(self, address, script):
        core = ScriptCore(script)
        Node(self.s, core, self.ports[address]).start()
        self.cores[address] = core
        return core

    def outcomes(self, src, dst):
        return [t[3] for t in self.m.trace if t[1] == src and t[2] == dst]


class Delivery(unittest.TestCase):
    def test_frame_measurements_and_airtime(self):
        b = _Bench(3, airtime=lambda length: 10 + length)
        b.m.link(0, 1, rssi=-70, snr=7)
        a = b.script(0, [(100, _tx(0, 5))])
        r = b.script(1, [])
        c = b.script(2, [])
        b.s.run(1000)
        self.assertEqual(r.received(), [(115, bytes([0, 0, 0, 0, 0]), -70, 7)])
        self.assertEqual(a.of_kind(TX_DONE), [(115, (TX_DONE, 15))])
        self.assertEqual(c.events, [])

    def test_asymmetric_links(self):
        b = _Bench(3)
        b.m.link(0, 1, rssi=-70, snr=7)
        b.m.link(1, 0, rssi=-95, snr=-4)
        b.m.link(2, 0)  # one way: 0 hears 2, 2 does not hear 0
        c0 = b.script(0, [(100, _tx(0))])
        c1 = b.script(1, [(300, _tx(1))])
        c2 = b.script(2, [(500, _tx(2))])
        b.s.run(1000)
        self.assertEqual([(f[0], r, s) for _, f, r, s in c1.received()], [(0, -70, 7)])
        self.assertEqual(
            [(f[0], r, s) for _, f, r, s in c0.received()], [(1, -95, -4), (2, -80, 10)]
        )
        self.assertEqual(c2.received(), [])

    def test_frame_length_checked(self):
        b = _Bench(1)
        self.assertRaises(ValueError, b.ports[0].transmit, b"")
        self.assertRaises(ValueError, b.ports[0].transmit, bytes(256))


class Collisions(unittest.TestCase):
    def test_overlap_loses_both_touching_does_not(self):
        b = _Bench(3)
        b.m.link(0, 1)
        b.m.link(2, 1)
        b.script(0, [(100, _tx(0)), (400, _tx(0))])
        b.script(2, [(150, _tx(2)), (500, _tx(2))])
        r = b.script(1, [])
        b.s.run(1000)
        self.assertEqual([(t, f[0]) for t, f, _, _ in r.received()], [(500, 0), (600, 2)])
        self.assertEqual(b.outcomes(0, 1), [COLLISION, OK])
        self.assertEqual(b.outcomes(2, 1), [COLLISION, OK])
        self.assertEqual(b.m.stats(1).rx_collision, 2)

    def _capture(self, weak_rssi, strong_first):
        b = _Bench(3, capture_db=6)
        b.m.link(0, 1, rssi=-60)  # node 0 is the strong sender
        b.m.link(2, 1, rssi=weak_rssi)
        b.script(0, [(100 if strong_first else 150, _tx(0))])
        b.script(2, [(150 if strong_first else 100, _tx(2))])
        r = b.script(1, [])
        b.s.run(1000)
        return [f[0] for _, f, _, _ in r.received()]

    def test_capture_threshold(self):
        for strong_first in (False, True):
            self.assertEqual(self._capture(-66, strong_first), [0])
            self.assertEqual(self._capture(-63, strong_first), [])


class HalfDuplex(unittest.TestCase):
    def test_transmitting_node_does_not_receive(self):
        b = _Bench(3)
        b.m.link_both(0, 1)
        b.m.link(0, 2)
        c0 = b.script(0, [(120, _tx(0))])  # starts while receiving 1's frame
        c1 = b.script(1, [(100, _tx(1))])  # transmitting when 0's frame starts
        c2 = b.script(2, [])
        b.s.run(1000)
        self.assertEqual(c0.received(), [])
        self.assertEqual(c1.received(), [])
        self.assertEqual(b.outcomes(1, 0), [HALF_DUPLEX])
        self.assertEqual(b.outcomes(0, 1), [HALF_DUPLEX])
        self.assertEqual(len(c2.received()), 1)  # the frame itself went out
        self.assertEqual(b.m.stats(0).rx_half_duplex, 1)

    def test_transmit_while_transmitting_raises(self):
        b = _Bench(1)
        b.script(0, [(100, _tx(0)), (150, _tx(0))])
        self.assertRaises(RuntimeError, b.s.run, 1000)

    def test_sense_while_transmitting_raises(self):
        b = _Bench(1)
        b.script(0, [(100, _tx(0)), (150, SENSE_ACTION)])
        self.assertRaises(RuntimeError, b.s.run, 1000)


class Partition(unittest.TestCase):
    def test_cut_and_restore_at_given_times(self):
        b = _Bench(3)
        b.m.link_both(0, 1)
        b.m.link_both(0, 2)
        b.m.link_both(1, 2)
        times = [1000 * i for i in range(1, 11)] + [3450]
        times.sort()
        b.script(0, [(t, _tx(0)) for t in times])
        r1 = b.script(1, [])
        r2 = b.script(2, [])
        b.s.at(3500, b.m.partition, (0,), (1, 2))
        b.s.at(6500, b.m.heal, (0,), (1, 2))
        b.s.run(20000)
        sent_at = [t - 100 for t, _, _, _ in r1.received()]
        self.assertEqual(sent_at, [1000, 2000, 3000, 7000, 8000, 9000, 10000])
        self.assertEqual(len(r2.received()), 7)
        self.assertEqual(b.outcomes(0, 1).count(CUT), 1)  # 3450: cut in flight

    def test_cut_is_directed(self):
        b = _Bench(2)
        b.m.link_both(0, 1)
        b.m.cut(0, 1)
        c0 = b.script(0, [(100, _tx(0))])
        c1 = b.script(1, [(300, _tx(1))])
        b.s.run(1000)
        self.assertEqual(c1.received(), [])
        self.assertEqual(len(c0.received()), 1)
        b.m.restore(0, 1)
        self.assertTrue(b.m.get_link(0, 1).up)


class CarrierSense(unittest.TestCase):
    def test_busy_only_while_audible_transmission(self):
        b = _Bench(3, sense_ms=2)
        b.m.link(0, 1)
        b.script(0, [(100, _tx(0))])  # on air over [100, 200)
        script = [(t, SENSE_ACTION) for t in (50, 100, 150, 199, 200, 250)]
        c1 = b.script(1, script)
        c2 = b.script(2, [(150, SENSE_ACTION)])  # no link from 0: not audible
        b.s.run(1000)
        self.assertEqual(
            c1.of_kind(CHANNEL),
            [
                (52, (CHANNEL, False)),
                (102, (CHANNEL, True)),
                (152, (CHANNEL, True)),
                (201, (CHANNEL, True)),
                (202, (CHANNEL, False)),
                (252, (CHANNEL, False)),
            ],
        )
        self.assertEqual(c2.of_kind(CHANNEL), [(152, (CHANNEL, False))])
        self.assertEqual(b.m.stats(1).sense_busy, 3)

    def test_lost_frame_still_occupies_channel(self):
        b = _Bench(2)
        b.m.link(0, 1, loss=1.0)
        b.script(0, [(100, _tx(0))])
        c1 = b.script(1, [(150, SENSE_ACTION)])
        b.s.run(1000)
        self.assertEqual(c1.of_kind(CHANNEL), [(150, (CHANNEL, True))])
        self.assertEqual(c1.received(), [])


class Airtime(unittest.TestCase):
    def test_transmit_and_busy_time(self):
        b = _Bench(3, airtime=lambda length: 10 * length)
        b.m.link(0, 1)
        b.m.link(2, 1)
        b.script(0, [(100, _tx(0, 10)), (1000, _tx(0, 5))])
        b.script(2, [(150, _tx(2, 10))])
        b.script(1, [])
        b.s.run(5000)
        self.assertEqual(b.m.stats(0).tx_ms, 150)
        self.assertEqual(b.m.stats(0).tx_count, 2)
        self.assertEqual(b.m.stats(2).tx_ms, 100)
        # Busy over [100, 250) and [1000, 1050): overlaps counted once.
        self.assertEqual(b.m.stats(1).busy_ms, 200)


class SeededLoss(unittest.TestCase):
    def _pattern(self, seed):
        b = _Bench(2, seed=seed)
        b.m.link(0, 1, loss=0.3)
        b.script(0, [(1000 * i, _tx(0)) for i in range(1, 301)])
        r = b.script(1, [])
        b.s.run(400000)
        return [t for t, _, _, _ in r.received()], b.outcomes(0, 1)

    def test_same_seed_same_deliveries(self):
        self.assertEqual(self._pattern(5), self._pattern(5))

    def test_different_seed_different_deliveries(self):
        self.assertNotEqual(self._pattern(5)[0], self._pattern(6)[0])

    def test_loss_rate_follows_link(self):
        delivered, outcomes = self._pattern(5)
        self.assertEqual(len(outcomes), 300)
        self.assertEqual(outcomes.count(OK), len(delivered))
        self.assertTrue(60 < outcomes.count(LOSS) < 120, outcomes.count(LOSS))
