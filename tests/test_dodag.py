"""DODAG version, DODAG information and repair timers, node side (MESH_DESIGN §9; §11 port 9).

One board runs a real link engine with its `Dodag`, stepped on an idle
channel; neighbours are peers sealing frames under the network key, so that
version, rank and port 9 contents are chosen frame by frame.
"""

import unittest

from mesh import frame
from mesh import profile as profiles
from mesh.counter import FrameCounter
from mesh.dodag import (
    DESYNC_SLOTS,
    INFO_SEQUENCE,
    INFO_SIZE,
    TOPOLOGY_S,
    WINDOW_SLOTS,
    Dodag,
    DodagInfo,
    compare_versions,
    decode_info,
    default_info,
    encode_info,
    frame_sequence,
)
from mesh.frame import (
    BROADCAST,
    FIRST_HOP,
    LINK_LOCAL,
    RANK_INFINITE,
    ROOT,
    UPWARD,
    Header,
    Message,
)
from mesh.link import open_link
from mesh.parents import MAX_RANK_INCREASE
from mesh.ports import DODAG_INFO, PRESENCE
from mesh.radio import CHANNEL, RX, SENSE, TX, TX_DONE
from mesh.replay import ReplayTable
from mesh.security import KeyRing, Security
from mesh.trickle import IMIN_SLOTS
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))
_EU = profiles.get(profiles.EU_869)
EU = profiles.EU_869
# Presence period 10 s (Imax), topology 60 s, Imin 32 slots, MaxRankIncrease 56.
INFO = DodagInfo(0, 10, 60, 32, 56, EU)
PERIOD = 10000

N = 0x00A1  # the board under test
P = 0x00B1
Q = 0x00C1
S = 0x00D1


def _security(seed):
    platform = FakePlatform(XorShift32(seed))
    return Security(KeyRing(_KEY), FrameCounter(platform.storage, platform.random), ReplayTable())


class Peer:
    """A neighbour sealing frames of chosen version and rank."""

    def __init__(self, address, seed):
        self.address = address
        self.security = _security(seed)

    def frame(self, rank, version, messages=None, dest=BROADCAST):
        if messages is None:
            messages = [Message(LINK_LOCAL, 0, 20, b"data")]
        buf = bytearray(frame.MAX_FRAME)
        n = frame.encode(buf, Header(dest, self.address, 0, rank, version), messages)
        self.security.seal(buf, n)
        return bytes(buf[:n])

    def info(self, rank, version, info=INFO, seq=0):
        payload = encode_info(info, None, seq)
        return self.frame(rank, version, [Message(LINK_LOCAL, 0, DODAG_INFO, payload)])

    def presence(self, rank, version, seq, parent=ROOT):
        """Its own presence (mode 6, root sequence seq) to parent: overheard by the bench."""
        msg = Message(FIRST_HOP, 0, PRESENCE, bytes([seq, 0, 0x5A]))
        return self.frame(rank, version, [msg], parent)

    def solicitation(self, version=0):
        return self.frame(RANK_INFINITE, version, [Message(LINK_LOCAL, 0, DODAG_INFO, b"")])


class Bench:
    """One board with a `Dodag`, stepped at its deadlines on an idle channel.

    ``frames`` lists (time, header, messages) of the frames it sent.
    """

    def __init__(self, address=N, seed=1, info=None, version=0, sequence=None):
        platform = FakePlatform(XorShift32(seed))
        engine = open_link(
            address,
            _KEY,
            platform.storage,
            platform.retained,
            platform.random,
            _EU,
            7,
        )
        self.dodag = Dodag(engine, EU, info, version, sequence=sequence)
        engine.next_hop = self.dodag.next_hop
        self.engine = engine
        self.imin = self.dodag.imin_ms
        self.now = 0
        self.frames = []
        self._opener = _security(99)
        self._do(engine.step(0, None))

    def _do(self, actions):
        actions = list(actions)
        while actions:
            action = actions[0]
            if action[0] == SENSE:
                actions = list(self.engine.step(self.now, (CHANNEL, False)))
                continue
            f = action[1]
            buf = bytearray(f)
            h = Header(0, 0, 0, 0)
            if self._opener.receive(buf, len(buf), h) is not None:
                raise AssertionError("frame does not open")
            msgs = frame.decode_messages(buf, frame.HEADER_SIZE, len(buf) - frame.TAG_SIZE)
            self.frames.append((self.now, h, [(m.port, bytes(m.payload)) for m in msgs]))
            toa = self.engine.toa_ms(len(f))
            self.now += toa
            actions = list(self.engine.step(self.now, (TX_DONE, toa)))

    def advance(self, until):
        """Step at every deadline up to until."""
        for _ in range(100000):
            d = self.engine.deadline()
            if d is None or d > until:
                break
            if d > self.now:
                self.now = d
            self._do(self.engine.step(self.now, None))
        else:
            raise AssertionError("no progress")
        if until > self.now:
            self.now = until

    def rx(self, f):
        self._do(self.engine.step(self.now, (RX, f, -80, 10)))

    def wake(self):
        """Step now: after a call made outside a step (runner contract)."""
        self._do(self.engine.step(self.now, None))

    def port9(self, start=0, end=None):
        """Return [(time, header, payload)] of the port 9 messages sent in [start, end)."""
        return [
            (t, h, payload)
            for t, h, msgs in self.frames
            for port, payload in msgs
            if port == DODAG_INFO and t >= start and (end is None or t < end)
        ]

    def adverts(self, start=0, end=None):
        return [(t, h) for t, h, p in self.port9(start, end) if len(p) == INFO_SIZE]

    def solicitations(self, start=0, end=None):
        return [(t, h) for t, h, p in self.port9(start, end) if not p]

    def steady(self, keep=()):
        """Advance until the Trickle timer is at Imax, keeping peers heard (every 4 s)."""
        for _ in range(100):
            if self.dodag.trickle.at_imax():
                return
            self.advance(self.now + 4000)
            for peer, rank, version in keep:
                self.rx(peer.frame(rank, version))
        raise AssertionError("Imax not reached")


