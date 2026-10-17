"""Link engine, driven step by step (MESH_DESIGN §6, §8, §13, §14; R14; MP5)."""

import unittest

from mesh import frame, ports
from mesh import profile as profiles
from mesh.counter import COUNTER_LIMIT, RECORD_KEY
from mesh.frame import BROADCAST, ROOT, Header, Message
from mesh.link import (
    AUTH,
    BAD_CLASS,
    BAD_DEST,
    BAD_PORT,
    BROADCAST_CLASS,
    CW_MAX,
    CW_MIN,
    EXHAUSTED,
    KNOWN,
    NO_ROUTE,
    ORIGIN_QUOTA,
    QUEUE_FULL,
    REPLAYS,
    TOO_LONG,
    UNSUPPORTED,
    LinkEngine,
    SenderCounters,
    open_link,
)
from mesh.radio import CHANNEL, RX, RX_ERROR, SENSE, SENSE_ACTION, TX, TX_DONE
from mesh.txqueue import TxQueue
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))
_OTHER_KEY = bytes(range(1, 17))
_EU = profiles.get(profiles.EU_869)
_ATTEMPTS = 6  # carrier senses before giving up (§8)
_BUDGET = 10000  # scaled duty-cycle budget per 60 s window
_WINDOW = 60000

A = 0x00A1
B = 0x00B1
C = 0x00C1


def _engine(address, seed=1, key=_KEY, hop=None, start=None, profile=_EU, budget=_BUDGET):
    """Return a started engine; hop: static next hop of every message."""
    platform = FakePlatform(XorShift32(seed))
    if start is not None:
        platform.storage.set(RECORD_KEY, start.to_bytes(4, "little"))
        platform.storage.commit()
    next_hop = None if hop is None else (lambda msg: hop)
    engine = open_link(
        address,
        key,
        platform.storage,
        platform.retained,
        platform.random,
        profile,
        7,
        next_hop,
        budget_ms=budget,
        window_ms=_WINDOW,
    )
    engine.step(0, None)
    return engine


def _actions(engine, now, event=None):
    return list(engine.step(now, event))


def _drain(engine, now, actions=None):
    """Transmit everything engine has queued on an idle channel; return (frames, now).

    actions: those of the engine's last step, if it was not driven by _drain.
    """
    frames = []
    if not actions:
        actions = _actions(engine, now)
    while actions:
        action = actions[0]
        if action[0] == SENSE:
            actions = _actions(engine, now, (CHANNEL, False))
        else:
            frames.append(action[1])
            toa = engine.toa_ms(len(action[1]))
            now += toa
            actions = _actions(engine, now, (TX_DONE, toa))
    return frames, now


def _rx(engine, now, data):
    return _actions(engine, now, (RX, data, -80, 10))


class _Inbox:
    """Handler recording (origin, port, payload) of delivered messages."""

    def __init__(self):
        self.messages = []

    def __call__(self, origin, msg, header):
        self.messages.append((origin, msg.port, bytes(msg.payload)))


