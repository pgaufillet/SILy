"""Presence, topology and node information reports (MESH_DESIGN §11 ports 1, 2, 5; §16; R20, R24).

Codecs on their own; the reporter on a line root — R — N of real link
engines with their routing (`sim.net`, SF7, presence period 10 s, topology
period 60 s), a sniffer opening every frame with the network key.
"""

import unittest

from mesh import frame
from mesh.counter import FrameCounter
from mesh.dodag import TOPOLOGY_ON_CHANGE, DodagInfo
from mesh.frame import FIRST_HOP, ROOT, UPWARD
from mesh.ports import NODE_INFO, PRESENCE, TOPOLOGY
from mesh.profile import EU_869
from mesh.replay import ReplayTable
from mesh.reports import (
    ADDRESS_CONFLICT,
    AUTH_FAILURES,
    DUTY_HIGH,
    FIRST_REPORT,
    MARGIN_UNKNOWN,
    MAX_EXTENSION,
    NEXT_KEY,
    NODE_INFO_EVERY,
    QUEUE_OVERFLOW,
    RANK_ERRORS,
    TAILS,
    NodeInfo,
    Presence,
    decode_node_info,
    decode_presence,
    decode_topology,
    encode_node_info,
    encode_presence,
    encode_topology,
    jittered,
    margin_db,
    restore_parent,
)
from mesh.routing import Routing
from mesh.security import KeyRing, Security
from sim.net import Network
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
PERIOD = 10000
TOPO = 60000
R = 0x0010
N = 0x00A1
E = "eve"


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


class PresenceCodec(unittest.TestCase):
    def test_first_hop_form_round_trip(self):
        p = Presence(FIRST_REPORT | NEXT_KEY, b"\x5a")
        payload = encode_presence(p)
        self.assertEqual(payload, b"\x81\x5a")  # flags, SILy extension: 17 B frame (§11)
        self.assertEqual(decode_presence(payload, parent=False), p)

    def test_upward_form_with_tails_round_trip(self):
        p = Presence(RANK_ERRORS, b"\x01\x02", 0x1B07, [(200, 0x1234, 0xFFFF), (16, 0, 1)])
        payload = encode_presence(p)
        self.assertEqual(payload[:4], b"\x07\x1b\x18\x02")  # parent, flags with bit 3, count
        self.assertEqual(payload[4:9], b"\xc8\x34\x12\xff\xff")
        self.assertEqual(len(payload), 2 + 1 + 1 + 2 * 5 + 2)
        q = decode_presence(payload)
        self.assertEqual(q.flags, RANK_ERRORS | TAILS)
        self.assertEqual((q.parent, q.tails, q.extension), (0x1B07, p.tails, b"\x01\x02"))

    def test_tails_flag_follows_the_tails(self):
        self.assertEqual(encode_presence(Presence(TAILS)), b"\x00")
        self.assertEqual(decode_presence(b"\x05\x00\x02").flags, 0x02)

    def test_restore_parent(self):
        own = encode_presence(Presence(QUEUE_OVERFLOW, b"\x07"))
        q = decode_presence(restore_parent(own, 0x0010))
        self.assertEqual((q.parent, q.flags, q.extension), (0x0010, QUEUE_OVERFLOW, b"\x07"))

    def test_malformed(self):
        for payload, parent in (
            (b"", False),
            (b"\x01\x00", True),
            (b"\x08", False),  # tails flag without count
            (b"\x08\x00", False),  # zero tails
            (b"\x08\x01\x10\x00\x00\x00", False),  # truncated tail
            (b"\x08\x09" + bytes(45), False),  # above 8 flows
        ):
            with self.assertRaises(ValueError):
                decode_presence(payload, parent)
        for p in (Presence(256), Presence(0, b"", 0x10000), Presence(0, b"", None, [(256, 0, 0)])):
            with self.assertRaises(ValueError):
                encode_presence(p)


