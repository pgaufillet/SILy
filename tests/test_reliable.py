"""Class 1 pieces: port 3 and 4 codecs, identity, duplicate cache, timeouts, rules
(MESH_DESIGN §9, §11 ports 3 and 4, §12 class 1, §16).

Engine-level tests drive link engines step by step, frames passed by hand.
"""

import unittest

from mesh import frame, ports
from mesh import profile as profiles
from mesh.counter import FrameCounter
from mesh.frame import BROADCAST, DOWNWARD, FIRST_HOP, LINK_LOCAL, P2P, ROOT, UPWARD, Message
from mesh.link import CW_MAX, CW_MIN, MAX_ATTEMPTS, REPLY_FRAME, open_link
from mesh.link import NO_ROUTE as SUBMIT_NO_ROUTE
from mesh.neighbours import silence_ms
from mesh.radio import CHANNEL, RX, SENSE, TX_DONE
from mesh.reliable import (
    ACK_BATCH,
    ATTEMPTS,
    ERROR_SIZE,
    HOLD,
    LAZY_BATCH,
    RETRY_DOUBLINGS,
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


def _next_frame(engine, now, until):
    """Follow the engine's timers up to until, on a free channel, to its next frame.

    Returns (frame, start, end) of that frame, or (None, now, now) if none
    starts by until.
    """
    actions = list(engine.step(now, None))
    while True:
        while actions:
            if actions[0][0] == SENSE:
                actions = list(engine.step(now, (CHANNEL, False)))
                continue
            f = actions[0][1]
            toa = engine.toa_ms(len(f))
            engine.step(now + toa, (TX_DONE, toa))
            return f, now, now + toa
        d = engine.deadline()
        if d is None or d > until:
            return None, now, now
        now = max(now, d)
        actions = list(engine.step(now, None))


def _rx(engine, now, f):
    return list(engine.step(now, (RX, f, -80, 10)))


def _after_reply(engine):
    """Return the longest wait of engine's next access after a frame it sent to the root
    ended: the reply window, then a backoff of [0, CW_MIN] slots (§8)."""
    return engine.reply_ms + CW_MIN * engine.slot_ms + 1


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
        timeout = end + e.reliable.timeout_ms(R)
        self.assertEqual(e.deadline(), timeout)
        window = e.reliable.retry_window_ms()  # 64 slots: a first timeout
        f, start, _ = _next_frame(e, end, timeout + window)
        self.assertIsNotNone(f)  # the second attempt, after its retry delay
        self.assertTrue(timeout <= start < timeout + window, start - timeout)
        self.assertEqual(e.counters.retries, 1)

    def test_retry_delay_32_slots(self):
        for sf in (7, 9):
            e = _engine(N, R, sf=sf)
            self.assertEqual(e.reliable.retry_ms, 32 * e.slot_ms)


class RetryDelay(unittest.TestCase):
    """§12: a retry waits a random delay after the timeout, so that senders
    whose frames collided at their common next hop do not retry together."""

    def _retry(self, seed, hop):
        # Offset of the retry's start from the first frame's timeout, engine.
        e = _engine(N, hop, seed=seed)
        e.submit(20, 1, ROOT, b"x")
        f, _, end = _next_frame(e, 0, 0)
        timeout = end + e.reliable.timeout_ms(hop)
        f, start, _ = _next_frame(e, end, timeout + e.reliable.retry_window_ms())
        self.assertIsNotNone(f)
        self.assertEqual(e.counters.retries, 1)
        return start - timeout, e

    def test_senders_timed_out_together_retry_apart(self):
        # The same frame ending at the same time on 8 boards (8 seeds).
        for hop in (R, ROOT):
            offsets = [self._retry(seed, hop)[0] for seed in range(1, 9)]
            self.assertTrue(len(set(offsets)) >= 6, offsets)
            bound = 2 * self._retry(3, hop)[1].reliable.retry_ms  # 64 slots, any next hop
            self.assertTrue(min(offsets) >= 0 and max(offsets) < bound, (offsets, bound))
            self.assertEqual(self._retry(3, hop)[0], offsets[2])  # deterministic per seed

    def test_acknowledgement_during_the_delay_cancels_the_retry(self):
        # Towards the root: its acknowledgement of the first transmission
        # arrives after the timeout, before the delayed retry.
        r = _engine(R, ROOT, seed=2)
        root = _engine(ROOT, None, seed=3)
        root.reliable.acknowledge(X, 0, 0)  # any frame of the root
        (hello,), now = _run(root, 0, root.reliable.batch_ms)
        _rx(r, now, hello)
        r.submit(20, 1, ROOT, b"x")
        f, _, end = _next_frame(r, now, now)
        _rx(root, end, f)
        (ack,), _ = _run(root, end, end + root.reliable.batch_ms)
        timeout = end + r.reliable.timeout_ms(ROOT)
        f, _, _ = _next_frame(r, end, timeout)
        self.assertEqual((f, len(r.queue), r.counters.retries), (None, 1, 0))  # its retry waits
        self.assertTrue(r.deadline() > timeout + 1)
        _rx(r, timeout + 1, ack)
        self.assertEqual((len(r.queue), r.counters.acks_explicit), (0, 1))
        f, _, _ = _next_frame(r, timeout + 1, timeout + 4 * r.reliable.timeout_ms(ROOT))
        self.assertEqual((f, r.counters.retries), (None, 0))
        entry = r.neighbours.get(ROOT)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (1, 1))  # late: a success


