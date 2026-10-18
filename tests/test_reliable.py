"""Class 1 pieces: port 3 and 4 codecs, identity, duplicate cache, timeouts, rules
(MESH_DESIGN §9, §11 ports 3 and 4, §12 class 1, §16).

Engine-level tests drive link engines step by step, frames passed by hand.
"""

import unittest

from mesh import frame, ports
from mesh import profile as profiles
from mesh.counter import FrameCounter
from mesh.frame import BROADCAST, DOWNWARD, FIRST_HOP, LINK_LOCAL, P2P, ROOT, UPWARD, Message
from mesh.link import NO_ROUTE as SUBMIT_NO_ROUTE
from mesh.link import open_link
from mesh.radio import CHANNEL, RX, SENSE, TX_DONE
from mesh.reliable import (
    ACK_BATCH,
    ATTEMPTS,
    ERROR_SIZE,
    HOLD,
    DuplicateCache,
    decode_acks,
    decode_error,
    encode_acks,
    encode_error,
    identity,
)
from mesh.replay import ReplayTable
from mesh.security import KeyRing, Security
from mesh.txqueue import TxQueue
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))
_EU = profiles.get(profiles.EU_869)

N = 0x00A1
R = 0x0010
X = 0x00C1


class AckCodec(unittest.TestCase):
    def test_round_trip(self):
        entries = [(0x1234, 0xABCD), (0x0001, 0x0000), (0xFFFE, 0xFFFF)]
        payload = encode_acks(entries)
        self.assertEqual(payload[:4], b"\x34\x12\xcd\xab")  # little-endian
        self.assertEqual(decode_acks(payload), entries)
        batch = [(i, 1000 + i) for i in range(ACK_BATCH)]
        self.assertEqual(len(encode_acks(batch)), 32)
        self.assertEqual(decode_acks(encode_acks(batch)), batch)

    def test_counter_low_16_bits(self):
        self.assertEqual(decode_acks(encode_acks([(5, 0x123456)])), [(5, 0x3456)])

    def test_malformed(self):
        for payload in (b"", b"\x01\x02\x03", b"\x01\x02\x03\x04\x05"):
            with self.assertRaises(ValueError):
                decode_acks(payload)
        for entries in ([], [(0x10000, 1)], [(-1, 1)]):
            with self.assertRaises(ValueError):
                encode_acks(entries)


class ErrorCodec(unittest.TestCase):
    def test_round_trip(self):
        payload = encode_error(3, 0xFFFF, 0x3F2A, ROOT, 200, 0xBEEF)
        self.assertEqual(len(payload), ERROR_SIZE)
        self.assertEqual(payload, b"\x03\xff\xff\x2a\x3f\x00\x00\xc8\xef\xbe")
        self.assertEqual(decode_error(payload), (3, 0xFFFF, 0x3F2A, ROOT, 200, 0xBEEF))
        for reason in range(1, 7):
            fields = (reason, 0x1B07, 0x0000, 0x3F2A, 21, reason * 1000)
            self.assertEqual(decode_error(encode_error(*fields)), fields)

    def test_malformed(self):
        good = encode_error(1, 2, 3, 4, 5, 6)
        for payload in (good[:-1], good + b"\x00", b"\x00" + good[1:]):
            with self.assertRaises(ValueError):
                decode_error(payload)
        for fields in ((0, 1, 1, 1, 1, 1), (1, 0x10000, 1, 1, 1, 1), (1, 1, 1, 1, 256, 1)):
            with self.assertRaises(ValueError):
                encode_error(*fields)


