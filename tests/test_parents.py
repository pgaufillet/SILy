"""Rank, parent set and preferred parent (MESH_DESIGN §9; MP11, MP12)."""

import unittest

from mesh.frame import DOWNWARD, RANK_INFINITE, ROOT, UPWARD, Message
from mesh.neighbours import NeighbourTable
from mesh.parents import (
    ATTACHED,
    DETACHED,
    MAX_RANK_INCREASE,
    PARENT_CHANGED,
    RANK_CHANGED,
    RANK_INCREASED,
    SIZE,
    Parents,
    link_cost,
    path_cost,
)
from mesh.platform import randbelow
from mesh.reliable import HOLD
from sim.prng import XorShift32

_PERIOD = 1000  # presence period of the tests (ms)
_LIMIT_SF7 = -75  # demodulation limit at SF7, tenths of dB

A = 0x00A1
B = 0x00B1
C = 0x00C1
D = 0x00D1
E = 0x00E1
F = 0x00F1


def _snr(margin_x10):
    return (margin_x10 + _LIMIT_SF7) / 10


class Net:
    """A neighbour table fed frame by frame, with consecutive counters per sender."""

    def __init__(self, capacity=64):
        self.table = NeighbourTable(7, _PERIOD, capacity)
        self._counters = {}

    def hear(self, address, rank, now, version=0, margin=100, gap=1):
        counter = self._counters.get(address, 0) + gap
        self._counters[address] = counter
        return self.table.heard(address, counter, rank, version, -80, _snr(margin), now)

    def etx(self, address, acked, total):
        """Give address an ETX of total / acked through acknowledgement samples."""
        for i in range(total):
            self.table.acked(address, i < acked)

    def flagged(self):
        return sorted(n.address for n in self.table.entries() if n.parent)


class Costs(unittest.TestCase):
    def test_link_cost_is_eight_times_etx_rounded(self):
        self.assertEqual(link_cost(128), 8)  # ETX 1
        self.assertEqual(link_cost(192), 12)  # ETX 1.5
        self.assertEqual(link_cost(256), 16)  # ETX 2
        self.assertEqual(link_cost(512), 32)  # ETX 4
        self.assertEqual(link_cost(135), 8)  # 8.44
        self.assertEqual(link_cost(136), 9)  # 8.5 rounds up
        self.assertEqual(link_cost(100), 8)  # never below one hop

    def test_path_cost_adds_link_cost_to_rank(self):
        self.assertEqual(path_cost(0, 128), 8)
        self.assertEqual(path_cost(40, 256), 56)

    def test_cost_from_the_table_etx(self):
        net = Net()
        net.hear(A, 16, 0)
        net.etx(A, 2, 3)  # ETX 1.5
        p = Parents(net.table)
        self.assertEqual(p.candidate_cost(net.table.get(A), 0), 28)


class Candidates(unittest.TestCase):
    def setUp(self):
        # Attached through A (rank 16, ETX 1): rank 24.
        self.net = Net()
        self.net.hear(A, 16, 0)
        self.p = Parents(self.net.table)
        self.p.recompute(0)

    def cost(self, address, now):
        return self.p.candidate_cost(self.net.table.get(address), now)

    def test_rank_strictly_lower(self):
        self.assertEqual(self.p.rank, 24)
        self.net.hear(B, 24, 10)
        self.net.hear(C, 23, 10)
        self.assertIsNone(self.cost(B, 10))
        self.assertEqual(self.cost(C, 10), 31)

    def test_same_version_only(self):
        self.net.hear(B, 8, 10, version=1)
        self.assertIsNone(self.cost(B, 10))

    def test_heard_within_two_periods(self):
        self.net.hear(B, 8, 100)
        self.assertEqual(self.cost(B, 100 + 2 * _PERIOD - 1), 16)
        self.assertIsNone(self.cost(B, 100 + 2 * _PERIOD))

    def test_eligible_link_only(self):
        self.net.hear(B, 8, 10, margin=20)  # below 3 dB
        self.assertIsNone(self.cost(B, 10))
        self.net.hear(C, 8, 10)
        self.net.etx(C, 1, 5)  # ETX 5 > 4
        self.assertIsNone(self.cost(C, 10))
        self.net.hear(D, 8, 10)
        self.net.etx(D, 1, 4)  # ETX 4
        self.assertEqual(self.cost(D, 10), 40)

    def test_selection_ignores_non_candidates(self):
        self.net.hear(A, 16, 100)
        self.net.hear(B, 4, 100, version=1)
        self.net.hear(C, 24, 100)
        self.net.hear(D, 4, 100, margin=20)
        self.p.recompute(100)
        self.assertEqual(self.p.members(), (A,))
        self.assertEqual(self.p.rank, 24)

    def test_without_route_any_finite_rank(self):
        net = Net()
        net.hear(A, 200, 0)
        net.hear(B, RANK_INFINITE, 0)
        p = Parents(net.table)
        self.assertEqual(p.candidate_cost(net.table.get(A), 0), 208)
        self.assertIsNone(p.candidate_cost(net.table.get(B), 0))
        p.recompute(0)
        self.assertEqual((p.members(), p.rank), ((A,), 208))

    def test_path_cost_above_254_unusable(self):
        net = Net()
        net.hear(A, 247, 0)
        net.hear(B, 246, 0)
        p = Parents(net.table)
        self.assertIsNone(p.candidate_cost(net.table.get(A), 0))
        self.assertEqual(p.candidate_cost(net.table.get(B), 0), 254)
        p.recompute(0)
        self.assertEqual((p.members(), p.rank), ((B,), 254))


