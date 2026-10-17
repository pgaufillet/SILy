"""Link engines on the scripted medium (MESH_DESIGN §6, §7, §8, §13, §14).

Boards run `mesh.link.LinkEngine` with real security (one network key) on
`sim.net`; next hops are static. Duty-cycle windows are scaled down (60 s)
where a scenario must exhaust them, so that runs stay short.
"""

import unittest

from mesh import frame
from mesh.counter import FrameCounter
from mesh.frame import BROADCAST, ROOT
from mesh.link import AUTH, KNOWN, QUOTA, REPLAYS
from mesh.replay import ReplayTable
from mesh.security import AUTHENTICATION, KeyRing, Security
from sim.medium import COLLISION, OK
from sim.net import Network
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))
_WRONG_KEY = bytes(range(100, 116))

A = 0x00A1
B = 0x00B1
F = 0x00F1
R = 0x0010
E = "eve"  # the attacker's radio


def _to(hop):
    return lambda msg: hop


def _source(f):
    return frame.decode_header(f, len(f)).source


def _counter(f):
    return frame.decode_header(f, len(f)).counter


def _opens(f, key):
    """Return the reception result of frame f for a fresh board holding key."""
    platform = FakePlatform(XorShift32(99))
    security = Security(
        KeyRing(key), FrameCounter(platform.storage, platform.random), ReplayTable()
    )
    buf = bytearray(f)
    return security.receive(buf, len(buf), frame.Header(0, 0, 0, 0))


class Exchange(unittest.TestCase):
    def test_nodes_exchange_sealed_frames(self):
        net = Network(1, _KEY)
        root = net.add(ROOT, next_hop=_to(A))
        a = net.add(A, next_hop=_to(ROOT))
        b = net.add(B, next_hop=lambda msg: A if msg.dest == A else ROOT)
        eve = net.sniffer(E)
        net.full_mesh((ROOT, A, B, E))
        for board in (root, a, b):
            board.listen(20, 21)
        net.at(1000, a.submit, 20, 0, ROOT, b"punch from A")
        net.at(1200, b.submit, 20, 0, ROOT, b"punch from B")
        net.at(5000, root.submit, 21, 0, A, b"command to A")
        net.at(9000, b.submit, 21, 0, A, b"peer to peer")
        net.at(12000, root.submit, 21, 0, BROADCAST, b"to everyone")
        net.run(20000)
        self.assertEqual(sorted(root.inbox_from()), [(A, b"punch from A"), (B, b"punch from B")])
        self.assertEqual(
            a.inbox_from(), [(ROOT, b"command to A"), (B, b"peer to peer"), (ROOT, b"to everyone")]
        )
        self.assertEqual(b.inbox_from(), [(ROOT, b"to everyone")])
        # On air: ciphertext only, every frame authentic under the network key.
        frames = [f for _, f in eve.frames]
        self.assertEqual(len(frames), 5)
        for f in frames:
            self.assertFalse(b"punch" in f or b"command" in f or b"peer" in f)
            self.assertEqual(_opens(f, _KEY), None)
            self.assertEqual(_opens(f, _WRONG_KEY), AUTHENTICATION)
        for board in (root, a, b):
            c = board.engine.counters
            self.assertEqual((c.rx_auth, c.rx_replay, c.rx_malformed), (0, 0, 0))


