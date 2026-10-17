"""Transmit queue, fairness, aggregation (MESH_DESIGN §6, §7, §12)."""

import unittest

from mesh import frame, ports
from mesh.airtime import time_on_air_ms
from mesh.dutycycle import DutyCycle
from mesh.frame import FIRST_HOP, UPWARD, Header, Message
from mesh.txqueue import (
    ACCEPTED,
    DROPPED,
    EVICTED,
    FULL,
    LOCAL,
    OVER_QUOTA,
    REFUSED,
    REPLACED,
    UNACKED,
    TxQueue,
    accepted,
)
from sim.prng import XorShift32

_PARENT = 0x1B07
_OTHER = 0x2C11
_BUDGET = 324000  # EU profile
_P3 = 194400
_SF = 7
_APP_HIGH = 20  # application port at priority 1
_APP_LOW = 200  # application port at priority 3


def _toa(length):
    return time_on_air_ms(length, _SF)


def _up(origin, port=_APP_LOW, payload=b"x" * 40, cls=0, seq=None):
    """A forwarded upward message of origin."""
    if cls and seq is None:
        seq = 1
    return Message(UPWARD, cls, port, payload, origin, seq=seq)


def _own(port=_APP_LOW, payload=b"x" * 40, cls=0, seq=None):
    """The node's own message on its first hop."""
    if cls and seq is None:
        seq = 1
    return Message(FIRST_HOP, cls, port, payload, seq=seq)


def _send(q, dc=None, limit=frame.MAX_FRAME, now=0):
    """Pack and commit one frame; return its selection's messages ([] if none)."""
    sel = q.pack(now, dc or DutyCycle(_BUDGET), limit, _toa)
    if sel is None:
        return []
    messages = sel.messages
    q.commit(sel)
    return messages


def _key(m):
    return m.mode, m.cls, m.port, m.route is None, len(m.payload)


# Frame limit taking exactly one 40-byte upward message (57 B).
_ONE = frame.OVERHEAD + 2 + 2 + 40


