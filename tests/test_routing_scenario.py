"""Upward routing on scripted graphs (MESH_DESIGN §9 repair, loops; §11, §16; MP10, MP11, MP15).

Real link engines with their routing (`mesh.routing.Routing`) on `sim.net`,
SF7 (slot 52 ms, Imin 1.7 s), presence period 10 s, topology period 60 s.

Graph of 6 boards (both directions, SNR in dB)::

    root ── A ── C ── E        A, B: rank 8; C, D: rank 16 under A (better
      │  ╲  │  ╳  │  ╱         links), B their second parent; E: rank 24
      └─── B ── D ─┘           under C or D.

    root-A, root-B, A-B, C-D, C-E, D-E: 10; A-C, A-D: 12; B-C, B-D: 8.

Each node's presence carries a 2-byte counter in its application extension,
so that the root sees which reports arrive. Hidden terminals exist (the root
and C, D; A, B and E): reports lost to collisions are accepted by the design
(§8, K_MISS) and must be the only losses.
"""

import unittest

from mesh import frame
from mesh.counter import FrameCounter
from mesh.dodag import DodagInfo
from mesh.frame import RANK_INFINITE, ROOT
from mesh.neighbours import popcount16
from mesh.parents import MIN_HOP, SWITCH_THRESHOLD, link_cost
from mesh.ports import DODAG_INFO, PRESENCE
from mesh.profile import EU_869
from mesh.reliable import ATTEMPTS, identity
from mesh.replay import ReplayTable
from mesh.routing import Routing
from mesh.security import KeyRing, Security
from sim.net import Network
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
P = 10000
APP = 20
A, B, C, D, E = 0x00A1, 0x00B1, 0x00C1, 0x00D1, 0x00E1
NODES = (A, B, C, D, E)
LINKS = (
    (ROOT, A, 10),
    (ROOT, B, 10),
    (A, B, 10),
    (A, C, 12),
    (A, D, 12),
    (B, C, 8),
    (B, D, 8),
    (C, D, 10),
    (C, E, 10),
    (D, E, 10),
)
OFF = 10**9  # a board switched off for the rest of the run


def _open(f):
    """Return (header, messages) of a frame sealed under the network key."""
    platform = FakePlatform(XorShift32(99))
    security = Security(
        KeyRing(_KEY), FrameCounter(platform.storage, platform.random), ReplayTable()
    )
    buf = bytearray(f)
    h = frame.Header(0, 0, 0, 0)
    if security.receive(buf, len(buf), h) is not None:
        raise AssertionError("frame does not open")
    return h, frame.decode_messages(buf, frame.HEADER_SIZE, len(buf) - frame.TAG_SIZE)


class Mesh:
    """Boards with routing on scripted links; presence extensions counted."""

    def __init__(self, seed, names, links, sniffer=False):
        net = Network(seed, _KEY)
        self.net = net
        self.generated = {}  # origin -> [(time, counter, preferred parent)]
        self.presences = {}  # origin -> [(time, counter, parent)]
        self.boards = {}
        for n in names:
            self.boards[n] = net.add(n, routing=self._make(n))
        self.links = []
        for a, b, snr in links:
            net.link_both(a, b, snr=snr)
            self.links.append((a, b))
        self.root = self.boards[ROOT]
        self.root.listen(APP)
        self.imin = self.root.routing.dodag.imin_ms
        self.eve = None
        if sniffer:
            self.eve = net.sniffer("eve")
            for n in names:
                net.medium.link(n, "eve")

    def _make(self, name):
        def make(engine):
            if engine.is_root:
                r = Routing(engine, EU_869, INFO)
                r.on_report = self._report
                return r
            count = [0]

            def extension():
                count[0] += 1
                record = (self.net.now, count[0], r.parents.preferred)
                self.generated.setdefault(name, []).append(record)
                return bytes((count[0] & 0xFF, count[0] >> 8))

            r = Routing(engine, EU_869, INFO, extension=extension)
            return r

        return make

    def _report(self, port, origin, report, header):
        if port == PRESENCE:
            ext = report.extension
            self.presences.setdefault(origin, []).append(
                (self.net.now, ext[0] | ext[1] << 8, report.parent)
            )

    def run(self, until):
        self.net.run(until)

    def b(self, name):
        return self.boards[name]

    def preferred(self, name):
        b = self.boards[name]
        return None if b.engine is None else b.routing.parents.preferred

    def rank(self, name):
        b = self.boards[name]
        return None if b.engine is None else b.engine.rank

    def linked(self, a, b):
        link = self.net.medium.get_link(a, b)
        return link is not None and link.up

    def consistent(self, names):
        """Every attached node's parent chain reaches the root through up links,
        each node's view of its parent's rank below its own (rank rule), no
        loop; returns the list of problems."""
        problems = []
        for n in names:
            if self.rank(n) in (None, RANK_INFINITE):
                continue
            seen = [n]
            cur = n
            while cur != ROOT:
                p = self.preferred(cur)
                if p is None or not self.linked(cur, p) or not self.linked(p, cur):
                    problems.append((n, "parent", cur, p))
                    break
                view = self.b(cur).engine.neighbours.get(p)
                if view is None or view.rank >= self.rank(cur):
                    problems.append((n, "rank", cur, p))
                    break
                if p in seen:
                    problems.append((n, "loop", seen))
                    break
                seen.append(p)
                cur = p
        return problems

    def looped(self, names):
        """Return True if following preferred parents from names comes back to a node."""
        for n in names:
            seen = [n]
            p = self.preferred(n)
            while p is not None and p != ROOT:
                if p in seen:
                    return True
                seen.append(p)
                p = self.preferred(p)
        return False

    def loop_samples(self, names, start, until, step=50):
        """Sample every step ms in [start, until]: times at which names hold a loop."""
        samples = []

        def tick():
            if self.looped(names):
                samples.append(self.net.now)
            if self.net.now < until:
                self.net.at(self.net.now + step, tick)

        self.net.at(start, tick)
        return samples

    def radio_losses(self):
        """Receptions lost on the medium: collisions and half duplex."""
        total = 0
        for n in self.boards:
            st = self.net.medium.stats(n)
            total += st.rx_collision + st.rx_half_duplex
        return total

    def watch(self, names, start, step=5):
        """Record (time - start, name, preferred, rank) at each change."""
        log = []
        last = {}

        def tick():
            for n in names:
                state = (self.preferred(n), self.rank(n))
                if last.get(n) != state:
                    last[n] = state
                    log.append((self.net.now - start, n) + state)
            self.net.at(self.net.now + step, tick)

        self.net.at(start, tick)
        return log

    def frames(self, source):
        out = []
        for t, f in self.eve.frames:
            h, msgs = _open(f)
            if h.source == source:
                out.append((t, h, msgs))
        return out