class RetryBackoff(unittest.TestCase):
    """§12: the range of the retry delay doubles per frame of the node timed
    out since its last acknowledgement, the one timing out included, at most
    `RETRY_DOUBLINGS` times."""

    def _delays(self, seed, count):
        # N's frames to R, never acknowledged, R kept after each round of
        # attempts: [(offset of the retry from its frame's timeout, range of
        # that timeout's retry, read before it)] for count timeouts.
        e = _engine(N, R, seed=seed, reroute=lambda msg, hop: hop)
        rel = e.reliable
        e.submit(20, 1, ROOT, b"x")
        f, _, end = _next_frame(e, 0, 0)
        out = []
        for _ in range(count):
            window = rel.retry_window_ms()
            timeout = end + rel.timeout_ms(R)
            f, start, end = _next_frame(e, end, timeout + window)
            self.assertIsNotNone(f)
            out.append((start - timeout, window))
        return out

    def _to_r(self, count=1):
        # N, with R in its table, sends count class 1 messages in one frame
        # to R; R answers only when told.
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        r.reliable.acknowledge(X, 0, 0)  # any frame of R: R in N's table
        (hello,), _ = _run(r, 0)
        _rx(n, 0, hello)
        for i in range(count):
            n.submit(20 + i, 1, ROOT, b"m%d" % i)
        (f,), now = _run(n, 100)
        return n, r, f, now

    def _timed_out(self, n, end):
        # The frame that ended at end times out; returns its retry (frame, end).
        rel = n.reliable
        timeout = end + rel.timeout_ms(R)
        f, _, end = _next_frame(n, end, timeout + rel.retry_window_ms())
        self.assertIsNotNone(f)
        return f, end

    def test_range_doubles_per_frame_timed_out_up_to_three_times(self):
        w = _engine(N, R).reliable.retry_ms  # 32 slots
        self.assertEqual(RETRY_DOUBLINGS, 3)
        runs = [self._delays(seed, 6) for seed in range(1, 9)]  # 8 boards
        for run in runs:
            # 3 attempts, then fresh attempts towards R: the count goes on.
            self.assertEqual(
                [window for _, window in run], [2 * w, 4 * w, 8 * w, 8 * w, 8 * w, 8 * w]
            )
            for offset, window in run:
                self.assertTrue(0 <= offset < window, (offset, window))
        # The draws cover the doubled ranges, beyond the one before.
        for j in range(6):
            offsets = [run[j][0] for run in runs]
            self.assertTrue(max(offsets) >= runs[0][j][1] // 2, (j, offsets))

    def test_one_frame_timed_out_doubles_the_range_once(self):
        n, r, f, end = self._to_r(3)
        rel = n.reliable
        self.assertEqual(rel.retry_window_ms(), 2 * rel.retry_ms)
        f2, _ = self._timed_out(n, end)
        self.assertEqual(len(_messages(_KEY, f2)[1]), 3)  # the 3 retried together
        self.assertEqual((n.counters.retries, rel.retry_window_ms()), (3, 4 * rel.retry_ms))

    def test_explicit_acknowledgement_resets_the_range(self):
        n, r, f, end = self._to_r()
        rel = n.reliable
        f, end = self._timed_out(n, end)
        f, end = self._timed_out(n, end)  # the third attempt
        self.assertEqual(rel.retry_window_ms(), 8 * rel.retry_ms)
        r.reliable.acknowledge(N, frame.decode_header(f, len(f)).counter, end)
        (ack,), now = _run(r, end)
        _rx(n, now, ack)
        self.assertEqual((len(n.queue), n.counters.acks_explicit), (0, 1))
        self.assertEqual(rel.retry_window_ms(), 2 * rel.retry_ms)
        # The next frame timed out drew from 64 slots: the count restarted.
        n.submit(21, 1, ROOT, b"next")
        (f,), end = _run(n, now)
        self._timed_out(n, end)
        self.assertEqual(rel.retry_window_ms(), 4 * rel.retry_ms)

    def test_passive_acknowledgement_resets_the_range(self):
        n, r, f, end = self._to_r()
        rel = n.reliable
        f, end = self._timed_out(n, end)
        self.assertEqual(rel.retry_window_ms(), 4 * rel.retry_ms)
        actions = _rx(r, end, f)
        (fwd,), now = _run(r, end, end + 2 * r.slot_ms, actions)
        _rx(n, now, fwd)  # overheard (to the root)
        self.assertEqual((len(n.queue), n.counters.acks_passive), (0, 1))
        self.assertEqual(rel.retry_window_ms(), 2 * rel.retry_ms)

    def test_late_root_acknowledgement_resets_the_range(self):
        # Towards the root: its acknowledgement arrives after the timeout.
        r = _engine(R, ROOT, seed=2)
        root = _engine(ROOT, None, seed=3)
        root.reliable.acknowledge(X, 0, 0)  # any frame of the root
        (hello,), now = _run(root, 0, root.reliable.batch_ms)
        _rx(r, now, hello)
        r.submit(20, 1, ROOT, b"x")
        f, _, end = _next_frame(r, now, now)
        _rx(root, end, f)
        (ack,), _ = _run(root, end, end + root.reliable.batch_ms)
        rel = r.reliable
        timeout = end + rel.timeout_ms(ROOT)
        _run(r, end, timeout)
        self.assertEqual(rel.retry_window_ms(), 4 * rel.retry_ms)
        _rx(r, timeout + 1, ack)
        self.assertEqual((len(r.queue), rel.retry_window_ms()), (0, 2 * rel.retry_ms))


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
        f2, _, _ = _next_frame(n, end, timeout + n.reliable.retry_window_ms())
        self.assertEqual(len(_messages(_KEY, f2)[1]), 3)  # the second attempt, the 3 together
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


class _RootCase(unittest.TestCase):
    """A relay R sending class 1 messages to the root, its next hop."""

    def _relay(self, reroute=None):
        # R, with the root in its table, and the root.
        r = _engine(R, ROOT, seed=2, reroute=reroute)
        root = _engine(ROOT, None, seed=3)
        root.reliable.acknowledge(X, 0, 0)  # any frame of the root
        (hello,), now = _run(root, 0, root.reliable.batch_ms)
        _rx(r, now, hello)
        self.assertIsNotNone(r.neighbours.get(ROOT))
        return r, root, now

    def _send(self, r, now, i):
        # R sends one class 1 message to the root in a frame of its own,
        # after the reply window of its previous one (the sender waits, §8).
        r.submit(20 + i, 1, ROOT, b"m%d" % i)
        f, _, end = _next_frame(r, now, now + _after_reply(r))
        self.assertIsNotNone(f)
        return f, end

    def _failures(self, r):
        entry = r.neighbours.get(ROOT)
        return entry.ack_count - entry.acknowledged()


class RootSamples(_RootCase):
    """§9, §12: towards the root, one failed sample per batching delay."""

    def test_one_lost_batch_of_three_frames_one_failure(self):
        r, root, now = self._relay()
        ends = []
        for i in range(3):
            f, now = self._send(r, now, i)
            ends.append(now)
            _rx(root, now, f)
        # One batch frame acknowledges the 3 frames; R does not receive it.
        (batch,), _ = _run(root, now, ends[0] + root.reliable.batch_ms)
        entries = decode_acks(_messages(_KEY, batch)[1][0].payload)
        self.assertEqual([s for s, _ in entries], [R, R, R])
        timeout = r.reliable.timeout_ms(ROOT)
        self.assertTrue(timeout > r.reliable.batch_ms > ends[2] - ends[0])
        # The 3 timeouts (the retries wait their delays, or go): no failure
        # decided yet.
        _, now = _run(r, now, ends[2] + timeout)
        self.assertEqual(r.neighbours.get(ROOT).ack_count, 0)
        # Failures decided one root timeout after the timeouts, before any
        # retry's (a retry ends at least a timeout after its frame).
        _run(r, now, ends[2] + 2 * timeout)
        entry = r.neighbours.get(ROOT)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (1, 0))

    def test_failures_a_batching_delay_apart_counted_apart(self):
        r, root, now = self._relay()
        _, end1 = self._send(r, now, 0)
        _run(r, end1, end1 + r.reliable.batch_ms)
        _, end2 = self._send(r, end1 + r.reliable.batch_ms, 1)
        self.assertTrue(end2 - end1 >= r.reliable.batch_ms)
        timeout = r.reliable.timeout_ms(ROOT)
        # Both timed out (their retries wait their delays, or go), and their
        # failures decided a root timeout after the timeouts.
        _run(r, end2, end2 + 2 * timeout)
        self.assertEqual((len(r.queue), self._failures(r)), (2, 2))

    def test_every_attempt_towards_the_root_one_failure(self):
        # The attempts are a root timeout apart, longer than the batching
        # delay: 3 failed samples, then the message fails.
        r, root, now = self._relay()
        failures = []
        r.on_failure(lambda dest, port, seq, reason: failures.append(reason))
        _, end = self._send(r, now, 0)
        timeout = r.reliable.timeout_ms(ROOT)
        # One root timeout more after the last attempt (late acknowledgement).
        # Retry delays of 64 then 128 slots at most.
        _run(r, end, end + ATTEMPTS * (timeout + 1000) + 6 * r.reliable.retry_ms + timeout)
        self.assertEqual((r.counters.retries, self._failures(r)), (2, 3))
        self.assertEqual(failures, [1])