class ParentSet(unittest.TestCase):
    def test_three_lowest_path_costs_preferred_first(self):
        net = Net()
        for address, rank in ((A, 14), (B, 10), (C, 13), (D, 12), (E, 16)):
            net.hear(address, rank, 0)
        p = Parents(net.table)
        events = p.recompute(0)
        self.assertEqual(SIZE, 3)
        self.assertEqual(p.members(), (B, D, C))
        self.assertEqual(p.preferred, B)
        self.assertEqual((p.rank, p.cost, p.lowest), (18, 18, 18))
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED | ATTACHED)
        self.assertEqual(net.flagged(), sorted((B, C, D)))

    def test_flags_follow_the_set(self):
        net = Net()
        for address, rank in ((A, 4), (B, 6), (C, 8), (D, 10)):
            net.hear(address, rank, 0)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual(p.members(), (A, B, C))
        # C silent for 2 periods: D takes its place, C loses its flag.
        for address, rank in ((A, 4), (B, 6), (D, 10)):
            net.hear(address, rank, 1500)
        p.recompute(2000)
        self.assertEqual(p.members(), (A, B, D))
        self.assertEqual(net.flagged(), sorted((A, B, D)))
        self.assertFalse(net.table.get(C).parent)

    def test_eviction_spares_the_set(self):
        net = Net(capacity=4)
        for address, rank in ((A, 4), (B, 6), (C, 8)):
            net.hear(address, rank, 0)
        net.hear(D, RANK_INFINITE, 10)  # newest, not a parent
        p = Parents(net.table)
        p.recompute(10)
        net.hear(E, 30, 20)  # table full: the stalest non-parent goes
        self.assertIsNone(net.table.get(D))
        for address in (A, B, C, E):
            self.assertIsNotNone(net.table.get(address))

    def test_members_not_below_new_rank_leave(self):
        # Rank rule: after the rank decreases, a member of rank >= the new
        # rank leaves the set.
        net = Net()
        net.hear(A, 30, 0)
        net.hear(B, 33, 0)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual((p.members(), p.rank), ((A, B), 38))
        net.hear(C, 0, 100)
        p.recompute(100)
        self.assertEqual((p.members(), p.rank), ((C,), 8))
        self.assertEqual(net.flagged(), [C])