class WrongKey(unittest.TestCase):
    def test_wrong_key_counted_per_sender(self):
        net = Network(2, _KEY)
        root = net.add(ROOT)
        a = net.add(A, next_hop=_to(ROOT))
        w1 = net.add(0x0E1, key=_WRONG_KEY, next_hop=_to(ROOT))
        w2 = net.add(0x0E2, key=bytes(16), next_hop=_to(ROOT))
        net.full_mesh((ROOT, A, 0x0E1, 0x0E2))
        root.listen(20)
        for i in range(5):
            net.at(1000 + 2000 * i, a.submit, 20, 0, ROOT, b"genuine %d" % i)
            net.at(1500 + 2000 * i, w1.submit, 20, 0, ROOT, b"forged %d" % i)
        for i in range(3):
            net.at(1700 + 2000 * i, w2.submit, 20, 0, ROOT, b"foreign %d" % i)
        net.run(15000)
        self.assertEqual(root.received(), [b"genuine %d" % i for i in range(5)])
        r = root.engine
        sent1 = net.medium.stats(0x0E1).tx_count
        sent2 = net.medium.stats(0x0E2).tx_count
        self.assertEqual((sent1, sent2), (5, 3))
        self.assertEqual(r.senders.get(0x0E1, AUTH), 5)
        self.assertEqual(r.senders.get(0x0E2, AUTH), 3)
        self.assertEqual(r.senders.get(A, AUTH), 0)
        self.assertEqual(r.senders.get(0x0E1, KNOWN), 0)  # never authenticated
        self.assertEqual(r.counters.rx_auth, 8)
        # The wrong-key boards see the network's frames fail the same way.
        self.assertEqual(w1.engine.senders.get(A, AUTH), 5)


class ReplayAfterPowerLoss(unittest.TestCase):
    """§14: after a power loss the replay table is empty; old frames replay once."""

    def setUp(self):
        net = Network(3, _KEY)
        self.net = net
        self.r = net.add(R)
        self.s = net.add(A, next_hop=_to(R))
        self.eve = net.sniffer(E)
        net.full_mesh((R, A, E))
        self.r.listen(20)
        self.payloads = [b"punch %d" % i for i in range(5)]
        for i, p in enumerate(self.payloads):
            net.at(1000 + 1000 * i, self.s.submit, 20, 0, R, p)
        net.run(10000)
        self.recorded = [f for _, f in self.eve.frames if _source(f) == A]

    def _replay(self, frames, at):
        self.eve.inject(frames, at, 500)
        self.net.run(at + 500 * len(frames) + 1000)

    def test_replays_rejected_while_powered(self):
        self.assertEqual(len(self.recorded), 5)
        self.assertEqual(self.r.received(), self.payloads)
        self._replay(self.recorded, 11000)
        self.r.reset(off_ms=100)  # soft reset: the replay table survives
        self._replay(self.recorded, 20000)
        self.assertEqual(self.r.received(), self.payloads)
        self.assertEqual(self.r.engine.senders.get(A, REPLAYS), 5)  # since the reset
        self.assertEqual(self.r.boots, 2)

    def test_power_loss_lets_each_old_frame_through_once(self):
        self.r.power_loss(off_ms=100)
        self._replay(self.recorded, 11000)
        # Each recorded frame accepted once: old messages delivered again.
        self.assertEqual(self.r.received(), self.payloads * 2)
        self._replay(self.recorded, 20000)
        self._replay(list(reversed(self.recorded)), 25000)
        self.assertEqual(self.r.received(), self.payloads * 2)
        engine = self.r.engine
        self.assertEqual(engine.senders.get(A, REPLAYS), 10)
        # The genuine sender's next frame has a higher counter: accepted.
        self.net.at(30000, self.s.submit, 20, 0, R, b"fresh")
        self.net.run(32000)
        self.assertEqual(self.r.received()[-1], b"fresh")
        self._replay(self.recorded, 33000)
        self.assertEqual(len(self.r.received()), 11)
        self.assertEqual(engine.senders.get(A, REPLAYS), 15)
        self.assertEqual(engine.counters.rx_replay, 15)

    def test_replay_order_bounds_acceptances(self):
        # In decreasing counter order, the first replay sets the baseline at
        # the newest old frame: every other one is then rejected.
        self.r.power_loss(off_ms=100)
        self._replay(list(reversed(self.recorded)), 11000)
        self.assertEqual(self.r.received(), self.payloads + [self.payloads[-1]])
        self.assertEqual(self.r.engine.senders.get(A, REPLAYS), 4)
        # The baseline is the highest replayed counter: below the sender's next.
        table = self.r.engine.security.replay
        self.assertEqual(table.highest(A), max(_counter(f) for f in self.recorded))