class LazyRootAcknowledgements(unittest.TestCase):
    """§9 presence probe: the root acknowledges a presence frame of a root child
    with a lazy entry, sent with its next acknowledgement frame or beacon."""

    def test_lazy_entries_wait_for_the_beacon_flush(self):
        root = _engine(ROOT, None, seed=3)
        rel = root.reliable
        rel.acknowledge(R, 0x12345, 0, True)
        for i in range(ACK_BATCH + 1):  # more than a batch: still no frame
            rel.acknowledge(0x0100 + i, i, 0, True)
        frames, now = _run(root, 0, 10 * rel.batch_ms)
        self.assertEqual(frames, [])  # no batching delay of their own
        rel.flush_acks()  # routing, before the root beacon
        (f,), _ = _run(root, now)
        h, msgs = _messages(_KEY, f)
        self.assertEqual(h.dest, BROADCAST)  # in the beacon's frame
        # In messages of 8 entries, in one frame.
        self.assertEqual([len(decode_acks(m.payload)) for m in msgs], [ACK_BATCH, 2])
        self.assertEqual(decode_acks(msgs[0].payload)[0], (R, 0x2345))

    def test_lazy_entries_bounded(self):
        root = _engine(ROOT, None, seed=3)
        rel = root.reliable
        for i in range(LAZY_BATCH):
            rel.acknowledge(0x0100 + i, i, 0, True)
        (f,), _ = _run(root, 0)  # sent at the bound, in one frame
        entries = sum(len(decode_acks(m.payload)) for m in _messages(_KEY, f)[1])
        self.assertEqual(entries, LAZY_BATCH)

    def test_lazy_entry_goes_with_the_next_batch(self):
        root = _engine(ROOT, None, seed=3)
        rel = root.reliable
        rel.acknowledge(R, 7, 0, True)
        rel.acknowledge(X, 9, 1000)  # a class 1 frame: its batching delay
        frames, now = _run(root, 1000, 1000 + rel.batch_ms - 1)
        self.assertEqual(frames, [])
        (f,), _ = _run(root, now, 1000 + rel.batch_ms)
        h, msgs = _messages(_KEY, f)
        self.assertEqual(h.dest, BROADCAST)  # one frame for both
        self.assertEqual([decode_acks(m.payload) for m in msgs], [[(X, 9)], [(R, 7)]])

    def test_beacon_flush_leaves_the_batch_to_its_timing(self):
        # The batch keeps its own frame: the root sends as many frames as
        # before the probe, each one heard as a proof of its presence.
        root = _engine(ROOT, None, seed=3)
        rel = root.reliable
        rel.acknowledge(X, 9, 0)
        rel.acknowledge(R, 7, 0, True)
        rel.flush_acks()
        (f,), now = _run(root, 0, rel.batch_ms - 1)
        self.assertEqual(decode_acks(_messages(_KEY, f)[1][0].payload), [(R, 7)])
        (f,), _ = _run(root, now, rel.batch_ms)
        self.assertEqual(decode_acks(_messages(_KEY, f)[1][0].payload), [(X, 9)])


