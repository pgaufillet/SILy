"""Upward forwarding and data-path validation (MESH_DESIGN §9 loop freedom, §11, §12).

Line root — R — N of real link engines with their routing (`sim.net`, SF7,
presence period 10 s); frames are injected through N's engine and checked
at R and at the root.
"""

import unittest

from mesh.dodag import DodagInfo
from mesh.frame import FIRST_HOP, RANK_INFINITE, ROOT, UPWARD, Header, Message
from mesh.link import NOT_TAKEN, QUEUED
from mesh.ports import PRESENCE, TOPOLOGY
from mesh.profile import EU_869
from mesh.reports import RANK_ERRORS
from mesh.routing import Routing
from sim.net import Network

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
PERIOD = 10000
R = 0x0010
N = 0x00A1
X = 0x00B7  # an origin further down, not on the line
APP = 20


class Line:
    def __init__(self, seed):
        net = Network(seed, _KEY)
        self.net = net
        self.reports = []
        self.root = net.add(ROOT, routing=self._make)
        self.relay = net.add(R, routing=self._make)
        self.node = net.add(N, routing=self._make)
        net.link_both(ROOT, R)
        net.link_both(R, N)
        self.root.listen(APP)
        net.run(30000)
        assert [b.routing.parents.preferred for b in (self.relay, self.node)] == [ROOT, R]

    def _make(self, engine):
        r = Routing(engine, EU_869, INFO)
        if engine.is_root:
            r.on_report = lambda port, origin, rep, h: self.reports.append(
                (port, origin, rep, self.net.now)
            )
        return r

    def presences(self, origin, since=0):
        return [
            rep
            for port, o, rep, t in self.reports
            if port == PRESENCE and o == origin and t > since
        ]

    def send_from_node(self, msg, rank=None):
        """Queue msg on N towards R, N advertising rank (its own if None); step N."""
        e = self.node.engine
        if rank is not None:
            e.rank = rank
        e.send(msg, R)
        self.node.node.wake()

    def run(self, ms):
        self.net.run(self.net.now + ms)


class DataPathValidation(unittest.TestCase):
    def test_consistent_ranks_forward(self):
        line = Line(1)
        line.send_from_node(Message(FIRST_HOP, 1, APP, b"up", seq=1))
        line.run(5000)
        self.assertEqual(line.root.inbox_from(APP), [(N, b"up")])
        r = line.relay.routing
        self.assertEqual((r.dodag.counters.rank_errors, r.counters.loops), (0, 0))

    def test_rank_error_drops_class0_resets_trickle_and_flags(self):
        line = Line(2)
        relay = line.relay.routing
        line.relay.routing.dodag.trickle.stop()  # at Imax, or stopped: a reset shows
        # N advertises a rank not higher than R's (8): an inconsistency.
        start = line.net.now
        line.send_from_node(Message(FIRST_HOP, 0, APP, b"dg"), rank=8)
        line.run(200)
        self.assertEqual(relay.dodag.counters.rank_errors, 1)
        self.assertEqual(relay.counters.rank_dropped, 1)
        self.assertEqual(relay.dodag.trickle.interval, relay.dodag.imin_ms)
        line.run(3000)
        self.assertEqual(line.root.inbox_from(APP), [])
        # The relay's next presence reports the rank error (§11 bit 4), once.
        line.run(PERIOD + 1500)
        flags = [p.flags for p in line.presences(R, start)]
        self.assertTrue(len(flags) >= 2, flags)
        self.assertEqual((flags[0], flags[1]), (RANK_ERRORS, 0))

    def test_rank_error_forwards_class1(self):
        line = Line(3)
        line.send_from_node(Message(FIRST_HOP, 1, APP, b"c1", seq=7), rank=4)
        line.run(5000)
        self.assertEqual(line.root.inbox_from(APP), [(N, b"c1")])
        r = line.relay.routing
        self.assertEqual((r.dodag.counters.rank_errors, r.counters.loops), (1, 0))
        self.assertEqual(line.relay.engine.counters.duplicates, 0)

    def test_other_version_is_not_compared(self):
        line = Line(4)
        e = line.node.engine
        e.dodag_version = (e.dodag_version - 1) & 0x0F  # an older version
        line.send_from_node(Message(FIRST_HOP, 0, APP, b"old"), rank=4)
        line.run(3000)
        self.assertEqual(line.relay.routing.dodag.counters.rank_errors, 0)
        self.assertEqual(line.root.inbox_from(APP), [(N, b"old")])

    def test_duplicate_with_rank_error_is_a_loop(self):
        line = Line(5)
        msg = Message(UPWARD, 1, APP, b"m", origin=X, seq=40)
        line.send_from_node(msg)
        line.run(3000)
        self.assertEqual(line.root.inbox_from(APP), [(X, b"m")])
        relay = line.relay
        # The same message again from a consistent sender: a retransmission.
        line.send_from_node(Message(UPWARD, 1, APP, b"m", origin=X, seq=40))
        line.run(3000)
        self.assertEqual(relay.engine.counters.duplicates, 1)
        self.assertEqual(relay.routing.counters.loops, 0)
        # Again from a sender whose rank is not higher: it came back, a loop.
        line.send_from_node(Message(UPWARD, 1, APP, b"m", origin=X, seq=40), rank=8)
        line.run(3000)
        self.assertEqual(relay.engine.counters.duplicates, 2)
        self.assertEqual(relay.routing.counters.loops, 1)
        self.assertEqual(relay.routing.dodag.counters.rank_errors, 1)
        # Dropped (acknowledged): the root got it once, N holds nothing.
        self.assertEqual(line.root.inbox_from(APP), [(X, b"m")])
        self.assertEqual(len(line.node.engine.queue), 0)

    def test_own_message_back_is_a_loop(self):
        line = Line(6)
        line.send_from_node(Message(UPWARD, 1, APP, b"mine", origin=R, seq=3))
        line.run(5000)
        r = line.relay.routing
        self.assertEqual(r.counters.loops, 1)
        self.assertEqual(line.root.inbox_from(APP), [])
        # Taken: acknowledged, so its sender stops.
        self.assertEqual(len(line.node.engine.queue), 0)
        self.assertEqual(line.node.engine.counters.retries, 0)