class Identity(unittest.TestCase):
    def test_first_hop_and_forwarded_copy_match(self):
        own = Message(FIRST_HOP, 1, 20, b"x", seq=7)
        forwarded = Message(UPWARD, 1, 20, b"x", origin=N, seq=7)
        self.assertEqual(identity(own, N, R), (N, ROOT, 20, 7))
        self.assertEqual(identity(forwarded, R, ROOT), identity(own, N, R))

    def test_route_and_hops_do_not_matter(self):
        a = Message(DOWNWARD, 1, 21, b"y", dest=N, seq=9, route=(R,))
        b = Message(DOWNWARD, 1, 21, b"y", dest=N, seq=9, route=())
        self.assertEqual(identity(a, ROOT, R), (ROOT, N, 21, 9))
        self.assertEqual(identity(b, R, N), identity(a, ROOT, R))
        p2p = Message(P2P, 2, 30, b"z", N, X, 4)
        self.assertEqual(identity(p2p, R, ROOT), (N, X, 30, 4))
        self.assertNotEqual(identity(p2p, R, ROOT), (N, X, 30, 5))

    def test_link_local_and_broadcast(self):
        self.assertEqual(identity(Message(LINK_LOCAL, 1, 8, b"", seq=3), N, R), (N, R, 8, 3))
        self.assertEqual(identity(Message(LINK_LOCAL, 0, 3, b""), N, R), None)
        bc = Message(frame.NODE_BROADCAST, 0, 200, b"", origin=N, seq=1)
        self.assertEqual(identity(bc, N, BROADCAST), None)


class Duplicates(unittest.TestCase):
    def test_capacity_evicts_the_oldest(self):
        cache = DuplicateCache(128)
        for i in range(128):
            cache.add((N, ROOT, 20, i), i)
        self.assertEqual(len(cache), 128)
        self.assertTrue(cache.seen((N, ROOT, 20, 0), 200, 1000))
        cache.add((N, ROOT, 20, 128), 200)
        self.assertEqual((len(cache), cache.evictions), (128, 1))
        self.assertFalse(cache.seen((N, ROOT, 20, 0), 200, 1000))
        self.assertTrue(cache.seen((N, ROOT, 20, 1), 200, 1000))
        self.assertTrue(cache.seen((N, ROOT, 20, 128), 200, 1000))

    def test_entries_age_out(self):
        cache = DuplicateCache(4)
        cache.add((N, ROOT, 20, 1), 1000)
        self.assertTrue(cache.seen((N, ROOT, 20, 1), 1999, 1000))
        self.assertFalse(cache.seen((N, ROOT, 20, 1), 2000, 1000))
        self.assertEqual(len(cache), 0)
        self.assertFalse(cache.seen((N, ROOT, 20, 2), 0, 1000))

    def test_add_refreshes(self):
        cache = DuplicateCache(4)
        cache.add((N, ROOT, 20, 1), 0)
        cache.add((N, ROOT, 20, 1), 900)
        self.assertTrue(cache.seen((N, ROOT, 20, 1), 1500, 1000))
        self.assertEqual(len(cache), 1)


# --- Engines driven by hand ------------------------------------------------------


def _engine(address, hop, seed=1, sf=7, reroute=None):
    platform = FakePlatform(XorShift32(seed))
    next_hop = hop if callable(hop) else (lambda msg: hop)
    engine = open_link(
        address,
        _KEY,
        platform.storage,
        platform.retained,
        platform.random,
        _EU,
        sf,
        next_hop,
        reroute,
        budget_ms=60000,
        window_ms=600000,
    )
    engine.step(0, None)
    return engine


def _run(engine, now, until=None, actions=None):
    """Transmit on a free channel, then follow the engine's timers up to until.

    actions: those of the engine's last step, if any. Returns (frames, now):
    what the engine sent, nobody answering.
    """
    frames = []
    if not actions:
        actions = list(engine.step(now, None))
    while True:
        while actions:
            action = actions[0]
            if action[0] == SENSE:
                actions = list(engine.step(now, (CHANNEL, False)))
            else:
                frames.append(action[1])
                toa = engine.toa_ms(len(action[1]))
                now += toa
                actions = list(engine.step(now, (TX_DONE, toa)))
        d = engine.deadline()
        if until is None or d is None or d > until:
            return frames, now
        now = max(now, d)
        actions = list(engine.step(now, None))


