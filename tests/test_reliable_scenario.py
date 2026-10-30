"""Class 1 on a 3-node line with static next hops (MESH_DESIGN §12 class 1; MP5, MP15).

Line N — R — root (N and the root do not hear each other); boards run real
link engines on `sim.net`. A sniffer hears every board and opens their
frames with the network key, to check what goes on air.
"""

import unittest

from mesh import frame, ports
from mesh.counter import FrameCounter
from mesh.frame import ROOT
from mesh.link import destination
from mesh.reliable import (
    ACK_BATCH,
    HOLD,
    NO_HOP,
    NO_ROUTE,
    UNREACHABLE,
    decode_acks,
    decode_error,
)
from mesh.replay import ReplayTable
from mesh.security import KeyRing, Security
from sim.net import Network
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))

N = 0x00A1  # leaf
R = 0x0010  # relay
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


class Line:
    """N — R — root with static next hops; ``up`` is R's upward next hop."""

    def __init__(self, seed, n_reroute=None, budget_ms=None, window_ms=None):
        net = Network(seed, _KEY, budget_ms=budget_ms, window_ms=window_ms)
        self.net = net
        self.up = ROOT
        self.n_hop = R
        self.root = net.add(ROOT, next_hop=lambda m: R)
        self.relay = net.add(R, next_hop=self._relay_hop)
        self.node = net.add(N, next_hop=lambda m: self.n_hop, reroute=n_reroute)
        net.link_both(N, R)
        net.link_both(R, ROOT)
        self.eve = net.sniffer(E)
        for name in (N, R, ROOT):
            net.medium.link(name, E)
        self.root.listen(20, 200)
        self.node.listen(21)

    def _relay_hop(self, msg):
        return N if destination(msg) == N else self.up

    def frames(self, source=None):
        """Return [(time, header, messages)] of the frames on air (from source)."""
        out = []
        for t, f in self.eve.frames:
            h, msgs = _open(f)
            if source is None or h.source == source:
                out.append((t, h, msgs))
        return out

    def ack_frames(self, source):
        """Return [(time, header, entries)] of the port 3 frames sent by source."""
        out = []
        for t, h, msgs in self.frames(source):
            for m in msgs:
                if m.port == ports.ACK:
                    out.append((t, h, decode_acks(m.payload)))
        return out

    def data_frames(self, source):
        """Frames of source carrying at least one application message."""
        return [(t, h, m) for t, h, m in self.frames(source) if any(x.port >= 16 for x in m)]