class FloodingNode(unittest.TestCase):
    """One node floods through a relay; the others' traffic keeps flowing.

    Nodes A, B and the flooder F hear each other and the relay R, the only
    neighbour of the root. A and B each send a 150-byte message every 15 s;
    F submits one every 100 ms. Duty cycle 9 % over a 60 s window: R can
    forward about 20 such frames a minute, fewer than F alone offers.
    """

    WINDOW = 60000
    BUDGET = 5400
    END = 600000

    @classmethod
    def setUpClass(cls):
        net = Network(4, _KEY, budget_ms=cls.BUDGET, window_ms=cls.WINDOW, trace=True)
        cls.net = net
        root = net.add(ROOT)
        relay = net.add(R, next_hop=_to(ROOT))
        nodes = dict((a, net.add(a, next_hop=_to(R))) for a in (A, B, F))
        net.full_mesh((R, A, B, F))
        net.link_both(R, ROOT)
        root.listen(20)
        cls.offered = {A: 0, B: 0}
        cls.flood_results = []
        stop = cls.END - 2 * cls.WINDOW  # leave time to drain
        for a, phase in ((A, 1000), (B, 8500)):
            t = phase
            while t < stop:
                net.at(t, cls._offer, nodes[a], a, t)
                t += 15000
        t = 500
        while t < stop:
            net.at(t, cls._flood, nodes[F], t)
            t += 100
        net.run(cls.END, limit=2000000)
        cls.root = root
        cls.relay = relay
        cls.nodes = nodes

    @classmethod
    def _offer(cls, board, address, t):
        result = board.submit(20, 0, ROOT, b"%04x %8d " % (address, t) + bytes(136))
        if result == 0:
            cls.offered[address] += 1

    @classmethod
    def _flood(cls, board, t):
        cls.flood_results.append(board.submit(20, 0, ROOT, b"F %8d " % t + bytes(139)))

    def _delivered(self, origin):
        return [r for r in self.root.inbox if r[1] == origin]

    def _delays(self, origin):
        # Payloads start with "<origin> <submission time> " or "F <time> ".
        start = 2 if origin == F else 5
        return [t - int(p[start : start + 8]) for t, _, _, p in self._delivered(origin)]

    def test_others_delivered_despite_the_flood(self):
        for a in (A, B):
            delivered = self._delivered(a)
            self.assertTrue(self.offered[a] >= 30, self.offered)
            # Every message offered by A and B reaches the root, once.
            self.assertEqual(len(delivered), self.offered[a], (hex(a), len(delivered)))
            self.assertEqual(len(set(r[3] for r in delivered)), len(delivered))
            # Within about one window: the relay's round robin serves A and B
            # at every turn; they wait for the relay's duty-cycle window only.
            self.assertTrue(max(self._delays(a)) < self.WINDOW + 10000, max(self._delays(a)))
        # The flooder's messages queue up behind its own share.
        mean = lambda d: sum(d) // len(d)  # noqa: E731
        self.assertTrue(mean(self._delays(F)) > 3 * mean(self._delays(A)))

    def test_flooder_gets_the_rest_not_more(self):
        flood = len(self._delivered(F))
        relay_frames = self.net.medium.stats(R).tx_count
        others = len(self._delivered(A)) + len(self._delivered(B))
        # The relay is saturated and sends one 150-byte message per frame; F
        # gets what A and B leave of its capacity.
        self.assertEqual(flood + others, relay_frames)
        self.assertTrue(flood > others, (flood, others))
        # Every frame of F heard by the relay was forwarded, is still queued,
        # or was refused by the relay's per-origin quota (§7).
        relay = self.relay.engine
        trace = self.net.medium.trace
        heard = sum(1 for _, src, dst, fate in trace if (src, dst, fate) == (F, R, OK))
        refused = relay.senders.get(F, QUOTA)
        self.assertTrue(refused > 0)
        self.assertEqual(flood + refused + len(relay.queue), heard)
        self.assertEqual((relay.senders.get(A, QUOTA), relay.senders.get(B, QUOTA)), (0, 0))
        # Most of F's flood never left F: its own queue refuses it (MP5).
        self.assertTrue(sum(1 for r in self.flood_results if r < 0) > 10 * heard)

    def test_duty_cycle_holds_for_every_board(self):
        windows = self.END // self.WINDOW + 1
        for name in (R, F, A, B):
            stats = self.net.medium.stats(name)
            self.assertTrue(stats.tx_ms <= windows * self.BUDGET, (hex(name), stats.tx_ms))
        # The relay and the flooder use their budget: the limit is what binds.
        for name in (R, F):
            used = self.net.medium.stats(name).tx_ms
            self.assertTrue(used >= (windows - 3) * self.BUDGET, (hex(name), used))
        for board in (self.relay, self.nodes[F]):
            dc = board.engine.dutycycle
            self.assertTrue(dc.used(self.END) <= self.BUDGET)
            self.assertTrue(board.engine.counters.duty_waits > 0)