class TopologyCodec(unittest.TestCase):
    def test_round_trip(self):
        entries = [(0x0010, 18, True, 100), (0xA1B2, -5, False, 63), (0xFFFE, -128, False, 0)]
        payload = encode_topology(entries)
        self.assertEqual(payload[0], 3)
        self.assertEqual(payload[1:5], b"\x10\x00\x12\xe4")  # 0x80 | 100
        self.assertEqual(payload[5:9], b"\xb2\xa1\xfb\x3f")
        self.assertEqual(len(payload), 13)
        self.assertEqual(decode_topology(payload), entries)
        self.assertEqual(decode_topology(encode_topology([])), [])

    def test_malformed(self):
        for payload in (b"", b"\x01\x10\x00\x12", b"\x01\x10\x00\x12\x65", b"\x00\x00"):
            with self.assertRaises(ValueError):
                decode_topology(payload)
        for entries in ([(0x10000, 0, False, 0)], [(1, 128, False, 0)], [(1, 0, False, 101)]):
            with self.assertRaises(ValueError):
                encode_topology(entries)

    def test_margin_rounding_and_saturation(self):
        self.assertEqual(
            [margin_db(t) for t in (175, 174, -24, -25, 2000, -2000, None)],
            [18, 17, -2, -2, 127, -127, MARGIN_UNKNOWN],
        )


class NodeInfoCodec(unittest.TestCase):
    def test_round_trip(self):
        info = NodeInfo(b"\x24\x0a\xc4\x12\x34\x56", 0x0203, "Poste 31")
        payload = encode_node_info(info)
        self.assertEqual(payload[:9], b"\x24\x0a\xc4\x12\x34\x56\x01\x03\x02")
        self.assertEqual(payload[9:], b"Poste 31")
        self.assertEqual(decode_node_info(payload), info)
        unnamed = NodeInfo(bytes(6), 1, "")
        self.assertEqual(decode_node_info(encode_node_info(unnamed)), unnamed)

    def test_malformed(self):
        for payload in (bytes(8), bytes(26), bytes(9) + b"\xff\xfe"):
            with self.assertRaises(ValueError):
                decode_node_info(payload)
        for info in (
            NodeInfo(bytes(5)),
            NodeInfo(bytes(6), 0x10000),
            NodeInfo(bytes(6), 0, "x" * 17),
        ):
            with self.assertRaises(ValueError):
                encode_node_info(info)


class Jitter(unittest.TestCase):
    def test_within_ten_percent_and_spread(self):
        rng = XorShift32(5)
        values = [jittered(10000, rng) for _ in range(400)]
        self.assertTrue(min(values) >= 9000 and max(values) <= 11000)
        self.assertTrue(min(values) < 9200 and max(values) > 10800)


class Line:
    """root — R — N with routing; reports received by the root in ``reports``."""

    def __init__(
        self,
        seed,
        info=INFO,
        extension=None,
        tails=None,
        node_info=None,
        budget_ms=None,
        window_ms=None,
    ):
        net = Network(seed, _KEY, budget_ms=budget_ms, window_ms=window_ms)
        self.net = net
        self.reports = []  # (time, port, origin, report)
        self.ext_calls = 0
        self.root = net.add(ROOT, routing=self._make(info, None, None, None))
        self.relay = net.add(R, routing=self._make(info, None, None, None))
        self.node = net.add(N, routing=self._make(info, extension, tails, node_info))
        net.link_both(ROOT, R)
        net.link_both(R, N)
        self.eve = net.sniffer(E)
        for name in (ROOT, R, N):
            net.medium.link(name, E)

    def _make(self, info, extension, tails, node_info):
        def make(engine):
            r = Routing(
                engine, EU_869, info, extension=extension, tails=tails, node_info=node_info
            )
            if engine.is_root:
                r.on_report = self._report
            return r

        return make

    def _report(self, port, origin, report, header):
        self.reports.append((self.net.now, port, origin, report))

    def of(self, port, origin):
        return [(t, r) for t, p, o, r in self.reports if p == port and o == origin]

    def frames(self, source):
        out = []
        for t, f in self.eve.frames:
            h, msgs = _open(f)
            if h.source == source:
                out.append((t, h, msgs, len(f)))
        return out

    def formed(self):
        self.net.run(30000)
        assert [b.routing.parents.preferred for b in (self.relay, self.node)] == [ROOT, R]


