"""DODAG on a scripted line (MESH_DESIGN §9 Trickle, DODAG version, loop freedom; MP10, MP14).

Line root — A — B — C — D (each board hears its line neighbours only) runs
real link engines with their routing (`mesh.routing.Routing`: `Dodag`,
presence and topology reports) on `sim.net`, at SF7 (Imin 1.7 s) with a
presence period of 10 s. The presence reports, forwarded by the relays, keep
every parent hearing its children.
"""

import unittest

from mesh.dodag import DodagInfo
from mesh.frame import RANK_INFINITE, ROOT
from mesh.neighbours import silence_ms
from mesh.parents import MIN_HOP, RANK_HYSTERESIS, link_cost
from mesh.ports import DODAG_INFO, PRESENCE
from mesh.profile import EU_869
from mesh.routing import Routing
from sim.medium import OK
from sim.net import Network

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
PERIOD = 10000
A, B, C, D = 0x00A1, 0x00B1, 0x00C1, 0x00D1


def _routing(reports):
    def make(engine):
        r = Routing(engine, EU_869, INFO)
        r.on_report = lambda port, origin, report, header: reports.append((port, origin))
        return r

    return make


class Line:
    def __init__(self, seed):
        net = Network(seed, _KEY)
        self.net = net
        self.names = (ROOT, A, B, C, D)
        self.reports = []
        self.boards = [net.add(n, routing=_routing(self.reports)) for n in self.names]
        for a, b in zip(self.names, self.names[1:]):
            net.link_both(a, b)
        self.root = self.boards[0]
        self.nodes = self.boards[1:]
        self.imin = self.root.routing.dodag.imin_ms

    def run(self, until):
        self.net.run(until)

    def attached(self):
        return [b.routing is not None and b.routing.attached for b in self.nodes]

    def ranks(self):
        return [b.engine.rank for b in self.nodes]

    def port9(self, b):
        return b.engine.counters.tx_messages[DODAG_INFO]

    def check_ranks(self, test):
        """Each rank is the parent's, as the node last heard it, plus the link
        cost the node sees (§9), within the rank hysteresis.

        The link cost is that of an eligible link (8 to 32): a frame lost to a
        hidden terminal (A and C, B and D) while the windows are short raises
        an ETX above 1.
        """
        for b in self.nodes:
            view = b.engine.neighbours.get(b.routing.parents.preferred)
            cost = link_cost(view.etx())
            step = b.engine.rank - view.rank
            test.assertTrue(abs(step - cost) < RANK_HYSTERESIS, (step, cost, self.ranks()))
            test.assertTrue(MIN_HOP <= cost <= 4 * MIN_HOP, (cost, self.ranks()))

    def counts(self):
        """Return (port 9 messages sent, Trickle resets) of each board."""
        return [(self.port9(b), b.routing.dodag.trickle.resets) for b in self.boards]

    def check_quiet(self, test, before):
        """Since before (`counts`), a node sent port 9 messages only after a
        Trickle reset (§9: nothing at Imax). A frame lost to a hidden terminal
        (A and C, B and D) is a failed acknowledgement sample: the ETX, then
        the rank, may rise, which resets Trickle."""
        for b, (sent, resets) in zip(self.boards[1:], before[1:]):
            if b.routing.dodag.trickle.resets == resets:
                test.assertEqual(self.port9(b), sent, (b.address, self.counts()))

    def record(self):
        """Trace the medium and record the DODAG information frames sent from
        now on: returns a list of (source, start, end, information, version);
        information False for a solicitation."""
        self.net.medium.trace = []
        out = []
        for b in self.boards:
            engine = b.engine
            base = engine.sent

            def sent(sel, counter, now, base=base, engine=engine, name=b.name):
                base(sel, counter, now)
                for m in sel.messages:
                    if m.port == DODAG_INFO:
                        info = len(m.payload) > 0
                        out.append((name, now, now + sel.toa_ms, info, engine.dodag_version))

            engine.sent = sent
        return out

    def lost(self, frames, source, dest, info, until):
        """Count the frames of `record` sent by source before until and lost
        at dest (collision, half duplex): DODAG information of the newest
        version if info, else solicitations."""
        newest = max(f[4] for f in frames)
        trace = self.net.medium.trace
        n = 0
        for src, start, end, kind, version in frames:
            if src != source or kind != info or start >= until:
                continue
            if info and version != newest:
                continue
            for t, s, d, fate in trace:
                if s == source and d == dest and start < t <= end + 2 and fate != OK:
                    n += 1
        return n

    def steady(self, limit=300000):
        """Run until every board's Trickle timer reaches Imax."""
        while not all(b.routing.dodag.trickle.at_imax() for b in self.boards):
            if self.net.now > limit:
                raise AssertionError("Imax not reached")
            self.run(self.net.now + 1000)