class CarrierSense(unittest.TestCase):
    def _run(self, hears):
        """X sends a long frame; Y submits during it; Z hears both."""
        net = Network(5, _KEY, trace=True)
        x = net.add(0x0101, next_hop=_to(0x0303))
        y = net.add(0x0202, next_hop=_to(0x0303))
        z = net.add(0x0303)
        net.link_both(0x0101, 0x0303)
        net.link_both(0x0202, 0x0303)
        if hears:
            net.link_both(0x0101, 0x0202)
        z.listen(20)
        net.at(1000, x.submit, 20, 0, 0x0303, bytes(200))
        net.at(1100, y.submit, 20, 0, 0x0303, b"short")
        net.run(10000)
        return net, x, y, z

    def test_sensing_defers_until_the_channel_is_free(self):
        net, x, y, z = self._run(True)
        self.assertEqual(sorted(len(p) for p in z.received()), [5, 200])
        ends = dict((src, end) for end, src, dst, fate in net.medium.trace if dst == 0x0303)
        y_start = ends[0x0202] - y.engine.toa_ms(13 + 2 + 4 + 5)
        self.assertTrue(y_start >= ends[0x0101], (y_start, ends))
        self.assertTrue(y.engine.counters.busy >= 1)
        self.assertEqual(y.engine.counters.backoffs, y.engine.counters.busy)

    def test_hidden_sender_collides(self):
        # The same traffic without carrier sense between X and Y: collision.
        net, x, y, z = self._run(False)
        self.assertEqual(z.received(), [])
        fates = [fate for _, _, dst, fate in net.medium.trace if dst == 0x0303]
        self.assertEqual(fates, [COLLISION, COLLISION])
        self.assertEqual(y.engine.counters.busy, 0)

    def test_jammed_channel_drops_reports_keeps_class1(self):
        net = Network(6, _KEY)
        node = net.add(A, next_hop=_to(B))
        receiver = net.add(B)
        jammer = net.sniffer(E)
        net.full_mesh((A, B, E))
        receiver.listen(20, 200)
        # Back-to-back 255-byte frames for 10 s (about 400 ms each).
        jammer.inject([bytes(255)] * 24, 1000, 401)
        net.at(1500, node.submit, 200, 0, B, b"low priority report")
        net.at(1600, node.submit, 200, 1, B, b"class 1")
        net.run(10000)
        e = node.engine
        self.assertTrue(e.counters.give_ups >= 1)
        self.assertTrue(e.counters.senses >= 6 * e.counters.give_ups)
        self.assertEqual(e.counters.access_dropped, 1)  # the report, at the first give-up
        self.assertEqual((len(e.queue), e.counters.tx_frames), (1, 0))
        net.run(20000)
        self.assertEqual(receiver.received(), [b"class 1"])  # sent once the jam ends


class DuplicateAddress(unittest.TestCase):
    def test_two_boards_with_one_address(self):
        net = Network(7, _KEY)
        root = net.add(ROOT)
        first = net.add("first", address=0x0042, next_hop=_to(ROOT))
        second = net.add("second", address=0x0042, next_hop=_to(ROOT))
        net.full_mesh((ROOT, "first", "second"))
        root.listen(20)
        net.at(1000, second.submit, 20, 0, ROOT, b"from the second")
        net.run(5000)
        self.assertEqual(root.received(), [b"from the second"])
        self.assertTrue(first.engine.address_conflict)
        self.assertEqual(first.engine.counters.duplicate_address, 1)
        self.assertFalse(second.engine.address_conflict)
        self.assertEqual(net.medium.stats("first").rx_ok, 1)