class ImmediateReply(_RootCase):
    """§8 reply window: the root answers at once a relaying root child (one that
    forwarded an upward message for another node within the silence limit) for
    its presence probe and its retransmissions, with the batch and lazy entries
    waiting; the rest keeps the batch rules."""

    def _relaying(self, r, root, now):
        # R forwards a class 0 message of N to the root: a relaying root child.
        r.send(Message(UPWARD, 0, 20, b"fwd", origin=N), ROOT)
        f, _, end = _next_frame(r, now, now + _after_reply(r))
        _rx(root, end, f)
        return end

    def _probe(self, r, now):
        # R's own presence to the root (its link's probe, §9).
        r.send(Message(FIRST_HOP, 0, ports.PRESENCE, b"\x00\x00\x00"), ROOT)
        f, _, end = _next_frame(r, now, now + _after_reply(r))
        self.assertIsNotNone(f)
        return f, end

    def _at_once(self, root, now, f):
        # The root receives f at now: the frames it sends at once, no timer followed.
        return _run(root, now, None, _rx(root, now, f))[0]

    def _entries(self, f):
        # (link destination, ports, acknowledgement entries) of a root frame.
        h, msgs = _messages(_KEY, f)
        entries = [e for m in msgs if m.port == ports.ACK for e in decode_acks(m.payload)]
        return h.dest, [m.port for m in msgs], entries

    def test_probe_of_a_relaying_child_answered_at_once(self):
        r, root, now = self._relay()
        now = self._relaying(r, root, now)
        f, end = self._probe(r, now)
        (reply,) = self._at_once(root, end, f)
        c16 = frame.decode_header(f, len(f)).counter & 0xFFFF
        self.assertEqual(self._entries(reply), (R, [ports.ACK], [(R, c16)]))
        self.assertEqual(root.counters.replies, 1)

    def test_retransmission_of_a_relaying_child_answered_at_once(self):
        r, root, now = self._relay()
        now = self._relaying(r, root, now)
        f1, end1 = self._send(r, now, 0)
        self.assertEqual(self._at_once(root, end1, f1), [])  # a first copy: batched
        (batch,), _ = _run(root, end1, end1 + root.reliable.batch_ms)  # lost at R
        timeout = r.reliable.timeout_ms(ROOT)
        f2, _, end2 = _next_frame(r, end1, end1 + timeout + r.reliable.retry_window_ms())
        self.assertEqual(r.counters.retries, 1)
        (reply,) = self._at_once(root, end2, f2)
        c16 = frame.decode_header(f2, len(f2)).counter & 0xFFFF
        self.assertEqual(self._entries(reply), (R, [ports.ACK], [(R, c16)]))
        self.assertEqual((root.counters.duplicates, root.counters.replies), (1, 1))
        _rx(r, end2 + 100, reply)
        self.assertEqual((len(r.queue), r.counters.acks_explicit), (0, 1))

    def test_reply_takes_the_waiting_entries_up_to_a_batch(self):
        r, root, now = self._relay()
        now = self._relaying(r, root, now)
        rel = root.reliable
        batch = [(X, i) for i in range(3)]
        lazy = [(X, 10 + i) for i in range(6)]
        for source, counter in batch:
            rel.acknowledge(source, counter, now)
        for source, counter in lazy:
            rel.acknowledge(source, counter, now, True)
        f, end = self._probe(r, now)
        (reply,), t = _run(root, end, None, _rx(root, end, f))
        c16 = frame.decode_header(f, len(f)).counter & 0xFFFF
        dest, ports_sent, entries = self._entries(reply)
        self.assertEqual((dest, ports_sent), (BROADCAST, [ports.ACK]))  # several sources
        self.assertEqual(entries, [(R, c16)] + batch + lazy[:4])  # ACK_BATCH entries
        # The batch left with the reply: nothing at its delay; the lazy
        # entries left over go with the beacon's frame.
        frames, t = _run(root, t, now + 2 * rel.batch_ms)
        self.assertEqual(frames, [])
        rel.flush_acks()
        (beacon,), _ = _run(root, t)
        self.assertEqual(self._entries(beacon)[2], lazy[4:])

    def test_first_copy_and_other_children_batched(self):
        # A first copy of a relaying child, the probe and the retransmission
        # of a child that relays for no node: no reply.
        r, root, now = self._relay()
        f, end = self._probe(r, now)
        self.assertEqual(self._at_once(root, end, f), [])  # lazy: for the beacon
        f1, end1 = self._send(r, end, 0)
        self.assertEqual(self._at_once(root, end1, f1), [])  # first copy: batched
        _run(root, end1, end1 + root.reliable.batch_ms)  # the batch, lost at R
        timeout = r.reliable.timeout_ms(ROOT)
        f2, _, end2 = _next_frame(r, end1, end1 + timeout + r.reliable.retry_window_ms())
        self.assertIsNotNone(f2)
        self.assertEqual(self._at_once(root, end2, f2), [])  # retransmission: batched
        self.assertEqual((root.counters.duplicates, root.counters.replies), (1, 0))

    def test_relaying_within_the_silence_limit(self):
        for after, replies in ((-1, 1), (0, 0)):
            r, root, now = self._relay()
            relayed = self._relaying(r, root, now)
            silence = silence_ms(root.neighbours.period_ms)
            f, _ = self._probe(r, relayed)
            self.assertEqual(len(self._at_once(root, relayed + silence + after, f)), replies)
            self.assertEqual(root.counters.replies, replies)

    def test_reply_sent_first_without_backoff(self):
        root = _engine(ROOT, None, seed=3)
        root.send(Message(DOWNWARD, 0, 20, bytes(100), dest=N), N)
        self.assertEqual(list(root.step(0, None)), [(SENSE,)])
        root.step(0, (CHANNEL, True))  # busy: a backoff of one slot at least
        self.assertTrue(root.deadline() >= root.slot_ms)
        root.reliable.acknowledge(R, 7, 1, reply=True)
        self.assertEqual(list(root.step(1, None)), [(SENSE,)])  # at once
        ((_, f),) = list(root.step(1, (CHANNEL, False)))
        self.assertEqual(self._entries(f), (R, [ports.ACK], [(R, 7)]))  # the reply alone
        toa = root.toa_ms(len(f))
        (f,), _ = _run(root, 1 + toa, None, list(root.step(1 + toa, (TX_DONE, toa))))
        self.assertEqual(self._entries(f)[1], [20])  # then the downward message

    def test_busy_sense_with_a_reply_pending_waits_one_slot(self):
        # The skip is once: a busy sense while the reply is pending is followed
        # by one slot, never by a sense in the step of its answer (on the
        # board the answer comes within the step that asked: the runner would
        # spin while the channel reads busy); the window does not grow.
        root = _engine(ROOT, None, seed=3)
        root.reliable.acknowledge(R, 7, 0, reply=True)
        now = 0
        self.assertEqual(list(root.step(now, None)), [(SENSE,)])
        for _ in range(MAX_ATTEMPTS - 1):
            self.assertEqual(list(root.step(now, (CHANNEL, True))), [])
            self.assertEqual(root.deadline(), now + root.slot_ms)
            now += root.slot_ms
            self.assertEqual(list(root.step(now, None)), [(SENSE,)])
        ((_, f),) = list(root.step(now, (CHANNEL, False)))
        self.assertEqual(self._entries(f), (R, [ports.ACK], [(R, 7)]))
        self.assertEqual((root.counters.busy, root.counters.give_ups), (MAX_ATTEMPTS - 1, 0))

    def test_busy_senses_with_a_reply_pending_give_up_as_usual(self):
        # MAX_ATTEMPTS busy senses: the give-up backoff of [1, CW_MAX] slots,
        # not a sense at once; then a new procedure, the reply first.
        waits = set()
        for seed in range(12):
            root = _engine(ROOT, None, seed=seed)
            root.reliable.acknowledge(R, 7, 0, reply=True)
            now = 0
            actions = list(root.step(now, None))
            for _ in range(MAX_ATTEMPTS):
                self.assertEqual(actions, [(SENSE,)])
                self.assertEqual(list(root.step(now, (CHANNEL, True))), [])
                last, now = now, root.deadline()
                actions = list(root.step(now, None))
            self.assertEqual(root.counters.give_ups, 1)
            self.assertEqual((now - last) % root.slot_ms, 0)
            waits.add((now - last) // root.slot_ms)
            self.assertEqual(actions, [(SENSE,)])
            ((_, f),) = list(root.step(now, (CHANNEL, False)))
            self.assertEqual(self._entries(f), (R, [ports.ACK], [(R, 7)]))
        self.assertTrue(min(waits) >= 1 and max(waits) <= CW_MAX, sorted(waits))
        self.assertTrue(max(waits) > 1, sorted(waits))  # drawn, not the reply's slot

    def test_reply_queued_while_sensing_another_frame_keeps_its_rules(self):
        # The frame sensed before the reply was queued goes first; the reply
        # stays pending: a busy sense waits one slot, and it leaves alone in
        # a short frame, though a longer message to the same node waits.
        root = _engine(ROOT, None, seed=3)
        root.send(Message(DOWNWARD, 0, 20, bytes(100), dest=N), N)
        self.assertEqual(list(root.step(0, None)), [(SENSE,)])
        root.send(Message(DOWNWARD, 0, 21, bytes(100), dest=R), R)
        root.reliable.acknowledge(R, 7, 0, reply=True)
        ((_, f),) = list(root.step(0, (CHANNEL, False)))
        self.assertEqual(self._entries(f)[1], [20])
        now = root.toa_ms(len(f))
        self.assertEqual(list(root.step(now, (TX_DONE, now))), [(SENSE,)])
        self.assertEqual(list(root.step(now, (CHANNEL, True))), [])
        self.assertEqual(root.deadline(), now + root.slot_ms)
        now += root.slot_ms
        self.assertEqual(list(root.step(now, None)), [(SENSE,)])
        ((_, f),) = list(root.step(now, (CHANNEL, False)))
        self.assertTrue(len(f) <= REPLY_FRAME, len(f))
        self.assertEqual(self._entries(f), (R, [ports.ACK], [(R, 7)]))
        toa = root.toa_ms(len(f))
        (f,), _ = _run(root, now + toa, None, list(root.step(now + toa, (TX_DONE, toa))))
        self.assertEqual(self._entries(f)[1], [21])  # then the message to R


class LateRootAcknowledgements(_RootCase):
    """§12: towards the root, a late acknowledgement of an earlier transmission
    is accepted; a failure is decided one root timeout after the timeout."""

    def test_late_acknowledgement_after_the_retry_is_a_success(self):
        r, root, now = self._relay()
        delivered = []
        root.bind(20, lambda origin, msg, h: delivered.append(origin))
        f, end = self._send(r, now, 0)
        _rx(root, end, f)  # the root has it; its acknowledgement is late
        timeout = r.reliable.timeout_ms(ROOT)
        # The timeout, then the retry after its delay.
        f2, _, end2 = _next_frame(r, end, end + timeout + r.reliable.retry_window_ms())
        self.assertEqual((r.counters.retries, self._failures(r)), (1, 0))
        actions = _rx(root, end2, f2)  # the retry too: a duplicate, acknowledged again
        acks, now = _run(root, end2, end2 + root.reliable.batch_ms, actions)
        self.assertTrue(now < end + 2 * timeout and now < end2 + timeout)
        self.assertEqual((delivered, root.counters.duplicates), ([R], 1))
        for a in acks:
            _rx(r, now, a)
        self.assertEqual(len(r.queue), 0)
        entry = r.neighbours.get(ROOT)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (1, 1))
        # Nothing more: no further attempt, no failed sample.
        frames, _ = _run(r, now, end + 2 * ATTEMPTS * timeout)
        self.assertEqual((frames, r.counters.retries, self._failures(r)), ([], 1, 0))

    def test_late_acknowledgement_of_the_last_attempt_cancels_the_drop(self):
        calls = []
        r, root, now = self._relay(reroute=lambda msg, hop: calls.append(hop))
        timeout = r.reliable.timeout_ms(ROOT)
        _, end = self._send(r, now, 0)
        _, _, end = _next_frame(r, end, end + timeout + r.reliable.retry_window_ms())
        f3, _, end = _next_frame(
            r, end, end + timeout + r.reliable.retry_window_ms()
        )  # the third attempt
        self.assertEqual((f3 is None, r.counters.retries), (False, 2))
        _run(r, end, end + timeout)  # its timeout
        _rx(root, end, f3)
        (ack,), _ = _run(root, end, end + root.reliable.batch_ms)
        late = end + timeout + timeout // 2
        _rx(r, late, ack)
        self.assertEqual((len(r.queue), calls), (0, []))
        _run(r, late, end + 3 * timeout)
        self.assertEqual((calls, self._failures(r)), ([], 2))

    def test_acknowledgement_of_an_earlier_transmission_removes_the_message(self):
        # The root has the first transmission only; its acknowledgement
        # comes while R waits for the retry's, or once R holds the message.
        for held in (False, True):
            r, root, now = self._relay()
            f, end = self._send(r, now, 0)
            _rx(root, end, f)
            (ack,), _ = _run(root, end, end + root.reliable.batch_ms)
            timeout = r.reliable.timeout_ms(ROOT)
            f2, _, end2 = _next_frame(r, end, end + timeout + r.reliable.retry_window_ms())
            self.assertIsNotNone(f2)  # the retry, lost
            if held:
                ((slot, msg),) = r.queue.select(lambda m: True, True)
                r.reliable.hold(slot, msg, end2)
                self.assertEqual((r.reliable.pending(), r.reliable.held()), (0, 1))
            _rx(r, end2, ack)
            self.assertEqual((len(r.queue), r.counters.acks_explicit), (0, 1))
            frames, _ = _run(r, end2, end + 4 * timeout)
            self.assertEqual((frames, self._failures(r)), ([], 0))
            self.assertEqual(r.neighbours.get(ROOT).acknowledged(), 1)  # late: a success

    def test_no_acknowledgement_fails_one_root_timeout_after_the_last(self):
        r, root, now = self._relay()
        failures = []
        r.on_failure(lambda dest, port, seq, reason: failures.append((reason, r.now)))
        timeout = r.reliable.timeout_ms(ROOT)
        _, end = self._send(r, now, 0)
        f2, _, end2 = _next_frame(r, end, end + timeout + r.reliable.retry_window_ms())
        self.assertEqual((f2 is None, self._failures(r)), (False, 0))  # failure not decided yet
        _run(r, end2, end + 2 * timeout)
        self.assertEqual(self._failures(r), 1)  # one root timeout after the timeout
        f3, _, end3 = _next_frame(
            r, end + 2 * timeout, end2 + timeout + r.reliable.retry_window_ms()
        )
        self.assertEqual((f3 is None, r.counters.retries), (False, 2))
        _run(r, end3, end3 + timeout)
        self.assertEqual((failures, len(r.queue)), ([], 1))  # waits one root timeout more
        _run(r, end3 + timeout, end3 + 2 * timeout)
        self.assertEqual(failures, [(1, end3 + 2 * timeout)])
        self.assertEqual((self._failures(r), len(r.queue)), (3, 0))

    def test_failures_decided_together_in_frame_order(self):
        # Frames A, B, then C a batching delay after B; A's late
        # acknowledgement frees its pending failure before C times out. B's
        # and C's failures, decided in one step, are a batching delay apart:
        # two samples, whatever the order C and B were recorded in.
        r, root, now = self._relay()
        timeout = r.reliable.timeout_ms(ROOT)
        fa, end_a = self._send(r, now, 0)
        _rx(root, end_a, fa)
        (ack,), _ = _run(root, end_a, end_a + root.reliable.batch_ms)
        fb, end_b = self._send(r, end_a, 1)
        # B waits for the reply window after A (§8): C ends, rather than
        # starts, a batching delay after B, so that it is sent before A
        # times out (C's frame is as long as B's).
        _, end_c = self._send(r, end_b + r.reliable.batch_ms - r.toa_ms(len(fb)), 2)
        self.assertTrue(end_c < end_a + timeout and end_c - end_b >= r.reliable.batch_ms)
        # A times out (its retry waits its delay, or is sent).
        _, now = _run(r, end_c, end_a + timeout)
        _rx(r, now, ack)  # A acknowledged late: a success
        # B and C time out: their retries go after their delays.
        _, now = _run(r, now, end_c + timeout)
        self.assertTrue(now < end_b + 2 * timeout)  # B's failure not decided yet
        self.assertEqual((len(r.queue), self._failures(r)), (2, 0))
        r.step(end_c + 2 * timeout, None)  # both failures due in this step
        entry = r.neighbours.get(ROOT)
        self.assertEqual((entry.ack_count, entry.acknowledged()), (3, 1))