class Admission(unittest.TestCase):
    def test_quota_applies_only_above_half_the_queue(self):
        q = TxQueue()
        for _ in range(16):
            self.assertEqual(q.offer(_up(0xA), _PARENT), ACCEPTED)
            self.assertEqual(q.offer(_up(0xB), _PARENT), ACCEPTED)
        # 32 messages: not above half, the quota does not apply yet.
        self.assertEqual(q.offer(_up(0xA), _PARENT), ACCEPTED)
        self.assertEqual(q.holding(0xA), 17)
        # 33 messages: an origin holding 16 or more is refused...
        self.assertEqual(q.offer(_up(0xA), _PARENT), DROPPED)
        self.assertEqual(q.reason, OVER_QUOTA)
        self.assertEqual(q.offer(_up(0xB), _PARENT), DROPPED)
        self.assertEqual(q.reason, OVER_QUOTA)
        # ...an origin below 16 is not, own traffic included.
        for _ in range(15):
            self.assertEqual(q.offer(_up(0xC), _PARENT), ACCEPTED)
            self.assertEqual(q.offer(_own(), _PARENT, local=True), ACCEPTED)
        self.assertEqual(q.offer(_up(0xC), _PARENT), ACCEPTED)
        self.assertEqual(q.offer(_own(), _PARENT, local=True), REFUSED)
        self.assertEqual(q.reason, FULL)
        self.assertEqual(len(q), 64)
        self.assertEqual(q.holding(LOCAL), 15)

    def test_quota_refusal_per_class(self):
        q = TxQueue()
        for _ in range(17):
            q.offer(_up(0xA), _PARENT)
        for _ in range(16):
            q.offer(_own(), _PARENT, local=True)
        self.assertEqual(len(q), 33)
        self.assertEqual(q.offer(_up(0xA, cls=1), _PARENT), UNACKED)
        self.assertEqual(q.offer(_up(0xA, cls=2), _PARENT), UNACKED)
        self.assertEqual(q.offer(_up(0xA, cls=0), _PARENT), DROPPED)
        self.assertEqual(q.offer(_own(cls=2), _PARENT, local=True), REFUSED)
        self.assertEqual(q.reason, OVER_QUOTA)
        self.assertEqual(q.refusals, [1, 1, 2])
        self.assertEqual(len(q), 33)

    def test_full_queue_refusals_per_class(self):
        q = TxQueue()
        for origin in range(1, 9):
            for _ in range(8):
                self.assertEqual(q.offer(_up(origin, cls=1), _PARENT), ACCEPTED)
        self.assertFalse(q.overflow)
        outcomes = (
            q.offer(_own(), _PARENT, local=True),
            q.offer(_own(cls=1), _PARENT, local=True),
            q.offer(_up(0x50, cls=0), _PARENT),
            q.offer(_up(0x50, cls=1), _PARENT),
            q.offer(_up(0x50, cls=2), _PARENT),
        )
        self.assertEqual(outcomes, (REFUSED, REFUSED, DROPPED, UNACKED, UNACKED))
        self.assertEqual(q.reason, FULL)
        self.assertEqual(q.refusals, [2, 2, 1])
        self.assertTrue(q.overflow)
        self.assertFalse(any(accepted(o) for o in outcomes))
        self.assertEqual(len(q), 64)

    def test_full_queue_drops_forwarded_class0_lowest_priority_oldest_first(self):
        q = TxQueue()
        old3 = _up(0xA, port=ports.TOPOLOGY)
        p2 = _up(0xB, port=ports.PRESENCE, payload=b"ppp")
        new3 = _up(0xC, port=ports.TOPOLOGY)
        own3 = _own(port=ports.TOPOLOGY)
        for m in (old3, p2, new3):
            q.offer(m, _PARENT)
        q.offer(own3, _PARENT, local=True)  # never evicted: local
        origin = 0x100
        while len(q) < 64:
            q.offer(_up(origin, cls=1), _PARENT)  # never evicted: class 1
            origin += 1
        arrival = _up(0xD, port=ports.PRESENCE, payload=b"ppp")
        self.assertEqual(q.offer(arrival, _PARENT), EVICTED)
        self.assertIs(q.victim, old3)
        self.assertEqual(q.victim_origin, 0xA)
        self.assertEqual(q.offer(_up(0xE, port=ports.PRESENCE, payload=b"ppp"), _PARENT), EVICTED)
        self.assertIs(q.victim, new3)
        # Same priority: the oldest presence goes.
        self.assertEqual(q.offer(_up(0xF, port=ports.PRESENCE, payload=b"ppp"), _PARENT), EVICTED)
        self.assertIs(q.victim, p2)
        # A less urgent newcomer never evicts a more urgent message.
        self.assertEqual(q.offer(_up(0x10, port=ports.TOPOLOGY), _PARENT), DROPPED)
        self.assertIsNone(q.victim)
        # Local submissions and class 1 never evict.
        own2 = _own(port=ports.PRESENCE, payload=b"p")
        self.assertEqual(q.offer(own2, _PARENT, local=True), REFUSED)
        self.assertEqual(q.offer(_up(0x11, port=_APP_HIGH, cls=1), _PARENT), UNACKED)
        self.assertEqual(q.evictions, 3)
        self.assertEqual(q.refusals, [2, 1, 0])
        self.assertEqual(q.holding(LOCAL), 1)
        self.assertEqual(len(q), 64)

    def test_replace_by_newer_keeps_the_place(self):
        q = TxQueue()
        older = _own(port=ports.PRESENCE, payload=b"old")
        q.offer(older, _PARENT, local=True, replace=True)
        q.offer(_up(0xA, port=ports.PRESENCE, payload=b"aaa"), _PARENT, replace=True)
        newer = _own(port=ports.PRESENCE, payload=b"new")
        self.assertEqual(q.offer(newer, _PARENT, local=True, replace=True), REPLACED)
        self.assertEqual(len(q), 2)
        self.assertEqual(q.holding(LOCAL), 1)
        # The newer report took the older's place: sent first, alone.
        sent = _send(q, limit=frame.OVERHEAD + 2 + 3)
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0], newer)

    def test_replace_needs_same_origin_port_and_class_0(self):
        q = TxQueue()
        q.offer(_up(0xA, port=ports.PRESENCE, payload=b"aaa"), _PARENT, replace=True)
        cases = (
            _up(0xB, port=ports.PRESENCE, payload=b"bbb"),  # other origin
            _up(0xA, port=ports.TOPOLOGY),  # other port
        )
        for m in cases:
            self.assertEqual(q.offer(m, _PARENT, replace=True), ACCEPTED)
        # Not asked for this port.
        self.assertEqual(q.offer(_up(0xA, port=ports.PRESENCE, payload=b"a2"), _PARENT), ACCEPTED)
        q.offer(_up(0xA, port=_APP_HIGH, cls=1), _PARENT)
        self.assertEqual(q.offer(_up(0xA, port=_APP_HIGH, cls=1), _PARENT, replace=True), ACCEPTED)
        # Own messages and forwarded ones are different origins.
        self.assertEqual(
            q.offer(_own(port=ports.PRESENCE, payload=b"o"), _PARENT, local=True, replace=True),
            ACCEPTED,
        )
        self.assertEqual(len(q), 7)

    def test_replace_allowed_over_quota(self):
        q = TxQueue()
        q.offer(_up(0xA, port=ports.PRESENCE, payload=b"aaa"), _PARENT, replace=True)
        for _ in range(32):
            q.offer(_up(0xA), _PARENT)
        self.assertEqual(q.offer(_up(0xA), _PARENT), DROPPED)
        newer = _up(0xA, port=ports.PRESENCE, payload=b"new")
        self.assertEqual(q.offer(newer, _PARENT, replace=True), REPLACED)
        self.assertEqual(q.holding(0xA), 33)

    def test_invalid_messages_raise(self):
        q = TxQueue()
        self.assertRaises(ValueError, q.offer, _up(0xA, port=0), _PARENT)
        self.assertRaises(ValueError, q.offer, _up(0xA, payload=bytes(239)), _PARENT)
        self.assertRaises(ValueError, q.offer, Message(UPWARD, 1, _APP_LOW, b"", 0xA), _PARENT)
        self.assertEqual(len(q), 0)
        # The largest message a frame carries is accepted.
        self.assertEqual(q.offer(_up(0xA, payload=bytes(238)), _PARENT), ACCEPTED)