class Forwarding(unittest.TestCase):
    def _header(self, line):
        return Header(R, N, 1, 16, line.relay.engine.dodag_version)

    def test_reports_replace_older_ones_of_the_same_origin(self):
        line = Line(7)
        e = line.relay.engine
        h = self._header(line)
        now = line.net.now
        base = len(e.queue)
        p1 = Message(UPWARD, 0, PRESENCE, b"\x10\x00\x00\x01", origin=X)
        p2 = Message(UPWARD, 0, PRESENCE, b"\x10\x00\x00\x02", origin=X)
        t1 = Message(UPWARD, 0, TOPOLOGY, b"\x00", origin=X)
        a1 = Message(UPWARD, 0, APP, b"a", origin=X)
        a2 = Message(UPWARD, 0, APP, b"b", origin=X)
        for m in (p1, p2, t1, a1, a2):
            self.assertEqual(e.forward(m, h, now), QUEUED)
        # Presence replaced; topology and application messages kept.
        self.assertEqual(len(e.queue), base + 4)
        queued = [m for m in e.queue._msg if m is not None and m.origin == X]
        self.assertEqual(
            sorted(bytes(m.payload) for m in queued if m.port == PRESENCE), [b"\x10\x00\x00\x02"]
        )

    def test_root_decodes_reports_and_counts_malformed(self):
        line = Line(8)
        # A first-hop presence without flags: the relay restores the parent,
        # the root finds it too short.
        line.send_from_node(Message(FIRST_HOP, 0, PRESENCE, b""))
        line.run(3000)
        self.assertEqual(line.root.routing.counters.malformed, 1)
        self.assertTrue(line.root.routing.counters.reports >= 4)

    def test_no_route_drops_class0(self):
        line = Line(9)
        line.relay.routing.parents.detach(line.net.now)
        line.relay.routing.parents.events()
        h = self._header(line)
        e = line.relay.engine
        self.assertEqual(
            e.forward(Message(UPWARD, 0, APP, b"x", origin=X), h, line.net.now), NOT_TAKEN
        )
        self.assertEqual(e.counters.no_route, 1)


class LocalRepair(unittest.TestCase):
    def test_attempts_exhausted_on_the_only_parent_poisons_at_once(self):
        line = Line(10)
        n = line.node
        reroutes = []
        original = n.routing.parents.next_parent

        def next_parent(hop, now=None):
            reroutes.append(line.net.now)
            return original(hop, now)

        n.routing.parents.next_parent = next_parent
        line.net.medium.cut(R, N)  # N no longer hears R: no acknowledgement
        start = line.net.now
        adverts = n.routing.dodag.counters.adverts
        n.submit(APP, 1, ROOT, b"lost")
        line.run(3000)
        self.assertEqual(len(reroutes), 1)
        # Applied in the step of the reroute: rank 255 in the header, the
        # poison on air within the desynchronisation delay, the message held.
        self.assertEqual((n.engine.rank, n.routing.parents.preferred), (RANK_INFINITE, None))
        d = n.routing.dodag
        self.assertEqual(d.counters.poisons, 1)
        self.assertEqual(d.counters.adverts, adverts + 1)
        self.assertEqual(n.engine.reliable.held(), 1)
        self.assertTrue(reroutes[0] - start < 3000)
