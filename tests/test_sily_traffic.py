"""Test traffic (sily.traffic): the node's sender, the gateway's accounting.

Unit tests drive `Sender` and `Receiver` with injected clocks, random
sources and submissions. `Scenario` runs them on the simulator: a gateway
and three nodes with their real mesh routing (`mesh.routing.Routing` on
`sim.net`), one node behind a lossy link, one two hops away and rebooted
during the run; the gateway's figures are checked against the loss of the
links and the nodes' own counts.
"""

import struct
import unittest

from mesh.airtime import time_on_air_us
from mesh.dodag import DodagInfo
from mesh.frame import FIRST_HOP, ROOT, UPWARD, Header, Message, frame_size
from mesh.link import MAX_PAYLOAD, QUEUE_FULL
from mesh.profile import EU_869, get
from mesh.routing import Routing
from sily import traffic
from sily.traffic import HEAD, HEAD_SIZE, PORT, WINDOW, Receiver, Sender
from sim.net import Network, Observer
from sim.prng import XorShift32


class Submissions:
    """A submit function recording (port, cls, dest, payload); returns result."""

    def __init__(self, result=0):
        self.result = result
        self.calls = []

    def __call__(self, port, cls, dest, payload):
        self.calls.append((port, cls, dest, bytes(payload)))
        return self.result

    def heads(self):
        return [struct.unpack_from(HEAD, c[3], 0) for c in self.calls]


def _sender(period_ms=10000, size=HEAD_SIZE, jitter_pct=0, result=0, boot=3):
    sub = Submissions(result)
    return sub, Sender(sub, XorShift32(5), boot, period_ms, size, jitter_pct)


def _run(sender, until, attached=True, start=0):
    """Call tick as the task would until until (ms); return the submission times."""
    now = start
    times = []
    while now < until:
        before = sender.seq
        wait = sender.tick(now, attached)
        if sender.seq != before:
            times.append(now)
        now += wait
    return times


def _run_n(sender, n, start=0):
    """Call tick until n more submissions; return their times and the time reached."""
    now = start
    times = []
    while len(times) < n:
        before = sender.seq
        wait = sender.tick(now, True)
        if sender.seq != before:
            times.append(now)
        now += wait
    return times, now


class Sending(unittest.TestCase):
    def test_waits_for_the_first_attachment(self):
        sub, s = _sender()
        self.assertEqual(s.tick(0, False), traffic.CHECK_MS)
        self.assertEqual(s.tick(1000, False), traffic.CHECK_MS)
        self.assertEqual(sub.calls, [])
        times = _run(s, 30000, start=2000)
        self.assertTrue(2000 <= times[0] < 12000, times)
        self.assertEqual(len(sub.calls), len(times))

    def test_goes_on_while_detached_after_the_first_attachment(self):
        sub, s = _sender()
        _run(s, 15000)
        n = len(sub.calls)
        _run(s, 60000, attached=False, start=15000)
        self.assertTrue(len(sub.calls) >= n + 4, sub.calls)

    def test_payload(self):
        sub, s = _sender(size=24, boot=0x10007)
        times, _ = _run_n(s, 3)
        self.assertEqual(len(sub.calls), 3)
        for i, (port, cls, dest, payload) in enumerate(sub.calls):
            self.assertEqual((port, cls, dest, len(payload)), (PORT, 1, ROOT, 24))
            self.assertEqual(struct.unpack_from(HEAD, payload, 0), (7, i, times[i]))
            self.assertEqual(payload[HEAD_SIZE:], bytes(24 - HEAD_SIZE))

    def test_fixed_period_without_jitter(self):
        _, s = _sender(period_ms=4000)
        times = _run(s, 60000)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertEqual(set(gaps), {4000})

    def test_jittered_intervals_keep_the_mean_period(self):
        _, s = _sender(period_ms=10000, jitter_pct=25)
        times = _run(s, 3600000)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(min(gaps) >= 7500 and max(gaps) <= 12500, (min(gaps), max(gaps)))
        self.assertTrue(len(set(gaps)) > 50)  # not in step
        mean = sum(gaps) / len(gaps)
        self.assertTrue(9500 < mean < 10500, mean)

    def test_late_task_sends_once(self):
        sub, s = _sender(period_ms=1000)
        _run(s, 5000)
        n = len(sub.calls)
        wait = s.tick(20000, True)  # the task was held up for 15 s
        self.assertEqual(len(sub.calls), n + 1)
        self.assertEqual(wait, 1000)
        self.assertEqual(s.tick(20500, True), 500)
        self.assertEqual(len(sub.calls), n + 1)

    def test_refusals_keep_their_number(self):
        sub, s = _sender(result=QUEUE_FULL)
        _, now = _run_n(s, 2)
        self.assertEqual([h[1] for h in sub.heads()], [0, 1])
        self.assertEqual(s.minute(), {"n": 2, "ok": 0, "ref": [[QUEUE_FULL, 2]], "fail": []})
        sub.result = 9
        _run_n(s, 2, now)
        self.assertEqual([h[1] for h in sub.heads()], [0, 1, 2, 3])
        self.assertEqual((s.seq, s.ok), (4, 2))

    def test_failures_counted_on_its_port_only(self):
        _, s = _sender()
        s.failed(ROOT, PORT, 4, 3)
        s.failed(ROOT, PORT, 5, 3)
        s.failed(ROOT, PORT, 6, 1)
        s.failed(ROOT, 20, 7, 3)
        self.assertEqual(s.minute()["fail"], [[1, 1], [3, 2]])
        st = s.status()
        self.assertEqual((st["failed"], st["failures"]), (3, [[1, 1], [3, 2]]))

    def test_size_below_the_header_refused(self):
        with self.assertRaises(ValueError):
            Sender(Submissions(), XorShift32(1), 1, 1000, HEAD_SIZE - 1)