class _Case(unittest.TestCase):
    def setUp(self):
        self.p = Peer(P, 11)
        self.q = Peer(Q, 12)
        self.s = Peer(S, 13)

    def attached(self, version=3, info=INFO, seed=1):
        """Return a bench attached through P (rank 16): rank 24, at version."""
        b = Bench(seed=seed)
        b.advance(100)
        b.rx(self.p.info(16, version, info))
        self.assertEqual(b.dodag.parents.rank, 24)
        return b

    def assertAdvertWithin(self, b, start, version=None, rank=None):
        """One advertisement in [start + Imin/2, start + Imin + one frame], none before."""
        b.advance(start + b.imin + 200)
        sent = b.adverts(start)
        self.assertTrue(len(sent) >= 1, "no advertisement")
        t, h = sent[0]
        self.assertTrue(start + b.imin // 2 <= t <= start + b.imin + 200, t - start)
        if version is not None:
            self.assertEqual(h.dodag_version, version)
        if rank is not None:
            self.assertEqual(h.rank, rank)
        return t, h

    def assertQuiet(self, b, start, end):
        b.advance(end)
        self.assertEqual(b.port9(start, end), [])


class Codec(unittest.TestCase):
    def test_reference_payload(self):
        info = DodagInfo(1, 30, 0x1B4, 32, 56, 0xFF)
        payload = bytes(encode_info(info, None, 0xC3))
        self.assertEqual(payload, bytes([1, 30, 0, 0xB4, 1, 32, 56, 0xFF, 0xC3]))
        self.assertEqual(decode_info(payload), info)
        self.assertEqual(payload[INFO_SEQUENCE], 0xC3)

    def test_round_trip_extremes(self):
        info = DodagInfo(0xFF, 0xFFFF, 0xFFFF, 0xFF, 0xFF, 0)
        self.assertEqual(decode_info(encode_info(info)), info)

    def test_empty_is_a_solicitation(self):
        self.assertIsNone(decode_info(b""))

    def test_bad_payloads(self):
        good = bytes(encode_info(INFO))
        for bad in (good[:8], good + b"\x00", b"\x00"):
            self.assertRaises(ValueError, decode_info, bad)
        no_presence = bytearray(good)
        no_presence[1] = no_presence[2] = 0
        self.assertRaises(ValueError, decode_info, no_presence)
        no_imin = bytearray(good)
        no_imin[5] = 0
        self.assertRaises(ValueError, decode_info, no_imin)

    def test_encode_range(self):
        for bad in (
            DodagInfo(256, 30, 180, 32, 56, 1),
            DodagInfo(0, 0x10000, 180, 32, 56, 1),
            DodagInfo(0, 30, -1, 32, 56, 1),
            DodagInfo(0, 30, 180, 32, 56, 256),
        ):
            self.assertRaises(ValueError, encode_info, bad)
        for seq in (-1, 256):
            self.assertRaises(ValueError, encode_info, INFO, None, seq)

    def test_advertisement_frame_is_24_bytes(self):
        msg = Message(LINK_LOCAL, 0, DODAG_INFO, bytes(INFO_SIZE))
        self.assertEqual(frame.frame_size([msg]), 24)  # §11: 8 B of parameters + sequence

    def test_frame_sequence(self):
        # Carried by the DODAG information and the sender's own presence
        # (mode 6, first byte); not by a solicitation, a forwarded presence
        # (mode 1) or other messages.
        info = Message(LINK_LOCAL, 0, DODAG_INFO, encode_info(INFO, None, 77))
        own = Message(FIRST_HOP, 0, PRESENCE, bytes([78, 0, 0x5A]))
        data = Message(LINK_LOCAL, 0, 20, b"data")
        self.assertEqual(frame_sequence([data, info]), 77)
        self.assertEqual(frame_sequence([own]), 78)
        forwarded = Message(UPWARD, 0, PRESENCE, bytes([0x10, 0, 0, 0x5A]), origin=0x20)
        solicitation = Message(LINK_LOCAL, 0, DODAG_INFO, b"")
        self.assertIsNone(frame_sequence([forwarded, solicitation, data]))
        self.assertIsNone(frame_sequence([Message(FIRST_HOP, 0, PRESENCE, b"\x01")]))

    def test_default_parameters_follow_the_sf(self):
        info = default_info(9, EU)
        self.assertEqual((info.presence_s, info.topology_s), (60, TOPOLOGY_S[2]))
        self.assertEqual((info.imin_slots, info.max_increase, info.profile), (32, 56, EU))
        self.assertEqual((IMIN_SLOTS, MAX_RANK_INCREASE), (32, 56))


class Versions(unittest.TestCase):
    def test_serial_comparison(self):
        cases = (
            (1, 0, 1),
            (0, 1, -1),
            (5, 5, 0),
            (7, 0, 1),
            (9, 0, -1),
            (12, 5, 1),
        )
        for a, b, expected in cases:
            self.assertEqual(compare_versions(a, b), expected, (a, b))

    def test_wrap_around(self):
        self.assertEqual(compare_versions(0, 15), 1)
        self.assertEqual(compare_versions(15, 0), -1)
        self.assertEqual(compare_versions(2, 13), 1)  # 5 ahead through 0
        self.assertEqual(compare_versions(13, 2), -1)
        self.assertEqual(compare_versions(4, 13), 1)  # 7 ahead

    def test_eight_apart_not_comparable(self):
        self.assertIsNone(compare_versions(8, 0))
        self.assertIsNone(compare_versions(3, 11))
        self.assertIsNone(compare_versions(11, 3))


class Boot(_Case):
    def test_solicits_at_boot_then_once_per_presence_period(self):
        b = Bench()
        b.advance(65000)
        times = [t for t, _ in b.solicitations()]
        self.assertEqual(len(times), 3)
        self.assertTrue(0 <= times[0] <= b.imin, times)
        default = b.engine.neighbours.period_ms  # SF7 default until adoption
        self.assertEqual(default, 30000)
        for t0, t1 in zip(times, times[1:]):
            self.assertTrue(default <= t1 - t0 <= default + 200, times)
        h = b.solicitations()[0][1]
        self.assertEqual((h.dest, h.rank), (BROADCAST, RANK_INFINITE))
        self.assertEqual(b.adverts(), [])
        self.assertFalse(b.dodag.trickle.running)

    def test_boot_delay_is_random(self):
        firsts = set()
        for seed in range(1, 6):
            b = Bench(seed=seed)
            b.advance(b.imin + 100)
            firsts.add(b.solicitations()[0][0] // 100)
        self.assertTrue(len(firsts) >= 3, firsts)

    def test_adopts_the_first_version_and_its_parameters(self):
        b = Bench()
        b.advance(b.imin + 100)
        sol = b.solicitations()[0][0]  # at boot: it opened the answer window
        slot = b.engine.slot_ms
        info = DodagInfo(1, 20, 120, 16, 40, EU)
        b.rx(self.p.info(16, 3, info))
        d = b.dodag
        self.assertTrue(d.configured)
        self.assertEqual((d.version, b.engine.dodag_version), (3, 3))
        self.assertEqual(d.info, info)
        self.assertEqual(b.engine.neighbours.period_ms, 20000)
        self.assertEqual(d.topology_ms, 120000)
        imin = 16 * b.engine.slot_ms
        self.assertEqual(d.imin_ms, imin)
        self.assertEqual(d.trickle.imin, imin)
        self.assertEqual(d.trickle.imax, 20000)
        self.assertEqual(d.parents.hold_ms, 2 * imin)
        self.assertEqual(d.parents.window_ms, imin + WINDOW_SLOTS * slot)
        self.assertEqual(d.parents.max_increase, 40)
        self.assertEqual(d.counters.adoptions, 1)
        # Adopted at once, the parent selected at the close of the window
        # (opened under the default Imin of 32 slots).
        self.assertEqual((d.parents.preferred, b.engine.rank), (None, RANK_INFINITE))
        close = sol + (32 + WINDOW_SLOTS) * slot
        self.assertEqual(d.parents.window_until, close)
        b.advance(close - 1)
        self.assertEqual(b.engine.rank, RANK_INFINITE)
        b.advance(close)
        # First parent: attached, Trickle reset, advertised within Imin.
        self.assertEqual((d.parents.preferred, b.engine.rank), (P, 24))
        start = b.now
        b.imin = imin
        t, h = self.assertAdvertWithin(b, start, version=3, rank=24)
        self.assertEqual(b.port9(t)[0][2], bytes(encode_info(info)))
        b.advance(start + 30000)  # P still a parent (2.5 presence periods: 50 s)
        self.assertEqual(len(b.solicitations(start)), 0)

    def test_boot_takes_the_best_answer_of_its_window(self):
        b = Bench()
        b.advance(b.imin + 100)
        d = b.dodag
        close = d.parents.window_until
        self.assertEqual(close, b.solicitations()[0][0] + b.imin + WINDOW_SLOTS * b.engine.slot_ms)
        b.rx(self.p.info(24, 3))  # first answer: path cost 32
        self.assertEqual((d.version, b.engine.rank), (3, RANK_INFINITE))
        b.advance(b.now + 100)
        b.rx(self.q.info(16, 3))  # better by 8, less than the switch threshold
        b.advance(close)
        self.assertEqual((d.parents.members(), b.engine.rank), ((Q,), 24))
        # The version adopted in the window is owed at the attach: frames
        # without DODAG information do not suppress the advertisement.
        b.rx(self.p.frame(24, 3))
        b.rx(self.q.frame(16, 3))
        self.assertAdvertWithin(b, close, version=3, rank=24)

    def test_no_parent_before_adoption(self):
        b = Bench()
        b.advance(100)
        b.rx(self.p.frame(16, 0))  # version 0 is the board's own default
        b.advance(b.imin + 100)
        self.assertFalse(b.dodag.configured)
        self.assertIsNone(b.dodag.parents.preferred)
        self.assertEqual(b.engine.rank, RANK_INFINITE)

    def test_foreign_profile_not_adopted_and_counted(self):
        b = Bench()
        b.advance(100)
        foreign = DodagInfo(0, 10, 60, 32, 56, 2)
        b.rx(self.p.info(16, 3, foreign))
        b.rx(self.p.info(16, 3, DodagInfo(0, 10, 60, 32, 56, profiles.CUSTOM)))
        d = b.dodag
        self.assertEqual(d.counters.foreign_profile, 2)
        self.assertFalse(d.configured)
        self.assertEqual((d.version, d.counters.adoptions), (0, 0))
        self.assertEqual(b.engine.neighbours.period_ms, 30000)
        self.assertEqual(b.engine.rank, RANK_INFINITE)
        b.rx(self.p.info(16, 3))
        self.assertTrue(d.configured)
        self.assertEqual((d.version, d.counters.foreign_profile), (3, 2))

    def test_malformed_information_counted(self):
        b = Bench()
        b.advance(100)
        b.rx(self.p.frame(16, 3, [Message(LINK_LOCAL, 0, DODAG_INFO, b"\x00\x0a")]))
        self.assertEqual(b.dodag.counters.malformed, 1)
        self.assertFalse(b.dodag.configured)

    def test_information_of_rank_255_not_adopted(self):
        b = Bench()
        b.advance(100)
        b.rx(self.p.info(RANK_INFINITE, 3))
        self.assertFalse(b.dodag.configured)


class Adoption(_Case):
    def test_newer_version_adopted_with_its_parameters(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        info = DodagInfo(0, 20, 60, 32, 40, EU)
        b.rx(self.q.info(16, 4, info))
        d = b.dodag
        self.assertEqual((d.version, b.engine.dodag_version), (4, 4))
        self.assertEqual(b.engine.neighbours.period_ms, 20000)
        self.assertEqual(d.parents.max_increase, 40)
        # Parents of the new version only: Q, not P (still at version 3).
        self.assertEqual(d.parents.members(), (Q,))
        self.assertEqual(d.parents.lowest, 24)
        self.assertTrue(d.trickle.need_info)
        self.assertAdvertWithin(b, start, version=4, rank=24)

    def test_new_version_adopted_through_the_parent(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        b.rx(self.p.info(20, 4))
        self.assertEqual((b.dodag.version, b.dodag.parents.preferred, b.engine.rank), (4, P, 28))
        self.assertEqual(b.dodag.counters.poisons, 0)

    def test_wrap_around_adoption(self):
        b = self.attached(15)
        b.rx(self.q.info(16, 0))
        self.assertEqual(b.dodag.version, 0)
        b.rx(self.s.info(16, 15))  # now older: not adopted
        self.assertEqual(b.dodag.version, 0)
        self.assertEqual(b.dodag.counters.older, 1)

    def test_older_version_not_adopted_resets_trickle(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        self.assertQuiet(b, b.now, b.now + PERIOD)
        start = b.now
        resets = b.dodag.trickle.resets
        b.rx(self.q.info(16, 2))
        self.assertEqual(b.dodag.version, 3)
        self.assertEqual(b.dodag.counters.older, 1)
        self.assertEqual(b.dodag.trickle.resets, resets + 1)
        self.assertAdvertWithin(b, start, version=3, rank=24)

    def test_older_version_data_frame_resets_trickle(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.rx(self.q.frame(16, 1))
        self.assertAdvertWithin(b, start, version=3)

    def test_older_version_of_rank_255_ignored(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.rx(self.q.frame(RANK_INFINITE, 1))
        self.assertEqual(b.dodag.counters.older, 0)
        self.assertQuiet(b, start, start + 2 * b.imin)

    def test_version_eight_apart_ignored(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.rx(self.q.info(16, 11))
        self.assertEqual(b.dodag.version, 3)
        self.assertEqual(b.dodag.counters.incomparable, 1)
        self.assertQuiet(b, start, start + 2 * b.imin)

    def test_newer_version_heard_solicits_and_keeps_parents(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.rx(self.p.frame(16, 4))  # the parent moved to version 4
        d = b.dodag
        self.assertEqual(d.counters.newer, 1)
        sol = b.solicitations(start)
        self.assertEqual(len(sol), 1)
        self.assertTrue(sol[0][0] - start < 200)
        self.assertEqual(sol[0][1].dodag_version, 3)
        # Behind: parents kept, upward traffic keeps its next hop; another
        # frame of the newer version does not solicit again.
        b.advance(start + b.imin // 2)
        b.rx(self.p.frame(16, 4))
        self.assertEqual((d.parents.preferred, b.engine.rank, d.counters.poisons), (P, 24, 0))
        self.assertEqual(d.next_hop(Message(UPWARD, 0, 20, b"up")), P)
        self.assertEqual(len(b.solicitations(start)), 1)
        b.rx(self.p.info(16, 4))
        self.assertEqual((d.version, d.parents.preferred, b.engine.rank), (4, P, 24))

    def test_behind_keeps_its_parent_and_solicits_with_backoff(self):
        # The parent moved to version 4, its DODAG information lost (TLA+
        # `DODAG_line_versions_lag`): no detach; the node keeps it at its own
        # rank and solicits again after Imin, the gap doubling up to the
        # presence period, until it adopts the version.
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        d = b.dodag
        b.rx(self.p.frame(40, 4))  # a rank of another version: not compared
        for k in range(1, 9):  # heard within the silence limit
            b.advance(start + 4000 * k)
            b.rx(self.p.frame(40, 4))
        self.assertEqual((d.parents.preferred, b.engine.rank, d.counters.poisons), (P, 24, 0))
        self.assertEqual((d.version, d.parents.behind), (3, True))
        self.assertEqual(d.next_hop(Message(UPWARD, 0, 20, b"up")), P)
        times = [t for t, h in b.solicitations(start)]
        gaps = [t1 - t0 for t0, t1 in zip(times, times[1:])]
        expected = [b.imin, 2 * b.imin, 4 * b.imin, PERIOD, PERIOD]
        self.assertEqual(len(gaps), len(expected), gaps)
        for gap, e in zip(gaps, expected):
            self.assertTrue(abs(gap - e) < 300, (gaps, expected))
        # The answer comes through: adopted, no more solicitations.
        b.rx(self.p.info(40, 4))
        self.assertEqual((d.version, d.parents.preferred, b.engine.rank), (4, P, 48))
        self.assertFalse(d.parents.behind)
        count = len(times)
        b.advance(b.now + 2 * PERIOD)
        self.assertEqual(len(b.solicitations(start)), count)
        self.assertEqual((d.parents.preferred, d.counters.poisons), (P, 0))

    def test_behind_keeps_selecting_in_its_version(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        d = b.dodag
        b.rx(self.q.frame(16, 4))  # Q moved to version 4; P has not
        # S's presence to its parent, overheard: at the node's sequence (0),
        # path cost 12, better than P's by 12.
        b.rx(self.s.presence(4, 3, 0))
        self.assertEqual((d.parents.preferred, b.engine.rank), (S, 12))
        self.assertEqual(d.counters.poisons, 0)
        self.assertTrue(d.parents.behind)

    def test_parent_lost_while_behind_detaches_and_ends_the_solicitations(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        d = b.dodag
        b.rx(self.p.frame(16, 4))
        b.advance(start + 2 * PERIOD)
        self.assertEqual((d.parents.preferred, d.counters.poisons), (P, 0))
        self.assertTrue(d.parents.behind)
        detach = start + 5 * PERIOD // 2  # P's silence limit (§9)
        b.advance(detach + 100)
        self.assertEqual((d.counters.poisons, b.engine.rank), (1, RANK_INFINITE))
        self.assertFalse(d.parents.behind)
        # No solicitation of the backoff during the hold: the hold's own
        # one at its end (rank 255), then once per presence period.
        late = DESYNC_SLOTS * b.engine.slot_ms + 100
        b.advance(detach + 2 * b.imin - 1)
        self.assertEqual(b.solicitations(detach), [])
        b.advance(detach + 2 * b.imin + late)
        sol = b.solicitations(detach)
        self.assertEqual(len(sol), 1)
        self.assertEqual(sol[0][1].rank, RANK_INFINITE)

    def test_detached_node_adopts_another_version_only_after_its_solicitation(self):
        # Q, heard during the hold, may be a child that lost the poison, in
        # an older version (TLA+ `DODAG_diamond_versions_adopt`): adopting
        # its version would attach the node through its own child.
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        d = b.dodag
        b.rx(self.p.frame(RANK_INFINITE, 3))  # the parent's poison: detached
        self.assertEqual((d.counters.poisons, b.engine.rank), (1, RANK_INFINITE))
        b.advance(start + b.imin)
        b.rx(self.q.info(16, 2))
        b.rx(self.s.info(16, 4))
        self.assertEqual((d.version, b.engine.rank, d.counters.adoptions), (3, RANK_INFINITE, 1))
        late = DESYNC_SLOTS * b.engine.slot_ms + 100
        b.advance(start + 2 * b.imin + late)
        self.assertEqual((d.version, b.engine.rank), (3, RANK_INFINITE))
        # The hold ended by a solicitation at its end (the one asked again
        # after Imin, or its own).
        ended = d.parents.poisoned_at
        self.assertTrue(ended >= start + 2 * b.imin, ended)
        self.assertTrue(ended in [t for t, h in b.solicitations(start)])
        # Heard after the solicitation: adopted at once, attached at any
        # rank at the close of the answer window.
        b.rx(self.q.info(40, 2))
        self.assertEqual((d.version, d.parents.preferred, b.engine.rank), (2, None, RANK_INFINITE))
        b.advance(d.parents.window_until)
        self.assertEqual((d.version, d.parents.preferred, b.engine.rank), (2, Q, 48))


class Resets(_Case):
    def test_quiet_at_imax(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        for _ in range(10):
            b.advance(b.now + 4000)
            b.rx(self.p.frame(16, 3))
        self.assertEqual(b.port9(start), [])
        self.assertEqual(b.dodag.parents.preferred, P)

    def test_intervals_double_after_a_reset(self):
        b = self.attached(3)
        start = b.now
        b.advance(start + 2 * PERIOD - 500)  # P, not heard again, is still a parent
        times = [t - start for t, _ in b.adverts(start)]
        # One advertisement per interval Imin, 2 Imin, 4 Imin; Imax (10 s)
        # caps the fourth interval: none from then on.
        bounds = [(0, 1)]
        interval_start = b.imin
        for interval in (2 * b.imin, 4 * b.imin):
            bounds.append((interval_start / b.imin, (interval_start + interval) / b.imin))
            interval_start += interval
        self.assertEqual(len(times), 3, times)
        for t, (lo, hi) in zip(times, bounds):
            self.assertTrue(lo * b.imin <= t < hi * b.imin + 200, (times, bounds))
        self.assertTrue(b.dodag.trickle.at_imax())

    def test_solicitation_resets(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.rx(self.s.solicitation())
        self.assertEqual(b.dodag.counters.solicited, 1)
        self.assertAdvertWithin(b, start, version=3, rank=24)

    def test_detached_node_ignores_solicitations(self):
        b = Bench()
        b.advance(b.imin + 100)
        start = b.now
        b.rx(self.s.solicitation())
        self.assertEqual(b.dodag.counters.solicited, 1)
        self.assertQuiet(b, start, start + 2 * b.imin)

    def test_rank_error_advertises_at_once_then_resets(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3), (self.q, 20, 3)])
        start = b.now
        b.dodag.rank_error(start)
        b.wake()
        self.assertEqual(b.dodag.counters.rank_errors, 1)
        self.assertEqual(b.dodag.trickle.interval, b.imin)
        # At once, whatever the frames heard meanwhile (no suppression).
        b.rx(self.p.frame(16, 3))
        b.rx(self.q.frame(20, 3))
        b.advance(start + 200)
        sent = b.adverts(start)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].rank, 24)
        # At most once per Imin: a second error within Imin only resets.
        b.dodag.rank_error(b.now)
        b.wake()
        b.rx(self.p.frame(16, 3))
        b.rx(self.q.frame(20, 3))
        b.advance(start + b.imin - 1)
        self.assertEqual(len(b.adverts(start)), 1)
        self.assertEqual(b.dodag.counters.rank_errors, 2)
        b.dodag.rank_error(b.now + 1)
        b.advance(b.now + 200)
        self.assertEqual(len(b.adverts(start)), 2)

    def test_generic_reset(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.dodag.reset(start)
        b.wake()
        self.assertAdvertWithin(b, start)

    def test_two_frames_heard_suppress_one_does_not(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3), (self.q, 20, 3)])
        start = b.now
        suppressed = b.dodag.trickle.suppressed
        b.dodag.reset(start)
        b.rx(self.p.frame(16, 3))
        b.rx(self.q.frame(20, 3))
        self.assertQuiet(b, start, start + b.imin)
        self.assertEqual(b.dodag.trickle.suppressed, suppressed + 1)
        start = b.now
        b.dodag.reset(start)
        b.rx(self.p.frame(16, 3))
        b.rx(self.q.frame(20, 1))  # another version: not counted (and resets)
        self.assertAdvertWithin(b, start)

    def test_own_frame_suppresses(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.dodag.reset(start)
        self.assertEqual(b.engine.submit(200, 0, ROOT, b"report"), 0)
        b.wake()
        b.advance(start + b.imin)
        self.assertEqual(len(b.frames), len(b.port9()) + 1)  # the report went out
        self.assertEqual(b.port9(start), [])

    def test_rank_increase_resets(self):
        b = self.attached(3)
        b.rx(self.q.presence(20, 3, 0))  # overheard: Q at the node's sequence
        b.steady([(self.p, 16, 3), (self.q, 20, 3)])
        self.assertEqual(b.dodag.parents.members(), (P, Q))
        last_p = b.now
        start = None
        for _ in range(8):  # P falls silent, Q is still heard
            b.advance(b.now + 4000)
            b.rx(self.q.frame(20, 3))
            if b.engine.rank != 24:
                break
        # Switched to Q at P's silence deadline (2.5 presence periods).
        self.assertEqual((b.dodag.parents.preferred, b.engine.rank), (Q, 28))
        start = last_p + 5 * PERIOD // 2
        self.assertEqual(b.dodag.counters.poisons, 0)
        t, h = self.assertAdvertWithin(b, start, rank=28)

    def test_poison_hold_and_rejoin(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        last_p = b.now
        detach = last_p + 5 * PERIOD // 2  # silence limit (§9)
        b.advance(detach + 100)
        d = b.dodag
        self.assertEqual((d.counters.poisons, b.engine.rank), (1, RANK_INFINITE))
        # Rank 255 advertised at once (after a random delay of at most 4
        # slots, so that nodes detaching on one event do not collide).
        b.advance(detach + DESYNC_SLOTS * b.engine.slot_ms + 100)
        sent = b.adverts(detach)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0] - detach < DESYNC_SLOTS * b.engine.slot_ms + 100)
        self.assertEqual(sent[0][1].rank, RANK_INFINITE)
        # Q heard during the hold: no selection before 2 Imin, nor at its
        # end (Q may be a child that lost the poison): a solicitation first.
        b.advance(detach + b.imin)
        b.rx(self.q.frame(20, 3))
        self.assertEqual(b.engine.rank, RANK_INFINITE)
        b.advance(detach + 2 * b.imin - 1)
        self.assertEqual(b.engine.rank, RANK_INFINITE)
        late = DESYNC_SLOTS * b.engine.slot_ms + 100
        b.advance(detach + 2 * b.imin + late)
        sol = b.solicitations(detach)
        self.assertEqual(len(sol), 1)
        self.assertEqual((d.parents.preferred, b.engine.rank), (None, RANK_INFINITE))
        self.assertEqual(d.parents.poisoned_at, sol[0][0])
        # Answer window of Imin + 8 slots from the solicitation: Q heard
        # first, then S, better by less than the switch threshold.
        close = sol[0][0] + b.imin + WINDOW_SLOTS * b.engine.slot_ms
        self.assertEqual(d.parents.window_until, close)
        # Their presences overheard, at the node's sequence (0, kept with L
        # across the detach): below L (24), feasible.
        b.rx(self.q.presence(20, 3, 0))
        b.advance(b.now + b.imin // 2)
        b.rx(self.s.presence(12, 3, 0))
        b.advance(close - 1)
        self.assertEqual((d.parents.preferred, b.engine.rank), (None, RANK_INFINITE))
        # Rejoined at the close through the best one heard after the solicitation.
        b.advance(close)
        self.assertEqual((d.parents.preferred, b.engine.rank), (S, 20))
        self.assertEqual(d.parents.members(), (S,))
        # First parent after the detach: Trickle reset.
        self.assertAdvertWithin(b, close, rank=20)

    def test_empty_window_then_first_candidate_heard(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        d = b.dodag
        b.rx(self.p.frame(RANK_INFINITE, 3))  # the parent's poison: detached
        late = DESYNC_SLOTS * b.engine.slot_ms + 100
        b.advance(b.now + 2 * b.imin + late)
        close = d.parents.window_until
        sol = b.solicitations()[-1][0]
        self.assertEqual(close, sol + b.imin + WINDOW_SLOTS * b.engine.slot_ms)
        b.advance(close)
        self.assertIsNone(d.parents.window_until)
        self.assertEqual(b.engine.rank, RANK_INFINITE)
        # Nothing heard in the window: the first candidate heard is taken
        # (presences overheard at the node's sequence, below L).
        b.advance(close + 500)
        b.rx(self.q.presence(20, 3, 0))
        self.assertEqual((d.parents.preferred, b.engine.rank), (Q, 28))
        b.rx(self.s.presence(12, 3, 0))  # better by 8: kept (switch threshold 12)
        self.assertEqual((d.parents.preferred, b.engine.rank), (Q, 28))

    def test_detached_until_the_solicitation_goes_on_air(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        detach = b.now + 5 * PERIOD // 2  # silence limit (§9)
        b.advance(detach + DESYNC_SLOTS * b.engine.slot_ms + 100)
        self.assertEqual(len(b.adverts(detach)), 1)  # the poison
        # The duty-cycle window is spent: the solicitation waits.
        dc = b.engine.dutycycle
        dc.start(b.now, dc.budget_ms)
        dc.done(b.now, dc.budget_ms)
        b.advance(detach + 2 * b.imin + 2 * PERIOD)
        b.rx(self.q.frame(20, 3))
        self.assertEqual(b.solicitations(detach), [])
        self.assertEqual((b.dodag.parents.preferred, b.engine.rank), (None, RANK_INFINITE))
        # The window releases airtime: the solicitation leaves, then Q's
        # next frame is a candidate.
        b.advance(b.now + 3600000 + PERIOD)
        sol = b.solicitations(detach)
        self.assertTrue(sol)
        self.assertEqual((b.engine.rank, b.dodag.parents.poisoned_at), (RANK_INFINITE, sol[0][0]))
        b.rx(self.q.frame(20, 3))
        self.assertEqual((b.dodag.parents.preferred, b.engine.rank), (Q, 28))

    def test_parent_poison_detaches_its_child_at_once(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        start = b.now
        b.rx(self.p.frame(RANK_INFINITE, 3))
        self.assertEqual((b.dodag.counters.poisons, b.engine.rank), (1, RANK_INFINITE))
        b.advance(start + DESYNC_SLOTS * b.engine.slot_ms + 100)
        sent = b.adverts(start)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].rank, RANK_INFINITE)

    def test_poison_hold_then_solicit(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        detach = b.now + 5 * PERIOD // 2  # silence limit (§9)
        b.advance(detach + 2 * b.imin + 300)
        sol = b.solicitations(detach)
        self.assertEqual(len(sol), 1)
        late = DESYNC_SLOTS * b.engine.slot_ms + 100
        self.assertTrue(detach + 2 * b.imin <= sol[0][0] < detach + 2 * b.imin + late)
        self.assertEqual(sol[0][1].rank, RANK_INFINITE)
        self.assertFalse(b.dodag.trickle.running)
        self.assertEqual(len(b.adverts(detach)), 1)  # the poison only
        # Then once per presence period while without route.
        b.advance(detach + 2 * b.imin + 2 * PERIOD + 3 * late)
        times = [t for t, _ in b.solicitations(detach)]
        self.assertEqual(len(times), 3)
        for t0, t1 in zip(times, times[1:]):
            self.assertTrue(PERIOD <= t1 - t0 <= PERIOD + late, times)
        # An answer heard in the window of the third solicitation: rejoined
        # at its close.
        b.rx(self.p.info(16, 3))
        close = b.dodag.parents.window_until
        self.assertEqual(close, times[2] + b.imin + WINDOW_SLOTS * b.engine.slot_ms)
        self.assertEqual(b.engine.rank, RANK_INFINITE)
        b.advance(close)
        self.assertEqual((b.dodag.parents.preferred, b.engine.rank), (P, 24))
        b.advance(b.now + PERIOD + 500)
        self.assertEqual(len(b.solicitations(detach)), 3)


class Sequence(_Case):
    """The root sequence of a node (§9): read from every frame heard,
    overheard ones included, held from the preferred parent, carried by the
    DODAG information."""

    def test_adoption_takes_the_sequence_of_the_information(self):
        b = Bench()
        b.advance(100)
        b.rx(self.p.info(16, 3, seq=77))
        p = b.dodag.parents
        self.assertEqual((p.preferred, p.sequence, b.dodag.sequence), (P, 77, 77))
        self.assertEqual(b.engine.neighbours.get(P).sequence, 77)

    def test_overheard_frame_records_the_sequence(self):
        b = self.attached(3)
        overheard = b.engine.counters.rx_overheard
        b.rx(self.q.presence(20, 3, 5))  # Q's presence to the root
        self.assertEqual(b.engine.counters.rx_overheard, overheard + 1)
        self.assertEqual(b.engine.neighbours.get(Q).sequence, 5)
        b.rx(self.q.frame(20, 3, dest=S))  # no sequence: the last one kept
        self.assertEqual(b.engine.neighbours.get(Q).sequence, 5)
        b.rx(self.q.solicitation(3))  # none either
        self.assertEqual(b.engine.neighbours.get(Q).sequence, 5)

    def test_parent_sequence_taken_and_advertised(self):
        b = self.attached(3)
        b.steady([(self.p, 16, 3)])
        d = b.dodag
        self.assertEqual((d.parents.sequence, d.parents.lowest), (0, 24))
        # P's presence to the root, overheard, carries 1: taken, L restarted.
        b.rx(self.p.presence(16, 3, 1))
        self.assertEqual((d.parents.sequence, d.sequence, d.parents.sequence_at), (1, 1, b.now))
        self.assertEqual(d.parents.lowest, 24)
        # A neighbour's own presence carrying an older one does not change it.
        b.rx(self.q.presence(8, 3, 0))
        self.assertEqual(d.sequence, 1)
        # The node's next DODAG information carries 1.
        start = b.now
        d.reset(start)
        b.wake()
        b.advance(start + b.imin + 200)
        payloads = [p for t, h, p in b.port9(start) if p]
        self.assertTrue(payloads)
        self.assertEqual([p[INFO_SEQUENCE] for p in payloads], [1] * len(payloads))

    def test_poison_carries_the_sequence_kept(self):
        b = self.attached(3)
        b.rx(self.p.presence(16, 3, 9))
        b.rx(self.p.frame(RANK_INFINITE, 3))  # the parent's poison: detached
        late = DESYNC_SLOTS * b.engine.slot_ms + 100
        start = b.now
        b.advance(start + late)
        poison = [(h.rank, p[INFO_SEQUENCE]) for t, h, p in b.port9(start) if p]
        self.assertEqual(poison, [(RANK_INFINITE, 9)])
        self.assertEqual((b.dodag.parents.sequence, b.dodag.parents.lowest), (9, 24))

    def test_frame_sensed_clear_when_grounding_ends_floats(self):
        # The channel found clear at the very moment G runs out without
        # advance: the frame leaves with the floating flag (§9).
        b = self.attached(3)
        p = b.dodag.parents
        self.assertTrue(p.grounded)
        due = p.float_at()
        while b.now + 4000 < due:  # P kept heard, no newer sequence
            b.advance(b.now + 4000)
            b.rx(self.p.frame(16, 3))
        b.advance(due - 1)
        self.assertTrue(b.now < due and p.grounded)
        self.assertEqual(b.engine.submit(200, 0, ROOT, b"report"), 0)
        self.assertEqual(list(b.engine.step(b.now, None)), [(SENSE,)])
        sent = [a[1] for a in b.engine.step(due, (CHANNEL, False)) if a[0] == TX]
        self.assertEqual(len(sent), 1)
        buf = bytearray(sent[0])
        h = Header(0, 0, 0, 0)
        self.assertIsNone(b._opener.receive(buf, len(buf), h))
        self.assertFalse(p.grounded)
        self.assertEqual(h.floating, 1)


class Root(_Case):
    def root(self):
        b = Bench(address=ROOT, info=INFO, version=5)
        self.assertEqual((b.engine.rank, b.engine.dodag_version), (0, 5))
        return b

    def test_beacon_every_half_presence_period_at_imax(self):
        # Trickle at Imax sends nothing: every advertisement is a beacon,
        # every half period +-10 % (§9: three lost beacons stay under the
        # silence limit of 2.5 periods).
        b = self.root()
        b.steady([(self.p, 8, 5), (self.q, 8, 5)])
        start = b.now
        for _ in range(int(5 * PERIOD / 2000)):
            b.advance(b.now + 2000)
            b.rx(self.p.frame(8, 5))
            b.rx(self.q.frame(8, 5))
        sent = b.adverts(start)
        self.assertTrue(9 <= len(sent) <= 11, len(sent))
        times = [t for t, _ in sent]
        gaps = [u - t for t, u in zip(times, times[1:])]
        self.assertTrue(min(gaps) >= PERIOD * 9 // 20, gaps)
        self.assertTrue(max(gaps) <= PERIOD * 11 // 20 + 200, gaps)
        payloads = [p for t, h, p in b.port9(start)]
        self.assertEqual(payloads[0][:INFO_SEQUENCE], bytes(encode_info(INFO))[:INFO_SEQUENCE])
        self.assertEqual(sent[0][1].rank, 0)
        # The root sequence incremented at each beacon, the last one carried.
        seqs = [p[INFO_SEQUENCE] for p in payloads]
        self.assertEqual(seqs, [(seqs[0] + i) & 0xFF for i in range(len(seqs))])
        self.assertEqual(b.dodag.sequence, seqs[-1])

    def test_beacon_not_postponed_by_trickle_resets(self):
        # A solicitation every second keeps the root's Trickle timer at Imin,
        # every advertisement suppressed by 2 port 9 messages heard: the
        # beacon still leaves every half presence period +-10 % (§7, §9).
        b = self.root()
        b.steady([(self.p, 8, 5), (self.q, 8, 5)])
        start = b.now
        trickle = b.dodag.trickle
        sends, suppressed = trickle.sends, trickle.suppressed
        for _ in range(5 * PERIOD // 1000):
            b.advance(b.now + 1000)
            b.rx(self.s.solicitation(5))
            b.rx(self.p.info(8, 5))
            b.rx(self.q.info(8, 5))
        self.assertEqual(trickle.interval, b.imin)
        times = [start] + [t for t, _ in b.adverts(start)]
        gaps = [u - t for t, u in zip(times, times[1:])]
        self.assertTrue(9 <= len(gaps) <= 11, gaps)
        self.assertTrue(max(gaps) <= PERIOD * 11 // 20 + 200, gaps)
        self.assertTrue(min(gaps[1:]) >= PERIOD * 9 // 20, gaps)
        # Every Trickle decision suppressed, one at least every 2 Imin (a
        # decision comes Imin/2 to Imin after a reset; a reset while it is
        # pending keeps it).
        self.assertEqual(trickle.sends, sends)
        self.assertTrue(trickle.suppressed - suppressed >= 5 * PERIOD // (2 * b.imin), gaps)

    def test_urgent_advertisement_raises_a_queued_beacon(self):
        # A Trickle advertisement (priority 1) due while the beacon
        # (priority 2) still waits in the queue merges with it at priority 1:
        # with 90 % of the budget used, priority 2 is refused (and dropped,
        # §6), priority 1 still goes.
        b = self.root()
        b.steady([(self.p, 8, 5)])
        start = b.now
        dc = b.engine.dutycycle
        dc.start(start, dc.budget_ms * 9 // 10)
        dc.done(start, dc.budget_ms * 9 // 10)
        adverts = b.dodag.counters.adverts
        b.dodag._advertise(False)  # the beacon, not yet on air
        b.dodag._advertise(True)  # a reset's advertisement in the same step
        b.wake()
        b.advance(start + 500)
        self.assertEqual(len(b.adverts(start)), 1)
        self.assertEqual(b.dodag.counters.adverts, adverts + 1)

    def test_boot_advertises(self):
        b = self.root()
        self.assertAdvertWithin(b, 0, version=5, rank=0)
        self.assertEqual(b.solicitations(), [])

    def test_set_version(self):
        b = self.root()
        b.steady()
        start = b.now
        info = DodagInfo(0, 20, 60, 32, 56, EU)
        b.dodag.set_version(6, start, info)
        b.wake()
        self.assertEqual((b.dodag.version, b.engine.dodag_version), (6, 6))
        self.assertEqual(b.engine.neighbours.period_ms, 20000)
        t, h = self.assertAdvertWithin(b, start, version=6, rank=0)
        self.assertEqual(b.port9(t)[0][2], bytes(encode_info(info, None, b.dodag.sequence)))
        # The beacon re-armed on half the new period (20 s), +-10 %.
        self.assertTrue(start + 9000 <= b.dodag._beacon_at <= start + 11000, b.dodag._beacon_at)

    def test_boot_sequence_after_the_newest_heard(self):
        # §9: the one after the newest heard while the root listens (here
        # until its first DODAG information), in serial order.
        b = self.root()
        b.rx(self.p.info(8, 4, seq=200))
        b.rx(self.q.presence(16, 4, 198, parent=P))  # overheard, older
        b.rx(self.s.info(8, 4, seq=255))  # newer: 55 ahead
        t, h = self.assertAdvertWithin(b, 0, version=5, rank=0)
        self.assertEqual(b.port9(t)[0][2][INFO_SEQUENCE], 0)  # after 255
        self.assertEqual(b.dodag.sequence, 0)
        # Heard later, nothing changes; the beacon increments.
        b.rx(self.p.info(8, 5, seq=100))
        self.assertEqual(self.beacons(b, 1), [1])

    def beacons(self, b, count):
        """Advance past count beacons; return the sequences they carried."""
        out = []
        for _ in range(count):
            at = b.dodag._beacon_at
            b.advance(at + 300)
            out.append(b.port9(at)[0][2][INFO_SEQUENCE])
        return out

    def test_boot_sequence_without_any_heard_or_given(self):
        b = self.root()
        t, h = self.assertAdvertWithin(b, 0)
        self.assertEqual(b.port9(t)[0][2][INFO_SEQUENCE], 0)
        b = Bench(address=ROOT, info=INFO, version=5, sequence=0x1FE)  # 8 bits: 254
        b.rx(self.p.info(8, 4, seq=10))  # a first sequence given: no listening
        t, h = self.assertAdvertWithin(b, 0)
        self.assertEqual(b.port9(t)[0][2][INFO_SEQUENCE], 254)
        self.assertEqual(self.beacons(b, 2), [255, 0])  # wrap-around

    def test_first_beacon_within_half_a_period_of_boot(self):
        b = self.root()
        self.assertTrue(PERIOD * 9 // 20 <= b.dodag._beacon_at <= PERIOD * 11 // 20)

    def test_root_adopts_nothing(self):
        b = self.root()
        b.rx(self.p.info(8, 6))
        self.assertEqual((b.dodag.version, b.dodag.counters.newer), (5, 1))
        self.assertEqual(b.dodag.counters.solicits, 0)

    def after_beacon(self, b):
        """Advance just past the root's next beacon: none for 0.45 periods."""
        sent = len(b.adverts())
        while len(b.adverts()) == sent:
            b.advance(b.now + 50)

    def test_root_answers_solicitations_and_older_versions(self):
        # Each time right after a beacon, so that the advertisement within
        # Imin is Trickle's answer, not the next beacon.
        b = self.root()
        b.steady()
        self.after_beacon(b)
        start = b.now
        b.rx(self.s.solicitation())
        self.assertAdvertWithin(b, start, version=5)
        b.steady()
        self.after_beacon(b)
        start = b.now
        b.rx(self.p.frame(8, 4))
        self.assertAdvertWithin(b, start, version=5)

    def test_set_version_on_a_node_refused(self):
        b = Bench()
        self.assertRaises(ValueError, b.dodag.set_version, 1, 0)