class Submission(unittest.TestCase):
    def test_accepted_messages_get_sequences(self):
        e = _engine(A, hop=B)
        self.assertEqual(e.submit(20, 0, ROOT, b"datagram"), 0)
        s = e.submit(20, 1, B, b"class 1")
        self.assertTrue(0 <= s <= 0xFFFF)
        self.assertEqual(e.submit(20, 1, ROOT, b"again"), (s + 1) & 0xFFFF)
        self.assertEqual(e.submit(200, 0, BROADCAST, b"all"), (s + 2) & 0xFFFF)
        self.assertEqual(len(e.queue), 4)

    def test_refusals_at_submission(self):
        e = _engine(A, hop=B)
        for port in (0, 1, 3, 9, 10, 15, 256, -1, "20"):
            self.assertEqual(e.submit(port, 0, ROOT, b"x"), BAD_PORT, port)
        for cls in (3, -1, None):
            self.assertEqual(e.submit(20, cls, ROOT, b"x"), BAD_CLASS, cls)
        self.assertEqual(e.submit(20, 2, ROOT, b"x"), UNSUPPORTED)
        for dest in (A, -1, 0x10000, None):
            self.assertEqual(e.submit(20, 0, dest, b"x"), BAD_DEST, dest)
        self.assertEqual(e.submit(20, 1, BROADCAST, b"x"), BROADCAST_CLASS)
        self.assertEqual(e.submit(20, 0, ROOT, bytes(201)), TOO_LONG)
        self.assertEqual(len(e.queue), 0)
        self.assertEqual(e.submit(20, 0, ROOT, bytes(200)), 0)
        self.assertEqual(len(e.queue), 1)

    def test_no_next_hop_refused(self):
        e = _engine(A)
        self.assertEqual(e.submit(20, 0, ROOT, b"x"), NO_ROUTE)
        self.assertTrue(e.submit(20, 0, BROADCAST, b"x") >= 0)  # no route needed

    def test_frame_limit_of_the_profile(self):
        # A profile capping the transmission time caps the payload below 200 B.
        capped = profiles.Profile(0x7F, "capped", 869525000, 125000, 5, None, None, 1000)
        capped.max_tx_ms = 100
        e = _engine(A, hop=B, profile=capped)
        self.assertTrue(e.limit < 100)
        room = e.limit - frame.OVERHEAD - 2
        self.assertEqual(e.submit(20, 0, ROOT, bytes(room + 1)), TOO_LONG)
        self.assertEqual(e.submit(20, 0, ROOT, bytes(room)), 0)

    def test_queue_full_and_own_quota(self):
        e = _engine(A, hop=B)
        e.set_queue(TxQueue(8, 2))
        results = [e.submit(20, 0, ROOT, b"m%d" % i) for i in range(6)]
        # Own traffic is one origin: refused once the queue is more than half
        # full and the origin holds its quota.
        self.assertEqual(results, [0, 0, 0, 0, 0, ORIGIN_QUOTA])
        e.set_queue(TxQueue(4))
        results = [e.submit(20, 0, ROOT, b"m%d" % i) for i in range(5)]
        self.assertEqual(results, [0, 0, 0, 0, QUEUE_FULL])
        self.assertEqual(e.counters.refused_full, 1)
        self.assertEqual(e.counters.refused_quota, 1)

    def test_exhausted_counter_fails_queued_messages(self):
        hops = {B: B, C: C}
        e = _engine(A, start=COUNTER_LIMIT - 1)
        e.next_hop = lambda msg: hops[msg.dest]
        failures = []
        e.on_failure(lambda dest, port, seq, reason: failures.append((dest, port, seq, reason)))
        s1 = e.submit(20, 1, B, b"first")
        s2 = e.submit(21, 1, C, b"second")
        self.assertEqual(e.submit(22, 0, C, b"datagram"), 0)
        frames, now = _drain(e, 10)
        self.assertEqual(len(frames), 1)  # the last counter
        self.assertEqual(frame.decode_header(frames[0], len(frames[0])).counter, 0xFFFFFE)
        self.assertEqual(sorted(failures), [(B, 20, s1, EXHAUSTED), (C, 21, s2, EXHAUSTED)])
        self.assertEqual(len(e.queue), 0)
        self.assertEqual(e.submit(20, 0, B, b"late"), EXHAUSTED)


