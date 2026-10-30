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

    def test_duplicate_held_with_rank_error_is_a_loop(self):
        line = Line(5)
        relay = line.relay
        line.net.medium.cut(R, ROOT)  # R keeps its copy: no acknowledgement
        line.send_from_node(Message(UPWARD, 1, APP, b"m", origin=X, seq=40))
        line.run(1000)
        self.assertEqual(relay.engine.reliable.pending(), 1)
        # The same message again from a consistent sender: a retransmission.
        line.send_from_node(Message(UPWARD, 1, APP, b"m", origin=X, seq=40))
        line.run(500)
        self.assertEqual(relay.engine.counters.duplicates, 1)
        self.assertEqual(relay.routing.counters.loops, 0)
        # Again from a sender whose rank is not higher: it came back, a loop.
        line.send_from_node(Message(UPWARD, 1, APP, b"m", origin=X, seq=40), rank=8)
        line.run(500)
        self.assertEqual(relay.engine.counters.duplicates, 2)
        self.assertEqual(relay.routing.counters.loops, 1)
        self.assertEqual(relay.routing.dodag.counters.rank_errors, 1)
        # Dropped (acknowledged): N holds nothing, R one copy; the root gets
        # it once the link is back.
        self.assertEqual((len(line.node.engine.queue), relay.engine.counters.retaken), (0, 0))
        line.net.medium.restore(R, ROOT)
        line.run(10000)
        self.assertEqual(line.root.inbox_from(APP), [(X, b"m")])

    def test_duplicate_handed_over_is_taken_again(self):
        # Custody (§12): R no longer holds a message it forwarded; a
        # duplicate (a retransmission after a lost acknowledgement, or a
        # loop) is forwarded again and acknowledged; the root filters it.
        line = Line(11)
        msg = Message(UPWARD, 1, APP, b"m", origin=X, seq=41)
        line.send_from_node(msg)
        line.run(3000)
        self.assertEqual(line.root.inbox_from(APP), [(X, b"m")])
        relay = line.relay
        line.send_from_node(Message(UPWARD, 1, APP, b"m", origin=X, seq=41))
        line.run(3000)
        c = relay.engine.counters
        self.assertEqual((c.duplicates, c.retaken), (0, 1))
        self.assertEqual(line.node.engine.counters.acks_explicit, 1)
        self.assertEqual((len(line.node.engine.queue), line.node.engine.counters.retries), (0, 0))
        self.assertEqual(line.root.inbox_from(APP), [(X, b"m")])
        self.assertEqual(line.root.engine.counters.duplicates, 1)

    def test_own_message_back_while_held_is_dropped(self):
        line = Line(6)
        relay = line.relay
        line.net.medium.cut(R, ROOT)
        seq = relay.submit(APP, 1, ROOT, b"mine")
        line.run(1000)
        self.assertEqual(relay.engine.reliable.pending(), 1)
        line.send_from_node(Message(UPWARD, 1, APP, b"mine", origin=R, seq=seq))
        line.run(500)
        self.assertEqual(relay.routing.counters.loops, 1)
        rel = relay.engine.reliable
        self.assertEqual(rel.pending() + rel.held(), 1)  # not taken again
        # Taken: acknowledged, so its sender stops; R's own copy goes on.
        self.assertEqual(len(line.node.engine.queue), 0)
        self.assertEqual(line.node.engine.counters.retries, 0)
        line.net.medium.restore(R, ROOT)
        line.run(10000)
        self.assertEqual(line.root.inbox_from(APP), [(R, b"mine")])

    def test_own_message_back_after_handed_over_is_taken_again(self):
        # R holds no copy of its message (sequence 3): back from a loop, it
        # is forwarded again (custody, §12), counted as a loop.
        line = Line(6)
        line.send_from_node(Message(UPWARD, 1, APP, b"mine", origin=R, seq=3))
        line.run(5000)
        r = line.relay.routing
        self.assertEqual(r.counters.loops, 1)
        self.assertEqual(line.root.inbox_from(APP), [(R, b"mine")])
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
        # 3 attempts, the last 2 each after a retry delay (§12): 64, 128 slots.
        limit = 3000 + 6 * n.engine.reliable.retry_ms
        while not reroutes and line.net.now - start < limit:
            line.run(100)
        line.run(800)  # the desynchronisation delay
        self.assertEqual(len(reroutes), 1)
        # Applied in the step of the reroute: rank 255 in the header, the
        # poison on air within the desynchronisation delay, the message held.
        self.assertEqual((n.engine.rank, n.routing.parents.preferred), (RANK_INFINITE, None))
        d = n.routing.dodag
        self.assertEqual(d.counters.poisons, 1)
        self.assertEqual(d.counters.adverts, adverts + 1)
        self.assertEqual(n.engine.reliable.held(), 1)
        self.assertTrue(reroutes[0] - start < limit)

    def test_detach_holds_waiting_copies(self):
        # N's message waits for R's acknowledgement when R poisons: N
        # detaches and holds it, instead of retrying it towards a parent of
        # rank 255 (§9 detach, §12 holding during repair).
        line = Line(12)
        n = line.node
        line.net.medium.cut(N, R)  # R never gets it
        n.submit(APP, 1, ROOT, b"w")
        line.run(300)
        rel = n.engine.reliable
        self.assertEqual((rel.pending(), rel.held()), (1, 0))
        line.relay.routing.parents.detach(line.net.now)
        line.relay.node.wake()
        line.run(rel.timeout_ms(R) + 1000)  # its timeout passed, within the hold
        self.assertEqual(n.engine.rank, RANK_INFINITE)
        self.assertEqual((rel.pending(), rel.held(), n.engine.counters.retries), (0, 1, 0))

    def test_root_kept_after_exhausted_attempts_until_its_link_is_ineligible(self):
        # The root no longer hears R, which still hears its beacons: R's
        # attempts towards the root all fail. While the link is eligible the
        # root, R's only candidate, is kept (§12 upward failure): no detach,
        # fresh attempts; the failed samples then make the link ineligible
        # and R leaves the root.
        line = Line(13)
        r = line.relay
        parents = r.routing.parents
        reroutes = []
        original = parents.next_parent

        def next_parent(hop, now=None):
            hop = original(hop, now)
            entry = r.engine.neighbours.get(ROOT)
            reroutes.append((hop, entry is not None and entry.eligible()))
            return hop

        parents.next_parent = next_parent
        line.net.medium.cut(R, ROOT)
        r.submit(APP, 1, ROOT, b"unheard")
        while not reroutes:
            line.run(500)
        self.assertEqual(reroutes, [(ROOT, True)])
        self.assertEqual((parents.preferred, parents.detaches, parents.drops), (ROOT, 0, 0))
        rel = r.engine.reliable
        self.assertEqual((len(r.engine.queue), rel.held()), (1, 0))
        # Left once the link is ineligible: a detach (no other candidate).
        while not parents.detaches:
            line.run(100)
        self.assertFalse(r.engine.neighbours.get(ROOT).eligible())
        self.assertTrue(all(hop == ROOT for hop, eligible in reroutes if eligible), reroutes)
        self.assertEqual((rel.held(), r.engine.counters.unreachable), (1, 0))
