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
from sim.net import Network, Observer

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
PERIOD = 10000
A, B, C, D = 0x00A1, 0x00B1, 0x00C1, 0x00D1
R = 0x0101  # a relay


def _routing(reports, sequence=None):
    # sequence: the root's first root sequence (`mesh.dodag.Dodag`).
    def make(engine):
        r = Routing(engine, EU_869, INFO, sequence=sequence)
        r.on_report = lambda port, origin, report, header: reports.append((port, origin))
        return r

    return make


class Line:
    def __init__(self, seed, sequence=None):
        net = Network(seed, _KEY)
        self.net = net
        self.names = (ROOT, A, B, C, D)
        self.reports = []
        self.boards = [net.add(n, routing=_routing(self.reports, sequence)) for n in self.names]
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


class WeakRootVersion(unittest.TestCase):
    """Global repair heard first over links below the margin (the layout of
    the large-network `star-domain` topology): root — R — {A, B, C}; A, B
    and C hear each other, and the root at a margin of -1.5 dB. The root's
    DODAG information of the new version reaches them at the moment R adopts
    it, before R sends anything in it: adopted then, it would leave them
    without a parent of that version (the root's link is not eligible) and
    detach them, ungrounded until they rejoin. They stay behind instead and
    adopt through R (§9 DODAG version)."""

    def test_new_version_over_a_weak_link_detaches_nobody(self):
        net = Network(5, _KEY)
        reports = []
        boards = [net.add(n, routing=_routing(reports)) for n in (ROOT, R, A, B, C)]
        net.link_both(ROOT, R)
        for x in (A, B, C):
            net.link_both(R, x)
            net.link_both(ROOT, x, rssi=-120, snr=-9)  # margin -1.5 dB at SF7
        for x, y in ((A, B), (A, C), (B, C)):
            net.link_both(x, y)
        root, nodes = boards[0], boards[2:]
        imin = root.routing.dodag.imin_ms
        net.run(30 * imin + 3 * PERIOD)
        self.assertEqual([b.routing.parents.preferred for b in nodes], [R] * 3)
        self.assertTrue(all(b.routing.parents.grounded for b in nodes))
        poisons = [b.routing.dodag.counters.poisons for b in nodes]
        floats = [b.routing.parents.floats for b in nodes]
        start = net.now
        root.routing.dodag.set_version(1, start)
        root.node.wake()
        net.run(start + 6 * imin)
        self.assertEqual([b.routing.dodag.version for b in nodes], [1] * 3)
        self.assertEqual([b.routing.dodag.counters.poisons for b in nodes], poisons)
        self.assertEqual([b.routing.parents.floats for b in nodes], floats)
        self.assertEqual([b.routing.parents.preferred for b in nodes], [R] * 3)


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


class DeadUplinkIdle(unittest.TestCase):
    """MP10 over a dead uplink without traffic: root, A and B, each hearing the
    two others; A's frames no longer reach the root, A still hears it, and
    nobody sends class 1 traffic. A's presences to the root are the probes
    of its link (§9): A leaves it and rejoins through B (before the probe,
    A stayed attached over the dead link for ever)."""

    def test_idle_root_child_rejoins_through_its_neighbour(self):
        net = Network(21, _KEY)
        reports = []
        boards = {n: net.add(n, routing=_routing(reports)) for n in (ROOT, A, B)}
        for x, y in ((ROOT, A), (ROOT, B), (A, B)):
            net.link_both(x, y)
        net.run(6 * PERIOD)
        a = boards[A].routing
        self.assertEqual([boards[n].routing.parents.preferred for n in (A, B)], [ROOT, ROOT])
        cut = net.now
        unacked = a.reporter.counters.presence_unacked
        net.medium.cut(A, ROOT)
        # Left after 4 failed presence samples (about 4 periods, 5.5 at
        # most, §9 repair table): a switch to B when B is feasible (a newer
        # root sequence), otherwise a detach, the hold, the solicitation and
        # the rejoin through B (the root's link unreachable until its
        # failures age out); kept: B is no worse than the root by the switch
        # threshold (one hop).
        net.run(cut + 7 * PERIOD)
        self.assertEqual((a.attached, a.parents.preferred), (True, B))
        self.assertTrue(a.reporter.counters.presence_unacked - unacked >= 4)
        poisons = a.dodag.counters.poisons
        self.assertTrue(poisons <= 1, poisons)
        net.run(cut + 12 * PERIOD)
        state = (a.attached, a.parents.preferred, a.dodag.counters.poisons)
        self.assertEqual(state, (True, B, poisons))
        # The root hears A's reports again, through B.
        since = len(reports)
        net.run(net.now + 2 * PERIOD)
        self.assertIn((PRESENCE, A), reports[since:])


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