class RootRun(_RootCase):
    """§9 reachability of the root: the run of consecutive failed samples
    counts the acknowledgement window's failures (one per lost batch) and
    ends at any acknowledgement of the root, explicit or late."""

    def test_one_lost_batch_one_failure_of_the_run(self):
        r, root, now = self._relay()
        ends = []
        for i in range(3):
            f, now = self._send(r, now, i)
            ends.append(now)
            _rx(root, now, f)
        _run(root, now, ends[0] + root.reliable.batch_ms)  # the batch, lost
        _run(r, now, ends[2] + 2 * r.reliable.timeout_ms(ROOT))
        self.assertEqual(r.neighbours.get(ROOT).run, 1)

    def test_acknowledgement_ends_the_run(self):
        # Two frames fail (a batching delay apart), then the root
        # acknowledges a third one: the run ends, the time is recorded.
        r, root, now = self._relay()
        _, end1 = self._send(r, now, 0)
        _run(r, end1, end1 + r.reliable.batch_ms)
        _, end2 = self._send(r, end1 + r.reliable.batch_ms, 1)
        timeout = r.reliable.timeout_ms(ROOT)
        _, now = _run(r, end2, end2 + 2 * timeout)
        entry = r.neighbours.get(ROOT)
        self.assertEqual(entry.run, 2)
        r.submit(30, 1, ROOT, b"z")
        f, _, end = _next_frame(r, now, now)
        _rx(root, end, f)
        (ack,), now = _run(root, end, end + root.reliable.batch_ms)
        _rx(r, now, ack)
        self.assertEqual((entry.run, entry.answered), (0, now))

    def test_acknowledgement_after_the_failure_decided_ends_the_run(self):
        # The root's acknowledgement of R's first transmission comes after
        # its failure was decided (no sample, a late one): it ends the run.
        r, root, now = self._relay()
        f, end = self._send(r, now, 0)
        _rx(root, end, f)
        (ack,), _ = _run(root, end, end + root.reliable.batch_ms)
        timeout = r.reliable.timeout_ms(ROOT)
        f2, _, end2 = _next_frame(r, end, end + timeout + r.reliable.retry_window_ms())
        self.assertIsNotNone(f2)  # the retry, lost
        _, now = _run(r, end2, end + 2 * timeout)
        entry = r.neighbours.get(ROOT)
        self.assertEqual((entry.run, self._failures(r), len(r.queue)), (1, 1, 1))
        _rx(r, now, ack)
        self.assertEqual(len(r.queue), 0)
        self.assertEqual((entry.run, entry.answered, self._failures(r)), (0, now, 1))
        self.assertEqual(entry.acknowledged(), 0)  # no sample: its failure stays

    def test_acknowledgement_of_another_node_does_not_end_the_run(self):
        r, root, now = self._relay()
        _, end = self._send(r, now, 0)
        _, now = _run(r, end, end + 2 * r.reliable.timeout_ms(ROOT))
        entry = r.neighbours.get(ROOT)
        self.assertEqual(entry.run, 1)
        root.reliable.acknowledge(X, 7, now)
        (ack,), now = _run(root, now, now + root.reliable.batch_ms)
        _rx(r, now, ack)
        self.assertEqual(entry.run, 1)