class Order(unittest.TestCase):
    def test_strict_priority(self):
        q = TxQueue()
        q.offer(_up(0xA, port=ports.TOPOLOGY), _PARENT)
        q.offer(_up(0xB, port=ports.PRESENCE, payload=b"x" * 40), _PARENT)
        q.offer(_up(0xC, port=_APP_HIGH), _PARENT)
        ports_sent = [_send(q, limit=_ONE)[0].port for _ in range(3)]
        self.assertEqual(ports_sent, [_APP_HIGH, ports.PRESENCE, ports.TOPOLOGY])

    def test_sustained_priority_1_load_starves_priority_3(self):
        q = TxQueue()
        q.offer(_up(0xA, port=ports.TOPOLOGY), _PARENT)
        for i in range(20):
            q.offer(_up(0x100 + i, port=_APP_HIGH), _PARENT)
            self.assertEqual(_send(q, limit=_ONE)[0].port, _APP_HIGH)
        self.assertEqual(_send(q, limit=_ONE)[0].port, ports.TOPOLOGY)

    def test_round_robin_by_origin_own_traffic_one_origin(self):
        q = TxQueue()
        for i in range(5):
            q.offer(_up(0xA, payload=bytes((i,)) * 40), _PARENT)
            q.offer(_up(0xB, payload=bytes((i,)) * 40), _PARENT)
        # Own messages on several priority-3 ports count as one origin.
        own_ports = (ports.TOPOLOGY, ports.NODE_INFO, 130, 131, 132)
        for i, port in enumerate(own_ports):
            q.offer(_own(port=port, payload=bytes((i,)) * 42), _PARENT, local=True)
        served = []
        for _ in range(15):
            m = _send(q, limit=_ONE)[0]
            served.append((LOCAL if m.mode == FIRST_HOP else m.origin, m.payload[0]))
        for k in range(0, 15, 3):
            self.assertEqual(sorted(o for o, _ in served[k : k + 3]), sorted((LOCAL, 0xA, 0xB)))
        # Age within an origin.
        for origin in (LOCAL, 0xA, 0xB):
            self.assertEqual([i for o, i in served if o == origin], [0, 1, 2, 3, 4])

    def test_round_robin_within_a_frame(self):
        q = TxQueue()
        for origin in (0xA, 0xB, 0xC):
            for _ in range(6):
                q.offer(_up(origin, payload=b"x" * 20), _PARENT)
        # 13 + 4 + n * 22 <= 255: 10 messages per frame.
        first = _send(q)
        self.assertEqual(len(first), 10)
        counts = [sum(1 for m in first if m.origin == o) for o in (0xA, 0xB, 0xC)]
        self.assertEqual(sorted(counts), [3, 3, 4])
        # The next frame starts after the origin served last: shares even out.
        second = _send(q)
        totals = [sum(1 for m in first + second if m.origin == o) for o in (0xA, 0xB, 0xC)]
        self.assertEqual(totals, [6, 6, 6])