def episodes(samples, step=50):
    """Durations of the runs of consecutive samples (a loop that lasted)."""
    runs = []
    for t in samples:
        if runs and t - runs[-1][1] <= step:
            runs[-1][1] = t
        else:
            runs.append([t, t])
    return [b - a for a, b in runs]


def six(seed, sniffer=False):
    return Mesh(seed, (ROOT,) + NODES, LINKS, sniffer)


def switched(log, name, old):
    """Time of name's first change away from parent old (None if none)."""
    for t, n, p, _ in log:
        if n == name and p != old:
            return t, p
    return None


class Forming(unittest.TestCase):
    def test_dodag_forms_and_every_presence_reaches_the_root(self):
        m = six(1)
        m.run(12 * m.imin)
        self.assertEqual([m.rank(n) is not None and m.rank(n) < 255 for n in NODES], [True] * 5)
        self.assertEqual(m.consistent(NODES), [])
        self.assertEqual([m.preferred(n) for n in (A, B)], [ROOT, ROOT])
        # C, D: A or B, at a path cost within the switch threshold of the
        # other's (§9). A and B attach each at the close of its own answer
        # window: C or D, whose window closed before, takes the first one
        # heard (A by its larger margin only if both were candidates then);
        # a frame lost to a collision while the windows fill may also make
        # one cheaper, by less than the switch threshold.
        for n in (C, D):
            p = m.b(n).routing.parents
            cost = dict(
                (x, p.candidate_cost(m.b(n).engine.neighbours.get(x), m.net.now)) for x in (A, B)
            )
            self.assertTrue(p.preferred in (A, B), cost)
            self.assertTrue(cost[p.preferred] - min(cost.values()) < SWITCH_THRESHOLD, cost)
        self.assertTrue(m.preferred(E) in (C, D))
        # One hop of ETX close to 1 each (a frame lost to a collision while
        # the windows fill raises an ETX a little).
        for n in NODES:
            view = m.b(n).engine.neighbours.get(m.preferred(n))
            step = m.rank(n) - view.rank  # path cost, eligible links: ETX <= 4
            self.assertTrue(8 <= step <= 32, (n, step))
        losses = m.radio_losses()
        window = m.net.now
        before = {}
        for n in NODES:
            e, r = m.b(n).engine, m.b(n).routing
            before[n] = (e.counters.no_route, e.counters.refused_full, r.counters.rank_dropped)
        m.run(12 * m.imin + 12 * P)
        # Every node is seen by the root, each report with the parent the
        # node had when it sent it (restored by the first relay, §11).
        lost = 0
        for n in NODES:
            sent = dict((c, p) for t, c, p in m.generated[n] if window <= t < m.net.now - 2000)
            got = dict((c, p) for t, c, p in m.presences.get(n, []))
            self.assertTrue(len(sent) >= 10, (n, sent))
            missing = [c for c in sent if c not in got]
            self.assertTrue(len(missing) * 2 <= len(sent), (n, missing))
            lost += len(missing)
            for c, parent in got.items():
                self.assertEqual(parent, sent.get(c, parent), (n, c))
        # Any loss is a reception lost on the scripted medium (hidden
        # terminals, §8) or a counted drop (a report met during a rank
        # change, §9); the queues refuse nothing; no loop.
        drops = 0
        for n in NODES:
            e, r = m.b(n).engine, m.b(n).routing
            now = (e.counters.no_route, e.counters.refused_full, r.counters.rank_dropped)
            drops += now[0] - before[n][0] + now[2] - before[n][2]
            self.assertEqual(now[1], before[n][1], n)
            self.assertEqual(r.counters.loops, 0)
        self.assertTrue(lost <= m.radio_losses() - losses + drops, (lost, drops))
        self.assertEqual(m.consistent(NODES), [])