class SwitchThreshold(unittest.TestCase):
    def setUp(self):
        self.net = Net()
        self.net.hear(A, 16, 0)
        self.p = Parents(self.net.table)
        self.p.recompute(0)  # through A: path cost 24

    def test_gain_of_11_keeps_the_preferred_parent(self):
        self.net.hear(B, 5, 100)  # path cost 13
        events = self.p.recompute(100)
        self.assertEqual(self.p.preferred, A)
        self.assertEqual(self.p.members(), (A, B))
        self.assertEqual(self.p.rank, 24)
        self.assertEqual(events, 0)
        self.assertEqual(self.p.switches, 0)

    def test_gain_of_12_switches(self):
        self.net.hear(B, 4, 100)  # path cost 12
        events = self.p.recompute(100)
        self.assertEqual(self.p.preferred, B)
        self.assertEqual(self.p.rank, 12)
        self.assertEqual(self.p.members(), (B,))  # A (16) not below 12
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED)
        self.assertEqual(self.p.switches, 1)

    def test_preferred_leaving_the_set_switches(self):
        # Three candidates all better than A by less than 12 push A out of the set.
        for address, rank in ((B, 13), (C, 14), (D, 15)):
            self.net.hear(address, rank, 100)
        self.p.recompute(100)
        self.assertEqual(self.p.preferred, B)
        self.assertEqual(self.p.members(), (B, C, D))
        self.assertEqual(self.p.rank, 21)
        self.assertFalse(self.net.table.get(A).parent)

    def test_preferred_lost_switches(self):
        self.net.hear(B, 10, 100)
        self.net.etx(B, 1, 2)  # path cost 26, worse than A
        self.p.recompute(100)
        self.assertEqual(self.p.members(), (A, B))
        self.net.hear(B, 10, 1900)
        events = self.p.recompute(2000)  # A silent for 2 periods
        self.assertEqual(self.p.preferred, B)
        # A change of preferred parent advertises at once, even 2 apart.
        self.assertEqual(self.p.rank, 26)
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)


class RankHysteresis(unittest.TestCase):
    def setUp(self):
        self.net = Net()
        self.net.hear(A, 16, 0)
        self.p = Parents(self.net.table)
        self.p.recompute(0)  # rank 24

    def step(self, rank, now):
        self.net.hear(A, rank, now)
        return self.p.recompute(now)

    def test_increase_of_3_not_advertised(self):
        self.assertEqual(self.step(19, 100), 0)
        self.assertEqual((self.p.rank, self.p.cost), (24, 27))

    def test_increase_of_4_advertised(self):
        self.assertEqual(self.step(20, 100), RANK_CHANGED | RANK_INCREASED)
        self.assertEqual(self.p.rank, 28)

    def test_decrease_of_3_not_advertised_4_advertised(self):
        self.step(20, 100)
        self.assertEqual(self.step(17, 200), 0)
        self.assertEqual(self.p.rank, 28)
        self.assertEqual(self.step(16, 300), RANK_CHANGED)
        self.assertEqual(self.p.rank, 24)

    def test_lowest_follows_advertised_rank_only(self):
        self.step(13, 100)  # path cost 21, 3 below: not advertised
        self.assertEqual((self.p.rank, self.p.lowest), (24, 24))
        self.step(12, 200)
        self.assertEqual((self.p.rank, self.p.lowest), (20, 20))
        self.step(16, 300)
        self.assertEqual((self.p.rank, self.p.lowest), (24, 20))


class BoundedIncrease(unittest.TestCase):
    """L + 56 within a DODAG version: beyond, the node detaches."""

    def setUp(self):
        net = self.net = Net()
        net.hear(ROOT, 0, 0)
        net.etx(ROOT, 1, 2)  # path cost 16
        net.hear(B, 15, 0)
        net.etx(B, 1, 4)  # path cost 47
        p = self.p = Parents(net.table)
        self.first = (p.recompute(0), p.rank, p.lowest, p.members())
        # The root falls silent: local repair through B, rank 47.
        net.hear(B, 15, 1500)
        self.second = (p.recompute(2000), p.rank, p.lowest, p.members())

    def test_local_repair_keeps_l(self):
        self.assertEqual(self.first, (PARENT_CHANGED | RANK_CHANGED | ATTACHED, 16, 16, (ROOT, B)))
        self.assertEqual(
            self.second, (PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED, 47, 16, (B,))
        )
        self.assertEqual(self.p.bound(), 16 + MAX_RANK_INCREASE)

    def test_path_cost_l_plus_56_accepted(self):
        self.net.hear(D, 40, 3500)
        self.net.etx(D, 1, 4)  # path cost 72 = L + 56
        self.p.recompute(4000)
        self.assertEqual((self.p.rank, self.p.members()), (72, (D,)))

    def test_path_cost_above_l_plus_56_detaches(self):
        self.net.hear(C, 46, 3500)
        self.net.etx(C, 1, 4)  # path cost 78 > L + 56
        events = self.p.recompute(4000)  # B silent
        self.assertEqual(self.p.rank, RANK_INFINITE)
        self.assertEqual(self.p.members(), ())
        self.assertIsNone(self.p.lowest)
        self.assertEqual(self.p.detaches, 1)
        self.assertEqual(events, DETACHED | PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual(self.net.flagged(), [])
        # Rejoin through a neighbour heard after the poison, at any rank: L restarts.
        self.net.hear(C, 46, 4100)
        events = self.p.recompute(4100)
        self.assertEqual((self.p.rank, self.p.lowest, self.p.members()), (78, 78, (C,)))
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED | ATTACHED)

    def test_bound_prefers_a_usable_candidate(self):
        self.net.hear(C, 46, 3500)
        self.net.etx(C, 1, 4)  # 78: beyond the bound
        self.net.hear(D, 40, 3500)
        self.net.etx(D, 1, 4)  # 72
        self.p.recompute(4000)
        self.assertEqual((self.p.rank, self.p.members()), (72, (D,)))

    def test_max_increase_is_a_parameter(self):
        self.p.max_increase = 30
        self.net.hear(B, 15, 3500)
        self.net.hear(B, 15, 3600)  # B still heard: path cost 47 > 16 + 30
        self.p.recompute(3600)
        self.assertEqual(self.p.rank, RANK_INFINITE)