class Acknowledgements(unittest.TestCase):
    def test_passive_ack_by_overhearing_the_relay(self):
        line = Line(11)
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"punch")
        line.net.run(10000)
        self.assertEqual(line.root.received(), [b"punch"])
        n = line.node.engine
        self.assertEqual((n.counters.acks_passive, n.counters.acks_explicit), (1, 0))
        self.assertEqual((len(n.queue), n.counters.retries), (0, 0))
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (1, 1))
        # The relay forwarded once and sent no acknowledgement: the forward
        # was the acknowledgement.
        self.assertEqual(len(line.data_frames(R)), 1)
        self.assertEqual(line.ack_frames(R), [])
        self.assertEqual(line.relay.engine.counters.ack_entries, 0)
        # The root, final destination, acknowledged the relay's frame in a
        # batch, after its batching delay.
        acks = line.ack_frames(ROOT)
        self.assertEqual(len(acks), 1)
        (t_fwd, h_fwd, _) = line.data_frames(R)[0]
        t_ack, h_ack, entries = acks[0]
        self.assertEqual(entries, [(R, h_fwd.counter & 0xFFFF)])
        self.assertEqual(h_ack.dest, R)
        rel = line.root.engine.reliable
        self.assertTrue(rel.batch_ms <= t_ack - t_fwd < rel.batch_ms + 200, t_ack - t_fwd)
        r = line.relay.engine
        self.assertEqual((r.counters.acks_explicit, len(r.queue)), (1, 0))

    def test_immediate_ack_by_the_destination(self):
        line = Line(12)
        line.net.at(1000, line.root.submit, 21, 1, N, b"command")
        line.net.run(10000)
        self.assertEqual(line.node.received(), [b"command"])
        root, r, n = line.root.engine, line.relay.engine, line.node.engine
        self.assertEqual(root.counters.acks_passive, 1)  # overheard R forward it
        self.assertEqual((r.counters.acks_explicit, len(r.queue)), (1, 0))
        acks = line.ack_frames(N)
        self.assertEqual(len(acks), 1)
        t_fwd, h_fwd, _ = line.data_frames(R)[0]
        t_ack, h_ack, entries = acks[0]
        self.assertEqual((h_ack.dest, entries), (R, [(R, h_fwd.counter & 0xFFFF)]))
        # At once: the frame's time on air plus carrier sense, far below a
        # timeout (the end of R's frame is t_fwd, the end of N's ack t_ack).
        self.assertTrue(t_ack - t_fwd <= n.toa_ms(frame.OVERHEAD + 2 + 4) + 5, t_ack - t_fwd)
        self.assertTrue(t_ack - t_fwd < r.reliable.timeout_ms(N) // 4)

    def test_duplicate_reacknowledged_not_forwarded(self):
        line = Line(13)
        n = line.node.engine
        overheard = n.overheard
        missed = []

        def deaf_once(h, msgs, now):
            if not missed:
                missed.append(now)  # N misses R's forward
                return
            overheard(h, msgs, now)

        n.overheard = deaf_once
        n.reliable.retry_ms = 1  # N retries at its timeout, while R holds its copy
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"punch")
        line.net.run(10000)
        self.assertEqual(len(missed), 1)
        self.assertEqual(line.root.received(), [b"punch"])  # once
        r = line.relay.engine
        self.assertEqual(r.counters.duplicates, 1)
        self.assertEqual(len(line.data_frames(R)), 1)  # not forwarded again
        self.assertEqual(n.counters.retries, 1)
        self.assertEqual((n.counters.acks_passive, n.counters.acks_explicit), (0, 1))
        frames_n = line.data_frames(N)
        acks = line.ack_frames(R)
        self.assertEqual((len(frames_n), len(acks)), (2, 1))
        self.assertEqual(acks[0][2], [(N, frames_n[1][1].counter & 0xFFFF)])
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (2, 1))
        self.assertEqual(len(n.queue), 0)


class RootBatching(unittest.TestCase):
    def test_eight_entries_per_frame_then_delay(self):
        line = Line(14)
        count = ACK_BATCH + 2
        for i in range(count):
            line.net.at(1000 + 200 * i, line.relay.submit, 20, 1, ROOT, b"m%d" % i)
        line.net.run(15000)
        self.assertEqual(sorted(line.root.received()), sorted(b"m%d" % i for i in range(count)))
        sent = line.data_frames(R)
        self.assertEqual(len(sent), count)  # one frame per message
        acks = line.ack_frames(ROOT)
        self.assertEqual([len(e) for _, _, e in acks], [ACK_BATCH, 2])
        counters = [h.counter & 0xFFFF for _, h, _ in sent]
        self.assertEqual([c for _, c in acks[0][2]], counters[:ACK_BATCH])
        self.assertEqual([c for _, c in acks[1][2]], counters[ACK_BATCH:])
        self.assertEqual(acks[0][1].dest, R)
        # The full batch leaves at once; the rest after the batching delay.
        rel = line.root.engine.reliable
        self.assertTrue(acks[0][0] - sent[ACK_BATCH - 1][0] < 200)
        self.assertTrue(acks[1][0] - sent[ACK_BATCH][0] >= rel.batch_ms)
        r = line.relay.engine
        self.assertEqual(
            (r.counters.acks_explicit, r.counters.retries, len(r.queue)), (count, 0, 0)
        )

    def test_entries_of_several_sources_to_every_neighbour(self):
        net = Network(15, _KEY)
        a, b = 0x00A1, 0x00B1
        root = net.add(ROOT, next_hop=lambda m: m.dest)
        boards = [net.add(x, next_hop=lambda m: ROOT) for x in (a, b)]
        net.full_mesh((ROOT, a, b))
        eve = net.sniffer(E)
        for name in (ROOT, a, b):
            net.medium.link(name, E)
        root.listen(20)
        net.at(1000, boards[0].submit, 20, 1, ROOT, b"from a")
        net.at(1300, boards[1].submit, 20, 1, ROOT, b"from b")
        net.run(8000)
        self.assertEqual(sorted(root.received()), [b"from a", b"from b"])
        acks = []
        for _, f in eve.frames:
            h, msgs = _open(f)
            if h.source == ROOT:
                acks.extend((h.dest, decode_acks(m.payload)) for m in msgs)
        self.assertEqual(len(acks), 1)
        dest, entries = acks[0]
        self.assertEqual((dest, sorted(s for s, _ in entries)), (frame.BROADCAST, [a, b]))
        for board in boards:
            c = board.engine.counters
            self.assertEqual((c.acks_explicit, c.retries, len(board.engine.queue)), (1, 0, 0))