class Packing(unittest.TestCase):
    def test_relay_bundles_thirty_presence_reports(self):
        # §11 sizes: 13 + 4 + 30 x 6 = 197 B.
        q = TxQueue()
        report = b"\x07\x1b\x00\x05"
        reports = [_up(0x100 + i, port=ports.PRESENCE, payload=report) for i in range(30)]
        for m in reports:
            q.offer(m, _PARENT, replace=True)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        self.assertEqual((sel.count, sel.size, sel.dest), (30, 197, _PARENT))
        self.assertEqual(sel.toa_ms, _toa(197))
        buf = bytearray(frame.MAX_FRAME)
        self.assertEqual(frame.encode(buf, Header(_PARENT, 0x3F2A, 1, 8), sel.messages), 197)
        _, decoded = frame.decode(buf, 197)
        self.assertEqual(sorted(m.origin for m in decoded), [0x100 + i for i in range(30)])

    def test_one_next_hop_per_frame_head_first(self):
        q = TxQueue()
        q.offer(_up(0xA, port=ports.PRESENCE, payload=b"ppp"), _OTHER)
        q.offer(_up(0xB, port=ports.TOPOLOGY), _PARENT)
        q.offer(_up(0xC, port=ports.PRESENCE, payload=b"ppp"), _PARENT)
        q.offer(_up(0xD, port=ports.TOPOLOGY), _OTHER)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        # Head: the first presence (priority 2) is bound to _OTHER.
        self.assertEqual(sel.dest, _OTHER)
        self.assertEqual(sorted(m.origin for m in sel.messages), [0xA, 0xD])
        q.commit(sel)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        self.assertEqual(sel.dest, _PARENT)
        self.assertEqual(sorted(m.origin for m in sel.messages), [0xB, 0xC])

    def test_mixed_frame_within_each_allowance(self):
        dc = DutyCycle(_BUDGET)
        dc.start(0, _P3 - 50)  # priority 3 has 50 ms left: no frame fits
        dc.done(0, 0)
        q = TxQueue()
        q.offer(_up(0xA, port=_APP_HIGH), _PARENT)
        q.offer(_up(0xB, port=ports.TOPOLOGY), _PARENT)
        q.offer(_up(0xC, port=ports.PRESENCE, payload=b"x" * 40), _PARENT)
        sel = q.pack(0, dc, frame.MAX_FRAME, _toa)
        self.assertEqual(sorted(m.origin for m in sel.messages), [0xA, 0xC])
        self.assertEqual(sel.priority, 2)
        q.commit(sel)
        # The priority-3 message stays queued, refused by its own allowance.
        self.assertIsNone(q.pack(0, dc, frame.MAX_FRAME, _toa))
        self.assertEqual(len(q), 1)
        # Once the window frees up, it goes.
        self.assertEqual(_send(q, dc, now=3700000)[0].origin, 0xB)

    def test_priority_3_joins_while_its_allowance_holds(self):
        q = TxQueue()
        q.offer(_up(0xA, port=_APP_HIGH), _PARENT)
        q.offer(_up(0xB, port=ports.TOPOLOGY), _PARENT)
        # Room for a 57-byte frame (108 ms) at priority 3, not a 101-byte one.
        dc = DutyCycle(_BUDGET)
        dc.start(0, _P3 - _toa(_ONE))
        dc.done(0, 0)
        sel = q.pack(0, dc, frame.MAX_FRAME, _toa)
        self.assertEqual([m.origin for m in sel.messages], [0xA])
        # The same message alone (another frame) fits the allowance.
        q.commit(sel)
        self.assertEqual(_send(q, dc)[0].origin, 0xB)

    def test_frame_limit(self):
        q = TxQueue()
        for i in range(8):
            q.offer(_up(0x10 + i), _PARENT)
        limit = frame.OVERHEAD + 4 + 3 * 42
        sel = q.pack(0, DutyCycle(_BUDGET), limit, _toa)
        self.assertEqual((sel.count, sel.size), (3, limit))
        sel = q.pack(0, DutyCycle(_BUDGET), limit - 1, _toa)
        self.assertEqual(sel.count, 2)

    def test_nothing_to_send(self):
        q = TxQueue()
        self.assertIsNone(q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa))
        q.offer(_up(0xA, payload=b"x" * 100), _PARENT)
        self.assertIsNone(q.pack(0, DutyCycle(_BUDGET), 100, _toa))

    def test_pack_changes_nothing_until_commit(self):
        q = TxQueue()
        for i in range(4):
            q.offer(_up(0x10 + i), _PARENT)
        first = list(q.pack(0, DutyCycle(_BUDGET), _ONE, _toa).messages)
        again = q.pack(0, DutyCycle(_BUDGET), _ONE, _toa)
        self.assertEqual(len(q), 4)
        self.assertIs(again.messages[0], first[0])
        q.commit(again)
        self.assertEqual(len(q), 3)
        self.assertIsNot(_send(q, limit=_ONE)[0], first[0])

    def test_random_queues_pack_into_valid_frames(self):
        rng = XorShift32(5)
        kinds = (
            (ports.PRESENCE, 4),
            (ports.TOPOLOGY, 25),
            (_APP_HIGH, 10),
            (_APP_LOW, 10),
            (ports.NODE_INFO, 15),
        )
        buf = bytearray(frame.MAX_FRAME)
        for _ in range(40):
            q = TxQueue()
            for _ in range(rng.next32() % 64 + 1):
                port, length = kinds[rng.next32() % len(kinds)]
                cls = rng.next32() % 3 if port >= 16 else 0
                hop = (_PARENT, _OTHER)[rng.next32() % 2]
                origin = rng.next32() % 6
                seq = rng.next32() & 0xFFFF if cls else None
                if origin:
                    q.offer(_up(origin, port, bytes(length), cls, seq), hop)
                else:
                    q.offer(_own(port, bytes(length), cls, seq), hop, local=True)
            limit = 60 + rng.next32() % 196
            sel = q.pack(0, DutyCycle(_BUDGET), limit, _toa)
            if sel is None:
                continue
            self.assertLessEqual(sel.size, limit)
            self.assertEqual(frame.frame_size(sel.messages), sel.size)
            self.assertEqual(frame.encode(buf, Header(sel.dest, 1, 1, 8), sel.messages), sel.size)
            # Compatible messages are consecutive (one header per group).
            seen = []
            previous = None
            for m in sel.messages:
                k = _key(m)
                if k != previous:
                    self.assertFalse(k in seen)
                    seen.append(k)
                    previous = k