class _Advances(Observer):
    """`sim.net.Observer` recording when each node's root sequence advances
    (``Parents.sequence_at``, read after every step: it changes inside steps
    only), and whether it stayed attached."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.times = dict((b.address, []) for b in nodes)
        self.detached = set()
        self._last = dict((b.address, b.routing.parents.sequence_at) for b in nodes)

    def stepped(self, node, event):
        for b in self.nodes:
            p = b.routing.parents
            if p.rank == RANK_INFINITE:
                self.detached.add(b.address)
            if p.sequence_at != self._last[b.address]:
                self._last[b.address] = p.sequence_at
                self.times[b.address].append(p.sequence_at)


class RootSequence(unittest.TestCase):
    """No false float material on a grounded path (§9 root sequence and
    grounding): on the line, D 4 hops down, every node's sequence advances
    at least once within each window of G = 2.5 + min(8, ceil(rank / 8))
    presence periods, read from frames overheard as well: a node's parent
    sends its presence to the grandparent, and Trickle is silent at Imax,
    so that reading only the frames addressed to the node (and the DODAG
    information) would leave it without an advance beyond hop 1."""

    def test_held_sequence_advances_within_g(self):
        line = Line(11)
        line.run(12 * line.imin)
        line.steady()
        self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])
        watch = _Advances(line.nodes)
        line.net.observers.append(watch)
        start = line.net.now
        end = start + 12 * PERIOD
        line.run(end)
        self.assertEqual(watch.detached, set())
        root = line.root.routing.dodag.sequence
        for b in line.nodes:
            p = b.routing.parents
            hops = min(8, (p.rank + MIN_HOP - 1) // MIN_HOP)
            g = (5 + 2 * hops) * PERIOD // 2
            times = [start] + watch.times[b.address] + [end]
            gaps = [u - t for t, u in zip(times, times[1:])]
            self.assertTrue(max(gaps) < g, (b.address, p.rank, gaps))
            # Advances in at least half the periods (a lost or collided frame
            # skips one: 9 of 12 seen beyond hop 1 on some seeds), and never
            # ahead of the root's sequence.
            self.assertTrue(len(watch.times[b.address]) >= 6, (b.address, gaps))
            self.assertTrue(0 <= (root - p.sequence) & 0xFF < 2 * (hops + 1), (root, p.sequence))

    def test_no_false_float_under_load(self):
        """§9 grounding on the line under class 1 load: every node, D 4 hops
        down, stays grounded (no float: its sequence, read from overheard
        frames too, advances within G), its frames never carry the floating
        flag, and every message is delivered."""
        line = Line(12)
        line.root.listen(20)
        line.run(12 * line.imin)
        line.steady()
        self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])
        flagged = []  # (time, node) of frames sent with the floating flag
        for b in line.nodes:
            base = b.engine.sent

            def sent(sel, counter, now, base=base, engine=b.engine, name=b.name):
                base(sel, counter, now)
                if engine.floating:
                    flagged.append((now, name))

            b.engine.sent = sent
        watch = _Advances(line.nodes)
        line.net.observers.append(watch)
        start = line.net.now
        end = start + 12 * PERIOD
        sent = []

        def load(i=0):
            b = line.nodes[i % 4]
            sent.append(b.submit(20, 1, ROOT, b"m%d" % i))
            if line.net.now < end - 3 * PERIOD:
                line.net.at(line.net.now + 700, load, i + 1)

        line.net.at(start + 100, load)
        line.run(end)
        self.assertEqual(watch.detached, set())
        self.assertTrue(len(sent) > 120 and min(sent) >= 0, sent)
        self.assertEqual([b.routing.parents.floats for b in line.nodes], [0] * 4)
        self.assertEqual([b.routing.parents.grounded for b in line.nodes], [True] * 4)
        self.assertEqual(flagged, [])
        # Every message delivered (class 1: a copy retried after a lost
        # acknowledgement may come again once the root's duplicate entry has
        # aged out, 6 periods after the last copy).
        self.assertEqual(len(set(line.root.received(20))), len(sent))
        self.assertEqual([b.failures for b in line.nodes], [[]] * 4)
        # D's sequence advanced: it came from frames of C addressed to B
        # (its presence), overheard; the frames addressed to D are
        # acknowledgements only.
        self.assertTrue(watch.times[D], watch.times)


class _Wrap(_Advances):
    """`_Advances`, plus the root's sequences in order (``root``) and the
    nodes found holding a sequence ahead of the root's (serial arithmetic,
    computed here, not by the code under test) after any step (``ahead``:
    (time, node, its sequence, the root's), the first 10)."""

    def __init__(self, root, nodes, net):
        super().__init__(nodes)
        self.dodag = root.routing.dodag
        self.net = net
        self.root = [self.dodag.sequence]
        self.ahead = []

    def stepped(self, node, event):
        super().stepped(node, event)
        seq = self.dodag.sequence
        if seq != self.root[-1]:
            self.root.append(seq)
        for b in self.nodes:
            held = b.routing.parents.sequence
            ahead = held is not None and 0 < (held - seq) & 0xFF < 128
            if ahead and len(self.ahead) < 10:
                self.ahead.append((self.net.now, b.address, held, seq))


class RootSequenceWrap(unittest.TestCase):
    """§9 root sequence, 8 bits in serial arithmetic: on the line, the
    root's sequence passes from 255 to 0 while the network runs, D sending
    class 1 messages. Nothing changes at the wrap: no detach, no float, no
    floating flag, every node's sequence advances within G and is never
    ahead of the root's, and every message of D is delivered (a plain
    comparison would refuse 0 after 255: every node would float)."""

    def test_wrap_around_keeps_the_line_grounded(self):
        line = Line(11, sequence=245)  # about 2 increments per period
        line.root.listen(20)
        line.run(12 * line.imin)
        line.steady()
        self.assertEqual([b.routing.parents.preferred for b in line.nodes], [ROOT, A, B, C])
        start_seq = line.root.routing.dodag.sequence
        self.assertTrue(240 <= start_seq <= 254, start_seq)  # the wrap is still to come
        flagged = []
        for b in line.nodes:
            base = b.engine.sent

            def sent(sel, counter, now, base=base, engine=b.engine, name=b.name):
                base(sel, counter, now)
                if engine.floating:
                    flagged.append((now, name))

            b.engine.sent = sent
        watch = _Wrap(line.root, line.nodes, line.net)
        line.net.observers.append(watch)
        start = line.net.now
        end = start + 12 * PERIOD
        d = line.nodes[3]
        sent = []

        def load(i=0):
            sent.append(d.submit(20, 1, ROOT, b"d%d" % i))
            if line.net.now < end - 3 * PERIOD:
                line.net.at(line.net.now + 2000, load, i + 1)

        line.net.at(start + 100, load)
        line.run(end)
        seqs = watch.root
        self.assertIn(255, seqs)
        self.assertEqual(seqs[seqs.index(255) + 1], 0, seqs)
        self.assertTrue(seqs[-1] >= 4, seqs)  # well past the wrap
        self.assertEqual(watch.detached, set())
        self.assertEqual(watch.ahead, [])
        self.assertEqual([b.routing.parents.floats for b in line.nodes], [0] * 4)
        self.assertEqual([b.routing.parents.grounded for b in line.nodes], [True] * 4)
        self.assertEqual(flagged, [])
        for b in line.nodes:
            p = b.routing.parents
            hops = min(8, (p.rank + MIN_HOP - 1) // MIN_HOP)
            g = (5 + 2 * hops) * PERIOD // 2
            times = [start] + watch.times[b.address] + [end]
            gaps = [u - t for t, u in zip(times, times[1:])]
            self.assertTrue(max(gaps) < g, (b.address, p.rank, gaps))
        self.assertTrue(len(sent) > 40 and min(sent) >= 0, sent)
        self.assertEqual(len(set(line.root.received(20))), len(sent))
        self.assertEqual(d.failures, [])


class SequenceForgotten(unittest.TestCase):
    """§9: a sequence not advanced for 50 presence periods is forgotten.
    Root — A — B — C, the link between A and B cut both ways for about 65
    periods: B detaches and keeps the sequence it held, while the root's
    goes on (about 2 increments per period) until it is 128 to 200 ahead
    of it, where serial arithmetic no longer finds it newer and would
    refuse A for ever. Forgetting is checked at a selection, which a
    detached node makes when it hears a frame: C, detached behind B,
    solicits once per period, so B forgets within a period of the 50.
    Once the link is back, B takes A's sequence at any rank, attaches
    through A, is grounded and its class 1 message is delivered."""

    def test_forgotten_sequence_lets_b_rejoin(self):
        net = Network(5, _KEY)
        reports = []
        boards = {n: net.add(n, routing=_routing(reports)) for n in (ROOT, A, B, C)}
        net.link_both(ROOT, A)
        net.link_both(A, B)
        net.link_both(B, C)
        boards[ROOT].listen(20)
        net.run(6 * PERIOD)
        b = boards[B]
        p = b.routing.parents
        self.assertEqual((p.preferred, p.grounded), (A, True))
        self.assertEqual(boards[C].routing.parents.preferred, B)
        net.medium.cut(A, B)
        net.medium.cut(B, A)
        net.run(net.now + 3 * PERIOD)  # detached: no advance since
        held, at = p.sequence, p.sequence_at
        self.assertEqual(p.rank, RANK_INFINITE)
        self.assertIsNotNone(held)
        net.run(at + 50 * PERIOD - PERIOD // 2)
        self.assertEqual((p.sequence, p.sequence_at), (held, at))
        net.run(at + 51 * PERIOD + PERIOD // 4)  # C's next solicitation: forgotten
        self.assertEqual(p.sequence, None)
        net.run(at + 65 * PERIOD)
        ahead = (boards[ROOT].routing.dodag.sequence - held) & 0xFF
        self.assertTrue(128 <= ahead <= 200, ahead)
        restored = net.now
        net.medium.restore(A, B)
        net.medium.restore(B, A)
        # B solicits once per presence period while detached; A answers
        # within the answer window.
        bound = restored + 2 * PERIOD
        while not (p.preferred == A and p.grounded):
            self.assertTrue(net.now < bound, (p.preferred, p.rank, p.sequence))
            net.run(net.now + 100)
        b.submit(20, 1, ROOT, b"back")
        net.run(net.now + PERIOD)
        self.assertEqual(boards[ROOT].received(20), [b"back"])
        self.assertEqual(b.failures, [])