def _rx(engine, now, f):
    return list(engine.step(now, (RX, f, -80, 10)))


def _messages(key, f):
    """Return (header, messages) of a frame sealed under key."""
    platform = FakePlatform(XorShift32(98))
    sec = Security(KeyRing(key), FrameCounter(platform.storage, platform.random), ReplayTable())
    buf = bytearray(f)
    h = frame.Header(0, 0, 0, 0)
    assert sec.receive(buf, len(buf), h) is None
    return h, frame.decode_messages(buf, frame.HEADER_SIZE, len(buf) - frame.TAG_SIZE)


class Timeouts(unittest.TestCase):
    """§9 repair table: relay and root timeouts; §16 batching delay."""

    def _check(self, sf, relay_s, root_s, batch_s):
        e = _engine(N, R, sf=sf)
        rel = e.reliable
        # The tables round to 0.1 s; times on air here are rounded up to 1 ms.
        self.assertTrue(abs(rel.timeout_ms(R) - 1000 * relay_s) <= 100, rel.timeout_ms(R))
        self.assertTrue(abs(rel.timeout_ms(ROOT) - 1000 * root_s) <= 100, rel.timeout_ms(ROOT))
        self.assertTrue(abs(rel.batch_ms - 1000 * batch_s) <= 100, rel.batch_ms)

    def test_sf7(self):
        self._check(7, 0.7, 2.2, 1.8)

    def test_sf9(self):
        self._check(9, 2.2, 7.5, 6.2)

    def test_retry_after_timeout_from_the_frame_end(self):
        e = _engine(N, R)
        e.submit(20, 1, ROOT, b"x")
        frames, end = _run(e, 0)
        self.assertEqual(len(frames), 1)
        self.assertEqual(e.deadline(), end + e.reliable.timeout_ms(R))
        frames, now = _run(e, end, end + e.reliable.timeout_ms(R))
        self.assertEqual(len(frames), 1)  # the second attempt
        self.assertEqual(e.counters.retries, 1)


class LinkSamples(unittest.TestCase):
    """§9: one p_ack sample per frame, whatever the messages it carries."""

    def _frame_to_r(self, count=3):
        # N, with R in its table, sends count class 1 messages in one frame to R.
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        r.reliable.acknowledge(X, 0, 0)  # any frame of R: R in N's table
        (hello,), _ = _run(r, 0)
        _rx(n, 0, hello)
        for i in range(count):
            n.submit(20 + i, 1, ROOT, b"m%d" % i)
        (f,), now = _run(n, 100)
        self.assertEqual(len(_messages(_KEY, f)[1]), count)
        return n, r, f, now

    def test_one_sample_per_explicitly_acknowledged_frame(self):
        n, r, f, now = self._frame_to_r()
        r.reliable.acknowledge(N, frame.decode_header(f, len(f)).counter, now)
        (ack,), now = _run(r, now)
        _rx(n, now, ack)
        self.assertEqual((n.counters.acks_explicit, len(n.queue)), (3, 0))
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (1, 1))

    def test_one_sample_per_passively_acknowledged_frame(self):
        n, r, f, now = self._frame_to_r()
        actions = _rx(r, now, f)
        # The 3 forwarded in one frame, after the relay's random delay (§7).
        (fwd,), now = _run(r, now, now + 2 * r.slot_ms, actions)
        self.assertEqual(len(_messages(_KEY, fwd)[1]), 3)
        _rx(n, now, fwd)  # overheard (to the root)
        self.assertEqual((n.counters.acks_passive, len(n.queue)), (3, 0))
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (1, 1))

    def test_one_failure_per_timed_out_frame(self):
        n, r, f, end = self._frame_to_r()
        timeout = end + n.reliable.timeout_ms(R)
        frames, now = _run(n, end, timeout)
        self.assertEqual(len(frames), 1)  # the second attempt, the 3 together
        self.assertEqual(n.counters.retries, 3)
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged(), entry.fail_at), (1, 0, timeout))

    def test_one_lost_acknowledgement_keeps_the_d_in_estimate(self):
        # Fewer than 4 samples: ETX from d_in (R heard at 100 %), not 2 (§9).
        n, r, f, end = self._frame_to_r(1)
        _run(n, end, end + n.reliable.timeout_ms(R))
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged(), entry.d_in()), (1, 0, 100))
        self.assertEqual(entry.etx(), 128)