def _message(boot, seq, sent, size=HEAD_SIZE):
    payload = bytearray(size)
    struct.pack_into(HEAD, payload, 0, boot, seq, sent)
    return Message(UPWARD, 1, PORT, memoryview(payload), origin=5, seq=0)


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


def _header(source):
    """Header of a frame to the gateway from link source source."""
    return Header(ROOT, source, 1, 1)


def _receive(r, clock, origin, boot, numbers, sent=None, delay=100, offset=0, via=None, size=None):
    """Deliver numbers of boot from origin, through via (None: direct); node
    time sent (default number * 1000), received delay ms later on a gateway
    clock offset ms ahead; payload of size bytes (default the header's)."""
    for seq in numbers:
        node_t = seq * 1000 if sent is None else sent
        clock.t = node_t + offset + delay
        msg = _message(boot, seq, node_t, size or HEAD_SIZE)
        r.handler(origin, msg, _header(origin if via is None else via))
    r.drain()


def _row(r, origin):
    return dict(zip(traffic.FIELDS, r.origins[origin].row()))


class Delivery(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.r = Receiver(self.clock)

    def figures(self, origin=5):
        row = _row(self.r, origin)
        return tuple(row[k] for k in ("received", "expected", "duplicates", "reordered"))

    def test_in_order(self):
        _receive(self.r, self.clock, 5, 1, range(20))
        self.assertEqual(self.figures(), (20, 20, 0, 0))

    def test_gaps(self):
        _receive(self.r, self.clock, 5, 1, (0, 1, 2, 5, 6, 9))
        self.assertEqual(self.figures(), (6, 10, 0, 0))

    def test_span_starts_at_the_first_number_received(self):
        _receive(self.r, self.clock, 5, 1, (40, 41, 43))
        self.assertEqual(self.figures(), (3, 4, 0, 0))

    def test_duplicates(self):
        _receive(self.r, self.clock, 5, 1, (0, 1, 1, 2, 0, 2))
        self.assertEqual(self.figures(), (3, 3, 3, 0))

    def test_reordered(self):
        _receive(self.r, self.clock, 5, 1, (10, 12, 11, 9, 13))
        self.assertEqual(self.figures(), (5, 5, 0, 2))
        self.assertEqual(self.r.origins[5].lo, 9)

    def test_window_reuse_is_not_a_duplicate(self):
        top = WINDOW + 4
        _receive(self.r, self.clock, 5, 1, (0, 1, 2, 3, 4, 5, top, top - 1, top - 2))
        received, expected, dup, ordered = self.figures()
        self.assertEqual((received, dup, ordered), (9, 0, 2))
        self.assertEqual(expected, top + 1)

    def test_late_beyond_the_window(self):
        _receive(self.r, self.clock, 5, 1, (0, WINDOW + 10, 3))
        row = _row(self.r, 5)
        self.assertEqual((row["received"], row["late"], row["duplicates"]), (2, 1, 0))

    def test_reboot_starts_a_new_span(self):
        _receive(self.r, self.clock, 5, 1, (0, 1, 2, 4, 5, 6, 7, 8, 9))
        _receive(self.r, self.clock, 5, 2, (0, 1, 2, 3, 4))
        row = _row(self.r, 5)
        self.assertEqual((row["received"], row["expected"], row["duplicates"]), (14, 15, 0))
        self.assertEqual((row["boot"], row["boots"], row["stale"]), (2, 2, 0))
        _receive(self.r, self.clock, 5, 1, (3,))  # held at a relay across the reboot
        row = _row(self.r, 5)
        self.assertEqual((row["received"], row["expected"], row["stale"]), (14, 15, 1))
        self.assertEqual(row["boot"], 2)

    def test_boot_numbers_wrap(self):
        _receive(self.r, self.clock, 5, 0xFFFF, (0, 1))
        _receive(self.r, self.clock, 5, 0, (0,))
        row = _row(self.r, 5)
        self.assertEqual((row["boot"], row["boots"], row["stale"]), (0, 2, 0))

    def test_nodes_apart(self):
        _receive(self.r, self.clock, 5, 1, (0, 1, 3))
        _receive(self.r, self.clock, 6, 1, (0, 1, 2, 3))
        self.assertEqual(self.figures(5), (3, 4, 0, 0))
        self.assertEqual(self.figures(6), (4, 4, 0, 0))
        status = self.r.status()
        self.assertEqual([n["address"] for n in status["nodes"]], [5, 6])
        self.assertEqual([n["delivery"] for n in status["nodes"]], [0.75, 1.0])

    def test_handler_only_queues(self):
        r = Receiver(self.clock, capacity=2)
        for seq in range(3):
            r.handler(5, _message(1, seq, 0), _header(5))
        r.handler(5, Message(UPWARD, 1, PORT, b"short", origin=5, seq=0), _header(5))
        self.assertEqual(r.origins, {})
        self.assertEqual((len(r.queue), r.queue.overflow, r.malformed), (2, 1, 1))
        tt = r.minute()
        self.assertEqual((tt["mal"], tt["q"]), (1, 1))
        self.assertEqual(tt["o"][0][:4], [5, 1, 2, 2])

    def test_nothing_received_no_figures(self):
        self.assertIsNone(self.r.minute())
        self.assertEqual(self.r.status()["nodes"], [])


class Latency(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.r = Receiver(self.clock)

    def latency(self, origin=5):
        row = _row(self.r, origin)
        return tuple(row[k] for k in ("samples", "median_ms", "p90_ms", "max_ms"))

    def period(self, boot, numbers, delays, offset):
        for seq, delay in zip(numbers, delays):
            _receive(self.r, self.clock, 5, boot, (seq,), delay=delay, offset=offset)
        self.r.minute()

    def test_above_the_smallest_whatever_the_offset(self):
        for offset in (-3600000, 0, 123456):
            self.r = Receiver(self.clock)
            delays = (500, 100, 1000, 300, 200, 400, 600, 700, 800, 900)
            self.period(1, range(10), delays, offset)
            self.assertEqual(self.latency(), (10, 400, 800, 900), offset)

    def test_no_sample_no_figure(self):
        self.period(1, (0, 1), (100, 200), 0)
        self.r.minute()
        self.assertEqual(self.latency(), (0, None, None, None))

    def test_duplicates_give_no_sample(self):
        self.period(1, (0, 0, 0), (100, 5000, 9000), 0)
        self.assertEqual(self.latency(), (1, 0, 0, 0))

    def test_minimum_over_the_last_periods(self):
        seq = 0
        self.period(1, (seq,), (100,), 0)
        for _ in range(traffic.BASELINE - 1):
            seq += 1
            self.period(1, (seq,), (300,), 0)
            self.assertEqual(self.latency(), (1, 200, 200, 200))
        seq += 1
        self.period(1, (seq,), (300,), 0)  # the 100 ms period has left the window
        self.assertEqual(self.latency(), (1, 0, 0, 0))

    def test_reboot_restarts_the_minimum(self):
        self.period(1, (0, 1), (100, 400), 0)
        self.assertEqual(self.latency(), (2, 0, 300, 300))
        self.period(2, (0, 1), (700, 900), 50000)  # the node's clock restarted
        self.assertEqual(self.latency(), (2, 0, 200, 200))

    def test_samples_bounded(self):
        n = traffic.SAMPLES_MAX + 10
        self.period(1, range(n), [100] * n, 0)
        self.assertEqual(self.latency()[0], traffic.SAMPLES_MAX)
        self.assertEqual(_row(self.r, 5)["received"], n)


PROFILE = get(EU_869)


def _air_us(length, sf=7):
    return time_on_air_us(length, sf, PROFILE.bandwidth_hz, PROFILE.coding_rate)


class Offset(unittest.TestCase):
    """The clock offset of each period: smallest reception minus submission
    time, less the time on air of the hops the gateway can tell."""

    def setUp(self):
        self.clock = Clock()
        self.r = Receiver(self.clock, sf=7, profile=PROFILE)
        self.one = _air_us(traffic.first_frame_bytes(HEAD_SIZE)) // 1000
        self.two = (
            _air_us(traffic.first_frame_bytes(HEAD_SIZE)) + _air_us(traffic.frame_bytes(HEAD_SIZE))
        ) // 1000

    def offset(self, origin=5):
        row = _row(self.r, origin)
        return row["offset_t"], row["offset_ms"]

    def test_time_on_air_at_sf7(self):
        # 27 and 29 B frames: 66.816 ms each at SF7, 125 kHz, CR 4/5.
        self.assertEqual((self.one, self.two), (66, 133))
        self.assertEqual(self.r.air(HEAD_SIZE, True), 66)
        self.assertEqual(self.r.air(HEAD_SIZE, False), 133)

    def test_smallest_of_the_period_less_one_hop(self):
        offset = 3600000  # the gateway booted an hour before the node
        for seq, delay in zip(range(5), (180, 95, 140, 120, 300)):
            _receive(self.r, self.clock, 5, 1, (seq,), delay=delay, offset=offset)
        self.r.minute()
        self.assertEqual(self.offset(), (1000, offset + 95 - self.one))

    def test_relayed_less_two_hops(self):
        _receive(self.r, self.clock, 5, 1, (0, 1), delay=400, offset=-2000, via=6)
        _receive(self.r, self.clock, 5, 1, (2,), delay=150, offset=-2000)
        self.r.minute()
        # 400 - 133 = 267 relayed against 150 - 66 = 84 direct.
        self.assertEqual(self.offset(), (2000, -2000 + 150 - self.one))
        _receive(self.r, self.clock, 5, 1, (3, 4), delay=200, offset=-2000, via=6)
        _receive(self.r, self.clock, 5, 1, (5,), delay=150, offset=-2000)
        self.r.minute()
        # 200 - 133 = 67 relayed against 84 direct.
        self.assertEqual(self.offset(), (3000, -2000 + 200 - self.two))

    def test_time_on_air_of_the_size_received(self):
        _receive(self.r, self.clock, 5, 1, (0,), delay=200, size=200)
        self.r.minute()
        one = _air_us(traffic.first_frame_bytes(200)) // 1000
        self.assertEqual(self.offset(), (0, 200 - one))
        self.assertTrue(one > self.one, one)
        _receive(self.r, self.clock, 5, 1, (1,), delay=200)
        self.r.minute()
        self.assertEqual(self.offset(), (1000, 200 - self.one))

    def test_without_radio_settings_no_time_on_air(self):
        self.r = Receiver(self.clock)
        _receive(self.r, self.clock, 5, 1, (0, 1), delay=120, offset=500, via=6)
        self.r.minute()
        self.assertEqual(self.offset(), (0, 620))

    def test_each_period_apart(self):
        _receive(self.r, self.clock, 5, 1, (0,), delay=100)
        self.r.minute()
        _receive(self.r, self.clock, 5, 1, (1,), delay=300)
        self.r.minute()
        self.assertEqual(self.offset(), (1000, 300 - self.one))  # not the earlier 100
        self.r.minute()
        self.assertEqual(self.offset(), (None, None))

    def test_first_receptions_only(self):
        _receive(self.r, self.clock, 5, 1, (0, 1), delay=300)
        _receive(self.r, self.clock, 5, 1, (1,), delay=100)  # a duplicate
        self.r.minute()
        self.assertEqual(self.offset(), (0, 300 - self.one))

    def test_reboot_drops_the_old_boot(self):
        _receive(self.r, self.clock, 5, 1, (0,), delay=100, offset=0)
        _receive(self.r, self.clock, 5, 2, (0,), delay=400, offset=50000)
        self.r.minute()
        self.assertEqual(self.offset(), (0, 50000 + 400 - self.one))

    def test_minute_record_columns(self):
        _receive(self.r, self.clock, 5, 1, (0, 1), delay=100)
        row = self.r.minute()["o"][0]
        self.assertEqual(len(row), len(traffic.FIELDS))
        self.assertEqual(row[-2:], [0, 100 - self.one])


class Bounds(unittest.TestCase):
    def test_largest_size_within_the_engine(self):
        self.assertEqual(traffic.SIZE_MAX, MAX_PAYLOAD)

    def test_frame_of_a_forwarded_message(self):
        msg = Message(UPWARD, 1, PORT, bytes(30), origin=7, seq=1)
        self.assertEqual(traffic.frame_bytes(30), frame_size([msg]))

    def test_frame_of_a_first_hop_message(self):
        msg = Message(FIRST_HOP, 1, PORT, bytes(30), seq=1)
        self.assertEqual(traffic.first_frame_bytes(30), frame_size([msg]))


# --- Simulator scenario ------------------------------------------------------------

KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)  # presence 10 s, topology 60 s
A, B, C = 0x0A, 0x0B, 0x0C
LOSS = 0.3  # each way on the gateway-B link
PERIOD_MS = 10000
SIZE = 20
STOP_MS = 25 * 60000  # the senders stop
END_MS = 28 * 60000
REBOOT_MS = 12 * 60000  # C loses power
MINUTE_MS = 60000


class _Failures(Observer):
    def __init__(self, scenario):
        self.scenario = scenario

    def failed(self, board, dest, port, seq, reason):
        self.scenario.senders[board.name][-1].failed(dest, port, seq, reason)


class Scenario(unittest.TestCase):
    """Gateway and three nodes, real routing; the real sender and receiver.

    Links (both ways, no loss but B's)::

        gateway ── A ── C          C two hops away, rebooted at 12 min
           │
           B                       30 % of the frames lost each way

    Every node sends a 20-byte message every 10 s (± 25 %) from its first
    attachment until 25 min; the run ends at 28 min (class 1 holding,
    retries and failures settled).
    """

    @classmethod
    def setUpClass(cls):
        net = Network(11, KEY)
        cls.net = net
        cls.receiver = Receiver(lambda: net.now, sf=net.sf, profile=net.profile)
        cls.senders = {A: [], B: [], C: []}  # per node, one per boot
        cls.running = {}  # node -> its sender while the board is on
        cls.minutes = []
        net.observers.append(_Failures(cls))
        root = net.add(ROOT, routing=lambda e: Routing(e, EU_869, INFO))
        root.engine.bind(PORT, cls.receiver.handler)
        for n in (A, B, C):
            net.add(n, routing=lambda e: Routing(e, EU_869, INFO))
        net.link_both(ROOT, A)
        net.link_both(A, C)
        net.link_both(ROOT, B, loss=LOSS)
        for n in (A, B, C):
            cls._start(n, 1)
        net.at(REBOOT_MS, cls._reboot)
        for t in range(MINUTE_MS, END_MS + 1, MINUTE_MS):
            net.at(t, cls._minute)
        net.run(END_MS)

    @classmethod
    def _start(cls, name, boot):
        net = cls.net
        board = net.boards[name]
        sender = Sender(board.submit, XorShift32(100 * name + boot), boot, PERIOD_MS, SIZE, 25)
        cls.senders[name].append(sender)
        cls.running[name] = sender

        def tick():
            if sender is not cls.running[name] or net.now >= STOP_MS:
                return  # its board lost power, or the end of the traffic
            routing = board.routing
            attached = routing is not None and routing.attached
            net.scheduler.after(sender.tick(net.now, attached), tick)

        net.at(net.now, tick)

    @classmethod
    def _reboot(cls):
        cls.running[C] = None
        cls.net.boards[C].power_loss(off_ms=3000)
        cls.net.scheduler.after(3000, cls._start, C, 2)

    @classmethod
    def _minute(cls):
        cls.minutes.append(cls.receiver.minute())

    def row(self, name):
        return dict(zip(traffic.FIELDS, self.receiver.origins[name].row()))

    def submitted(self, name):
        return sum(s.seq for s in self.senders[name])

    def failed(self, name):
        return sum(sum(s.failures.values()) for s in self.senders[name])

    def test_every_node_sent(self):
        for name in (A, B, C):
            n = self.submitted(name)
            self.assertTrue(n >= 120, (name, n))  # about 2 minutes to attach
            self.assertEqual(sum(sum(s.refused.values()) for s in self.senders[name]), 0)

    def test_lossless_nodes_fully_delivered(self):
        for name in (A, C):
            row = self.row(name)
            self.assertEqual(row["received"], row["expected"], (name, row))
            self.assertEqual((row["duplicates"], row["late"], row["stale"]), (0, 0, 0), name)
            self.assertEqual(self.failed(name), 0, name)
        self.assertEqual(self.row(A)["received"], self.submitted(A))

    def test_reboot_neither_losses_nor_duplicates(self):
        row = self.row(C)
        self.assertEqual((row["boot"], row["boots"]), (2, 2))
        first, second = self.senders[C]
        self.assertEqual(row["received"], second.seq + self.receiver.origins[C].closed)
        # messages queued at C when it lost power: after the last received, not counted
        self.assertTrue(first.seq - 2 <= self.receiver.origins[C].closed <= first.seq)

    def test_lossy_node_within_the_bounds_of_its_link(self):
        row = self.row(B)
        lost = row["expected"] - row["received"]
        n = row["expected"]
        # Three attempts per message, each lost with probability LOSS:
        # LOSS ** 3 = 2.7 % expected.
        self.assertTrue(0 < lost <= 3 * LOSS**3 * n, (lost, n))
        self.assertEqual(row["received"] + lost, n)
        # Every message missing at the gateway was reported failed at B (no
        # silent loss); B may also report messages that arrived (their
        # acknowledgement lost each time).
        self.assertTrue(self.failed(B) >= lost, (self.failed(B), lost))
        self.assertTrue(row["expected"] >= self.submitted(B) - 1)

    def test_latency_figures(self):
        rows = {A: [], B: [], C: []}
        for tt in self.minutes:
            if tt is None:
                continue
            for r in tt["o"]:
                if r[9]:
                    rows[r[0]].append(r[9:13])
        for name in (A, B, C):
            self.assertTrue(len(rows[name]) >= 18, (name, len(rows[name])))
            for _, med, p90, top in rows[name]:
                self.assertTrue(0 <= med <= p90 <= top, (name, med, p90, top))
        # Retries on B's lossy link show above its best case; A's minute
        # medians stay near its own (its retries follow collisions only).
        worst = max(r[3] for r in rows[B])
        self.assertTrue(worst > 1000, worst)
        self.assertTrue(max(r[1] for r in rows[A]) < 1000, rows[A])

    def test_clock_offsets(self):
        # One clock for every board: each offset is what is left of the
        # fastest delivery's delay once the time on air of the hops known is
        # taken off, never negative. Direct nodes (A, B): the sensing and
        # rounding; C, two hops through A, both frames taken off.
        offsets = {A: [], B: [], C: []}
        for i, tt in enumerate(self.minutes):
            if tt is None:
                continue
            minute_t = (i + 1) * MINUTE_MS
            for r in tt["o"]:
                if r[14] is not None:
                    self.assertTrue(minute_t - MINUTE_MS < r[13] <= minute_t, (i, r))
                    offsets[r[0]].append(r[14])
        one_hop = _air_us(traffic.first_frame_bytes(SIZE)) // 1000
        for name in (A, B, C):
            self.assertTrue(len(offsets[name]) >= 18, (name, len(offsets[name])))
            self.assertTrue(min(offsets[name]) >= 0, (name, offsets[name]))
        self.assertTrue(min(offsets[A]) <= 5, offsets[A])
        self.assertTrue(min(offsets[C]) < one_hop, (one_hop, offsets[C]))

    def test_minute_record_rows(self):
        last = self.minutes[-1]
        self.assertEqual([r[0] for r in last["o"]], [A, B, C])
        self.assertEqual((last["mal"], last["q"], last["ovf"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