class Commit(unittest.TestCase):
    def test_reliable_messages_wait_for_acknowledgement(self):
        q = TxQueue()
        datagram = _up(0xA, port=_APP_HIGH)
        reliable = _up(0xB, port=_APP_HIGH, cls=1, seq=7)
        q.offer(datagram, _PARENT)
        q.offer(reliable, _PARENT)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        slot = sel.slots[sel.messages.index(reliable)]
        q.commit(sel)
        self.assertEqual(len(q), 1)
        self.assertTrue(q.waiting(slot, reliable))
        self.assertEqual(q.attempts(slot, reliable), 1)
        self.assertEqual(_send(q), [])  # not sent again while waiting
        self.assertTrue(q.retry(slot, reliable))
        self.assertEqual(_send(q), [reliable])
        self.assertEqual(q.attempts(slot, reliable), 2)
        # Another next hop restarts the attempts.
        q.retry(slot, reliable, hop=_OTHER)
        self.assertEqual(q.attempts(slot, reliable), 0)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        self.assertEqual(sel.dest, _OTHER)
        q.commit(sel)
        self.assertTrue(q.remove(slot, reliable))
        self.assertEqual(len(q), 0)
        # Stale handles are ignored.
        self.assertFalse(q.remove(slot, reliable))
        self.assertFalse(q.retry(slot, reliable))
        q.offer(datagram, _PARENT)
        self.assertFalse(q.remove(slot, reliable))
        self.assertEqual(len(q), 1)

    def test_waiting_messages_hold_their_entry(self):
        q = TxQueue(size=4, quota=2)
        for i in range(4):
            q.offer(_up(0x10 + i, cls=1), _PARENT)
        _send(q)
        self.assertEqual(len(q), 4)
        self.assertEqual(q.offer(_up(0x20, cls=1), _PARENT), UNACKED)

    def test_commit_leaves_a_replaced_entry(self):
        q = TxQueue()
        q.offer(_own(port=ports.PRESENCE, payload=b"old"), _PARENT, local=True, replace=True)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        newer = _own(port=ports.PRESENCE, payload=b"new")
        self.assertEqual(q.offer(newer, _PARENT, local=True, replace=True), REPLACED)
        q.commit(sel)
        self.assertEqual(_send(q), [newer])