class Forming(unittest.TestCase):
    def test_forms_and_settles(self):
        line = Line(3)
        line.run(12 * line.imin)
        self.assertEqual(line.attached(), [True] * 4)
        self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])
        line.check_ranks(self)
        line.steady()
        # Steady state: nodes advertise nothing (without a reset), the root
        # beacons every half period (0.45 to 0.55 periods apart).
        start = line.net.now
        before = line.counts()
        line.run(start + 6 * PERIOD)
        line.check_quiet(self, before)
        beacons = line.port9(line.root) - before[0][0]
        self.assertTrue(10 <= beacons <= 14, beacons)
        self.assertEqual([b.routing.dodag.counters.poisons for b in line.nodes], [0] * 4)
        self.assertEqual(line.attached(), [True] * 4)
        # The presence reports of every node reach the root.
        origins = set(o for port, o in line.reports if port == PRESENCE)
        self.assertEqual(origins, set((A, B, C, D)))


class GlobalRepair(unittest.TestCase):
    """A new version crosses the line one Imin per hop when no frame of the
    hop's exchange is lost (§9 global repair wave).

    Losses are frequent: the parent's presence after its adoption makes the
    child behind, whose solicitation resets its own children too; their
    answers (old version) fall in the window of the parent's advertisement
    and collide with it at the child (hidden terminals). The child then
    solicits with backoff (Imin, doubling): n frames of the exchange lost
    (the parent's advertisements at the child, the child's solicitations at
    the parent) cost at most the next n gaps, the hop staying within
    (2^(n+1) - 1) Imin.
    """

    def test_new_version_crosses_one_imin_per_hop(self):
        line = Line(6)
        line.run(12 * line.imin)
        line.steady()
        frames = line.record()
        start = line.net.now
        info = DodagInfo(0, 12, 60, 32, 48, EU_869)
        line.root.routing.dodag.set_version(1, start, info)
        line.root.node.wake()
        adopted = {0: 0}

        def watch():
            for hop, b in enumerate(line.nodes, 1):
                if b.routing.dodag.version == 1 and hop not in adopted:
                    adopted[hop] = line.net.now - start
            if len(adopted) < 5:
                line.net.at(line.net.now + 10, watch)

        line.net.at(start, watch)
        while len(adopted) < 5 and line.net.now < start + 4 * PERIOD:
            line.run(line.net.now + line.imin)
        self.assertEqual(sorted(adopted), [0, 1, 2, 3, 4])
        lost = []
        for hop in range(1, 5):
            parent, node = line.names[hop - 1], line.names[hop]
            until = start + adopted[hop]
            n = line.lost(frames, parent, node, True, until)
            n += line.lost(frames, node, parent, False, until)
            lost.append(n)
            # One Imin per hop without loss, plus the frame and channel
            # access of the hop; each loss adds the next backoff gap.
            bound = (2 ** (n + 1) - 1) * line.imin + 150
            self.assertTrue(adopted[hop] - adopted[hop - 1] <= bound, (hop, adopted, lost))
        line.net.medium.trace = None
        self.assertTrue(adopted[1] < adopted[2] < adopted[3] < adopted[4], adopted)
        # Parameters carried; no detach, ranks rebuilt as before.
        self.assertEqual([b.engine.neighbours.period_ms for b in line.boards], [12000] * 5)
        self.assertEqual([b.routing.parents.max_increase for b in line.nodes], [48] * 4)
        self.assertEqual([b.engine.dodag_version for b in line.boards], [1] * 5)
        self.assertEqual([b.routing.dodag.counters.poisons for b in line.nodes], [0] * 4)
        # Ranks rebuilt along the same parents.
        self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])
        line.check_ranks(self)
        line.steady()
        start = line.net.now
        before = line.counts()
        line.run(start + 4 * 12000)
        line.check_quiet(self, before)