class Dispatch(unittest.TestCase):
    def test_bind_checks_ports(self):
        e = _engine(A)
        for port in (0, 1, 9, 15, 256, -1):
            self.assertRaises(ValueError, e.bind, port, print)
        for port in (0, 11, 15, 16, 200):
            self.assertRaises(ValueError, e.bind_mesh, port, print)
        e.bind(16, print)
        e.bind(255, print)
        e.bind_mesh(ports.DODAG_INFO, print)
        e.bind_mesh(ports.ACK, print)

    def test_messages_dispatched_by_port(self):
        sender = _engine(A, hop=B)
        receiver = _engine(B, seed=2)
        on20 = _Inbox()
        on200 = _Inbox()
        receiver.bind(20, on20)
        receiver.bind(200, on200)
        sender.submit(20, 0, B, b"twenty")
        sender.submit(200, 0, B, b"two hundred")
        sender.submit(21, 0, B, b"unbound")
        frames, now = _drain(sender, 0)
        self.assertEqual(len(frames), 1)  # aggregated: one next hop
        _rx(receiver, now, frames[0])
        self.assertEqual(on20.messages, [(A, 20, b"twenty")])
        self.assertEqual(on200.messages, [(A, 200, b"two hundred")])
        c = receiver.counters
        self.assertEqual((c.delivered, c.rx_no_handler, c.rx_frames), (2, 1, 1))
        self.assertEqual((c.rx_messages[20], c.rx_messages[21], c.rx_messages[200]), (1, 1, 1))
        self.assertEqual(sender.counters.tx_messages[20], 1)

    def test_upward_forwarded_then_delivered_at_root(self):
        node = _engine(A, hop=B)
        relay = _engine(B, seed=2, hop=ROOT)
        root = _engine(ROOT, seed=3)
        inbox = _Inbox()
        relay.bind(20, inbox)
        root.bind(20, inbox)
        node.submit(20, 0, ROOT, b"up")
        frames, now = _drain(node, 0)
        actions = _rx(relay, now, frames[0])
        self.assertEqual(inbox.messages, [])
        self.assertEqual((relay.counters.forwarded, len(relay.queue)), (1, 1))
        # Forwarding starts after a random delay of at most one slot (§7).
        if not actions:
            self.assertTrue(now < relay.deadline() <= now + relay.slot_ms)
            now = relay.deadline()
        frames, now = _drain(relay, now, actions)
        header = frame.decode_header(frames[0], len(frames[0]))
        self.assertEqual((header.source, header.dest), (B, ROOT))
        _rx(root, now, frames[0])
        self.assertEqual(inbox.messages, [(A, 20, b"up")])  # origin restored from mode 6

    def test_downward_and_broadcast_origins(self):
        root = _engine(ROOT, hop=A)
        node = _engine(A, seed=2, hop=ROOT)
        inbox = _Inbox()
        node.bind(30, inbox)
        root.submit(30, 0, A, b"down")
        root.submit(30, 0, BROADCAST, b"everyone")
        frames, now = _drain(root, 0)
        for f in frames:
            _rx(node, now, f)
        self.assertEqual(sorted(inbox.messages), [(ROOT, 30, b"down"), (ROOT, 30, b"everyone")])

    def test_mesh_port_link_local(self):
        sender = _engine(A)
        receiver = _engine(B, seed=2)
        inbox = _Inbox()
        receiver.bind_mesh(ports.DODAG_INFO, inbox)
        msg = Message(frame.LINK_LOCAL, 0, ports.DODAG_INFO, b"")
        sender.send(msg, BROADCAST, trickle_reset=True)
        frames, now = _drain(sender, 0)
        _rx(receiver, now, frames[0])
        self.assertEqual(inbox.messages, [(A, ports.DODAG_INFO, b"")])

    def test_other_link_destination_overheard(self):
        sender = _engine(A, hop=C)
        bystander = _engine(B, seed=2, hop=ROOT)
        inbox = _Inbox()
        bystander.bind(20, inbox)
        overheard = []
        bystander.overheard = lambda h, msgs, now: overheard.append((h.dest, len(msgs)))
        sender.submit(20, 0, B, b"via C")  # P2P to B, but the next hop is C
        frames, now = _drain(sender, 0)
        _rx(bystander, now, frames[0])
        self.assertEqual(inbox.messages, [])
        self.assertEqual(overheard, [(C, 1)])
        self.assertEqual(bystander.counters.rx_overheard, 1)
        self.assertEqual(len(bystander.queue), 0)


