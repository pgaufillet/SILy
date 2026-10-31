"""Neighbour table and link estimation (MESH_DESIGN §9 link estimation; MP12, MP13)."""

import unittest

from mesh import profile as profiles
from mesh.frame import BROADCAST, ROOT
from mesh.link import open_link
from mesh.neighbours import (
    ACK_MIN,
    ASYMMETRY_POINTS,
    CAPACITY,
    ETX_MAX,
    ETX_ONE,
    UNREACH_RUN,
    NeighbourTable,
    asymmetric,
    popcount16,
    presence_ms,
    silence_ms,
)
from mesh.radio import CHANNEL, RX, SENSE, TX, TX_DONE
from sim.net import Network
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = bytes(range(16))
_EU = profiles.get(profiles.EU_869)

A = 0x00A1
B = 0x00B1
C = 0x00C1

_LIMIT_SF7 = -75  # demodulation limit at SF7, tenths of dB
_PERIOD = 1000  # presence period of the unit tests (ms)


def _snr(margin_x10):
    """Return the packet SNR (dB) giving margin_x10 at SF7."""
    return (margin_x10 + _LIMIT_SF7) / 10


def _table(capacity=CAPACITY):
    return NeighbourTable(7, _PERIOD, capacity)


def _hear(table, address, counters, now=0, margin_x10=100, rank=8, version=3):
    """Feed one frame per counter of counters; return the last result."""
    n = None
    for c in counters:
        n = table.heard(address, c, rank, version, -80, _snr(margin_x10), now)
    return n


class Popcount(unittest.TestCase):
    def test_counts_bits(self):
        for x in (0, 1, 0x8000, 0xFFFF, 0x5555, 0x00F0, 0x1234):
            self.assertEqual(popcount16(x), bin(x).count("1"), hex(x))


class DeliveryRatio(unittest.TestCase):
    def test_first_frame_and_consecutive_frames(self):
        t = _table()
        n = _hear(t, A, [100])
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (1, 1, 100))
        _hear(t, A, range(101, 140))
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (16, 16, 100))
        self.assertTrue(n.rx_bits <= 0xFFFF)  # fixed-size window

    def test_counter_gaps_are_losses(self):
        t = _table()
        n = _hear(t, A, [10, 11, 13, 14])  # 12 lost
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (4, 5, 80))
        _hear(t, A, [18])  # 15-17 lost
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (5, 9, 56))

    def test_window_holds_the_last_16_counter_values(self):
        t = _table()
        n = _hear(t, A, range(0, 60, 2))  # every other frame lost
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (8, 16, 50))
        _hear(t, A, range(60, 76))  # 16 frames without loss push the losses out
        self.assertEqual(n.d_in(), 100)
        _hear(t, A, range(80, 96, 4))  # 3 of each 4 lost
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (4, 16, 25))

    def test_gap_of_16_kept_above_restarts(self):
        t = _table()
        n = _hear(t, A, range(16))
        _hear(t, A, [31])  # gap 16: 15 values lost, the window ends on 31
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (1, 16, 6))
        _hear(t, A, range(32, 47))
        self.assertEqual((n.received(), n.rx_span), (16, 16))
        _hear(t, A, [64])  # gap 17: restart with this frame alone
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (1, 1, 100))
        _hear(t, A, [66])
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (2, 3, 67))

    def test_stale_counter_ignored(self):
        t = _table()
        n = _hear(t, A, [50, 51], now=10, rank=16)
        self.assertEqual(t.heard(A, 51, 40, 4, -80, 5, 20), None)
        self.assertEqual(t.heard(A, 30, 40, 4, -80, 5, 20), None)
        self.assertEqual(t.stale, 2)
        self.assertEqual((n.counter, n.heard, n.rank, n.version), (51, 10, 16, 3))
        self.assertEqual((n.received(), n.rx_span), (2, 2))

    def test_rank_version_and_time_of_the_last_frame(self):
        t = _table()
        n = t.heard(A, 7, 24, 3, -80, 5, 100)
        t.heard(A, 8, 40, 4, -80, 5, 250)
        self.assertEqual((n.address, n.rank, n.version, n.heard), (A, 40, 4, 250))
        self.assertEqual(n.age(400), 150)