class RelayRun(unittest.TestCase):
    """§9 reachability of a relay link (the preferred parent as the last
    candidate): every acknowledgement of the relay ends the run of failed
    samples, explicit or passive, with a sample or without."""

    def _to_r(self, *groups):
        # N, with R in its table, sends one frame to R per group of class 1
        # messages; R answers only when told. Returns n, r, the frames and
        # their ends.
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        r.reliable.acknowledge(X, 0, 0)  # any frame of R: R in N's table
        (hello,), self.rt = _run(r, 0)  # R's own clock
        _rx(n, 0, hello)
        frames = []
        now = 100
        for group in groups:
            for i in group:
                n.submit(20 + i, 1, ROOT, b"m%d" % i)
            (f,), now = _run(n, now)
            self.assertEqual(len(_messages(_KEY, f)[1]), len(group))
            frames.append((f, now))
        return n, r, frames

    def _forward(self, r, f, count=None):
        # R's frame forwarding the first count messages of N's frame f, after
        # the reply window of R's previous frame to the root (§8).
        msgs = _messages(_KEY, f)[1][:count]
        for m in msgs:
            r.send(Message(UPWARD, m.cls, m.port, bytes(m.payload), origin=N, seq=m.seq), ROOT)
        fwd, _, self.rt = _next_frame(r, self.rt, self.rt + _after_reply(r))
        self.assertIsNotNone(fwd)
        return fwd

    def test_passive_acknowledgement_ends_the_run(self):
        n, r, ((f, end),) = self._to_r([0])
        _run(n, end, end + n.reliable.timeout_ms(R))  # timed out: a failed sample
        entry = n.neighbours.get(R)
        self.assertEqual(entry.run, 1)
        now = end + n.reliable.timeout_ms(R) + 1
        _rx(n, now, self._forward(r, f))  # R's late forward, overheard
        self.assertEqual((entry.run, entry.answered, n.counters.acks_passive), (0, now, 1))

    def test_passive_acknowledgement_without_sample_ends_the_run(self):
        # F0 (m0), then F1 (m1, m2). R's forward of m1 gives F1's sample; F0
        # then times out; R's forward of m2, F1 sampled already, gives no
        # sample but ends the run.
        n, r, ((f0, end0), (f1, end1)) = self._to_r([0], [1, 2])
        fwd1 = self._forward(r, f1, 1)
        fwd2 = self._forward(r, f1)  # m2 again with m1: m1 no longer waits
        timeout = n.reliable.timeout_ms(R)
        _rx(n, end1, fwd1)
        entry = n.neighbours.get(R)
        self.assertEqual((entry.ack_count, entry.acknowledged(), len(n.queue)), (1, 1, 2))
        _run(n, end1, end0 + timeout)
        self.assertEqual((entry.ack_count, entry.acknowledged(), entry.run), (2, 1, 1))
        self.assertTrue(end0 + timeout < end1 + timeout)  # F1 still waiting
        _rx(n, end0 + timeout + 1, fwd2)
        self.assertEqual(n.counters.acks_passive, 2)
        self.assertEqual((entry.ack_count, entry.run, entry.answered), (2, 0, end0 + timeout + 1))

    def test_explicit_acknowledgement_after_the_timeout_ends_the_run(self):
        # R's explicit acknowledgement comes after N's timeout (no sample:
        # the frame failed already): it ends the run.
        n, r, ((f, end),) = self._to_r([0])
        timeout = n.reliable.timeout_ms(R)
        _run(n, end, end + timeout)
        entry = n.neighbours.get(R)
        self.assertEqual(entry.run, 1)
        r.reliable.acknowledge(N, frame.decode_header(f, len(f)).counter, self.rt)
        (ack,), _ = _run(r, self.rt)
        _rx(n, end + timeout + 1, ack)
        self.assertEqual((entry.run, entry.answered), (0, end + timeout + 1))
        self.assertEqual((entry.ack_count, entry.acknowledged()), (1, 0))

    def test_acknowledgement_of_another_node_does_not_end_the_run(self):
        n, r, ((f, end),) = self._to_r([0])
        timeout = n.reliable.timeout_ms(R)
        _run(n, end, end + timeout)
        r.reliable.acknowledge(X, 7, self.rt)
        (ack,), _ = _run(r, self.rt)
        _rx(n, end + timeout + 1, ack)
        self.assertEqual(n.neighbours.get(R).run, 1)