class Reception(unittest.TestCase):
    def setUp(self):
        self.sender = _engine(A, hop=B)
        self.receiver = _engine(B, seed=2)
        self.inbox = _Inbox()
        self.receiver.bind(20, self.inbox)
        self.heard = []
        self.receiver.heard = lambda h, rssi, snr, now: self.heard.append((h.source, h.counter))

    def _frames(self, payloads, now=0):
        for p in payloads:
            self.sender.submit(20, 0, B, p)
            frames, now = _drain(self.sender, now)
        return frames

    def test_frame_is_sealed(self):
        self.sender.submit(20, 0, B, b"secret punch data")
        frames, _ = _drain(self.sender, 0)
        f = frames[0]
        self.assertFalse(b"secret" in f)
        h = frame.decode_header(f, len(f))
        self.assertEqual((h.source, h.dest, h.rank), (A, B, frame.RANK_INFINITE))
        self.assertEqual(h.counter, self.sender.security.counter.peek() - 1)

    def test_replay_counted_per_sender(self):
        f = self._frames([b"one"])[0]
        _rx(self.receiver, 100, f)
        _rx(self.receiver, 200, f)
        _rx(self.receiver, 300, f)
        self.assertEqual(self.inbox.messages, [(A, 20, b"one")])
        self.assertEqual(self.receiver.counters.rx_replay, 2)
        self.assertEqual(self.receiver.senders.get(A, REPLAYS), 2)
        self.assertEqual(self.heard, [(A, frame.decode_header(f, len(f)).counter)])

    def test_wrong_key_counted_per_sender(self):
        stranger = _engine(C, seed=3, key=_OTHER_KEY, hop=B)
        stranger.submit(20, 0, B, b"forged")
        frames, now = _drain(stranger, 0)
        _rx(self.receiver, now, frames[0])
        _rx(self.receiver, now + 1, frames[0])  # not a replay: never authenticated
        genuine = self._frames([b"real"], now + 2)[0]
        _rx(self.receiver, now + 1000, genuine)
        r = self.receiver
        self.assertEqual(self.inbox.messages, [(A, 20, b"real")])
        self.assertEqual((r.counters.rx_auth, r.counters.rx_replay), (2, 0))
        self.assertEqual(r.senders.get(C, AUTH), 2)
        self.assertEqual(r.senders.get(C, KNOWN), 0)
        self.assertEqual(r.senders.get(A, AUTH), 0)
        self.assertEqual([s for s, _ in self.heard], [A])

    def test_authentication_failure_of_a_known_sender(self):
        f = self._frames([b"one"])[0]
        _rx(self.receiver, 100, f)
        tampered = bytearray(f)
        tampered[-1] ^= 1
        _rx(self.receiver, 200, bytes(tampered))
        self.assertEqual(self.receiver.senders.get(A, AUTH), 1)
        self.assertEqual(self.receiver.senders.get(A, KNOWN), 1)

    def test_version_length_and_crc_errors_counted(self):
        f = bytearray(self._frames([b"one"])[0])
        f[0] &= 0x3F  # protocol version 0
        _rx(self.receiver, 100, bytes(f))
        _rx(self.receiver, 101, b"short")
        _rx(self.receiver, 102, bytes(256))
        self.receiver.step(103, (RX_ERROR, -120, -15))
        c = self.receiver.counters
        self.assertEqual((c.rx_version, c.rx_length, c.rx_crc, c.rx_frames), (1, 2, 1, 0))
        self.assertEqual(self.heard, [])

    def test_malformed_body_dropped_whole(self):
        # A sealed frame whose second message uses the reserved mode 7.
        buf = bytearray(frame.MAX_FRAME)
        first = Message(frame.P2P, 0, 20, b"ok", A, B)
        second = Message(frame.P2P, 0, 20, b"not bundled", A, B)
        n = frame.encode(buf, Header(B, A, 0, 255), [first, second])
        body = frame.HEADER_SIZE + 2 + 1 + 4 + 2  # control, port, length, addresses, payload
        buf[body] = 7 << 5 | 1
        self.sender.security.seal(buf, n)
        _rx(self.receiver, 100, bytes(buf[:n]))
        self.assertEqual(self.receiver.counters.rx_malformed, 1)
        self.assertEqual(self.inbox.messages, [])

    def test_duplicate_address_detected(self):
        twin = _engine(B, seed=7, hop=A)  # another board with the receiver's address
        twin.submit(20, 0, A, b"from the twin")
        frames, now = _drain(twin, 0)
        f = bytearray(frames[0])
        self.assertFalse(self.receiver.address_conflict)
        # The twin addressed A; the frame from the own address is caught at
        # step 4 whatever its link destination.
        _rx(self.receiver, now, bytes(f))
        r = self.receiver
        self.assertTrue(r.address_conflict)
        self.assertEqual(r.counters.duplicate_address, 1)
        self.assertEqual((self.heard, r.counters.rx_overheard), ([], 0))

    def test_sender_counters_bounded(self):
        s = SenderCounters(capacity=2)
        s.add(1, AUTH)
        s.add(2, REPLAYS)
        s.add(1, AUTH)
        s.add(3, AUTH)  # evicts 2, the least recently updated
        self.assertEqual(s.addresses(), [1, 3])
        self.assertEqual((s.get(1, AUTH), s.get(2, REPLAYS), s.evictions), (2, 0, 1))