class LostInformation(unittest.TestCase):
    """Global repair with A's DODAG information lost at B for 3 presence
    periods (TLA+ `DODAG_line_versions_lag`): B hears A's other frames of
    the new version, so it is behind. It keeps A as parent at its own rank,
    forwards through it and solicits with backoff: no node detaches, and B
    then its subtree adopt the version once an answer comes through."""

    def test_lagging_node_keeps_its_subtree(self):
        line = Line(5)
        line.run(12 * line.imin)
        line.steady()
        a, b = line.nodes[0], line.nodes[1]
        medium = line.net.medium
        losing = [True]
        base = a.engine.sent

        def sent(sel, counter, now):
            base(sel, counter, now)
            if losing[0] and any(m.port == DODAG_INFO and len(m.payload) for m in sel.messages):
                medium.cut(A, B)  # while on air: lost at B only
                line.net.at(now + sel.toa_ms + 1, medium.restore, A, B)

        def stop():
            losing[0] = False

        a.engine.sent = sent
        start = line.net.now
        line.root.routing.dodag.set_version(1, start)
        line.root.node.wake()
        end = start + 3 * PERIOD
        line.net.at(end, stop)
        reports = len(line.reports)
        d = b.routing.dodag
        solicits = d.counters.solicits  # those of the forming (boot) excluded
        line.run(end)
        self.assertEqual([x.routing.dodag.counters.poisons for x in line.nodes], [0] * 4)
        self.assertEqual(line.attached(), [True] * 4)
        self.assertEqual(d.parents.preferred, A)
        self.assertEqual(a.routing.dodag.version, 1)
        self.assertEqual((d.version, d.parents.behind), (0, True))
        # Upward traffic kept flowing through B: presence reports of its
        # subtree, forwarded by B, reached the root during the change (the
        # 3 reports of one node may all be lost to hidden terminals: class
        # 0, no retry, §8).
        origins = set(o for port, o in line.reports[reports:] if port == PRESENCE)
        self.assertTrue(set((C, D)) & origins, origins)
        # Solicitations with backoff (Imin, doubling up to the presence
        # period): at most 6 in 3 periods, where one per Imin would be 18.
        solicits = d.counters.solicits - solicits
        self.assertTrue(2 <= solicits <= 6, solicits)
        # Answers come through: B adopts at its next frame at the latest
        # (a solicitation within the presence period), then C and D.
        line.run(end + PERIOD + 6 * line.imin)
        self.assertEqual([x.engine.dodag_version for x in line.boards], [1] * 5)
        self.assertEqual([x.routing.dodag.counters.poisons for x in line.nodes], [0] * 4)
        self.assertEqual([x.routing.parents.preferred for x in line.nodes], [ROOT, A, B, C])
        self.assertFalse(d.parents.behind)
        line.check_ranks(self)


class RootBeaconsLost(unittest.TestCase):
    """A, the root's only child, hears nothing else from the root than its
    beacons (§9): the root's frames with a DODAG information message are lost
    at A for a while. Two consecutive lost beacons stay under the silence
    limit of 2.5 periods; a root switched off is detected at the limit."""

    def _lose(self, line, count):
        # The next count frames of the root carrying a DODAG information
        # message are lost at A (link cut while on air); returns their times.
        medium = line.net.medium
        engine = line.root.engine
        base = engine.sent
        times = []

        def sent(sel, counter, now):
            base(sel, counter, now)
            if len(times) < count and any(
                m.port == DODAG_INFO and len(m.payload) for m in sel.messages
            ):
                times.append(now)
                medium.cut(ROOT, A)
                line.net.at(now + sel.toa_ms + 1, medium.restore, ROOT, A)

        engine.sent = sent
        return times

    def test_two_lost_beacons_keep_the_subtree(self):
        for seed in (8, 9):
            line = Line(seed)
            line.run(12 * line.imin)
            line.steady()
            a = line.nodes[0]
            start = line.net.now
            lost = self._lose(line, 2)
            line.run(start + 4 * PERIOD)
            self.assertEqual(len(lost), 2)
            # A's last frame of the root is recent: it went on hearing
            # beacons after the losses.
            heard = a.engine.neighbours.get(ROOT).heard
            self.assertTrue(heard > lost[1], (lost, heard))
            self.assertEqual([b.routing.dodag.counters.poisons for b in line.nodes], [0] * 4)
            self.assertEqual(line.attached(), [True] * 4)
            self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])

    def test_root_off_detaches_its_child_at_the_silence_limit(self):
        line = Line(8)
        line.run(12 * line.imin)
        line.steady()
        a = line.nodes[0]
        off = line.net.now
        line.root.power_loss(off_ms=10**9)
        line.run(off + 1000)  # a frame of the root on air when switched off
        last = a.engine.neighbours.get(ROOT).heard
        limit = last + silence_ms(PERIOD)
        line.run(limit - 1)
        dodag = a.routing.dodag
        self.assertEqual((a.routing.parents.preferred, dodag.counters.poisons), (ROOT, 0))
        # Detached at the limit (within a frame on air), before the hold
        # of 2 Imin ends.
        line.run(limit + line.imin)
        self.assertEqual((dodag.counters.poisons, a.engine.rank), (1, RANK_INFINITE))
        detach = a.routing.parents.poisoned_at
        self.assertTrue(limit <= detach <= limit + 500, (limit, detach))