class KeptNextHop(_RootCase):
    """§12 upward failure: "next hop after failure" returning the same hop
    (routing keeps the root) gives the message fresh attempts towards it."""

    def test_fresh_attempts_towards_the_kept_hop(self):
        calls = []
        r, root, now = self._relay(reroute=lambda msg, hop: calls.append(hop) or hop)
        failures = []
        r.on_failure(lambda dest, port, seq, reason: failures.append(reason))
        f, end = self._send(r, now, 0)
        frames, _ = _run(r, end, end + 20 * r.reliable.timeout_ms(ROOT))
        frames.insert(0, f)
        self.assertEqual(failures, [])
        self.assertTrue(len(calls) >= 2, calls)
        self.assertEqual(set(calls), {ROOT})
        # 3 attempts per round, every round towards the root.
        self.assertTrue(3 * len(calls) <= len(frames) <= 3 * len(calls) + 3, len(frames))
        self.assertEqual({frame.decode_header(x, len(x)).dest for x in frames}, {ROOT})
        self.assertEqual((len(r.queue), r.counters.unreachable, r.counters.reroutes), (1, 0, 0))
        # Each one failed sample, the last one's possibly not decided yet.
        self.assertTrue(len(frames) - 1 <= self._failures(r) <= len(frames), self._failures(r))


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
        # Up to the hold after the last attempt (before its first poll).
        limit = (
            now + ATTEMPTS * rel.timeout_ms(R) + 6 * rel.retry_ms + 1000
        )  # delays: 64, 128 slots
        while not rel.held() and n.deadline() is not None and now < limit:
            _, now = _run(n, now, n.deadline())
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
        # No acknowledgement: the retry, after its delay.
        again, _, now = _next_frame(
            n, now, now + n.reliable.timeout_ms(R) + n.reliable.retry_window_ms()
        )
        actions = _rx(r, now, again)
        self.assertEqual((r.counters.duplicates, r.counters.retaken, _copies(r)), (1, 0, 1))
        # The forward (after at most a slot) and the acknowledgement, which
        # may wait for the reply window of the forward to the root (§8).
        until = now + r.slot_ms + r.toa_ms(r.limit) + _after_reply(r)
        frames, _ = _run(r, now, until, actions)
        ports_sent = [m.port for g in frames for m in _messages(_KEY, g)[1]]
        self.assertEqual(ports_sent.count(20), 1)  # forwarded once
        self.assertTrue(ports.ACK in ports_sent, ports_sent)  # acknowledged

    def test_duplicate_handed_over_is_taken_again_and_acknowledged(self):
        n, r, root, f, now = self._forwarded()
        # N missed R's forward (its passive acknowledgement): it retries.
        again, _, now = _next_frame(
            n, now, now + n.reliable.timeout_ms(R) + n.reliable.retry_window_ms()
        )
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
        f2, _, now = _next_frame(
            n, now, now + n.reliable.timeout_ms(R) + n.reliable.retry_window_ms()
        )
        self.assertIsNotNone(f2)
        actions = _rx(r, now, f2)
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
        frames, now = _run(n, 0, 3000 + 6 * n.reliable.retry_ms)  # delays: 64, 128 slots
        self.assertEqual(len(frames), ATTEMPTS)
        _rx(r, now, frames[0])
        two_periods = 2 * r.neighbours.period_ms  # §12: kept 2 presence periods
        _rx(r, now + two_periods - 1, frames[1])
        self.assertEqual((r.counters.duplicates, r.counters.forwarded), (1, 1))
        _rx(r, now + two_periods, frames[2])
        self.assertEqual((r.counters.duplicates, r.counters.forwarded), (1, 2))

    def _three_copies(self):
        # R's three attempts of one class 1 message to the root, none
        # acknowledged; the root delivers port 20 into the list returned.
        r = _engine(R, ROOT, seed=2)
        root = _engine(ROOT, None, seed=3)
        delivered = []
        root.bind(20, lambda origin, msg, h: delivered.append(origin))
        r.submit(20, 1, ROOT, b"x")
        frames, now = _run(r, 0, 3000 + 6 * r.reliable.retry_ms)
        self.assertEqual(len(frames), ATTEMPTS)
        return r, root, delivered, frames, now

    def test_duplicate_window_slides_at_the_destination(self):
        # §12: a copy dropped at its destination refreshes its entry, so the
        # window runs from the last copy received, not from the first.
        _, root, delivered, frames, now = self._three_copies()
        window = root.reliable.duplicate_ms()
        _rx(root, now, frames[0])
        _rx(root, now + window - 1, frames[1])
        _rx(root, now + 2 * window - 2, frames[2])  # beyond the first copy's window
        self.assertEqual((delivered, root.counters.duplicates), ([R], 2))

    def test_root_window_six_periods_and_256_entries(self):
        # §12: the root keeps 256 identities for 6 presence periods; the
        # other nodes 128 for 2 periods.
        r, root, delivered, frames, now = self._three_copies()
        period = root.neighbours.period_ms
        self.assertEqual(r.reliable.duplicate_ms(), 2 * period)
        _rx(root, now, frames[0])
        for i in range(255):  # 255 other identities: the first one stays
            root.reliable.remember((X, ROOT, 21, i), now)
        _rx(root, now + 6 * period - 1, frames[1])
        self.assertEqual((delivered, root.counters.duplicates), ([R], 1))
        _rx(root, now + 12 * period - 1, frames[2])  # 6 periods after the last copy
        self.assertEqual((delivered, root.counters.duplicates), ([R, R], 1))

    def test_never_an_error_about_an_error(self):
        # R forwards N's message to a root that never answers: R's error
        # goes to N, a neighbour, directly; N never answers either: kept
        # while N is heard, for at most the holding, it goes to the root
        # instead, fails in turn and is only counted.
        n = _engine(N, R)
        r = _engine(R, ROOT, seed=2)
        failures = []
        r.on_failure(lambda *f: failures.append(f))
        n.submit(20, 1, ROOT, b"x")
        (f,), now = _run(n, 0)
        _rx(r, now, f)
        frames, now = _run(r, now, now + 200000)
        sent = []
        for g in frames:
            h, msgs = _messages(_KEY, g)
            sent.append((h.dest, [(m.mode, m.port) for m in msgs]))
        data = (ROOT, [(UPWARD, 20)])
        direct = (N, [(LINK_LOCAL, ports.ERROR)])
        upward = (ROOT, [(FIRST_HOP, ports.ERROR)])
        k = sent.count(direct)
        self.assertEqual(sent, [data] * ATTEMPTS + [direct] * k + [upward] * ATTEMPTS)
        self.assertTrue(k > ATTEMPTS and k % ATTEMPTS == 0, k)
        c = r.counters
        self.assertEqual((c.errors_sent, c.errors_direct, c.errors_dropped), (1, 1, 1))
        self.assertEqual((failures, len(r.queue)), ([], 0))

    def test_error_takes_the_entry_of_the_failed_message_whatever_the_quota(self):
        # A relay without route holds 30 forwarded messages and 16 of its
        # own (its quota, the queue more than half full): when the forwarded
        # ones fail, each error takes the entry its message leaves instead of
        # being refused by the quota (a silent loss, MP5).
        e = _engine(R, lambda msg: None)
        rel = e.reliable
        period = e.neighbours.period_ms
        for i in range(30):
            msg = Message(UPWARD, 1, 20, b"f", origin=0x0100 + i, seq=i)
            e.queue.offer(msg, BROADCAST)
            rel.hold(e.queue.slot, msg, 0)
        for _ in range(16):
            e.submit(20, 1, ROOT, b"own")
        _run(e, period, period)  # own ones held from here: they fail later
        self.assertEqual((len(e.queue), e.queue.holding(-1)), (46, 16))
        _run(e, period, 2 * period + rel.poll_ms)
        errors = e.queue.select(lambda m: m.port == ports.ERROR, True)
        self.assertEqual(len(errors), 30)
        c = e.counters
        self.assertEqual((c.errors_sent, c.errors_dropped, len(e.queue)), (30, 0, 46))

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