class Failures(unittest.TestCase):
    def _hear_relay(self, line):
        # N hears R once, so that R is in N's neighbour table.
        line.net.at(300, line.relay.submit, 21, 0, N, b"hello")

    def test_three_attempts_then_failure(self):
        line = Line(16)
        self._hear_relay(line)
        line.net.at(900, line.net.medium.cut, N, R)
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"lost")
        line.net.run(20000)
        n = line.node.engine
        self.assertEqual(line.net.medium.stats(N).tx_count, 3)
        self.assertEqual(len(line.node.failures), 1)
        t, dest, port, seq, reason = line.node.failures[0]
        self.assertEqual((dest, port, reason), (ROOT, 20, UNREACHABLE))
        self.assertEqual(seq, line.data_frames(N)[0][2][0].seq)
        timeout = n.reliable.timeout_ms(R)
        # Before the second and the third attempts: ranges of 64 then 128 slots.
        delays = 6 * n.reliable.retry_ms
        self.assertTrue(1000 + 3 * timeout <= t < 1000 + 3 * timeout + delays + 1000, t)
        self.assertEqual((n.counters.retries, n.counters.unreachable, len(n.queue)), (2, 1, 0))
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (3, 0))
        self.assertEqual(line.root.received(), [])

    def test_alternative_next_hop_after_three_attempts(self):
        line = Line(17, n_reroute=lambda msg, hop: ROOT)
        line.net.link_both(N, ROOT)
        self._hear_relay(line)
        line.net.at(900, line.net.medium.cut, N, R)
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"rerouted")
        line.net.run(20000)
        self.assertEqual(line.root.received(), [b"rerouted"])
        n = line.node.engine
        self.assertEqual(line.node.failures, [])
        self.assertEqual((n.counters.reroutes, n.counters.retries), (1, 2))
        self.assertEqual((n.counters.acks_explicit, len(n.queue)), (1, 0))
        dests = [h.dest for _, h, _ in line.data_frames(N)]
        self.assertEqual(dests, [R, R, R, ROOT])

    def test_relay_failure_comes_back_as_an_error(self):
        # Downward: R cannot reach N; its error (port 4) reaches the root,
        # origin of the message.
        line = Line(18)
        line.net.at(900, line.net.medium.cut, R, N)
        line.net.at(1000, line.root.submit, 21, 1, N, b"command")
        line.net.run(20000)
        self.assertEqual(line.node.received(), [])
        r = line.relay.engine
        self.assertEqual((r.counters.unreachable, r.counters.errors_sent), (1, 1))
        self.assertEqual(len(line.root.failures), 1)
        _, dest, port, seq, reason = line.root.failures[0]
        self.assertEqual((dest, port, reason), (N, 21, UNREACHABLE))
        self.assertEqual(seq, line.data_frames(ROOT)[0][2][0].seq)
        errors = [m for _, _, msgs in line.frames(R) for m in msgs if m.port == ports.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertEqual(decode_error(errors[0].payload), (UNREACHABLE, N, ROOT, N, 21, seq))
        self.assertEqual(errors[0].cls, 1)
        self.assertEqual((len(r.queue), len(line.root.engine.queue)), (0, 0))


class Holding(unittest.TestCase):
    def test_own_message_held_then_fails_with_reason_3(self):
        line = Line(19)
        line.n_hop = None
        seq = []
        line.net.at(1000, lambda: seq.append(line.node.submit(20, 1, ROOT, b"held")))
        line.net.run(100000)
        n = line.node.engine
        self.assertTrue(seq[0] >= 0)
        self.assertEqual(line.net.medium.stats(N).tx_count, 0)
        self.assertEqual(len(line.node.failures), 1)
        t, dest, port, s, reason = line.node.failures[0]
        self.assertEqual((dest, port, s, reason), (ROOT, 20, seq[0], NO_ROUTE))
        hold = n.reliable.hold_ms()
        self.assertEqual(hold, 2 * n.neighbours.period_ms)
        self.assertTrue(1000 + hold <= t <= 1000 + hold + n.reliable.poll_ms, t)
        self.assertEqual((n.counters.held, n.counters.hold_expired, len(n.queue)), (1, 1, 0))

    def test_held_message_sent_when_the_route_returns(self):
        line = Line(20)
        line.n_hop = None
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"late")
        line.net.at(20000, setattr, line, "n_hop", R)
        line.net.run(30000)
        self.assertEqual(line.root.received(), [b"late"])
        n = line.node.engine
        self.assertEqual(line.node.failures, [])
        t_sent = line.data_frames(N)[0][0]
        self.assertTrue(20000 < t_sent <= 20000 + n.reliable.poll_ms + 200, t_sent)
        self.assertEqual((n.counters.acks_passive, len(n.queue)), (1, 0))

    def test_relay_holds_then_error_3_reaches_the_origin(self):
        line = Line(21)
        line.up = None  # R has no route
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"stranded")
        r_hold = line.relay.engine.reliable.hold_ms()
        # The route returns after the message's holding, within the error's.
        line.net.at(1000 + r_hold + 10000, setattr, line, "up", ROOT)
        line.net.run(1000 + r_hold + 20000)
        n, r, root = line.node.engine, line.relay.engine, line.root.engine
        # R held the message and acknowledged it at once (explicitly).
        self.assertEqual((n.counters.acks_explicit, n.counters.retries), (1, 0))
        self.assertTrue(line.ack_frames(R)[0][0] - line.data_frames(N)[0][0] < 100)
        self.assertEqual(
            (r.counters.held, r.counters.hold_expired, r.counters.errors_sent), (2, 1, 1)
        )
        # Error reason 3 to the root, forwarded by the root to the origin.
        self.assertEqual(line.root.received(), [])
        self.assertEqual(root.counters.errors_forwarded, 1)
        self.assertEqual(len(line.node.failures), 1)
        _, dest, port, seq, reason = line.node.failures[0]
        self.assertEqual((dest, port, reason), (ROOT, 20, NO_ROUTE))
        self.assertEqual(seq, line.data_frames(N)[0][2][0].seq)
        errors = [m for _, _, msgs in line.frames(R) for m in msgs if m.port == ports.ERROR]
        payloads = set(bytes(m.payload) for m in errors)
        self.assertEqual(len(payloads), 1)  # R's upward error, then the root's copy down
        self.assertEqual(decode_error(errors[0].payload), (NO_ROUTE, NO_HOP, N, ROOT, 20, seq))
        for engine in (n, r, root):
            self.assertEqual(len(engine.queue), 0)

    def test_reroute_hold_waits_for_a_route(self):
        # "next hop after failure" says no route for now (the parent set is
        # empty): held, then sent when a next hop appears.
        line = Line(22)

        def reroute(msg, hop):
            line.n_hop = None
            return HOLD

        def repaired():
            line.net.medium.restore(N, R)
            line.n_hop = R

        line.node.engine.reroute = reroute
        line.net.at(300, line.relay.submit, 21, 0, N, b"hello")
        line.net.at(900, line.net.medium.cut, N, R)
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"patient")
        line.net.at(16000, repaired)  # after the 3 attempts and their retry delays
        line.net.run(30000)
        self.assertEqual(line.root.received(), [b"patient"])
        n = line.node.engine
        self.assertEqual(line.node.failures, [])
        self.assertEqual((n.counters.held, n.counters.retries), (1, 2))
        self.assertEqual(len(line.data_frames(N)), 4)  # 3 attempts, then once resumed


