"""DODAG on a scripted line (MESH_DESIGN §9 Trickle, DODAG version, loop freedom; MP10, MP14).

Line root — A — B — C — D (each board hears its line neighbours only) runs
real link engines with their `Dodag` on `sim.net`, at SF7 (Imin 1.7 s) with a
presence period of 10 s. Until presence reports exist, each node sends a
stand-in: one class 0 upward message per presence period through its
preferred parent (relays forward it), which keeps its parents hearing it.
"""

import unittest

from mesh.dodag import Dodag, DodagInfo
from mesh.frame import RANK_INFINITE, ROOT
from mesh.ports import DODAG_INFO
from mesh.profile import EU_869
from sim.net import Network

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
PERIOD = 10000
REPORT = 200  # port of the stand-in presence
A, B, C, D = 0x00A1, 0x00B1, 0x00C1, 0x00D1


def _routing(engine):
    d = Dodag(engine, EU_869, INFO)
    engine.next_hop = d.next_hop
    engine.reroute = d.parents.reroute
    return d


class Line:
    def __init__(self, seed):
        net = Network(seed, _KEY)
        self.net = net
        self.names = (ROOT, A, B, C, D)
        self.boards = [net.add(n, routing=_routing) for n in self.names]
        for a, b in zip(self.names, self.names[1:]):
            net.link_both(a, b)
        self.root = self.boards[0]
        self.nodes = self.boards[1:]
        self.root.listen(REPORT)
        for i, b in enumerate(self.nodes):
            first = 1000 + 1900 * i
            net.at(first, self._report, b)
        self.imin = self.root.routing.imin_ms

    def _report(self, b):
        if b.engine is not None and b.routing.attached:
            b.submit(REPORT, 0, ROOT, b"here")
        period = b.engine.neighbours.period_ms if b.engine is not None else PERIOD
        self.net.at(self.net.now + period, self._report, b)

    def run(self, until):
        self.net.run(until)

    def attached(self):
        return [b.routing is not None and b.routing.attached for b in self.nodes]

    def ranks(self):
        return [b.engine.rank for b in self.nodes]

    def port9(self, b):
        return b.engine.counters.tx_messages[DODAG_INFO]

    def steady(self, limit=300000):
        """Run until every board's Trickle timer reaches Imax."""
        while not all(b.routing.trickle.at_imax() for b in self.boards):
            if self.net.now > limit:
                raise AssertionError("Imax not reached")
            self.run(self.net.now + 1000)


class Forming(unittest.TestCase):
    def test_forms_and_settles(self):
        line = Line(3)
        line.run(12 * line.imin)
        self.assertEqual(line.attached(), [True] * 4)
        self.assertEqual(line.ranks(), [8, 16, 24, 32])
        self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])
        line.steady()
        # Steady state: nodes advertise nothing, the root beacons once per period.
        start = line.net.now
        sent = [line.port9(b) for b in line.boards]
        line.run(start + 6 * PERIOD)
        after = [line.port9(b) for b in line.boards]
        self.assertEqual(after[1:], sent[1:])
        self.assertTrue(5 <= after[0] - sent[0] <= 7, (sent[0], after[0]))
        self.assertEqual([b.routing.counters.poisons for b in line.nodes], [0] * 4)
        self.assertEqual(line.attached(), [True] * 4)
        # The stand-in reports of every node reach the root.
        origins = set(o for o, _ in line.root.inbox_from(REPORT))
        self.assertEqual(origins, set((A, B, C, D)))


class GlobalRepair(unittest.TestCase):
    def test_new_version_crosses_one_imin_per_hop(self):
        line = Line(5)
        line.run(12 * line.imin)
        line.steady()
        start = line.net.now
        info = DodagInfo(0, 12, 60, 32, 48, EU_869)
        line.root.routing.set_version(1, start, info)
        line.root.node.wake()
        adopted = {}

        def watch():
            for hop, b in enumerate(line.nodes, 1):
                if b.routing.version == 1 and hop not in adopted:
                    adopted[hop] = line.net.now - start
            if len(adopted) < 4:
                line.net.at(line.net.now + 10, watch)

        line.net.at(start, watch)
        line.run(start + 6 * line.imin)
        self.assertEqual(sorted(adopted), [1, 2, 3, 4])
        for hop in range(1, 5):
            # One Imin per hop, plus the frame and channel access of each hop.
            self.assertTrue(adopted[hop] <= hop * (line.imin + 150), adopted)
        self.assertTrue(adopted[1] < adopted[2] < adopted[3] < adopted[4], adopted)
        # Parameters carried; no detach, ranks rebuilt as before.
        self.assertEqual([b.engine.neighbours.period_ms for b in line.boards], [12000] * 5)
        self.assertEqual([b.routing.parents.max_increase for b in line.nodes], [48] * 4)
        self.assertEqual([b.engine.dodag_version for b in line.boards], [1] * 5)
        self.assertEqual([b.routing.counters.poisons for b in line.nodes], [0] * 4)
        self.assertEqual(line.ranks(), [8, 16, 24, 32])
        line.steady()
        start = line.net.now
        sent = [line.port9(b) for b in line.nodes]
        line.run(start + 4 * 12000)
        self.assertEqual([line.port9(b) for b in line.nodes], sent)


class DetachRejoin(unittest.TestCase):
    def test_relay_lost_then_back(self):
        line = Line(7)
        line.run(12 * line.imin)
        line.steady()
        b_board = line.nodes[1]
        c, d = line.nodes[2], line.nodes[3]
        lost = line.net.now
        b_board.power_loss(off_ms=4 * PERIOD)
        line.run(lost + 2 * PERIOD + 3 * line.imin)
        # C lost its only parent: poisoned; D, whose only parent is C, too.
        self.assertEqual((c.routing.counters.poisons, d.routing.counters.poisons), (1, 1))
        self.assertEqual((c.engine.rank, d.engine.rank), (RANK_INFINITE, RANK_INFINITE))
        self.assertTrue(c.routing.counters.solicits >= 1)
        self.assertEqual(line.attached()[0], True)
        # B boots, rejoins through A; C and D rejoin behind it.
        line.run(lost + 4 * PERIOD + 2 * PERIOD)
        self.assertEqual(line.attached(), [True] * 4)
        self.assertEqual(line.ranks(), [8, 16, 24, 32])