class AcknowledgementWindow(unittest.TestCase):
    def test_unknown_neighbour_not_recorded(self):
        t = _table()
        self.assertFalse(t.acked(A, True, 0))
        self.assertEqual(len(t), 0)

    def test_window_of_16_messages(self):
        t = _table()
        n = _hear(t, A, [1])
        self.assertEqual(n.p_ack(), None)
        for ok in (False, False, False, True):
            self.assertTrue(t.acked(A, ok, 0))
        self.assertEqual((n.acknowledged(), n.ack_count, n.p_ack()), (1, 4, 25))
        for _ in range(12):
            t.acked(A, True, 0)
        self.assertEqual((n.acknowledged(), n.ack_count, n.p_ack()), (13, 16, 81))
        for _ in range(3):  # the 3 failures leave the window
            t.acked(A, True, 0)
        self.assertEqual((n.acknowledged(), n.ack_count, n.p_ack()), (16, 16, 100))
        t.acked(A, False, 0)
        self.assertEqual((n.acknowledged(), n.ack_count), (15, 16))
        self.assertTrue(n.ack_bits <= 0xFFFF)  # fixed-size window


class Etx(unittest.TestCase):
    def test_inverse_square_of_d_in_without_samples(self):
        t = _table()
        n = _hear(t, A, [0])
        self.assertEqual(n.etx(), ETX_ONE)  # one frame: d_in = 1
        _hear(t, A, range(1, 16))
        self.assertEqual(n.etx(), ETX_ONE)
        _hear(t, A, [17])  # 15 of 16: 256 / 225
        self.assertEqual(n.etx(), (256 * ETX_ONE + 112) // 225)
        t2 = _table()
        m = _hear(t2, B, range(0, 32, 2))  # d_in = 1/2: ETX 4
        self.assertEqual(m.etx(), 4 * ETX_ONE)
        m = _hear(t2, C, range(0, 48, 3))  # 6 of 16: (16/6)² = 7.1
        self.assertEqual(m.etx(), (256 * ETX_ONE + 18) // 36)

    def test_inverse_of_p_ack_once_used(self):
        t = _table()
        n = _hear(t, A, range(0, 32, 2))  # d_in = 1/2
        for ok in (True, True, False, True):
            t.acked(A, ok, 0)
        self.assertEqual(n.etx(), (4 * ETX_ONE + 1) // 3)  # 4/3, not 4
        for _ in range(12):
            t.acked(A, True, 0)
        self.assertEqual(n.etx(), (16 * ETX_ONE + 7) // 15)

    def test_heard_well_forwarding_poorly_loses_preference(self):
        # Asymmetric link seen locally (MP12): d_in = 1, p_ack = 1/4.
        t = _table()
        n = _hear(t, A, range(16))
        self.assertTrue(n.eligible())
        for ok in (True, False, False, False):
            t.acked(A, ok, 0)
        self.assertEqual(n.etx(), 4 * ETX_ONE)
        t.acked(A, False, 0)  # 1 of 5
        self.assertEqual(n.etx(), 5 * ETX_ONE)
        self.assertFalse(n.eligible())

    def test_no_acknowledgement_at_all_is_finite(self):
        t = _table()
        n = _hear(t, A, range(16))
        for _ in range(ACK_MIN):
            t.acked(A, False, 0)
        self.assertEqual(n.etx(), 5 * ETX_ONE)  # as if the next one succeeded
        self.assertFalse(n.eligible())
        for _ in range(4):
            t.acked(A, False, 0)
        self.assertEqual(n.etx(), 9 * ETX_ONE)

    def test_d_in_until_4_samples(self):
        # p_ack replaces 1/d_in² from ACK_MIN (4) samples on (§9): one lost
        # acknowledgement on a fresh window does not detach the children.
        self.assertEqual(ACK_MIN, 4)
        t = _table()
        n = _hear(t, A, range(0, 32, 2))  # d_in = 1/2: ETX 4
        for ok in (False, False, True):
            t.acked(A, ok, 0)
            self.assertEqual(n.etx(), 4 * ETX_ONE)
            self.assertFalse(n.latched)
        t.acked(A, True, 0)  # 2 of 4
        self.assertEqual(n.etx(), 2 * ETX_ONE)
        self.assertTrue(n.latched)

    def test_latch_keeps_p_ack_below_4_samples(self):
        # Once 4 samples were held, p_ack goes on with a shorter window (after
        # an ageing), without falling back to d_in; a new entry clears it.
        t = _table()
        n = _hear(t, A, range(0, 32, 2))  # d_in = 1/2: ETX 4
        for ok in (True, True, True, False):
            t.acked(A, ok, 0)
        self.assertEqual(t.expire(silence_ms(_PERIOD)), 0)  # failure aged
        self.assertEqual((n.ack_count, n.latched), (3, True))
        self.assertEqual(n.etx(), ETX_ONE)  # p_ack 3/3, not 1/d_in²
        t.remove(A)
        m = _hear(t, A, range(0, 32, 2), now=3000)
        self.assertEqual((m.ack_count, m.latched, m.fail_at), (0, False, None))
        t.acked(A, True, 3000)
        self.assertEqual(m.etx(), 4 * ETX_ONE)

    def test_etx_limit_of_eligibility(self):
        t = _table()
        self.assertTrue(_hear(t, A, range(0, 32, 2)).eligible())  # ETX 4
        self.assertFalse(_hear(t, B, [0, 2, 4, 6, 8, 10, 12, 15, 18, 21]).eligible())
        self.assertEqual(ETX_MAX, 4 * ETX_ONE)


class Margin(unittest.TestCase):
    def test_average_of_the_last_8_frames(self):
        t = _table()
        n = t.heard(A, 1, 8, 0, -80, -2, 0)  # SF7: -2 - (-7.5) = 5.5 dB
        self.assertEqual((n.margin(), n.last_margin()), (55, 55))
        samples = [100, 80, 60, 40, 20, 0, -20]
        for i, m in enumerate(samples):
            _hear(t, A, [2 + i], margin_x10=m)
        self.assertEqual(n.margin(), (55 + sum(samples) + 4) // 8)
        _hear(t, A, [9], margin_x10=200)  # 55 leaves the average
        self.assertEqual(n.margin(), (sum(samples) + 200 + 4) // 8)
        self.assertEqual(n.last_margin(), 200)
        for c in range(10, 18):
            _hear(t, A, [c], margin_x10=33)
        self.assertEqual(n.margin(), 33)

    def test_quarter_db_snr_and_other_sf(self):
        t = NeighbourTable(12, _PERIOD)  # limit -20 dB
        n = t.heard(A, 1, 8, 0, -120, -17.25, 0)
        self.assertEqual(n.margin(), 28)  # 2.75 dB rounded to tenths

    def test_saturated_snr_replaced_by_rssi_over_noise_floor(self):
        t = _table()
        n = t.heard(A, 1, 8, 0, -60, 9.5, 0)
        self.assertEqual(n.margin(), 170)  # no noise floor: packet SNR
        t.noise_floor = -110
        t.heard(B, 1, 8, 0, -60, 9.5, 0)
        self.assertEqual(t.get(B).margin(), 575)  # 50 dB above -7.5
        t.heard(C, 1, 8, 0, -60, 4.0, 0)
        self.assertEqual(t.get(C).margin(), 115)  # below +5 dB: packet SNR

    def test_without_measurement_not_eligible(self):
        t = _table()
        n = t.heard(A, 1, 8, 0, None, None, 0)
        self.assertEqual((n.margin(), n.last_margin(), n.eligible()), (None, None, False))


class Hysteresis(unittest.TestCase):
    def _average(self, t, n, margin_x10):
        # Eight frames at margin_x10: the average reaches it.
        _hear(t, A, range(n.counter + 1, n.counter + 9), margin_x10=margin_x10)
        self.assertEqual(n.margin(), margin_x10)

    def test_3_db_to_leave_6_db_to_return(self):
        t = _table()
        n = _hear(t, A, [0], margin_x10=40)
        self.assertTrue(n.eligible())  # a new link starts eligible from 3 dB
        self._average(t, n, 30)
        self.assertTrue(n.eligible())
        self._average(t, n, 29)
        self.assertFalse(n.eligible())
        for m in (30, 45, 59):
            self._average(t, n, m)
            self.assertFalse(n.eligible(), m)
        self._average(t, n, 60)
        self.assertTrue(n.eligible())
        self._average(t, n, 35)
        self.assertTrue(n.eligible())

    def test_follows_the_average_not_the_last_frame(self):
        t = _table()
        n = _hear(t, A, range(8), margin_x10=50)
        _hear(t, A, [8], margin_x10=0)  # one weak frame: average 44
        self.assertEqual(n.margin(), 44)
        self.assertTrue(n.eligible())

    def test_new_link_below_3_db_not_eligible(self):
        t = _table()
        self.assertFalse(_hear(t, A, [0], margin_x10=25).eligible())


class Capacity(unittest.TestCase):
    def _fill(self, t):
        for i in range(t.capacity):
            _hear(t, 0x100 + i, [1], now=10 * i)

    def test_64_entries_then_the_stalest_non_parent_evicted(self):
        t = _table()
        self.assertEqual(t.capacity, 64)
        self._fill(t)
        self.assertEqual(len(t), 64)
        for address in (0x100, 0x101, 0x102):  # the three stalest are parents
            self.assertTrue(t.set_parent(address))
        n = _hear(t, A, [5], now=1000)
        self.assertEqual(n.address, A)
        self.assertEqual((len(t), t.evictions), (64, 1))
        self.assertEqual(t.get(0x103), None)  # stalest non-parent
        for address in (0x100, 0x101, 0x102):
            self.assertTrue(t.get(address).parent)
        _hear(t, B, [5], now=1001)
        self.assertEqual(t.get(0x104), None)
        self.assertEqual(sorted(p.address for p in t.parents()), [0x100, 0x101, 0x102])

    def test_refreshed_entry_not_stalest(self):
        t = _table()
        self._fill(t)
        _hear(t, 0x100, [2], now=900)
        _hear(t, A, [5], now=1000)
        self.assertTrue(t.get(0x100) is not None)
        self.assertEqual(t.get(0x101), None)

    def test_unflagged_parent_evictable_again(self):
        t = _table(capacity=2)
        _hear(t, A, [1], now=0)
        _hear(t, B, [1], now=1)
        t.set_parent(A)
        t.set_parent(A, False)
        _hear(t, C, [1], now=2)
        self.assertEqual(t.get(A), None)

    def test_table_full_of_parents_refuses(self):
        t = _table(capacity=2)
        _hear(t, A, [1])
        _hear(t, B, [1])
        t.set_parent(A)
        t.set_parent(B)
        self.assertEqual(_hear(t, C, [1]), None)
        self.assertEqual((len(t), t.refused, t.evictions), (2, 1, 0))
        self.assertFalse(t.set_parent(C))

    def test_slots_stable_and_reused(self):
        t = _table(capacity=4)
        for i, address in enumerate((A, B, C)):
            _hear(t, address, [1], now=i)
        slots = [t.slot(i).address if t.slot(i) else None for i in range(4)]
        self.assertEqual(slots, [A, B, C, None])
        self.assertTrue(t.remove(B))
        self.assertFalse(t.remove(B))
        self.assertEqual([n.address for n in t.entries()], [A, C])
        n = _hear(t, 0x0D1, [3], now=5)
        self.assertTrue(t.slot(1) is n)
        self.assertEqual((n.rx_span, n.ack_count, n.parent), (1, 0, False))


class FailureAgeing(unittest.TestCase):
    def test_silence_limit_is_two_and_a_half_periods(self):
        self.assertEqual(silence_ms(1000), 2500)
        self.assertEqual(silence_ms(10001), 25002)

    def test_failures_age_out_successes_kept(self):
        # A link that failed during a cut, heard at 100 % since: its failures
        # leave the window 2.5 periods after the newest one (§9).
        t = _table()
        n = _hear(t, A, range(16))
        for ok in (True, False, True, False, False, False, False, False):
            t.acked(A, ok, 100)
        t.acked(A, False, 400)
        self.assertEqual((n.ack_count, n.acknowledged(), n.fail_at), (9, 2, 400))
        self.assertEqual(n.etx(), (9 * ETX_ONE + 1) // 2)  # 4.5
        self.assertFalse(n.eligible())
        t.expire(400 + silence_ms(_PERIOD) - 1)
        self.assertEqual((n.ack_count, n.acknowledged()), (9, 2))
        t.expire(400 + silence_ms(_PERIOD))
        self.assertEqual((n.ack_count, n.acknowledged(), n.ack_bits), (2, 2, 0b11))
        self.assertEqual((n.fail_at, t.aged), (None, 1))
        self.assertEqual(n.etx(), ETX_ONE)  # latched: p_ack 2/2
        self.assertTrue(n.eligible())

    def test_a_newer_failure_restarts_the_delay(self):
        t = _table()
        n = _hear(t, A, range(16))
        for ok in (True, True, True, False):
            t.acked(A, ok, 0)
        _hear(t, A, [16], now=2000)
        t.acked(A, True, 2000)
        t.acked(A, False, 2000)
        t.expire(silence_ms(_PERIOD))
        self.assertEqual((n.ack_count, n.acknowledged()), (6, 4))
        _hear(t, A, [17], now=2000 + silence_ms(_PERIOD))  # heard runs expire too
        self.assertEqual((n.ack_count, n.acknowledged()), (4, 4))

    def test_ageing_due_even_before_an_expiry(self):
        # The next check of the table includes the ageing deadline.
        t = _table()
        _hear(t, A, [1], now=0)
        n = _hear(t, B, [1], now=0)
        self.assertEqual(t.expire(1), 0)  # next check: removals at 4000
        for ok in (True, False, True, True):
            t.acked(B, ok, 10)
        t.expire(10 + silence_ms(_PERIOD))
        self.assertEqual((n.ack_count, t.aged), (3, 1))
        self.assertEqual(len(t), 2)


class RootReachability(unittest.TestCase):
    """§9: the link to the root is ineligible once unreachable (U consecutive
    failed samples and a silence limit without acknowledgement of the root),
    not above ETX 4; its link cost's ETX is capped at 4."""

    def _root(self, t, address=ROOT, now=0):
        n = _hear(t, address, range(16), now=now)
        # 1 of 5 acknowledged at now: ETX 5 > 4, the last 4 failed in a row.
        for ok in (True, False, False, False, False):
            t.acked(address, ok, now)
        return n

    def test_constants(self):
        self.assertEqual(UNREACH_RUN, 4)

    def test_root_eligible_above_etx_4_while_acknowledged_within_the_silence_limit(self):
        t = _table()
        silence = silence_ms(_PERIOD)
        n = self._root(t, now=100)
        self.assertEqual((n.etx(), n.run, n.answered), (5 * ETX_ONE, 4, 100))
        self.assertTrue(n.eligible(100, silence))
        self.assertTrue(n.eligible(100 + silence - 1, silence))
        # The same samples on another link: ineligible (ETX > 4).
        m = self._root(t, A, now=100)
        self.assertEqual(m.etx(), 5 * ETX_ONE)
        self.assertFalse(m.eligible(100, silence))
        self.assertFalse(m.eligible())

    def test_unreachable_after_the_run_and_the_silence_limit(self):
        t = _table()
        silence = silence_ms(_PERIOD)
        n = self._root(t, now=100)
        self.assertFalse(n.reachable(100 + silence, silence))
        self.assertFalse(n.eligible(100 + silence, silence))
        self.assertFalse(n.eligible())  # without the time: the run alone

    def test_the_run_alone_or_the_silence_alone_does_not_suffice(self):
        silence = silence_ms(_PERIOD)
        # 3 consecutive failed samples, the last acknowledgement (the entry's
        # creation) a silence limit old, the root heard: reachable.
        t = _table()
        n = _hear(t, ROOT, range(16))
        for i, at in enumerate((0, 2000, 2400)):
            _hear(t, ROOT, [16 + i], now=at)
            t.acked(ROOT, False, at)
        _hear(t, ROOT, [19], now=silence + 100)
        self.assertEqual((n.run, n.answered, t.aged), (3, 0, 0))
        self.assertTrue(n.eligible(silence + 100, silence))
        t.acked(ROOT, False, silence + 100)  # the 4th
        self.assertFalse(n.eligible(silence + 100, silence))
        # 4 in a row (10 in the window), the last acknowledgement recent: reachable.
        t = _table()
        n = _hear(t, ROOT, range(16))
        for ok in [False] * 6 + [True] + [False] * 4:
            t.acked(ROOT, ok, 1000)
        self.assertEqual((n.run, n.ack_count - n.acknowledged()), (4, 10))
        self.assertTrue(n.eligible(1000 + silence - 1, silence))
        self.assertFalse(n.eligible(1000 + silence, silence))

    def test_an_acknowledgement_makes_it_reachable_again(self):
        silence = silence_ms(_PERIOD)
        for sample in (True, False):
            t = _table()
            n = self._root(t)
            self.assertFalse(n.eligible(silence, silence))
            if sample:
                t.acked(ROOT, True, silence + 10)  # a success sample
            else:
                self.assertTrue(t.answered(ROOT, silence + 10))  # no sample (late)
            self.assertEqual((n.run, n.answered), (0, silence + 10))
            self.assertTrue(n.eligible(silence + 10, silence))
            self.assertTrue(n.eligible(3 * silence, silence))  # no failure since
            self.assertEqual(n.etx(), (6 * ETX_ONE + 1) // 2 if sample else 5 * ETX_ONE)
        self.assertFalse(_table().answered(ROOT, 0))

    def test_beacons_do_not_make_it_reachable(self):
        t = _table()
        silence = silence_ms(_PERIOD)
        n = _hear(t, ROOT, range(16))
        t.acked(ROOT, True, 0)
        for i, at in enumerate((0, 1000, 2000, 2400)):
            _hear(t, ROOT, [16 + i], now=at)  # a beacon, and a failed sample
            t.acked(ROOT, False, at)
        _hear(t, ROOT, [20], now=silence)
        self.assertEqual((n.run, n.answered, t.aged), (4, 0, 0))
        self.assertFalse(n.eligible(silence, silence))

    def test_ageing_ends_the_run_and_keeps_the_last_acknowledgement(self):
        # The run is counted over the window's failed samples: when they age
        # out, the link gets U more before it is left; the time of the last
        # acknowledgement stays.
        t = _table()
        silence = silence_ms(_PERIOD)
        n = self._root(t, now=100)
        _hear(t, ROOT, [16], now=100 + silence)
        self.assertEqual((n.fail_at, n.run, n.answered, t.aged), (None, 0, 100, 1))
        self.assertTrue(n.eligible(100 + silence, silence))
        for i in range(UNREACH_RUN):
            self.assertTrue(n.eligible(100 + silence, silence), i)
            t.acked(ROOT, False, 100 + silence)
        self.assertFalse(n.eligible(100 + silence, silence))

    def test_new_entry_starts_reachable_its_creation_as_last_acknowledgement(self):
        t = _table()
        silence = silence_ms(_PERIOD)
        self._root(t, now=0)
        t.remove(ROOT)
        n = _hear(t, ROOT, [40], now=5000)
        self.assertEqual((n.run, n.answered), (0, 5000))
        for _ in range(UNREACH_RUN):
            t.acked(ROOT, False, 5000)
        self.assertTrue(n.eligible(5000 + silence - 1, silence))
        self.assertFalse(n.eligible(5000 + silence, silence))

    def test_margin_rule_unchanged(self):
        t = _table()
        n = _hear(t, ROOT, range(8), margin_x10=20)  # below 3 dB from its first frame
        self.assertEqual(n.run, 0)
        self.assertFalse(n.eligible(0, silence_ms(_PERIOD)))

    def test_link_etx_capped_at_4_towards_the_root_only(self):
        t = _table()
        n = self._root(t)
        self.assertEqual((n.etx(), n.link_etx()), (5 * ETX_ONE, ETX_MAX))
        m = self._root(t, A)
        self.assertEqual(m.link_etx(), 5 * ETX_ONE)
        # At ETX <= 4 the ETX itself.
        for ok in (True,) * 10:
            t.acked(ROOT, ok, 0)
        self.assertEqual(n.link_etx(), n.etx())
        self.assertEqual(n.etx(), (15 * ETX_ONE + 5) // 11)


class RelayReachability(unittest.TestCase):
    """§9: a relay link judged by reachability (reach: the preferred parent
    as the node's last candidate, decided by routing) follows the root's
    rule; without reach a relay link keeps ETX <= 4 (MP12)."""

    def _relay(self, t, now=0):
        n = _hear(t, A, range(16), now=now)
        # 1 of 5 acknowledged at now: ETX 5 > 4, the last 4 failed in a row.
        for ok in (True, False, False, False, False):
            t.acked(A, ok, now)
        return n

    def test_eligible_above_etx_4_while_reachable_only_with_reach(self):
        t = _table()
        silence = silence_ms(_PERIOD)
        n = self._relay(t, now=100)
        self.assertEqual((n.etx(), n.run, n.answered), (5 * ETX_ONE, 4, 100))
        self.assertFalse(n.eligible(100, silence))
        self.assertTrue(n.eligible(100, silence, True))
        self.assertTrue(n.eligible(100 + silence - 1, silence, True))
        self.assertFalse(n.eligible(100 + silence, silence, True))
        self.assertFalse(n.eligible(None, None, True))  # without the time: the run alone

    def test_the_run_and_the_silence_both_needed(self):
        # The silence alone (test above: the run alone) does not suffice.
        silence = silence_ms(_PERIOD)
        t = _table()
        n = _hear(t, A, range(16))
        # ETX 10, 3 consecutive failed samples after the success at 0.
        for ok in [False] * 6 + [True] + [False] * 3:
            t.acked(A, ok, 0)
        self.assertEqual((n.etx(), n.run, n.answered), (10 * ETX_ONE, 3, 0))
        self.assertTrue(n.eligible(silence + 10, silence, True))
        t.acked(A, False, silence + 10)  # the 4th
        self.assertFalse(n.eligible(silence + 10, silence, True))

    def test_any_acknowledgement_makes_it_reachable_again(self):
        silence = silence_ms(_PERIOD)
        for sample in (True, False):
            t = _table()
            n = self._relay(t)
            self.assertFalse(n.eligible(silence, silence, True))
            if sample:
                t.acked(A, True, silence)  # explicit or passive, with a sample
            else:
                self.assertTrue(t.answered(A, silence))  # one that gives no sample
            self.assertEqual((n.run, n.answered), (0, silence))
            self.assertTrue(n.eligible(silence, silence, True))

    def test_restart_reach_as_a_new_entry(self):
        t = _table()
        silence = silence_ms(_PERIOD)
        n = self._relay(t)
        self.assertFalse(n.eligible(silence, silence, True))
        self.assertTrue(t.restart_reach(A, silence))
        self.assertEqual((n.run, n.answered, n.etx()), (0, silence, 5 * ETX_ONE))
        self.assertTrue(n.eligible(2 * silence, silence, True))  # the run restarted
        self.assertFalse(t.restart_reach(B, 0))

    def test_link_etx_capped_at_4_with_reach_only(self):
        t = _table()
        n = self._relay(t)
        self.assertEqual((n.link_etx(), n.link_etx(True)), (5 * ETX_ONE, ETX_MAX))
        for _ in range(10):
            t.acked(A, True, 0)
        self.assertEqual(n.link_etx(True), n.etx())  # at ETX <= 4, the ETX itself


class Expiry(unittest.TestCase):
    def test_removed_after_4_periods(self):
        t = _table()
        _hear(t, A, [1], now=0)
        _hear(t, B, [1], now=500)
        t.set_parent(A)  # parents expire too
        self.assertEqual(t.expire(3999), 0)
        self.assertEqual(len(t), 2)
        self.assertEqual(t.expire(4000), 1)
        self.assertEqual(t.get(A), None)
        self.assertTrue(t.get(B) is not None)
        self.assertEqual(t.expire(4499), 0)
        self.assertEqual(t.expire(4500), 1)
        self.assertEqual((len(t), t.expired), (0, 2))

    def test_frames_keep_an_entry_and_reception_expires_others(self):
        t = _table()
        _hear(t, A, [1], now=0)
        _hear(t, B, [1], now=0)
        _hear(t, A, [2], now=3000)
        _hear(t, C, [1], now=4000)  # expires B through heard
        self.assertEqual(sorted(n.address for n in t.entries()), [A, C])
        n = _hear(t, B, [2], now=4000)  # a returning neighbour starts afresh
        self.assertEqual((n.rx_span, n.received()), (1, 1))

    def test_period_change(self):
        t = _table()
        _hear(t, A, [1], now=0)
        self.assertEqual(t.expire(3000), 0)  # next check due at 4000
        t.set_period(500)
        self.assertEqual(t.expire(3000), 1)

    def test_default_period_of_the_sf(self):
        self.assertEqual(NeighbourTable(7).period_ms, 30000)
        self.assertEqual(NeighbourTable(9).period_ms, presence_ms(9))
        self.assertEqual(presence_ms(12), 600000)


class Asymmetry(unittest.TestCase):
    def test_more_than_30_points(self):
        self.assertEqual(ASYMMETRY_POINTS, 30)
        self.assertFalse(asymmetric(90, 60))
        self.assertTrue(asymmetric(90, 59))
        self.assertTrue(asymmetric(20, 51))
        self.assertFalse(asymmetric(50, 50))


# --- Link engine ------------------------------------------------------------


def _engine(address, seed, hop):
    platform = FakePlatform(XorShift32(seed))
    engine = open_link(
        address,
        _KEY,
        platform.storage,
        platform.retained,
        platform.random,
        _EU,
        7,
        lambda msg: hop,
        budget_ms=10000,
        window_ms=60000,
    )
    engine.step(0, None)
    return engine


def _frame(engine, now):
    """Return the next frame engine transmits on an idle channel."""
    actions = list(engine.step(now, None))
    while actions[0][0] == SENSE:
        actions = list(engine.step(now, (CHANNEL, False)))
    action = actions[0]
    if action[0] != TX:
        raise AssertionError("no transmission")
    engine.step(now + 100, (TX_DONE, 100))
    return action[1]


class EngineHook(unittest.TestCase):
    def test_every_authenticated_frame_feeds_the_table(self):
        a = _engine(A, 1, ROOT)
        c = _engine(C, 3, ROOT)
        a.rank = 24
        a.dodag_version = 5
        frames = []
        for i in range(4):
            a.submit(20, 0, ROOT, b"up %d" % i)
            frames.append(_frame(a, 1000 * i))
        frames.append(frames[3])  # replayed: rejected before the table
        for i in (0, 2, 3, 4):  # frame 1 lost
            c.step(1000 * i + 500, (RX, frames[i], -90, -1))
        n = c.neighbours.get(A)
        self.assertEqual((n.rank, n.version, n.heard), (24, 5, 3500))
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (3, 4, 75))
        self.assertEqual(n.margin(), 65)  # -1 dB at SF7
        self.assertEqual(c.counters.rx_overheard, 3)  # link destination ROOT
        self.assertEqual(c.counters.rx_replay, 1)
        self.assertEqual(len(c.neighbours), 1)

    def test_own_address_not_a_neighbour(self):
        a = _engine(A, 1, ROOT)
        twin = _engine(A, 2, ROOT)
        twin.submit(20, 0, BROADCAST, b"x")
        a.step(10, (RX, _frame(twin, 0), -90, 5))
        self.assertEqual(a.counters.duplicate_address, 1)
        self.assertEqual(len(a.neighbours), 0)


# --- Scenario on the scripted medium ------------------------------------------


def _to(hop):
    return lambda msg: hop


def _close(test, mean, loss):
    """Check a mean d_in (%) against a link loss.

    The window ends on a received frame, so its expectation is
    (1 + 15 (1 - loss)) / 16, above 1 - loss by at most 1/16.
    """
    p = 1 - loss
    test.assertTrue(abs(mean - 100 * (1 + 15 * p) / 16) < 3, mean)
    test.assertTrue(abs(mean - 100 * p) < 7, mean)


class LossyLinks(unittest.TestCase):
    """Two boards exchanging frames over a lossy, possibly asymmetric link."""

    FRAMES = 400

    def _run(self, seed, loss_ab, loss_ba):
        net = Network(seed, _KEY, budget_ms=3600000, window_ms=3600000)
        a = net.add(A, next_hop=_to(B))
        b = net.add(B, next_hop=_to(A))
        net.medium.link(A, B, loss_ab)
        net.medium.link(B, A, loss_ba)
        seen = {A: [], B: []}  # d_in samples of A at B, of B at A

        def sample(board, other):
            n = board.engine.neighbours.get(other)
            if n is not None:
                seen[other].append(n.d_in())

        for i in range(self.FRAMES):
            t = 1000 + 1000 * i
            net.at(t, a.submit, 20, 0, B, b"from a")
            net.at(t + 500, b.submit, 20, 0, A, b"from b")
            net.at(t + 400, sample, b, A)
            net.at(t + 900, sample, a, B)
        net.run(1000 + 1000 * self.FRAMES + 1000)
        self.assertEqual(net.medium.stats(A).tx_count, self.FRAMES)
        self.assertEqual(net.medium.stats(B).tx_count, self.FRAMES)
        mean_a = sum(seen[A]) / len(seen[A])  # A as heard by B
        mean_b = sum(seen[B]) / len(seen[B])
        return net, a, b, mean_a, mean_b

    def test_ratio_converges_to_the_loss(self):
        net, a, b, mean_a, mean_b = self._run(11, 0.3, 0.3)
        _close(self, mean_a, 0.3)
        _close(self, mean_b, 0.3)
        self.assertFalse(asymmetric(mean_a, mean_b))

    def test_asymmetric_link_detectable(self):
        net, a, b, mean_a, mean_b = self._run(12, 0.1, 0.6)
        _close(self, mean_a, 0.1)
        _close(self, mean_b, 0.6)
        self.assertTrue(asymmetric(mean_a, mean_b))
        # Without acknowledgement samples ETX assumes symmetry: the weak
        # direction is seen only from its receiving end.
        self.assertTrue(a.engine.neighbours.get(B).etx() > b.engine.neighbours.get(A).etx())

    def test_reboot_restarts_the_window(self):
        net = Network(13, _KEY, budget_ms=3600000, window_ms=3600000)
        a = net.add(A, next_hop=_to(B))
        b = net.add(B, next_hop=_to(A))
        net.medium.link(A, B, 0.0)
        net.medium.link(B, A, 0.0)
        for i in range(5):
            net.at(1000 * (i + 1), a.submit, 20, 0, B, b"before")
        net.run(6000)
        n = b.engine.neighbours.get(A)
        self.assertEqual((n.received(), n.rx_span), (5, 5))
        before = n.counter
        a.power_loss(off_ms=100)
        net.at(7000, a.submit, 20, 0, B, b"after")
        net.run(8000)
        self.assertTrue(n.counter - before > 16)  # the rest of the block skipped
        self.assertEqual((n.received(), n.rx_span, n.d_in()), (1, 1, 100))