class DetachRejoin(unittest.TestCase):
    def test_relay_lost_then_back(self):
        line = Line(7)
        line.run(12 * line.imin)
        line.steady()
        b_board = line.nodes[1]
        c, d = line.nodes[2], line.nodes[3]
        lost = line.net.now
        b_board.power_loss(off_ms=4 * PERIOD)
        line.run(lost + 5 * PERIOD // 2 + 3 * line.imin)  # silence limit, poison, hold
        # C lost its only parent: poisoned; D, whose only parent is C, too.
        # C's presences to the dead B fail (§9 presence samples): C's rank
        # may rise before the silence limit, D follows it (§9 rank
        # following) and detaches on C's poison only.
        self.assertEqual(c.routing.dodag.counters.poisons, 1)
        self.assertEqual(d.routing.dodag.counters.poisons, 1)
        self.assertEqual((c.engine.rank, d.engine.rank), (RANK_INFINITE, RANK_INFINITE))
        self.assertTrue(c.routing.dodag.counters.solicits >= 1)
        self.assertEqual(line.attached()[0], True)
        # B boots, rejoins through A; C and D rejoin behind it.
        line.run(lost + 4 * PERIOD + 2 * PERIOD)
        self.assertEqual(line.attached(), [True] * 4)
        self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])
        line.check_ranks(self)


class LinkCostRise(unittest.TestCase):
    """B's link to A degrades one way: A hears none of B's frames, B still
    hears A. B's class 1 messages (one per second) to A fail, their retries
    raise B's ETX to A in bursts, B's rank follows and goes on air, C follows
    it; when A drops out (attempts exhausted, ETX above 4), C may still be
    heard at the rank it had before following, below B's new rank but not
    below L: taking it would close the cycle B -> C -> B with no frame lost
    on the link between them (§9 loop freedom, candidates below L; TLA+
    `DODAG_chain_current_loop`)."""

    APP = 20

    def test_no_cycle_while_the_link_cost_rises(self):
        rises = []  # highest finite rank of B, per seed
        for seed in (2, 3, 4):
            line = Line(seed)
            line.run(12 * line.imin)
            line.steady()
            b, c = line.nodes[1], line.nodes[2]
            start = line.net.now
            line.net.medium.cut(B, A)
            highest = 0  # highest finite rank of B
            t = start
            while t < start + 12 * PERIOD:
                t += 50
                if (t - start) % 1000 == 0:
                    b.submit(self.APP, 1, ROOT, b"x")
                line.run(t)
                pb, pc = b.routing.parents, c.routing.parents
                self.assertFalse(
                    pb.preferred == C and pc.preferred == B, (seed, t - start, line.ranks())
                )
                if pb.rank != RANK_INFINITE and pb.rank > highest:
                    highest = pb.rank
            # B, without a candidate below L, detached.
            self.assertTrue(b.routing.dodag.counters.poisons >= 1, seed)
            rises.append(highest)
        # The rise did happen, beyond one hop above B's rank of 16, on one
        # seed at least: B's rank follows its ETX only when it selects again
        # on hearing A, so whether a given seed catches a rise depends on
        # the timing of the retries.
        self.assertTrue(max(rises) > 24, rises)