class MarginTieBreak(unittest.TestCase):
    def test_larger_margin_wins_equal_costs(self):
        net = Net()
        net.hear(A, 16, 0, margin=80)
        net.hear(B, 16, 0, margin=120)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual(p.members(), (B, A))

    def test_margin_decides_the_third_member(self):
        net = Net()
        for address, margin in ((A, 50), (B, 90), (C, 70), (D, 60)):
            net.hear(address, 16, 0, margin=margin)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual(p.members(), (B, C, D))

    def test_cost_before_margin(self):
        net = Net()
        net.hear(A, 15, 0, margin=40)
        net.hear(B, 16, 0, margin=150)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual(p.members(), (A, B))

    def test_lower_address_last_resort(self):
        for order in ((C, A, B), (B, C, A)):
            net = Net()
            for address in order:
                net.hear(address, 16, 0)
            p = Parents(net.table)
            p.recompute(0)
            self.assertEqual(p.members(), (A, B, C))


class NextParent(unittest.TestCase):
    def setUp(self):
        net = self.net = Net()
        net.hear(A, 8, 0)
        net.hear(B, 10, 0)
        net.hear(C, 12, 0)
        self.now = 0
        p = self.p = Parents(net.table, clock=lambda: self.now)
        p.recompute(0)
        p.events()

    def test_set(self):
        self.assertEqual((self.p.members(), self.p.rank), ((A, B, C), 16))

    def test_drop_preferred_uses_next(self):
        self.now = 100
        self.assertEqual(self.p.next_parent(A), B)
        self.assertEqual(self.p.members(), (B, C))
        self.assertEqual(self.p.rank, 18)
        self.assertFalse(self.net.table.get(A).parent)
        self.assertEqual(self.p.drops, 1)
        self.assertEqual(self.p.events(), PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)

    def test_dropped_until_heard_again(self):
        self.p.next_parent(A, 100)
        self.p.recompute(200)
        self.assertFalse(A in self.p.members())
        self.net.hear(A, 8, 300)
        self.p.recompute(300)
        # Back as a candidate, not preferred: 2 better only.
        self.assertEqual(self.p.members(), (B, A, C))

    def test_empty_set_holds_and_detaches(self):
        self.p.next_parent(A, 100)
        self.p.next_parent(B, 100)
        self.assertEqual(self.p.next_parent(C, 100), HOLD)
        self.assertEqual(self.p.rank, RANK_INFINITE)
        self.assertEqual(self.p.members(), ())
        self.assertTrue(self.p.events() & DETACHED)

    def test_reroute_upward_only(self):
        self.now = 100
        up = Message(UPWARD, 1, 20, b"x", origin=0x0042, seq=1)
        down = Message(DOWNWARD, 1, 20, b"x", dest=0x0042, seq=1)
        self.assertIsNone(self.p.reroute(down, A))
        self.assertEqual(self.p.members(), (A, B, C))
        self.assertEqual(self.p.reroute(up, A), B)

    def test_drop_of_another_member_keeps_preferred(self):
        self.assertEqual(self.p.next_parent(B, 100), A)
        self.assertEqual(self.p.members(), (A, C))