class Dropping(unittest.TestCase):
    def test_ready_excludes_waiting_messages(self):
        q = TxQueue()
        q.offer(_own(cls=1, seq=1), _PARENT, local=True)
        q.offer(_own(port=_APP_HIGH), _OTHER, local=True)
        self.assertEqual(q.ready(), 2)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        self.assertEqual(sel.dest, _OTHER)  # priority 1 first
        q.commit(sel)
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        q.commit(sel)  # the class 1 message now waits
        self.assertEqual((len(q), q.ready()), (1, 0))

    def test_drop_tests_every_entry_with_priority_and_origin(self):
        q = TxQueue()
        q.offer(_own(port=ports.PRESENCE, payload=b"\x00"), _PARENT, local=True)
        q.offer(_up(_OTHER, port=_APP_LOW), _PARENT)
        q.offer(_own(port=_APP_HIGH, cls=1, seq=3), _PARENT, local=True)
        q.commit(q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa))  # all in one frame
        self.assertEqual(len(q), 1)  # the class 1 message waits
        q.offer(_own(port=ports.PRESENCE, payload=b"\x01"), _PARENT, local=True)
        q.offer(_up(_OTHER, port=_APP_LOW), _PARENT)
        seen = []

        def test(msg, priority, local):
            seen.append((msg.port, priority, local))
            return msg.cls == 1 or not local

        self.assertEqual(q.drop(test), 2)
        self.assertEqual(sorted(seen), [(1, 2, True), (_APP_HIGH, 1, True), (_APP_LOW, 3, False)])
        self.assertEqual([m.port for m in _send(q)], [ports.PRESENCE])
        self.assertEqual(len(q), 0)

    def test_drop_within_a_selection(self):
        q = TxQueue()
        q.offer(_own(port=_APP_LOW), _PARENT, local=True)
        q.offer(_up(_OTHER, port=_APP_LOW), _OTHER)  # another next hop: not selected
        sel = q.pack(0, DutyCycle(_BUDGET), frame.MAX_FRAME, _toa)
        self.assertEqual(sel.count, 1)
        self.assertEqual(q.drop(lambda m, p, local: True, sel), 1)
        self.assertEqual(len(q), 1)
        self.assertEqual(q.holding(_OTHER), 1)