class Presences(unittest.TestCase):
    def test_first_hop_compression_and_relay_rewrite(self):
        line = Line(3, extension=lambda: b"\x5a")
        line.formed()
        line.net.run(line.net.now + 3 * PERIOD)
        own = [(t, h, m, n) for t, h, m, n in line.frames(N) if m[0].port == PRESENCE]
        self.assertTrue(len(own) >= 4)
        t, h, msgs, size = own[-1]
        # Mode 6, single message, no origin and no parent: 17 B (§11).
        self.assertEqual(
            (h.dest, len(msgs), msgs[0].mode, msgs[0].origin), (R, 1, FIRST_HOP, None)
        )
        self.assertEqual((size, bytes(msgs[0].payload)), (17, b"\x00\x5a"))
        # The relay forwards it in mode 1, origin and parent restored.
        fwd = []
        for _, rh, rmsgs, _ in line.frames(R):
            for m in rmsgs:
                if m.port == PRESENCE and m.mode == UPWARD:
                    fwd.append((rh.dest, m.origin, bytes(m.payload)))
        self.assertTrue(len(fwd) >= 4)
        self.assertEqual(fwd[-1], (ROOT, N, b"\x10\x00\x00\x5a"))
        # The root decodes both forms with their parent.
        by_n = line.of(PRESENCE, N)
        by_r = line.of(PRESENCE, R)
        self.assertEqual(set(r.parent for _, r in by_n), set((R,)))
        self.assertEqual(set(r.parent for _, r in by_r), set((ROOT,)))
        self.assertEqual(by_n[-1][1].extension, b"\x5a")
        self.assertEqual(line.root.routing.counters.malformed, 0)

    def test_periodic_with_jitter_and_first_report_flag(self):
        generated = []
        line = Line(4, extension=lambda: generated.append(line.net.now) or b"")
        line.formed()
        line.net.run(line.net.now + 12 * PERIOD)
        times = [t for t, r in line.of(PRESENCE, N)]
        flags = [r.flags for t, r in line.of(PRESENCE, N)]
        self.assertEqual(flags[0], FIRST_REPORT)
        self.assertEqual(set(flags[1:]), set((0,)))
        gaps = [b - a for a, b in zip(generated, generated[1:])]
        self.assertTrue(len(gaps) >= 12, gaps)
        # +-10 % around the period.
        self.assertTrue(min(gaps) >= 9000 and max(gaps) <= 11000, gaps)
        self.assertTrue(max(gaps) - min(gaps) > 300, gaps)  # jittered, not fixed
        self.assertEqual(line.node.routing.reporter.counters.presence, len(generated))
        # Every one reaches the root, but those lost to a collision at R (the
        # root and N are hidden from each other, §8).
        lost = len(generated) - len(times)
        self.assertTrue(0 <= lost <= line.net.medium.stats(R).rx_collision, (lost, times))

    def test_flags_since_last_report(self):
        line = Line(5)
        line.formed()
        e = line.node.engine
        rep = line.node.routing.reporter
        now = line.net.now
        e.queue.overflow = True
        e.address_conflict = True
        e.counters.rx_auth += 1
        line.node.routing.dodag.counters.rank_errors += 2
        f = rep.flags(now)
        self.assertEqual(f, QUEUE_OVERFLOW | ADDRESS_CONFLICT | AUTH_FAILURES | RANK_ERRORS)
        self.assertEqual((e.queue.overflow, e.address_conflict), (False, False))
        # Kept until a presence carrying them goes on air.
        self.assertEqual(rep.flags(now), f)
        rep._send_presence(now)
        line.node.node.wake()
        line.net.run(now + 1000)
        now = line.net.now
        # Nothing new since: no flag.
        self.assertEqual(rep.flags(now), 0)
        # Current state flags: next key loaded, duty-cycle use above 80 %.
        e.security.keys.set(1, bytes(16))
        self.assertEqual(rep.flags(now), NEXT_KEY)
        dc = e.dutycycle
        dc.start(now, dc.budget_ms * 81 // 100)
        dc.done(now, 0)
        self.assertEqual(rep.flags(now), NEXT_KEY | DUTY_HIGH)
        e.security.keys.set(1, None)
        self.assertEqual(rep.flags(now), DUTY_HIGH)

    def test_duty_cycle_flag_survives_refused_presence(self):
        # Bit 2 (§11): duty-cycle use above 80 % since the last report. Above
        # 80 %, presence (priority 2) is refused: the flag goes with the next
        # presence on air once use has fallen, and only with that one.
        line = Line(14, budget_ms=4000, window_ms=60000)
        line.formed()
        e = line.node.engine
        dc = e.dutycycle
        start = line.net.now
        size = e.toa_ms(frame.OVERHEAD + 2 + 2 + 200)  # one 200 B message per frame
        count = (dc.budget_ms * 85 // 100 - dc.used(start)) // size + 1
        for _ in range(count):
            self.assertTrue(line.node.submit(20, 0, ROOT, bytes(200)) >= 0)  # priority 1
        high = []

        def watch():
            if dc.used(line.net.now) * 100 > 80 * dc.budget_ms:
                high.append(line.net.now)
            line.net.at(line.net.now + 100, watch)

        line.net.at(start, watch)
        line.net.run(start + 2 * 60000)
        self.assertTrue(high and high[-1] - high[0] > 2 * PERIOD, high[-1:])
        self.assertTrue(e.counters.duty_dropped >= 1)  # presence refused meanwhile
        own = []
        for t, _, msgs, _ in line.frames(N):
            for m in msgs:
                if m.port == PRESENCE and t > start:
                    own.append((t, decode_presence(m.payload, parent=False).flags))
        while_high = [t for t, f in own if high[0] <= t <= high[-1]]
        self.assertEqual(while_high, [])
        after = [f for t, f in own if t > high[-1]]
        self.assertTrue(len(after) >= 2, own)
        self.assertEqual((after[0] & DUTY_HIGH, after[1] & DUTY_HIGH), (DUTY_HIGH, 0))

    def test_flags_reach_the_root(self):
        line = Line(6)
        line.formed()
        line.node.engine.queue.overflow = True
        line.node.routing.dodag.counters.rank_errors += 1
        line.net.run(line.net.now + 2 * PERIOD + 1500)
        flags = [r.flags for t, r in line.of(PRESENCE, N)]
        self.assertTrue(QUEUE_OVERFLOW | RANK_ERRORS in flags, flags)
        self.assertEqual(flags[-1], 0)

    def test_flag_kept_when_a_queued_presence_is_replaced(self):
        line = Line(7)
        line.formed()
        e = line.node.engine
        rep = line.node.routing.reporter
        before = len(line.of(PRESENCE, N))
        now = line.net.now
        e.queue.overflow = True
        rep._send_presence(now)  # queued with the flag, not on air yet
        first = rep._presence
        rep._send_presence(now)  # replaces it: the flag is carried over
        self.assertEqual(rep._presence[0], first[0])
        self.assertFalse(e.queue.holds(first[0], first[1]))
        line.node.node.wake()
        line.net.run(now + 1000)
        got = line.of(PRESENCE, N)[before:]
        self.assertEqual([r.flags for t, r in got], [QUEUE_OVERFLOW])

    def test_extension_hook_and_bound(self):
        values = [b"\x01", b"\x02\x03", bytes(MAX_EXTENSION + 1)]
        line = Line(8, extension=lambda: values[0])
        line.formed()
        line.net.run(line.net.now + PERIOD + 1500)
        self.assertEqual(line.of(PRESENCE, N)[-1][1].extension, b"\x01")
        values[0] = b"\x02\x03"
        line.net.run(line.net.now + PERIOD + 1500)
        self.assertEqual(line.of(PRESENCE, N)[-1][1].extension, b"\x02\x03")
        values[0] = bytes(MAX_EXTENSION + 1)
        line.net.run(line.net.now + PERIOD + 1500)
        self.assertEqual(line.of(PRESENCE, N)[-1][1].extension, b"")
        self.assertTrue(line.node.routing.reporter.counters.bad_extension >= 1)

    def test_tails_hook(self):
        tails = []
        line = Line(9, tails=lambda: tails)
        line.formed()
        tails.append((200, 7, 9))
        line.net.run(line.net.now + PERIOD + 1500)
        last = line.of(PRESENCE, N)[-1][1]
        self.assertEqual((last.flags & TAILS, last.tails), (TAILS, ((200, 7, 9),)))
        tails.pop()
        line.net.run(line.net.now + PERIOD + 1500)
        last = line.of(PRESENCE, N)[-1][1]
        self.assertEqual((last.flags & TAILS, last.tails), (0, ()))

    def test_no_presence_while_detached(self):
        line = Line(10)
        line.formed()
        line.net.medium.partition((N,), (R, ROOT, E))
        line.net.run(line.net.now + 3 * PERIOD)
        self.assertFalse(line.node.routing.attached)
        sent = line.node.routing.reporter.counters.presence
        line.net.run(line.net.now + 3 * PERIOD)
        self.assertEqual(line.node.routing.reporter.counters.presence, sent)
        # Back: a presence at once after the new parent.
        line.net.medium.heal((N,), (R, ROOT, E))
        healed = line.net.now
        line.net.run(healed + 2 * PERIOD)
        self.assertTrue(line.node.routing.attached)
        self.assertTrue(line.of(PRESENCE, N)[-1][0] > healed)


class PresenceAcknowledgement(unittest.TestCase):
    """§9: presence reports feed the parent's acknowledgement window, matched
    by origin when the parent forwards them; not when the parent is the root."""

    def test_forward_overheard_one_sample_per_presence(self):
        line = Line(15)
        line.formed()
        line.net.run(line.net.now + 6 * PERIOD)
        rep = line.node.routing.reporter.counters
        entry = line.node.engine.neighbours.get(R)
        self.assertTrue(rep.presence_acked >= 6, rep.presence_acked)
        self.assertEqual(entry.ack_count, rep.presence_acked + rep.presence_unacked)
        self.assertEqual(entry.acknowledged(), rep.presence_acked)
        # R's parent is the root: no sample of its presence.
        r = line.relay
        self.assertEqual(r.routing.reporter.counters.presence_acked, 0)
        self.assertEqual(r.routing.reporter.counters.presence_unacked, 0)
        self.assertEqual(r.engine.neighbours.get(ROOT).ack_count, 0)

    def test_parent_that_does_not_hear_the_node_is_left(self):
        # N hears R at 100 % but R no longer hears N, without class 1
        # traffic: the lost presence forwards make R's ETX exceed 4 (MP12),
        # and N leaves it (its only parent: it detaches).
        line = Line(16)
        line.formed()
        entry = line.node.engine.neighbours.get(R)
        samples = entry.ack_count
        self.assertTrue(samples >= 1 and entry.acknowledged() == samples)
        cut = line.net.now
        line.net.medium.cut(N, R)  # N -> R only
        log = []

        def watch():
            if not log and not line.node.routing.attached:
                log.append(line.net.now)
            line.net.at(line.net.now + 50, watch)

        line.net.at(cut, watch)
        # 3 failures per success leave ETX 4 (eligible); one more is needed,
        # one per presence period (+10 % jitter).
        line.net.run(cut + (3 * samples + 2) * 11 * PERIOD // 10 + 2000)
        self.assertTrue(log, (entry.ack_count, entry.acknowledged()))
        self.assertTrue(line.node.routing.reporter.counters.presence_unacked >= 3 * samples + 1)
        self.assertEqual(entry.d_in(), 100)


class Topologies(unittest.TestCase):
    def test_periodic_entries_and_node_information(self):
        info = NodeInfo(b"\x24\x0a\xc4\x00\x00\xa1", 0x0102, "N")
        line = Line(11, node_info=info)
        line.formed()
        line.net.run(line.net.now + 4 * TOPO)
        topo = line.of(TOPOLOGY, N)
        self.assertTrue(4 <= len(topo) <= 5, len(topo))
        gaps = [b[0] - a[0] for a, b in zip(topo, topo[1:])]
        self.assertTrue(min(gaps) >= 54000 - 300 and max(gaps) <= 66000 + 300, gaps)
        # Parent set first, flagged; margin: 10 dB SNR above the SF7 limit of
        # -7.5 dB. The relay, between two boards hidden from each other, may
        # lose a frame to a collision: its ratios stay close to 100 %.
        self.assertEqual(topo[-1][1], [(R, 18, True, 100)])
        relay = line.of(TOPOLOGY, R)[-1][1]
        self.assertEqual([e[:3] for e in relay], [(ROOT, 18, True), (N, 18, False)])
        self.assertTrue(all(e[3] >= 85 for e in relay), relay)
        # Node information with the first topology report only (then every 8th).
        infos = line.of(NODE_INFO, N)
        self.assertEqual([r for t, r in infos], [info])
        self.assertTrue(abs(infos[0][0] - topo[0][0]) < 1000)
        self.assertEqual(line.of(NODE_INFO, R), [])  # none configured
        rep = line.node.routing.reporter
        self.assertEqual(rep._info_left, NODE_INFO_EVERY - len(topo))

    def test_entries_bounded_parents_first(self):
        net = Network(12, _KEY)
        names = [ROOT, 0x20, 0x21] + list(range(0x30, 0x38))
        boards = {}
        for n in names:
            boards[n] = net.add(n, routing=lambda e: Routing(e, EU_869, INFO))
        x = 0x40
        boards[x] = net.add(x, routing=lambda e: Routing(e, EU_869, INFO))
        net.link_both(ROOT, 0x20, snr=8)
        net.link_both(ROOT, 0x21, snr=9)
        net.link_both(0x20, x, snr=12)
        net.link_both(0x21, x, snr=5)
        for i, n in enumerate(range(0x30, 0x38)):
            net.link_both(0x20, n)
            net.link_both(n, x, snr=-2 + i)
        net.run(60000)
        entries = boards[x].routing.reporter.topology(net.now)
        self.assertEqual(len(entries), 6)
        members = boards[x].routing.parents.members()
        self.assertTrue(len(members) >= 1)
        self.assertEqual([a for a, _, m, _ in entries[: len(members)]], list(members))
        self.assertTrue(all(m for _, _, m, _ in entries[: len(members)]))
        self.assertFalse(any(m for _, _, m, _ in entries[len(members) :]))
        # Others by delivery ratio, then margin, best first; the worst
        # neighbours are left out.
        others = [(r, mg) for _, mg, _, r in entries[len(members) :]]
        self.assertEqual(others, sorted(others, reverse=True))
        listed = set(a for a, _, _, _ in entries)
        for n in boards[x].engine.neighbours.entries():
            if n.address not in listed:
                self.assertTrue((n.d_in(), margin_db(n.margin())) <= others[-1], others)

    def test_on_change_in_deployment(self):
        info = DodagInfo(TOPOLOGY_ON_CHANGE, 10, 600, 32, 56, EU_869)
        line = Line(13, info=info)
        line.formed()
        line.net.run(line.net.now + 2 * PERIOD)
        sent = line.node.routing.reporter.counters.topology
        self.assertTrue(sent >= 1)
        line.net.run(line.net.now + 3 * PERIOD)
        self.assertEqual(line.node.routing.reporter.counters.topology, sent)  # no change
        # A new neighbour: a topology report at the next presence time.
        extra = line.net.add(0x0B, routing=lambda e: Routing(e, EU_869, info))
        line.net.link_both(0x0B, N)
        line.net.run(line.net.now + 2 * PERIOD + 1500)
        topo = line.of(TOPOLOGY, N)
        self.assertEqual(line.node.routing.reporter.counters.topology, sent + 1)
        self.assertTrue(0x0B in [a for a, _, _, _ in topo[-1][1]])
        self.assertTrue(extra.routing.attached)