class Detach(unittest.TestCase):
    def test_hold_then_neighbours_heard_after_poison(self):
        net = Net()
        net.hear(A, 8, 900)
        net.hear(B, 16, 900)
        p = Parents(net.table, hold_ms=500)
        p.recompute(900)
        self.assertEqual(p.detach(1000), DETACHED | PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual(p.rank, RANK_INFINITE)
        self.assertEqual(net.flagged(), [])
        net.hear(B, 16, 1100)
        self.assertEqual(p.recompute(1499), 0)  # holding
        self.assertEqual(p.rank, RANK_INFINITE)
        events = p.recompute(1500)
        # A, not heard since the poison, is no candidate.
        self.assertEqual((p.members(), p.rank), ((B,), 24))
        self.assertTrue(events & ATTACHED)
        self.assertIsNone(p.poisoned_at)

    def test_detach_without_route_has_no_rank_events(self):
        p = Parents(Net().table)
        self.assertEqual(p.detach(0), DETACHED)


class NewVersion(unittest.TestCase):
    def test_parents_of_new_version_any_rank(self):
        net = Net()
        net.hear(A, 8, 0)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual(p.rank, 16)
        net.hear(B, 100, 100, version=1)
        events = p.on_new_version(17, 100)  # 4 bits: version 1
        self.assertEqual(p.version, 1)
        self.assertEqual((p.members(), p.rank, p.lowest), ((B,), 108, 108))
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertFalse(net.table.get(A).parent)

    def test_no_parent_of_new_version_detaches(self):
        net = Net()
        net.hear(A, 8, 0)
        p = Parents(net.table)
        p.recompute(0)
        events = p.on_new_version(1, 100)
        self.assertEqual(p.rank, RANK_INFINITE)
        self.assertTrue(events & DETACHED)


class Root(unittest.TestCase):
    def test_root_rank_zero_no_parent(self):
        net = Net()
        net.hear(A, 8, 0)
        p = Parents(net.table, root=True)
        self.assertEqual(p.recompute(0), 0)
        self.assertEqual((p.rank, p.members(), p.preferred), (0, (), None))
        self.assertIsNone(p.next_parent(A, 0))
        self.assertEqual(p.on_new_version(3, 0), 0)
        self.assertEqual((p.version, p.rank), (3, 0))
        self.assertEqual(net.flagged(), [])


class Events(unittest.TestCase):
    def test_accumulated_until_read(self):
        net = Net()
        net.hear(A, 8, 0)
        p = Parents(net.table)
        p.recompute(0)
        net.hear(A, 12, 100)
        p.recompute(100)
        self.assertEqual(p.events(), PARENT_CHANGED | RANK_CHANGED | ATTACHED | RANK_INCREASED)
        self.assertEqual(p.events(), 0)


# --- Properties on random neighbour tables -----------------------------------------


def _reference(p, now):
    """Return the expected (rank, members) of p.recompute(now), computed afresh.

    Independent of the module: full sort of every candidate, float rounding.
    Valid without drops (`next_parent`) and with hold_ms = 0.
    """
    table = p.table
    table.expire(now)
    bound = 254 if p.lowest is None else min(254, p.lowest + p.max_increase)
    cands = []
    for n in table.entries():
        if n.version != p.version or n.rank >= p.rank or not n.eligible():
            continue
        if now - n.heard >= 2 * _PERIOD:
            continue
        if p.poisoned_at is not None and n.heard <= p.poisoned_at:
            continue
        cost = n.rank + max(8, int(8 * n.etx() / 128 + 0.5))
        if cost <= bound:
            cands.append((cost, -n.margin(), n.address, n.rank))
    cands.sort()
    top = cands[:3]
    if not top:
        return RANK_INFINITE, ()
    chosen = top[0]
    for key in top:
        if key[2] == p.preferred and key[0] - top[0][0] < 12:
            chosen = key
    rank = p.rank
    if chosen[2] != p.preferred or abs(chosen[0] - rank) >= 4:
        rank = chosen[0]
    members = [chosen[2]] + [k[2] for k in top if k is not chosen and k[3] < rank]
    return rank, tuple(members)


class _Scenario:
    """Random frames, acknowledgements, time and selections on one node."""

    def __init__(self, seed, drops):
        self.rng = XorShift32(seed)
        self.drops = drops
        self.net = Net(capacity=4 + self.below(13))
        self.now = 0
        self.p = Parents(
            self.net.table,
            max_increase=(20, 56)[self.below(2)],
            clock=lambda: self.now,
        )
        self.addresses = [1 + self.below(0xFFFE) for _ in range(6 + self.below(15))]
        self.ranks = {}

    def below(self, n):
        return randbelow(self.rng, n)

    def op(self, test):
        """Run one random operation; return its trace entry."""
        rng = self.rng
        p = self.p
        self.now += self.below(400)
        k = self.below(100)
        if k < 50:
            address = self.addresses[self.below(len(self.addresses))]
            last = self.ranks.get(address)
            if last is not None and last < RANK_INFINITE and self.below(2):
                rank = max(0, last + self.below(11) - 5)  # drift
            elif self.below(10) == 0:
                rank = RANK_INFINITE
            else:
                rank = self.below(160)
            self.ranks[address] = rank
            version = p.version if self.below(8) else (p.version + 1) & 15
            margin = 10 + self.below(140)
            self.net.hear(address, rank, self.now, version, margin, 1 + self.below(3))
        elif k < 60:
            address = self.addresses[self.below(len(self.addresses))]
            self.net.table.acked(address, rng.chance(0.7))
        elif k < 95:
            if self.drops:
                p.recompute(self.now)
            else:
                expected = _reference(p, self.now)
                p.recompute(self.now)
                test.assertEqual((p.rank, p.members()), expected)
            test.check(p)
        elif not self.drops:
            p.detach(self.now)
            test.check(p)
        elif p.preferred is not None:
            hop = p.next_parent(p.preferred)
            test.assertTrue(hop == HOLD or hop == p.preferred)
            test.check(p)
        return (self.now, p.rank, p.members())


class Properties(unittest.TestCase):
    SEEDS = 40
    OPS = 120

    def check(self, p):
        """Invariants right after a selection (recompute, next_parent, detach)."""
        table = p.table
        members = p.members()
        self.assertTrue(len(members) <= SIZE)
        self.assertEqual(len(set(members)), len(members))
        self.assertEqual(p.preferred, members[0] if members else None)
        self.assertEqual(p.rank == RANK_INFINITE, not members)
        for m in members:
            n = table.get(m)
            self.assertIsNotNone(n)
            self.assertTrue(n.rank < p.rank, "parent rank %d >= rank %d" % (n.rank, p.rank))
            self.assertEqual(n.version, p.version)
            self.assertTrue(n.parent)
        flagged = sorted(n.address for n in table.entries() if n.parent)
        self.assertEqual(flagged, sorted(members))
        if members:
            self.assertTrue(p.lowest <= p.rank <= min(254, p.lowest + p.max_increase))
            self.assertTrue(abs(p.cost - p.rank) < 4)

    def run_scenario(self, seed, drops):
        s = _Scenario(seed, drops)
        return [s.op(self) for _ in range(self.OPS)]

    def test_matches_reference_and_invariants(self):
        attached = 0
        for seed in range(self.SEEDS):
            trace = self.run_scenario(seed, False)
            attached += sum(1 for t in trace if t[2])
        # The scenarios do exercise attached states.
        self.assertTrue(attached > self.SEEDS * self.OPS // 4)

    def test_invariants_with_drops(self):
        for seed in range(self.SEEDS):
            self.run_scenario(1000 + seed, True)

    def test_deterministic(self):
        for seed in (7, 8, 9):
            self.assertEqual(self.run_scenario(seed, True), self.run_scenario(seed, True))

    def test_independent_of_slot_order(self):
        rng = XorShift32(42)
        for _ in range(30):
            frames = []
            for i in range(1 + randbelow(rng, 12)):
                frames.append(
                    (
                        0x100 + i,
                        randbelow(rng, 120),
                        10 + randbelow(rng, 140),
                        1 + randbelow(rng, 3),
                        randbelow(rng, 2),
                    )
                )
            results = []
            for order in (frames, frames[::-1]):
                net = Net()
                for address, rank, margin, gap, nack in order:
                    net.hear(address, rank, 0, margin=margin)
                    net.hear(address, rank, 0, margin=margin, gap=gap)
                    if nack:
                        net.table.acked(address, False)
                        net.table.acked(address, True)
                p = Parents(net.table)
                p.recompute(0)
                results.append((p.rank, p.members()))
            self.assertEqual(results[0], results[1])