def _flow(q, origin, tick, pending):
    m = _up(origin, payload=bytes((origin, tick & 0xFF)) * 20)
    outcome = q.offer(m, _PARENT)
    if origin != _FLOOD:
        pending.append((m, tick))
    return outcome


_FLOOD = 0xF0


class Fairness(unittest.TestCase):
    def test_flooding_origin_gets_only_its_share(self):
        # A relay with a flooding child and three modest ones (plus its own
        # traffic), all at priority 3, one frame per tick of at most 5 messages
        # (13 + 4 + 5 x 42 = 227 B).
        q = TxQueue()
        dc = DutyCycle(_BUDGET * 100)  # duty cycle not binding here
        pending = []  # modest messages not yet sent, with their tick
        delays = []
        flood_sent = 0
        flood_refused = 0
        others_offered = 0
        ticks = 200
        for tick in range(ticks):
            for _ in range(10):
                if not accepted(_flow(q, _FLOOD, tick, pending)):
                    flood_refused += 1
                self.assertLessEqual(q.holding(_FLOOD), 33)
            for origin, every in ((0xA, 1), (0xB, 2), (0xC, 3)):
                if tick % every == 0:
                    self.assertEqual(_flow(q, origin, tick, pending), ACCEPTED)
                    others_offered += 1
            if tick % 4 == 0:
                m = _own(payload=bytes((0, tick & 0xFF)) * 21)
                self.assertEqual(q.offer(m, _PARENT, local=True), ACCEPTED)
                pending.append((m, tick))
                others_offered += 1
            sent = _send(q, dc, now=tick * 1000)
            self.assertLessEqual(len(sent), 5)
            for m in sent:
                if m.mode == UPWARD and m.origin == _FLOOD:
                    flood_sent += 1
                    continue
                for j in range(len(pending)):
                    if pending[j][0] is m:
                        delays.append(tick - pending[j][1])
                        pending.pop(j)
                        break
        # Every modest message admitted and sent within one tick.
        self.assertEqual(len(delays), others_offered)
        self.assertLessEqual(max(delays), 1)
        self.assertEqual(pending, [])
        # The flood takes the rest of the capacity (5 - 1 - 1/2 - 1/3 - 1/4
        # messages per frame, own traffic sharing frames less tightly), no more.
        self.assertTrue(flood_refused > 0)
        self.assertGreaterEqual(flood_sent, 2 * ticks)
        self.assertLessEqual(flood_sent + others_offered, 5 * ticks)

    def test_flooding_origins_share_equally(self):
        q = TxQueue()
        dc = DutyCycle(_BUDGET * 100)
        origins = (0xA, 0xB, 0xC, 0xD)
        sent = {o: 0 for o in origins}
        for tick in range(100):
            for o in origins:
                for _ in range(10):
                    q.offer(_up(o), _PARENT)
            for m in _send(q, dc, now=tick * 1000):
                sent[m.origin] += 1
        self.assertEqual(sum(sent.values()), 500)
        self.assertLessEqual(max(sent.values()) - min(sent.values()), 1)