class RelayReboot(unittest.TestCase):
    """The relay reboots with the leaf's class 1 message queued, not yet forwarded."""

    def _run(self, kind, off_ms, seed=23):
        line = Line(seed)
        relay = line.relay
        engine = relay.engine
        forward = engine.forward
        queued = []

        def forward_then_reboot(msg, h, now):
            outcome = forward(msg, h, now)
            queued.append(len(engine.queue))
            # Before the forwarding delay (at least the carrier sense) ends.
            line.net.at(now, getattr(relay, kind), off_ms)
            return outcome

        engine.forward = forward_then_reboot
        line.net.at(1000, line.node.submit, 20, 1, ROOT, b"punch")
        line.net.run(30000)
        self.assertEqual(queued, [1])
        self.assertEqual(relay.boots, 2)
        return line

    def _delivered(self, line):
        self.assertEqual(line.root.received(), [b"punch"])
        self.assertEqual(line.node.failures, [])
        n = line.node.engine
        self.assertTrue(n.counters.retries >= 1)
        self.assertEqual(len(n.queue), 0)
        # The first life of the relay sent nothing: the message was queued.
        self.assertEqual(len(line.data_frames(R)), 1)

    def _failed(self, line):
        self.assertEqual(line.root.received(), [])
        self.assertEqual(len(line.node.failures), 1)
        self.assertEqual(line.node.failures[0][4], UNREACHABLE)
        self.assertEqual(line.data_frames(R), [])

    def test_reset_then_delivery(self):
        self._delivered(self._run("reset", 200))

    def test_power_loss_then_delivery(self):
        self._delivered(self._run("power_loss", 200))

    # Off beyond N's 3 attempts and their retry delays (64, 128 slots).
    def test_long_reset_ends_in_explicit_failure(self):
        self._failed(self._run("reset", 20000))

    def test_long_power_loss_ends_in_explicit_failure(self):
        self._failed(self._run("power_loss", 20000))


class LateAcknowledgement(unittest.TestCase):
    def test_relay_acks_when_it_cannot_forward_within_half_a_timeout(self):
        # R's duty cycle is above the priority-3 allowance: the low-priority
        # class 1 message waits in R's queue; R acknowledges explicitly.
        line = Line(24, budget_ms=10000, window_ms=60000)
        line.relay.engine.dutycycle.start(0, 7000)
        line.net.at(1000, line.node.submit, 200, 1, ROOT, b"low priority")
        line.net.run(5000)
        n, r = line.node.engine, line.relay.engine
        self.assertEqual((n.counters.acks_explicit, n.counters.retries, len(n.queue)), (1, 0, 0))
        self.assertEqual(r.counters.acks_late, 1)
        t_ack = line.ack_frames(R)[0][0]
        t_sent = line.data_frames(N)[0][0]
        half = r.reliable.timeout_ms(R) // 2
        self.assertTrue(half <= t_ack - t_sent < half + 200, t_ack - t_sent)
        self.assertEqual((line.root.received(), len(r.queue)), ([], 1))
        line.net.run(70000)  # the window releases airtime: forwarded
        self.assertEqual(line.root.received(), [b"low priority"])