def _copies(engine):
    """Class 1/2 entries of the engine's queue (queued, waiting or held)."""
    return len(engine.queue.select(lambda m: m.cls, True))


class Custody(unittest.TestCase):
    """§12 custody: a node other than the destination drops a duplicate only
    while it holds a copy (`Reliable.holding`); one it handed over is taken
    again and acknowledged; the destination always filters."""

    def test_holding_queued_waiting_held(self):
        n = _engine(N, R, reroute=lambda msg, hop: HOLD)
        seq = n.submit(20, 1, ROOT, b"x")
        key = (N, ROOT, 20, seq)
        rel = n.reliable
        self.assertTrue(rel.holding(key))  # queued
        self.assertFalse(rel.holding((N, ROOT, 20, seq + 1)))
        frames, now = _run(n, 0)
        self.assertEqual((len(frames), rel.pending()), (1, 1))
        self.assertTrue(rel.holding(key))  # waiting
        self.assertTrue(rel.holding(key, X))
        # Back from R itself, whose acknowledgement it waits for: R has it.
        self.assertFalse(rel.holding(key, R))
        frames, now = _run(n, now, now + ATTEMPTS * rel.timeout_ms(R) + 1000)
        self.assertEqual((rel.pending(), rel.held()), (0, 1))
        self.assertTrue(rel.holding(key, R))  # held
        ((slot, msg),) = n.queue.select(lambda m: True, True)
        n.queue.remove(slot, msg)  # failed, or acknowledged
        self.assertFalse(rel.holding(key))

    def _forwarded(self):
        # N's class 1 message forwarded by R and acknowledged by the root.
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        root = _engine(ROOT, None, seed=3)
        n.submit(20, 1, ROOT, b"x")
        (f,), now = _run(n, 0)
        actions = _rx(r, now, f)
        self.assertTrue(r.reliable.holding(identity(_messages(_KEY, f)[1][0], N, R)))
        (fwd,), now = _run(r, now, now + 2 * r.slot_ms, actions)
        _rx(root, now, fwd)
        (ack,), now = _run(root, now, now + root.reliable.batch_ms)
        _rx(r, now, ack)
        self.assertEqual(len(r.queue), 0)
        return n, r, root, f, now

    def test_duplicate_held_is_dropped(self):
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        n.submit(20, 1, ROOT, b"x")
        (f,), now = _run(n, 0)
        _rx(r, now, f)  # queued at R, not yet on air
        (again,), now = _run(n, now, now + n.reliable.timeout_ms(R))  # no acknowledgement
        actions = _rx(r, now, again)
        self.assertEqual((r.counters.duplicates, r.counters.retaken, _copies(r)), (1, 0, 1))
        frames, _ = _run(r, now, None, actions)
        ports_sent = [m.port for g in frames for m in _messages(_KEY, g)[1]]
        self.assertEqual(ports_sent.count(20), 1)  # forwarded once
        self.assertTrue(ports.ACK in ports_sent, ports_sent)  # acknowledged

    def test_duplicate_handed_over_is_taken_again_and_acknowledged(self):
        n, r, root, f, now = self._forwarded()
        # N missed R's forward (its passive acknowledgement): it retries.
        (again,), now = _run(n, now, now + n.reliable.timeout_ms(R))
        actions = _rx(r, now, again)
        c = r.counters
        self.assertEqual((c.duplicates, c.retaken, _copies(r)), (0, 1, 1))
        frames, now = _run(r, now, now + 2 * r.slot_ms, actions)
        ports_sent = sorted(m.port for g in frames for m in _messages(_KEY, g)[1])
        self.assertEqual(ports_sent, [ports.ACK, 20])  # explicit acknowledgement, forward
        ack = [g for g in frames if _messages(_KEY, g)[1][0].port == ports.ACK][0]
        _rx(n, now, ack)
        self.assertEqual(len(n.queue), 0)
        # The destination filters it although it holds no copy.
        fwd = [g for g in frames if _messages(_KEY, g)[1][0].port == 20][0]
        delivered = root.counters.delivered
        _rx(root, now, fwd)
        self.assertEqual((root.counters.duplicates, root.counters.delivered), (1, delivered))