class Access(unittest.TestCase):
    def test_carrier_sense_before_transmission(self):
        e = _engine(A, hop=B)
        e.submit(20, 0, ROOT, b"x")
        self.assertEqual(_actions(e, 5), [SENSE_ACTION])
        self.assertEqual(_actions(e, 5), [])  # waits for the answer
        actions = _actions(e, 6, (CHANNEL, False))
        self.assertEqual([a[0] for a in actions], [TX])
        self.assertEqual(e.dutycycle.used(6), e.toa_ms(len(actions[0][1])))

    def _busy(self, e, now):
        """Answer busy to a pending sense; return (backoff in slots, now)."""
        self.assertEqual(_actions(e, now, (CHANNEL, True)), [])
        wait = e.deadline() - now
        self.assertEqual(wait % e.slot_ms, 0)
        now = e.deadline()
        return wait // e.slot_ms, now

    def test_backoff_windows_double_up_to_32(self):
        drawn = [set() for _ in range(_ATTEMPTS - 1)]
        for seed in range(60):
            e = _engine(A, seed=seed, hop=B)
            e.submit(20, 1, ROOT, b"x")
            now = 0
            self.assertEqual(_actions(e, now), [SENSE_ACTION])
            for attempt in range(_ATTEMPTS - 1):
                k, now = self._busy(e, now)
                drawn[attempt].add(k)
                self.assertEqual(_actions(e, now), [SENSE_ACTION])
        cw = CW_MIN
        for attempt in range(_ATTEMPTS - 1):
            self.assertTrue(min(drawn[attempt]) >= 1)
            # Within [1, CW], and beyond the previous window: CW doubled.
            self.assertTrue(cw // 2 < max(drawn[attempt]) <= cw, (attempt, sorted(drawn[attempt])))
            cw = min(2 * cw, CW_MAX)
        self.assertEqual(e.slot_ms, e.toa_ms(17))  # presence frame at SF7

    def _give_up(self, e):
        """Answer busy to _ATTEMPTS senses; return (retry time, its wait in slots)."""
        now = 0
        self.assertEqual(_actions(e, now), [SENSE_ACTION])
        for attempt in range(_ATTEMPTS):
            self.assertEqual(e.counters.give_ups, 0)
            k, now = self._busy(e, now)
            if attempt < _ATTEMPTS - 1:
                self.assertEqual(_actions(e, now), [SENSE_ACTION])
        self.assertEqual(e.counters.give_ups, 1)
        return now, k

    def test_reports_dropped_after_six_busy_attempts(self):
        e = _engine(A, hop=B)
        e.submit(200, 0, ROOT, b"low priority report")  # priority 3
        presence = Message(frame.FIRST_HOP, 0, ports.PRESENCE, b"\x00\x01")
        e.send(presence, B)  # priority 2
        now, _ = self._give_up(e)
        self.assertEqual(len(e.queue), 0)
        self.assertEqual(e.counters.access_dropped, 2)
        self.assertEqual(_actions(e, now), [])  # nothing left to send
        self.assertEqual(e.counters.tx_frames, 0)

    def test_class1_and_priority1_kept_and_retried(self):
        e = _engine(A, hop=B)
        e.submit(200, 1, ROOT, b"class 1, priority 3")
        e.submit(20, 0, ROOT, b"class 0, priority 1")
        now, _ = self._give_up(e)
        self.assertEqual(len(e.queue), 2)
        self.assertEqual(e.counters.access_dropped, 0)
        # Retried later: a new procedure, with its own six attempts.
        self.assertEqual(_actions(e, now), [SENSE_ACTION])
        for _ in range(_ATTEMPTS - 1):
            _, now = self._busy(e, now)
            self.assertEqual(_actions(e, now), [SENSE_ACTION])
        self.assertEqual(e.counters.give_ups, 1)
        actions = _actions(e, now, (CHANNEL, False))
        self.assertEqual([a[0] for a in actions], [TX])
        self.assertEqual(e.counters.tx_messages[20] + e.counters.tx_messages[200], 2)

    def test_retry_after_give_up_within_32_slots(self):
        waits = set()
        for seed in range(40):
            e = _engine(A, seed=seed, hop=B)
            e.submit(20, 0, ROOT, b"x")
            waits.add(self._give_up(e)[1])
        self.assertTrue(min(waits) >= 1 and max(waits) <= CW_MAX, sorted(waits))
        self.assertTrue(max(waits) > CW_MAX // 2, sorted(waits))


class DutyCycleRules(unittest.TestCase):
    def test_charged_at_start_then_measured_excess(self):
        e = _engine(A, hop=B)
        e.submit(20, 0, ROOT, b"x" * 50)
        _actions(e, 0)
        tx = _actions(e, 1, (CHANNEL, False))[0][1]
        toa = e.toa_ms(len(tx))
        self.assertEqual(e.dutycycle.used(1), toa)
        e.step(1 + toa + 40, (TX_DONE, toa + 40))
        self.assertEqual(e.dutycycle.used(1 + toa + 40), toa + 40)

    def test_refused_reports_dropped_class1_kept(self):
        e = _engine(A, hop=B)
        presence = Message(frame.FIRST_HOP, 0, ports.PRESENCE, b"\x00\x01")
        e.dutycycle.start(0, 7000)  # above 60 % (priority 3), below 80 % (priority 2)
        e.submit(200, 0, ROOT, b"low report")  # priority 3, class 0: dropped
        e.submit(200, 1, ROOT, b"low class 1")  # priority 3, class 1: kept
        e.send(presence, B)  # priority 2: fits
        e.submit(20, 0, ROOT, b"high")  # priority 1: fits
        frames, now = _drain(e, 1)
        self.assertEqual(e.counters.duty_dropped, 1)
        sent = frame.decode_header(frames[0], len(frames[0]))
        self.assertEqual((len(frames), sent.dest), (1, B))
        self.assertEqual(e.counters.tx_messages[ports.PRESENCE], 1)
        self.assertEqual(e.counters.tx_messages[20], 1)
        self.assertEqual(e.counters.tx_messages[200], 0)
        self.assertEqual(len(e.queue), 1)  # the class 1 message waits for budget
        # Above 80 %: a presence report is dropped too.
        e.dutycycle.start(now, 1500)
        e.send(Message(frame.FIRST_HOP, 0, ports.PRESENCE, b"\x00\x02"), B)
        frames, now = _drain(e, now + 1)
        self.assertEqual((frames, e.counters.duty_dropped, len(e.queue)), ([], 2, 1))

    def test_waits_for_the_window_to_release_airtime(self):
        e = _engine(A, hop=B)
        e.dutycycle.start(0, _BUDGET)  # whole budget used at t = 0
        e.submit(20, 0, ROOT, b"high priority")  # priority 1: stays queued
        self.assertEqual(_actions(e, 10), [])
        self.assertEqual(len(e.queue), 1)
        steps = 0
        now = 10
        while True:
            now = e.deadline()
            self.assertTrue(now is not None)
            steps += 1
            actions = _actions(e, now)
            if actions:
                break
        self.assertEqual(actions, [SENSE_ACTION])
        # The charge counts for a whole window (60 bins of 1 s) plus a bin.
        self.assertTrue(_WINDOW <= now <= _WINDOW + 1000, now)
        self.assertTrue(steps <= 62, steps)
        self.assertTrue(e.counters.duty_waits >= 1)


class ForwardingDelay(unittest.TestCase):
    def test_relay_waits_up_to_one_slot(self):
        delays = set()
        for seed in range(40):
            node = _engine(A, seed=100 + seed, hop=B)
            relay = _engine(B, seed=seed, hop=ROOT)
            node.submit(20, 0, ROOT, b"up")
            frames, now = _drain(node, 0)
            actions = _rx(relay, now, frames[0])
            if actions:
                self.assertEqual(actions, [SENSE_ACTION])
                delays.add(0)
            else:
                delays.add(relay.deadline() - now)
                self.assertEqual(_actions(relay, relay.deadline()), [SENSE_ACTION])
        self.assertTrue(min(delays) >= 0 and max(delays) <= _engine(B).slot_ms, sorted(delays))
        self.assertTrue(len(delays) > 20, sorted(delays))


class EngineChecks(unittest.TestCase):
    def test_bad_address(self):
        p = FakePlatform(XorShift32(1))
        e = _engine(A)
        for address in (BROADCAST, -1, 0x10000):
            self.assertRaises(
                ValueError,
                LinkEngine,
                address,
                e.security,
                e.dutycycle,
                e.queue,
                p.random,
                7,
                _EU,
            )

    def test_unexpected_radio_events_ignored(self):
        e = _engine(A, hop=B)
        e.step(1, (CHANNEL, False))
        e.step(2, (TX_DONE, 30))
        self.assertEqual((e.counters.unexpected, e.counters.tx_frames), (2, 0))
        self.assertRaises(ValueError, e.step, 3, (99,))