def _grounded_hop(hop):
    # next_hop of routing (`Dodag.next_hop`): none for an upward class 1/2
    # message while the node is ungrounded.
    box = {}

    def next_hop(msg):
        rel = box["engine"].reliable
        if msg.cls and (msg.mode == UPWARD or msg.mode == FIRST_HOP) and rel.ungrounded:
            return None
        return hop

    return box, next_hop


class Ungrounded(unittest.TestCase):
    """§9, §12: an ungrounded node holds its upward class 1/2 messages (no
    forward, no retry, no drop of the parent) and fails a received one 2
    presence periods after the loss of grounding, whatever its arrival;
    grounded again, it sends them (`Reliable.grounding`)."""

    def _node(self, address, hop, seed=1):
        box, next_hop = _grounded_hop(hop)
        drops = []
        e = _engine(address, next_hop, seed, reroute=lambda msg, h: drops.append(h) or HOLD)
        box["engine"] = e
        return e, drops

    def test_queued_messages_held_at_the_loss_of_grounding(self):
        e, drops = self._node(N, R)
        rel = e.reliable
        e.submit(20, 1, ROOT, b"x")
        e.submit(20, 0, ROOT, b"report")  # class 0: still sent
        rel.grounding(False, 5, 5)
        frames, now = _run(e, 5, 5 + rel.hold_ms() // 2)
        ports_sent = [m.port for f in frames for m in _messages(_KEY, f)[1]]
        self.assertEqual(ports_sent, [20])
        self.assertEqual(_messages(_KEY, frames[0])[1][0].cls, 0)
        self.assertEqual((rel.held(), len(e.queue), drops), (1, 1, []))
        # Grounded again: sent at once.
        rel.grounding(True, None, now + 1)
        frames, _ = _run(e, now + 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual((rel.held(), rel.pending()), (0, 1))

    def test_waiting_message_held_at_its_timeout_no_retry_no_drop(self):
        e, drops = self._node(N, R)
        rel = e.reliable
        e.submit(20, 1, ROOT, b"x")
        (f,), now = _run(e, 0)
        self.assertEqual(rel.pending(), 1)
        rel.grounding(False, now, now)
        self.assertEqual(rel.pending(), 1)  # waiting: held at its timeout only
        frames, now = _run(e, now, now + 3 * rel.timeout_ms(R) + 8 * rel.retry_ms)
        self.assertEqual((frames, rel.pending(), rel.held()), ([], 0, 1))
        self.assertEqual((e.counters.retries, drops), (0, []))

    def test_received_message_fails_two_periods_after_the_loss_not_its_arrival(self):
        n, _ = self._node(N, R)
        r, _ = self._node(R, ROOT, seed=2)
        rr = r.reliable
        period = r.neighbours.period_ms
        lost = 100
        rr.grounding(False, lost, lost)
        failed = []
        fail = rr.fail
        rr.fail = lambda slot, msg, reason, hop, now: (
            failed.append((now, msg.port, reason)),
            fail(slot, msg, reason, hop, now),
        )
        seq = n.submit(20, 1, ROOT, b"x")
        (f,), now = _run(n, period)  # arrives one period after the loss
        actions = _rx(r, now, f)
        frames, now = _run(r, now, now + 2 * r.slot_ms, actions)
        ports_sent = [m.port for g in frames for m in _messages(_KEY, g)[1]]
        self.assertEqual(ports_sent, [ports.ACK])  # held and acknowledged, not forwarded
        self.assertEqual(rr.held(), 1)
        _rx(n, now, frames[0])
        self.assertEqual(len(n.queue), 0)
        failures = []
        n.on_failure(lambda *f: failures.append(f))
        frames, now = _run(r, now, lost + 2 * period + 2 * r.slot_ms)
        # Failed at the loss of grounding + 2 periods; its error goes to N,
        # a neighbour, directly (link-local: not held although R is
        # ungrounded), and N reports the failure.
        self.assertEqual(failed, [(lost + 2 * period, 20, 3)])
        (g,) = frames
        h, msgs = _messages(_KEY, g)
        self.assertEqual(
            (h.dest, [(m.mode, m.port) for m in msgs]), (N, [(LINK_LOCAL, ports.ERROR)])
        )
        _rx(n, now, g)
        self.assertEqual(failures, [(ROOT, 20, seq, 3)])
        self.assertEqual((r.counters.errors_sent, r.counters.errors_direct), (1, 1))

    def test_error_about_a_farther_origin_held_from_its_own_hold(self):
        # The message comes from N through X: N is no neighbour of R, so the
        # error goes upward; R being ungrounded, it is held 2 periods from
        # its own hold, then dropped (no path: never an error about an error).
        x, _ = self._node(X, R)
        r, _ = self._node(R, ROOT, seed=2)
        rr = r.reliable
        period = r.neighbours.period_ms
        lost = 100
        rr.grounding(False, lost, lost)
        failed = []
        fail = rr.fail
        rr.fail = lambda slot, msg, reason, hop, now: (
            failed.append((now, msg.port, msg.mode, reason)),
            fail(slot, msg, reason, hop, now),
        )
        x.send(Message(UPWARD, 1, 20, b"x", origin=N, seq=5), R)
        (f,), now = _run(x, period)
        actions = _rx(r, now, f)
        _, now = _run(r, now, lost + 4 * period, actions)
        self.assertEqual(failed[0], (lost + 2 * period, 20, UPWARD, 3))
        self.assertEqual(failed[1], (lost + 4 * period, ports.ERROR, FIRST_HOP, 3))
        c = r.counters
        self.assertEqual((c.errors_sent, c.errors_direct, c.errors_dropped), (1, 0, 1))

    def test_own_message_held_two_periods_from_its_hold(self):
        e, _ = self._node(N, R)
        rel = e.reliable
        period = e.neighbours.period_ms
        failures = []
        e.on_failure(lambda *f: failures.append((e.now,) + f))
        rel.grounding(False, 0, 0)
        e.submit(20, 1, ROOT, b"x")
        _run(e, period, 4 * period)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], period + 2 * period)
        self.assertEqual(failures[0][4], 3)