class Rules(unittest.TestCase):
    def test_class0_without_route_refused_class1_held(self):
        e = _engine(N, None)
        self.assertEqual(e.submit(20, 0, ROOT, b"x"), SUBMIT_NO_ROUTE)
        s = e.submit(20, 1, ROOT, b"y")
        self.assertTrue(s >= 0)
        frames, _ = _run(e, 1, 5000)
        self.assertEqual((frames, len(e.queue), e.reliable.held()), ([], 1, 1))

    def test_ack_only_from_the_next_hop_for_this_node(self):
        n = _engine(N, R, seed=1)
        r = _engine(R, ROOT, seed=2)
        x = _engine(X, ROOT, seed=3)
        n.submit(20, 1, ROOT, b"x")
        (f,), now = _run(n, 0)
        counter = frame.decode_header(f, len(f)).counter
        # X (not the next hop) acknowledges N's frame: ignored.
        x.reliable.acknowledge(N, counter, now)
        (ack_x,), now = _run(x, now)
        _rx(n, now, ack_x)
        self.assertEqual((n.counters.acks_explicit, len(n.queue)), (0, 1))
        # R acknowledges another node's frame with the same counter: ignored.
        r.reliable.acknowledge(X, counter, now)
        (ack_other,), now = _run(r, now)
        _rx(n, now, ack_other)
        self.assertEqual(len(n.queue), 1)
        r.reliable.acknowledge(N, counter, now)
        (ack_r,), now = _run(r, now)
        _rx(n, now, ack_r)
        self.assertEqual((n.counters.acks_explicit, len(n.queue)), (1, 0))
        self.assertEqual(n.neighbours.get(R).acknowledged(), 1)

    def test_one_entry_acknowledges_every_message_of_the_frame(self):
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        for i in range(3):
            n.submit(20 + i, 1, ROOT, b"m%d" % i)
        (f,), now = _run(n, 0)
        self.assertEqual(len(_messages(_KEY, f)[1]), 3)
        r.reliable.acknowledge(N, frame.decode_header(f, len(f)).counter, now)
        (ack,), now = _run(r, now)
        _rx(n, now, ack)
        self.assertEqual((n.counters.acks_explicit, len(n.queue)), (3, 0))

    def test_frame_with_a_refused_message_not_acknowledged(self):
        n = _engine(N, R)
        hops = lambda msg: N if msg.mode == DOWNWARD else ROOT  # noqa: E731
        r = _engine(R, hops, seed=2)
        r.set_queue(TxQueue(2))
        r.submit(20, 0, ROOT, b"fill 1")
        r.submit(20, 0, ROOT, b"fill 2")  # R's queue is full, not sent
        delivered = []
        r.bind(21, lambda origin, msg, h: delivered.append(bytes(msg.payload)))
        n.submit(21, 1, R, b"to R")  # P2P to R: delivered there
        n.submit(20, 1, ROOT, b"to the root")  # forwarded: refused by R
        (f1,), now = _run(n, 0)
        actions = _rx(r, now, f1)
        self.assertEqual(delivered, [b"to R"])
        self.assertEqual((r.counters.ack_withheld, r.counters.refused_full), (1, 1))
        self.assertEqual(r.counters.ack_entries, 0)
        # R frees its queue; N retries the whole frame: the delivered message
        # is a duplicate, the other one is queued; one entry acknowledges both.
        sent, now = _run(r, now, None, actions)
        self.assertEqual((len(sent), len(r.queue)), (1, 0))  # one frame to the root
        frames, now = _run(n, now, now + n.reliable.timeout_ms(R))
        self.assertEqual(len(frames), 1)
        actions = _rx(r, now, frames[0])
        self.assertEqual((delivered, r.counters.duplicates), ([b"to R"], 1))
        (ack, fwd), now = _run(r, now, None, actions)
        _, msgs = _messages(_KEY, ack)
        self.assertEqual(msgs[0].port, ports.ACK)
        _rx(n, now, ack)
        self.assertEqual((n.counters.acks_explicit, len(n.queue)), (2, 0))

    def test_duplicate_forgotten_after_two_presence_periods(self):
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        n.submit(20, 1, ROOT, b"x")
        frames, now = _run(n, 0, 3000)
        self.assertEqual(len(frames), ATTEMPTS)
        _rx(r, now, frames[0])
        two_periods = 2 * r.neighbours.period_ms  # §12: kept 2 presence periods
        _rx(r, now + two_periods - 1, frames[1])
        self.assertEqual((r.counters.duplicates, r.counters.forwarded), (1, 1))
        _rx(r, now + two_periods, frames[2])
        self.assertEqual((r.counters.duplicates, r.counters.forwarded), (1, 2))

    def test_never_an_error_about_an_error(self):
        # R forwards N's message to a root that never answers: R's error
        # fails in turn and is only counted.
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        failures = []
        r.on_failure(lambda *f: failures.append(f))
        n.submit(20, 1, ROOT, b"x")
        (f,), now = _run(n, 0)
        _rx(r, now, f)
        frames, now = _run(r, now, now + 60000)
        ports_sent = [[m.port for m in _messages(_KEY, g)[1]] for g in frames]
        self.assertEqual(ports_sent, [[20]] * ATTEMPTS + [[ports.ERROR]] * ATTEMPTS)
        c = r.counters
        self.assertEqual((c.unreachable, c.errors_sent, c.errors_dropped), (2, 1, 1))
        self.assertEqual((failures, len(r.queue)), ([], 0))

    def test_route_changed_resumes_held_messages_at_once(self):
        route = {"hop": None}
        e = _engine(N, lambda msg: route["hop"])
        e.submit(20, 1, ROOT, b"x")
        frames, now = _run(e, 10)
        self.assertEqual((frames, e.reliable.held()), ([], 1))
        route["hop"] = R
        e.reliable.route_changed(now + 1)
        frames, now = _run(e, now + 1)  # one step, well before the next poll
        self.assertEqual(len(frames), 1)
        self.assertEqual((e.reliable.held(), e.reliable.pending()), (0, 1))
        self.assertTrue(now < 10 + e.reliable.poll_ms)

    def test_malformed_or_foreign_errors_only_counted(self):
        e = _engine(N, R)
        failures = []
        e.on_failure(lambda *f: failures.append(f))
        h = frame.Header(N, ROOT, 1, 0)
        rel = e.reliable
        rel.on_error(ROOT, Message(DOWNWARD, 1, ports.ERROR, b"\x01\x02", dest=N, seq=1), h)
        foreign = encode_error(1, R, X, ROOT, 20, 5)
        rel.on_error(ROOT, Message(DOWNWARD, 1, ports.ERROR, foreign, dest=N, seq=2), h)
        self.assertEqual(failures, [])
        self.assertEqual((e.counters.errors_malformed, e.counters.errors_misrouted), (1, 1))
        own = encode_error(4, R, N, ROOT, 20, 6)
        rel.on_error(ROOT, Message(DOWNWARD, 1, ports.ERROR, own, dest=N, seq=3), h)
        self.assertEqual(failures, [(ROOT, 20, 6, 4)])