class RelayKilled(unittest.TestCase):
    def _formed(self, seed):
        """Formed graph, the relay of C, the other relay, the relay's children."""
        m = six(seed)
        m.run(12 * m.imin + 3 * P)
        relay = m.preferred(C)
        self.assertTrue(relay in (A, B))
        other = B if relay == A else A
        children = [n for n in (C, D) if m.preferred(n) == relay]
        return m, relay, other, children

    def test_children_reattach_within_the_silence_limit_idle(self):
        for seed in (2, 3):
            m, relay, other, children = self._formed(seed)
            kill = m.net.now
            log = m.watch((C, D, E), kill)
            m.b(relay).power_loss(off_ms=OFF)
            last = {}  # the relay's last frame heard by each child (one may be on air)

            def record(m=m, relay=relay, children=children, last=last):
                for n in children:
                    last[n] = m.b(n).engine.neighbours.get(relay).heard

            m.net.at(kill + 1000, record)
            m.run(kill + 4 * P)
            for n in children:
                t, parent = switched(log, n, relay)
                # No frame from the relay for 2.5 presence periods (§9 switch,
                # idle, strict), the step of the watch aside.
                self.assertEqual(parent, other)
                self.assertTrue(0 <= kill + t - (last[n] + 5 * P // 2) <= 10, (n, t))
                # Its last frame came at most a jittered period before.
                self.assertTrue(5 * P // 2 - 11 * P // 10 <= t <= 5 * P // 2 + 1000, (n, t))
                # A presence leaves at once, and the root learns the new parent
                # (the watch samples every 5 ms: t is up to 5 ms late).
                gen = [(tg, p) for tg, c, p in m.generated[n] if tg > kill + t - 5]
                self.assertEqual(gen[0][1], other)
                self.assertTrue(gen[0][0] - kill - t <= 4 * m.b(n).engine.slot_ms + 10)
                after = [p for tp, c, p in m.presences[n] if tp >= kill + t]
                self.assertEqual(after[0], other)
            self.assertEqual(m.consistent((other, C, D, E)), [])

    def test_child_with_traffic_reattaches_after_three_attempts(self):
        m, relay, other, children = self._formed(4)
        kill = m.net.now
        log = m.watch((C, D), kill)
        m.b(relay).power_loss(off_ms=OFF)
        c = m.b(C)
        sent = []

        def submit():
            if len(sent) < 3:
                sent.append((m.net.now, c.submit(APP, 1, ROOT, b"p%d" % len(sent))))
                m.net.at(m.net.now + 2000, submit)

        m.net.at(kill + 100, submit)
        m.run(kill + 3 * P)
        t, parent = switched(log, C, relay)
        self.assertEqual(parent, other)
        # Switch with traffic through a relay (§9 table, SF7: 2.1 s): 3
        # attempts, each the frame and its acknowledgement timeout.
        # Channel access may add a backoff (4 slots at first) per attempt.
        rel = c.engine.reliable
        frame_ms = c.engine.toa_ms(frame.OVERHEAD + 2 + 2 + 2)
        slot = c.engine.slot_ms
        bound = ATTEMPTS * (frame_ms + rel.relay_timeout_ms + 4 * slot)
        took = t - 100
        self.assertTrue(took <= bound, (took, bound))
        # At least one timeout: the failed attempts raise A's ETX (p_ack), so
        # that B may win by the switch threshold before the third one.
        self.assertTrue(took >= rel.relay_timeout_ms, took)
        # D, without traffic, switches when the relay has been silent for 2.5
        # periods. (A node that rejoins after its solicitation takes the
        # best candidate heard in its answer window, §9: it may be D, whose
        # parent is the dead relay; D then has traffic.)
        if D in children and not m.b(D).engine.counters.tx_messages[APP]:
            self.assertTrue(switched(log, D, relay)[0] >= 5 * P // 2 - 11 * P // 10)
        # Every class 1 message reached the root once (local repair, §1).
        got = [p for o, p in m.root.inbox_from(APP) if o == C]
        self.assertEqual(sorted(got), [b"p0", b"p1", b"p2"])
        self.assertEqual(c.failures, [])


class FollowParentRank(unittest.TestCase):
    """Diamond root — A, root — B, A — C, B — C, C — D (TLA+
    `DODAG_follow_follow_*`). C attaches through A, D through C, before
    the link B — C comes up; B's frames are then lost at C one in two at
    most, so that C hears B at d_in 9/16 (ETX 3.16 before acknowledgement
    samples, §9: link cost 25; 8/16, ETX 4, is still eligible). A dies: C
    falls back on B at a rank not below D's; D, whose only neighbour C is,
    keeps C and follows its rank increase (§9), attached all along."""

    def test_child_follows_a_local_repair(self):
        names = (ROOT, A, B, C, D)
        links = ((ROOT, A, 10), (ROOT, B, 10), (A, C, 10), (B, C, 8), (C, D, 10))
        m = Mesh(13, names, links)
        b, c, d = m.b(B), m.b(C), m.b(D)
        view = c.engine.neighbours
        medium = m.net.medium
        link = medium.get_link(B, C)
        sent = b.engine.sent

        def lossy(sel, counter, now):
            # B's frame is lost at C if C heard B's previous one and hearing
            # this one would raise d_in above 9/16.
            sent(sel, counter, now)
            n = view.get(B)
            link.up = True
            if n is not None and counter == n.counter + 1:
                span = min(n.rx_span + 1, 16)
                if popcount16(((n.rx_bits << 1) | 1) & 0xFFFF) * 16 > 9 * span:
                    link.up = False

        medium.cut(B, C)
        medium.cut(C, B)
        m.run(12 * m.imin + 3 * P)
        for _ in range(60):
            if [m.preferred(n) for n in (A, B, C, D)] == [ROOT, ROOT, A, C]:
                break
            m.run(m.net.now + 1000)
        else:
            raise AssertionError("diamond not formed")
        medium.restore(C, B)
        b.engine.sent = lossy
        # C's path cost through B not below D's rank (a frame lost to a
        # hidden terminal may raise an ETX for a while, §9 failure ageing).
        for _ in range(60):
            m.run(m.net.now + 1000)
            n = view.get(B)
            cost = None if n is None else c.routing.parents.candidate_cost(n, m.net.now)
            if cost is not None and cost >= m.rank(D):
                break
        else:
            raise AssertionError("link B - C not as scripted")
        self.assertEqual([m.preferred(n) for n in (A, B, C, D)], [ROOT, ROOT, A, C])
        kill = m.net.now
        log = m.watch((C, D), kill)
        m.b(A).power_loss(off_ms=OFF)
        m.run(kill + 5 * P)
        # C fell back on B (local repair) at a rank not below D's, which D
        # would no longer take as a parent under the rank rule alone.
        t, parent = switched(log, C, A)
        self.assertEqual(parent, B)
        rank_c = [r for u, n, p, r in log if n == C and u >= t][0]
        rank_d = [r for u, n, p, r in log if n == D and u <= t][-1]
        self.assertTrue(rank_d <= rank_c < RANK_INFINITE, log)
        # D kept C and followed: never detached, its rank above C's.
        self.assertEqual([p for u, n, p, r in log if n == D and p != C], [], log)
        self.assertTrue([r for u, n, p, r in log if n == D and r > rank_c], log)
        self.assertEqual(d.routing.dodag.counters.poisons, 0)
        self.assertEqual(c.routing.dodag.counters.poisons, 0)
        self.assertEqual([m.preferred(n) for n in (B, C, D)], [ROOT, B, C])
        b.engine.sent = sent
        link.up = True
        self.assertEqual(m.consistent((B, C, D)), [])


class LoopDuringRepair(unittest.TestCase):
    """Line root — A — B — C — D (+ sniffer). B stops hearing A and C (links
    A -> B and C -> B cut) and its view of C is stale: rank 8, as if C's
    frames since it moved under B had been lost. B's class 1 attempts to A
    fail; at the local repair B picks C, its own child, and C -> B comes
    back: a loop B -> C -> B. Each follows the other's rank increase (§9
    rank following), advertised at once on the rank errors of data-path
    validation: the ranks climb until B's path cost passes L + 56, and B,
    without another candidate, detaches (TLA+
    `DODAG_line_follow_witness_bound`)."""

    def test_loop_ends_at_the_rank_bound(self):
        names = (ROOT, A, B, C, D)
        links = ((ROOT, A, 10), (A, B, 10), (B, C, 10), (C, D, 10))
        m = Mesh(5, names, links, sniffer=True)
        m.run(12 * m.imin + 2 * P)
        self.assertEqual([m.preferred(n) for n in (A, B, C, D)], [ROOT, A, B, C])
        b, c, d = m.b(B), m.b(C), m.b(D)
        parents = b.routing.parents
        original = parents.next_parent

        def next_parent(hop, now=None):
            new = original(hop, now)
            if parents.preferred == C:
                m.net.medium.restore(C, B)  # the loop is closed
            return new

        parents.next_parent = next_parent
        returned = []  # B's own message, back to B
        forward = b.engine.forward

        def watched_forward(msg, header, now, replace=False):
            if msg.origin == B and msg.port == APP:
                returned.append(now)
            return forward(msg, header, now)

        b.engine.forward = watched_forward
        to_c = []  # B's frames to C holding its message (B's side: the sniffer may lose one)
        b_sent = b.engine.sent

        def watched_sent(sel, counter, now):
            b_sent(sel, counter, now)
            for x in sel.messages:
                if sel.dest == C and x.port == APP and x.origin is None:
                    to_c.append(x.seq)

        b.engine.sent = watched_sent
        start = m.net.now
        log = m.watch((B, C, D), start)
        loops = m.loop_samples((B, C, D), start, start + 12000)
        m.net.medium.cut(A, B)
        m.net.medium.cut(C, B)
        b.engine.neighbours.get(C).rank = 8
        mine = b.submit(APP, 1, ROOT, b"mine")
        sent_d = []

        def d_traffic():
            # D's class 1 messages enter once C has seen the inconsistency.
            if c.routing.dodag.counters.rank_errors and len(sent_d) < 2:
                sent_d.append(d.submit(APP, 1, ROOT, b"d%d" % len(sent_d)))
            if len(sent_d) < 2 and m.net.now < start + 20000:
                m.net.at(m.net.now + 20, d_traffic)

        m.net.at(start, d_traffic)
        m.run(start + 12000)
        loop_at, parent = switched(log, B, A)
        self.assertEqual(parent, C)  # the loop B -> C -> B formed
        # C saw B's rank not above its own (rank error: Trickle reset and an
        # advertisement at once); B follows C's new rank, C B's, and so on.
        # Each return of B's message to B is counted as a loop.
        self.assertTrue(c.routing.dodag.counters.rank_errors >= 1)
        self.assertTrue(b.routing.counters.loops >= len(returned), returned)
        # Broken by the bound: B kept C while its rank stayed within L + 56
        # (L at most its rank before), then detached, without another
        # neighbour. The climb takes 56 / 8 = 7 increases, each advertised
        # within an Imin.
        rank_b = [r for t, n, p, r in log if n == B][0]
        left = [(t, p) for t, n, p, r in log if n == B and t > loop_at and p != C]
        self.assertEqual(left[0][1], None)
        on_c = [r for t, n, p, r in log if n == B and loop_at <= t < left[0][0]]
        self.assertTrue(max(on_c) <= rank_b + INFO.max_increase, (rank_b, on_c))
        bound = (INFO.max_increase // MIN_HOP + 1) * m.imin
        self.assertTrue(left[0][0] - loop_at <= bound, (loop_at, left))
        # Cut from the root, B, C and D then detach and rejoin through each
        # other while their views are stale (§9 detach and rejoin): loops may
        # form again, each one short-lived.
        self.assertTrue(max(episodes(loops)) <= P + 2 * m.imin, episodes(loops))
        # B's message travels around a loop while it lasts (custody, §12: a
        # copy that comes back once its holder has handed it over is taken
        # again), and nothing more is on air once the loops are over and the
        # attempts of the last hop exhausted.
        rel = b.engine.reliable
        quiet = max(loops) + ATTEMPTS * (rel.relay_timeout_ms + 100)
        for src in (B, C):
            for t, h, msgs in m.frames(src):
                for msg in msgs:
                    if msg.port == APP and h.dest in (B, C) and t < start + 12000:
                        key = identity(msg, h.source, h.dest) + (src,)
                        if key[0] == B:
                            self.assertTrue(t <= quiet, (t - start, quiet - start, key))
        self.assertTrue(1 <= len(to_c) <= ATTEMPTS and set(to_c) == {mine}, to_c)
        # B's message had reached the root through A before the loop.
        self.assertEqual(m.root.inbox_from(APP)[0], (B, b"mine"))
        # The links come back: no loop, every attached node consistent.
        parents.next_parent = original
        b.engine.forward = forward
        b.engine.sent = b_sent
        m.net.medium.restore(A, B)
        m.run(m.net.now + 4 * P)
        self.assertEqual(m.consistent((A, B, C, D)), [])
        self.assertFalse(m.looped((A, B, C, D)))
        self.assertEqual(m.preferred(A), ROOT)
        # D's messages are delivered at most once each (they may be lost in
        # a loop of the isolated phase: dropped as duplicates, §9).
        self.assertEqual(len(sent_d), 2)
        delivered = [p for o, p in m.root.inbox_from(APP) if o == D]
        self.assertEqual(len(delivered), len(set(delivered)))


class OneWayCut(unittest.TestCase):
    """Root — A, B (hearing each other) — C. C's parent stops hearing C (one
    way) and C has no class 1 traffic: only its presence reports, no longer
    forwarded, tell it (§9 passive acknowledgement of presence). Its parent's
    ETX rises until another candidate wins by the switch threshold; once C
    sends nothing more there, the failures age out 2.5 periods after the
    newest one (§9): the link reads as good again, C still hearing it."""

    def test_presence_failures_then_ageing(self):
        names = (ROOT, A, B, C)
        links = ((ROOT, A, 10), (ROOT, B, 10), (A, B, 10), (A, C, 12), (B, C, 8))
        m = Mesh(8, names, links)
        m.run(12 * m.imin + 3 * P)
        first = m.preferred(C)
        self.assertTrue(first in (A, B))
        c = m.b(C)
        table = c.engine.neighbours
        entry = table.get(first)
        self.assertTrue(entry.ack_count >= 1)  # presence samples (a forward may be lost)
        failures = []  # times of the failed samples of first
        acked = table.acked

        def record(address, ok, *now):
            if address == first and not ok:
                failures.append(m.net.now)
            return acked(address, ok, *now)

        table.acked = record
        cut = m.net.now
        m.net.medium.cut(C, first)
        etx = []  # (time, ETX of first) at each change, the table read at that time
        left = []  # time C left first

        def watch():
            now = m.net.now
            if not left and m.preferred(C) != first:
                left.append(now - cut)
            table.expire(now)
            e = table.get(first)
            value = e.etx() if e is not None else None
            if not etx or etx[-1][1] != value:
                etx.append((now, value))
            m.net.at(now + 50, watch)

        m.net.at(cut, watch)
        m.run(cut + 20 * P)
        self.assertTrue(left, etx)
        t = left[0]
        # One failed sample per presence (none more after the switch, but a
        # presence whose wait was pending), then the switch.
        self.assertTrue(failures and failures[0] > cut, failures)
        self.assertTrue(len(failures) <= t // (9 * P // 10) + 2, (t, failures))
        gaps = [b - a for a, b in zip(failures, failures[1:])]
        self.assertTrue(min(gaps) >= 9 * P // 10 - 100, gaps)
        self.assertTrue(failures[-1] <= cut + t + P, (t, failures))
        # Aged out 2.5 periods after the newest failure (the watch's step
        # aside): successes kept, ETX 1 again; high until then.
        aged = failures[-1] + 5 * P // 2
        before = [v for u, v in etx if failures[-1] <= u < aged]
        self.assertTrue(before and min(before) > 128, (failures[-1], etx))
        self.assertEqual([v for u, v in etx if u >= aged], [128])
        self.assertTrue(etx[-1][0] - aged <= 50, (aged, etx[-1]))
        self.assertTrue(entry.latched and entry.ack_count == entry.acknowledged() >= 1)
        self.assertTrue(entry.d_in() >= 85, entry.d_in())  # heard well all along


class PartitionAndReturn(unittest.TestCase):
    def test_subtree_partitioned_then_healed(self):
        m = six(6)
        m.run(12 * m.imin + 3 * P)
        sub = (C, D, E)
        start = m.net.now
        log = m.watch(sub, start)
        m.net.medium.partition(sub, (ROOT, A, B))
        loops = m.loop_samples(sub, start, start + 5 * P)
        e = m.b(E)
        m.run(start + 5 * P)
        # Cut off: inside the subtree a node may follow a neighbour whose own
        # view is still stale (its parent heard less than 2.5 periods ago, a
        # poison lost to a collision), so a loop may form, but only for a
        # while: the next frame across it carries the ranks (data-path
        # validation, Trickle). Every node is detached for good once the
        # stale views have expired: 2.5 periods, then a poison wave (§9).
        for n in sub:
            states = [(t, r) for t, x, p, r in log if x == n]
            last = states[-1]
            self.assertEqual(last[1], RANK_INFINITE, (n, states))
            self.assertTrue(last[0] <= 5 * P // 2 + 3 * m.imin, (n, states))
        if loops:
            self.assertTrue(max(episodes(loops)) <= P + 2 * m.imin, episodes(loops))
        late = e.submit(APP, 1, ROOT, b"late")  # held: no route
        heal = m.net.now
        m.run(heal + 500)
        m.net.medium.heal(sub, (ROOT, A, B))
        m.run(heal + 3 * P)
        # Back: reattached within one solicitation period, routes consistent.
        for n in sub:
            back = [t for t, x, p, r in log if x == n and t > heal - start and r != RANK_INFINITE]
            self.assertTrue(back and back[0] - (heal - start) <= P + 3 * m.imin, (n, back))
        self.assertEqual(m.consistent(NODES), [])
        # No stale route kept: every presence the root got after the heal
        # names a parent the node really hears; the latest one names the
        # current parent unless the node changed parent after it.
        for n in sub:
            after = [x for x in m.presences[n] if x[0] > heal]
            self.assertTrue(after, n)
            for _, _, parent in after:
                self.assertTrue(m.linked(n, parent) and m.linked(parent, n), (n, parent))
            changed = [t for t, x, p, r in log if x == n][-1] + start
            generated = dict((c, t) for t, c, p in m.generated[n])
            if generated[after[-1][1]] > changed:
                self.assertEqual(after[-1][2], m.preferred(n))
        self.assertEqual([p for o, p in m.root.inbox_from(APP) if o == E], [b"late"])
        self.assertTrue(late >= 0)


class _Cutoff:
    """Line root — A — B — C, formed. ``cut(lose)``: A and B stop hearing
    each other (A dies for B: B's subtree is cut off), B detaches, C behind
    it; the first lose frames B sends at rank 255 are lost at C (link
    B -> C cut while each is on air; lose None: every one until B has a
    finite rank again). ``on_detach(now)`` is called at B's first frame at
    rank 255 (its poison); ``lost`` lists the times of the frames lost;
    ``poisoned`` the times C heard a frame of B at rank 255."""

    def __init__(self, seed):
        names = (ROOT, A, B, C)
        links = ((ROOT, A, 10), (A, B, 10), (B, C, 10))
        m = Mesh(seed, names, links)
        m.run(12 * m.imin + 2 * P)
        # Formed: ranks may still move (hidden terminals: root and B, A and
        # C), a parent's rank rise followed by its child (§9). Each link
        # to a parent also reads an ETX of 2 at most (cost 16): samples
        # lost to those hidden terminals while the windows are short can
        # leave a link at the ETX limit of 4, which one more loss makes
        # ineligible (a detach the scenarios do not script); failures age
        # out within the silence limit (§9).
        for _ in range(90):
            if (
                [m.preferred(n) for n in (A, B, C)] == [ROOT, A, B]
                and not m.consistent((A, B, C))
                and max(self._costs(m)) <= 2 * MIN_HOP
            ):
                break
            m.run(m.net.now + 1000)
        else:
            raise AssertionError("line not formed", self._costs(m))
        self.m = m
        self.b, self.c = m.b(B), m.b(C)
        self.lost = []  # times of B's frames lost at C
        self.detached = None  # time of B's first frame at rank 255
        self.poisoned = []  # times C heard a frame of B at rank 255
        self._left = 0
        self._sent = self.b.engine.sent
        self.b.engine.sent = self._sent_by_b
        self._heard = self.c.engine.heard
        self.c.engine.heard = self._heard_by_c

    @staticmethod
    def _costs(m):
        # Link cost (§9) of A, B and C towards their parents, from their view.
        out = []
        for n, p in ((A, ROOT), (B, A), (C, B)):
            view = m.b(n).engine.neighbours.get(p)
            out.append(link_cost(view.etx()) if view is not None else RANK_INFINITE)
        return out

    def _heard_by_c(self, header, rssi, snr, now):
        if header.source == B and header.rank == RANK_INFINITE:
            self.poisoned.append(now)
        return self._heard(header, rssi, snr, now)

    def check_loops(self, test, loops, log):
        """No loop through C while C heard B's frames at rank 255 (§9: one
        loss in a row per link). If B's next frame at rank 255 (its
        solicitation) was lost at C as well (collision, half duplex), two
        losses in a row: B may rejoin through C, and the loop ends by a
        switch or at the L + 56 bound, within one Imin per hop of increase
        (§9 loop freedom). Returns True if such a loop formed."""
        m = self.m
        if not loops:
            return False
        first = [t for t in self.poisoned if t > self.lost[-1]]
        test.assertTrue(not first or first[0] > loops[0], (self.lost, self.poisoned, loops[0]))
        bound = (INFO.max_increase // MIN_HOP + 1) * m.imin
        test.assertTrue(max(episodes(loops)) <= bound, (episodes(loops), bound, log))
        return True

    def _sent_by_b(self, sel, counter, now):
        self._sent(sel, counter, now)
        e = self.b.engine
        if e.rank != RANK_INFINITE:
            if self._left is None and self.lost:
                self._left = 0  # B has a rank again: losses over
            return
        if self.detached is None:
            self.detached = now
            self.on_detach(now)
        if self._left is None or self._left > 0:
            if self._left is not None:
                self._left -= 1
            self.lost.append(now)
            medium = self.m.net.medium
            medium.cut(B, C)
            self.m.net.at(now + sel.toa_ms + 1, medium.restore, B, C)

    def on_detach(self, now):
        pass

    def cut(self, lose):
        self._left = lose
        self.m.net.medium.cut(A, B)
        self.m.net.medium.cut(B, A)


class _Lagging(_Cutoff):
    """`_Cutoff` whose B loses its DODAG information at C while ``lagging``
    (link B -> C cut while each one is on air): after a new version, B
    adopts it and C stays behind it, in the old version."""

    def __init__(self, seed):
        self.lagging = True
        _Cutoff.__init__(self, seed)

    def _sent_by_b(self, sel, counter, now):
        _Cutoff._sent_by_b(self, sel, counter, now)
        if self.lagging and any(m.port == DODAG_INFO and len(m.payload) for m in sel.messages):
            medium = self.m.net.medium
            medium.cut(B, C)
            self.m.net.at(now + sel.toa_ms + 1, medium.restore, B, C)


class PoisonLost(unittest.TestCase):
    """B's poison is lost at C, which keeps B as parent and sends a frame
    during B's hold: B must not rejoin through C, its own child (TLA+
    `DODAG_line_solicit_nocycle`: no loop with at most one loss in a row
    per link). If B's solicitation is lost at C too (a collision, not
    scripted), the loop the design accepts after two losses in a row must
    end within its bound (`_Cutoff.check_loops`)."""

    def test_no_rejoin_through_a_child(self):
        s = _Cutoff(11)
        m, c = s.m, s.c
        sent = []

        def on_detach(now):
            # A class 0 message of C, still attached through B, reaches B
            # in the middle of its hold (dropped there: no route).
            m.net.at(now + m.imin, lambda: sent.append(c.submit(APP, 0, ROOT, b"c")))

        s.on_detach = on_detach
        start = m.net.now
        log = m.watch((B, C), start)
        loops = m.loop_samples((B, C), start, start + 6 * P)
        s.cut(1)
        m.run(start + 6 * P)
        self.assertEqual(len(s.lost), 1)
        self.assertEqual(sent, [0])
        if not s.check_loops(self, loops, log):
            self.assertEqual([p for t, n, p, r in log if n == B and p == C], [])
        # Both cut off: detached for good (C heard B's solicitation, or the
        # loop ended at the bound).
        self.assertEqual((m.rank(B), m.rank(C)), (RANK_INFINITE, RANK_INFINITE))
        # A heard again: both rejoin, consistent.
        m.net.medium.restore(A, B)
        m.net.medium.restore(B, A)
        m.run(m.net.now + 3 * P)
        self.assertEqual([m.preferred(n) for n in (A, B, C)], [ROOT, A, B])
        self.assertEqual(m.consistent((A, B, C)), [])


class AdoptionDuringTheHold(unittest.TestCase):
    """TLA+ `DODAG_diamond_versions_adopt` on a line: B adopts a new version,
    C stays behind it in the old one (B's DODAG information lost at C), then
    A dies for B: B detaches, its poison lost at C, which keeps B and
    advertises the old version during B's hold. B must not adopt it from
    its own child (a loop): a detached node adopts another version only
    after its solicitation, which C hears and detaches on."""

    def test_no_adoption_from_a_child(self):
        s = _Lagging(13)
        m, b, c = s.m, s.b, s.c
        start = m.net.now
        m.root.routing.dodag.set_version(1, start)
        m.root.node.wake()
        m.run(start + 2 * P)
        cd = c.routing.dodag
        self.assertEqual((b.routing.dodag.version, cd.version), (1, 0))
        self.assertEqual((cd.parents.preferred, cd.parents.behind), (B, True))

        def advertise():
            cd.rank_error(m.net.now)  # an advertisement at once
            c.node.wake()

        def on_detach(now):
            # C, still attached through B, advertises during B's hold, once
            # B's poison is over (B does not hear while it sends).
            m.net.at(now + 100, advertise)

        s.on_detach = on_detach
        infos = []  # (time, rank) of C's DODAG information
        base = c.engine.sent

        def sent(sel, counter, now):
            base(sel, counter, now)
            if any(m.port == DODAG_INFO and len(m.payload) for m in sel.messages):
                infos.append((now, c.engine.rank))

        c.engine.sent = sent
        cut = m.net.now
        log = m.watch((B, C), cut)
        loops = m.loop_samples((B, C), cut, cut + 3 * P)
        s.cut(1)
        m.run(cut + 5 * P // 2 + 4 * m.imin)
        self.assertEqual(len(s.lost), 1)  # B's poison
        self.assertEqual([p for t, n, p, r in log if n == B and p == C], [])
        self.assertEqual(loops, [])
        self.assertEqual(b.routing.dodag.version, 1)
        self.assertEqual((m.rank(B), m.rank(C)), (RANK_INFINITE, RANK_INFINITE))
        # C advertised the old version at a finite rank during B's hold
        # (B's solicitation ended it).
        ended = b.routing.parents.poisoned_at
        during = [t for t, r in infos if s.detached <= t < ended and r != RANK_INFINITE]
        self.assertTrue(during, (infos, s.detached, ended))
        # A heard again: both rejoin in the new version, consistent.
        s.lagging = False
        m.net.medium.restore(A, B)
        m.net.medium.restore(B, A)
        m.run(m.net.now + 3 * P)
        self.assertEqual([m.preferred(n) for n in (A, B, C)], [ROOT, A, B])
        self.assertEqual([m.b(n).engine.dodag_version for n in (A, B, C)], [1, 1, 1])
        self.assertEqual(m.consistent((A, B, C)), [])


class LoopCustody(unittest.TestCase):
    """TLA+ `DODAG_line_current_MP5` on the code: B detaches; C, which lost
    B's poison, sends its class 1 message to B, which holds it; B rejoins
    through C, its own child, and the message loops. B's frames at rank 255
    are lost at C until B rejoins: its poison, but also its explicit
    acknowledgements and solicitation, which in the code carry its rank
    255 in their header (the model's acknowledgements carry no rank)."""

    def test_looped_message_delivered_or_failed(self):
        s = _Cutoff(12)
        m, b, c = s.m, s.b, s.c
        sent = []

        def on_detach(now):
            # C's message reaches B shortly before its answer window closes
            # (C drops B after its class 1 attempts, about 2 s).
            p = b.routing.parents
            due = p.hold_until + p.window_ms - 1000
            m.net.at(due, lambda: sent.append(c.submit(APP, 1, ROOT, b"m")))
            # A heard again once the loop is over.
            m.net.at(now + 4 * m.imin, m.net.medium.restore, A, B)
            m.net.at(now + 4 * m.imin, m.net.medium.restore, B, A)

        s.on_detach = on_detach
        start = m.net.now
        log = m.watch((B, C), start)
        s.cut(None)
        m.run(start + 3 * P + 4 * m.imin + 3 * P)
        self.assertTrue(len(sent) == 1 and sent[0] >= 0, sent)
        # B rejoined through C (its poison and solicitation lost at C): the
        # loop formed, beyond what the solicitation tolerates (TLA+
        # `DODAG_line_solicit_consec2`).
        self.assertTrue([p for t, n, p, r in log if n == B and p == C], log)
        # MP5: delivered (once: the root filters the copies that custody
        # makes travel), or the origin told.
        got = [p for o, p in m.root.inbox_from(APP) if o == C]
        failed = [f for f in c.failures if f[3] == sent[0]]
        self.assertTrue((len(got), len(failed)) in ((1, 0), (0, 1)), (got, failed))


class RejoinBest(unittest.TestCase):
    """TLA+ `DODAG_rejoin_*` on the code: root — A — C and root — B — F — D
    — E. C (rank 16 under A) detaches when A dies; the links C — D and C —
    E come up at its poison (D at 24, E at 32: no candidates of C at 16,
    nor C, then detached, of D or E). After its solicitation C hears E
    early (a report E sends at once), and D, better by 8, less than the
    switch threshold: taking the first candidate heard would keep C on E
    for good. C waits for the close of its answer window (Imin + 8 slots
    after the solicitation) and takes D, the best candidate heard in it
    (§9)."""

    def test_rejoin_through_the_best_candidate_of_the_window(self):
        f = 0x00F1
        names = (ROOT, A, B, C, D, E, f)
        links = (
            (ROOT, A, 10),
            (A, C, 10),
            (ROOT, B, 10),
            (B, f, 10),
            (f, D, 10),
            (D, E, 10),
            (C, D, 10),
            (C, E, 10),
        )
        m = Mesh(14, names, links)
        medium = m.net.medium
        medium.partition((C,), (D, E))
        m.run(12 * m.imin + 3 * P)
        for _ in range(60):
            if [m.preferred(n) for n in (A, C, f, D, E)] == [ROOT, A, B, f, D]:
                break
            m.run(m.net.now + 1000)
        else:
            raise AssertionError("graph not formed")
        c, e = m.b(C), m.b(E)
        p = c.routing.parents
        solicited = []  # times of C's solicitations at rank 255
        heard = []  # (time, source) of the frames C hears after the first one
        base_sent, base_heard = c.engine.sent, c.engine.heard

        def sent(sel, counter, now):
            base_sent(sel, counter, now)
            if c.engine.rank != RANK_INFINITE:
                return
            medium.heal((C,), (D, E))  # from C's poison on
            if any(x.port == DODAG_INFO and not len(x.payload) for x in sel.messages):
                solicited.append(now)
                if len(solicited) == 1:  # E has a report to send (class 0)
                    m.net.at(now + 200, e.submit, APP, 0, ROOT, b"e")

        def on_heard(header, rssi, snr, now):
            if solicited and header.rank != RANK_INFINITE:
                heard.append((now, header.source))
            return base_heard(header, rssi, snr, now)

        c.engine.sent = sent
        c.engine.heard = on_heard
        kill = m.net.now
        log = m.watch((C,), kill)
        m.b(A).power_loss(off_ms=OFF)
        # Silence limit, hold, window (3.25 Imin and the random delays).
        m.run(kill + 5 * P // 2 + 5 * m.imin)
        self.assertEqual(c.routing.dodag.counters.poisons, 1)
        self.assertTrue(solicited, log)
        close = solicited[0] + c.routing.dodag.imin_ms + 8 * c.engine.slot_ms
        # Both heard in the window.
        sources = [s for t, s in heard if t < close]
        self.assertTrue(E in sources and D in sources, heard)
        # C rejoined at the close, through D, and keeps it.
        rejoin = [(t + kill, x) for t, n, x, r in log if t + kill > solicited[0]]
        self.assertTrue(rejoin, log)
        self.assertEqual(rejoin[0][1], D, log)
        self.assertTrue(close <= rejoin[0][0] <= close + 10, (close, rejoin[0]))
        self.assertEqual([x for t, x in rejoin if x != D], [], log)
        self.assertEqual([p.preferred, m.preferred(D), m.preferred(E)], [D, f, D])
        self.assertEqual(m.consistent((B, C, D, E, f)), [])
