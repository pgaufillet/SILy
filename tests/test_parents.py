"""Rank, parent set and preferred parent (MESH_DESIGN §9; MP11, MP12)."""

import unittest

from mesh.frame import DOWNWARD, RANK_INFINITE, ROOT, UPWARD, Message
from mesh.neighbours import ACK_MIN, NeighbourTable
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

    def etx(self, address, acked, total, now=0):
        """Give address an ETX of total / acked through acknowledgement samples at now.

        At least `ACK_MIN` samples (acked and total scaled): fewer give the
        d_in estimate (§9).
        """
        k = (ACK_MIN + total - 1) // total
        for i in range(k * total):
            self.table.acked(address, i < k * acked, now)

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

    def test_heard_within_the_silence_limit(self):
        # Silent from 2.5 periods on (strict, §9).
        self.net.hear(B, 8, 100)
        self.assertEqual(self.cost(B, 100 + 5 * _PERIOD // 2 - 1), 16)
        self.assertIsNone(self.cost(B, 100 + 5 * _PERIOD // 2))

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
        # C silent for 2.5 periods: D takes its place, C loses its flag.
        for address, rank in ((A, 4), (B, 6), (D, 10)):
            net.hear(address, rank, 2000)
        p.recompute(2500)
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
        self.net.etx(B, 1, 2, 100)  # path cost 26, worse than A
        self.p.recompute(100)
        self.assertEqual(self.p.members(), (A, B))
        self.net.hear(B, 10, 2400)
        events = self.p.recompute(2500)  # A silent for 2.5 periods
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
        # The root falls silent (2.5 periods): local repair through B, rank
        # 47. B's failures are renewed so that they do not age out (§9)
        # before the tests below end.
        net.hear(B, 15, 2400)
        net.etx(B, 1, 4, 2400)
        self.second = (p.recompute(2500), p.rank, p.lowest, p.members())

    def test_local_repair_keeps_l(self):
        self.assertEqual(self.first, (PARENT_CHANGED | RANK_CHANGED | ATTACHED, 16, 16, (ROOT, B)))
        self.assertEqual(
            self.second, (PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED, 47, 16, (B,))
        )
        self.assertEqual(self.p.bound(), 16 + MAX_RANK_INCREASE)

    def test_path_cost_l_plus_56_accepted(self):
        self.net.hear(B, 40, 3000)  # ETX 4: path cost 72 = L + 56, followed
        self.p.recompute(3000)
        self.assertEqual((self.p.rank, self.p.members()), (72, (B,)))

    def test_path_cost_above_l_plus_56_detaches(self):
        self.net.hear(B, 46, 3000)  # ETX 4: path cost 78 > L + 56
        events = self.p.recompute(3000)
        self.assertEqual(self.p.rank, RANK_INFINITE)
        self.assertEqual(self.p.members(), ())
        self.assertIsNone(self.p.lowest)
        self.assertEqual(self.p.detaches, 1)
        self.assertEqual(events, DETACHED | PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual(self.net.flagged(), [])
        # Rejoin through a neighbour heard after the poison, at any rank: L restarts.
        self.net.hear(C, 46, 3100)
        self.net.etx(C, 1, 4, 3100)
        events = self.p.recompute(3100)
        self.assertEqual((self.p.rank, self.p.lowest, self.p.members()), (78, 78, (C,)))
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED | ATTACHED)

    def test_bound_prefers_a_usable_candidate(self):
        # Bound 16 + 30 = 46: B (47) and C (47) beyond it, D (46) usable,
        # C and D both below L.
        self.p.max_increase = 30
        self.net.hear(C, 15, 3000)
        self.net.etx(C, 1, 4, 3000)
        self.net.hear(D, 14, 3000)
        self.net.etx(D, 1, 4, 3000)
        self.p.recompute(3000)
        self.assertEqual((self.p.rank, self.p.members()), (46, (D,)))

    def test_max_increase_is_a_parameter(self):
        self.p.max_increase = 30
        self.net.hear(B, 15, 3500)
        self.net.hear(B, 15, 3600)  # B still heard: path cost 47 > 16 + 30
        self.p.recompute(3600)
        self.assertEqual(self.p.rank, RANK_INFINITE)


class RankFollowing(unittest.TestCase):
    """The node follows its preferred parent's rank increase within L + 56 (§9,
    RPL); the other candidates stay strictly below L."""

    def setUp(self):
        # Through A (rank 16, ETX 1): rank 24, L 24, bound 80.
        self.net = Net()
        self.net.hear(A, 16, 0)
        self.p = Parents(self.net.table)
        self.p.recompute(0)

    def step(self, rank, now):
        self.net.hear(A, rank, now)
        return self.p.recompute(now)

    def test_rank_not_below_the_node_followed(self):
        events = self.step(30, 100)
        self.assertEqual((self.p.preferred, self.p.members(), self.p.rank), (A, (A,), 38))
        self.assertEqual(events, RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((self.p.lowest, self.p.detaches), (24, 0))
        self.assertEqual(self.net.flagged(), [A])

    def test_rank_equal_to_the_node_followed(self):
        self.assertEqual(self.step(24, 100), RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((self.p.preferred, self.p.rank), (A, 32))

    def test_other_candidates_strictly_below_the_node(self):
        self.net.hear(B, 24, 50)  # not below 24: no candidate, path cost 32 though
        self.net.hear(C, 23, 50)  # below: candidate, path cost 31
        self.p.recompute(50)
        self.assertEqual(self.p.members(), (A, C))
        self.step(30, 100)  # A's path cost 38: C better by 7 only
        self.assertEqual((self.p.members(), self.p.rank), ((A, C), 38))
        self.assertFalse(self.net.table.get(B).parent)

    def test_better_candidate_still_switches(self):
        self.net.hear(C, 10, 50)  # path cost 18: 6 better than A, kept
        self.p.recompute(50)
        self.assertEqual(self.p.preferred, A)
        events = self.step(30, 100)  # A's path cost 38: C better by 20
        self.assertEqual((self.p.members(), self.p.rank), ((C,), 18))
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED)

    def test_up_to_the_bound_then_detach(self):
        self.step(72, 100)  # path cost 80 = L + 56
        self.assertEqual((self.p.preferred, self.p.rank, self.p.lowest), (A, 80, 24))
        events = self.step(73, 200)  # 81 > L + 56
        self.assertEqual(events, DETACHED | PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((self.p.rank, self.p.members(), self.p.lowest), (RANK_INFINITE, (), None))
        self.assertEqual(self.p.detaches, 1)
        self.assertEqual(self.net.flagged(), [])

    def test_beyond_the_bound_another_candidate_taken(self):
        self.net.hear(B, 20, 50)  # path cost 28
        self.p.recompute(50)
        self.assertEqual(self.p.members(), (A, B))
        self.step(73, 100)  # A beyond L + 56: dropped, B below 24 taken
        self.assertEqual((self.p.members(), self.p.rank), ((B,), 28))
        self.assertEqual(self.p.detaches, 0)

    def test_poisoned_preferred_parent_not_followed(self):
        events = self.step(RANK_INFINITE, 100)
        self.assertTrue(events & DETACHED)
        self.assertEqual(self.p.rank, RANK_INFINITE)

    def test_dropped_preferred_parent_not_followed(self):
        self.net.hear(C, 23, 50)  # another candidate: A is dropped, not kept
        self.p.recompute(50)
        self.net.hear(A, 30, 100)
        self.assertEqual(self.p.next_parent(A, 100), C)
        self.assertEqual((self.p.members(), self.p.rank), ((C,), 31))


class BelowLowest(unittest.TestCase):
    """The candidates other than the preferred parent, and the other members,
    are strictly below L, not below the current rank (§9): after a rank
    increase, a neighbour between L and the new rank may be a descendant that
    has not heard the increase yet, so taking it may close a cycle (TLA+
    `DODAG_chain_current_loop`, `Thr = "low"`)."""

    def setUp(self):
        # Through A (rank 16, ETX 1): rank 24, L 24. B at 28 (above L), C at
        # 23 (below L).
        self.net = Net()
        self.net.hear(A, 16, 0)
        self.p = Parents(self.net.table)
        self.p.recompute(0)
        self.net.hear(A, 16, 50)
        self.net.hear(B, 28, 50)
        self.net.hear(C, 23, 50)
        self.p.recompute(50)
        self.assertEqual((self.p.members(), self.p.rank, self.p.lowest), ((A, C), 24, 24))

    def cost(self, address, now):
        return self.p.candidate_cost(self.net.table.get(address), now)

    def test_rank_rise_keeps_neighbours_above_l_out(self):
        self.net.etx(A, 1, 3, 100)  # ETX 3: path cost 16 + 24 = 40
        events = self.p.recompute(100)
        self.assertEqual(events, RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((self.p.preferred, self.p.rank, self.p.lowest), (A, 40, 24))
        # B, 28 < 40 but not below L: neither candidate nor member; C still both.
        self.assertIsNone(self.cost(B, 100))
        self.assertEqual(self.cost(C, 100), 31)
        self.assertEqual(self.p.members(), (A, C))
        self.assertEqual(self.net.flagged(), sorted((A, C)))

    def test_preferred_parent_lost_after_a_rise(self):
        self.net.etx(A, 1, 3, 100)
        self.p.recompute(100)
        self.net.etx(A, 0, 1, 200)  # ETX 5: A no longer eligible
        events = self.p.recompute(200)
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED)
        self.assertEqual((self.p.members(), self.p.rank, self.p.lowest), ((C,), 31, 24))
        self.assertFalse(self.net.table.get(B).parent)

    def test_only_neighbours_above_l_detaches(self):
        self.net.hear(B, 28, 2550)
        self.net.hear(A, 16, 2550)
        self.net.etx(A, 1, 3, 2550)
        self.p.recompute(2550)  # C silent for 2.5 periods since 50
        self.assertEqual((self.p.members(), self.p.rank, self.p.lowest), ((A,), 40, 24))
        # ETX 5, A the last candidate: kept while reachable (acknowledged at
        # 2550), its cost capped at 32.
        self.net.etx(A, 0, 1, 2600)
        self.p.recompute(2600)
        self.assertEqual((self.p.members(), self.p.rank, self.p.lowest), ((A,), 48, 24))
        # Unreachable a silence limit after its last acknowledgement: B
        # (28 < 48) refused.
        self.net.hear(B, 28, 5000)
        self.net.hear(A, 16, 5000)
        events = self.p.recompute(2550 + 5 * _PERIOD // 2)
        self.assertEqual(events, DETACHED | PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((self.p.members(), self.p.rank, self.p.lowest), ((), RANK_INFINITE, None))


class FollowDiamond(unittest.TestCase):
    """Diamond root - A, root - B, A - C, B - C (ETX 2), C - D (TLA+
    `DODAG_follow_follow_*`): A dies, C falls back on B at rank 24, D's
    rank; D, whose only neighbour C is, keeps C and follows."""

    def test_child_follows_a_local_repair(self):
        c_net = Net()
        c_net.hear(A, 8, 0)
        c_net.hear(B, 8, 0)
        c = Parents(c_net.table)
        c.recompute(0)
        d_net = Net()
        d_net.hear(C, c.rank, 0)
        d = Parents(d_net.table)
        d.recompute(0)
        self.assertEqual((c.members(), c.rank, d.rank), ((A, B), 16, 24))
        # A falls silent; B heard, over a link of ETX 2 (failures renewed so
        # that they do not age out).
        c_net.hear(B, 8, 2400)
        c_net.etx(B, 1, 2, 2400)
        c.recompute(2500)
        self.assertEqual((c.preferred, c.rank), (B, 24))
        d_net.hear(C, c.rank, 2600)
        events = d.recompute(2600)
        self.assertEqual((d.preferred, d.rank, d.detaches), (C, 32, 0))
        self.assertEqual(events, RANK_CHANGED | RANK_INCREASED)


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
        # C, the last candidate, is kept while it is one apart from drops
        # (KeepLast); silent, it is dropped.
        self.assertEqual(self.p.next_parent(C, 100), C)
        self.assertEqual(self.p.next_parent(C, 5 * _PERIOD // 2), HOLD)
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


class KeepRoot(unittest.TestCase):
    """§12 upward failure: the root is kept after exhausted attempts while it
    is a candidate apart from drops (TLA+ `DODAG`, Exhaust = "keeproot")."""

    def setUp(self):
        # A root child: the root (ETX 1) is its only candidate under L.
        net = self.net = Net()
        net.hear(ROOT, 0, 0)
        self.now = 0
        p = self.p = Parents(net.table, clock=lambda: self.now)
        p.recompute(0)
        p.events()
        self.assertEqual((p.members(), p.rank), ((ROOT,), 8))

    def _kept(self, hop):
        p = self.p
        self.assertEqual(hop, ROOT)
        self.assertEqual((p.members(), p.rank, p.drops, p.detaches), ((ROOT,), 8, 0, 0))
        self.assertEqual(p.events(), 0)
        self.assertTrue(self.net.table.get(ROOT).parent)

    def _left(self, hop):
        p = self.p
        self.assertEqual(hop, HOLD)
        self.assertEqual((p.members(), p.rank, p.drops), ((), RANK_INFINITE, 1))
        self.assertTrue(p.events() & DETACHED)

    def test_heard_and_eligible_root_kept(self):
        self.now = 100
        self._kept(self.p.next_parent(ROOT))
        # Again and again while it stays a candidate: no detach.
        self.now = 2 * _PERIOD
        self._kept(self.p.next_parent(ROOT))
        self._kept(self.p.reroute(Message(UPWARD, 1, 20, b"x", origin=A, seq=1), ROOT))

    def test_root_kept_whatever_the_other_candidates(self):
        # ETX 2 to the root: rank 16; B (rank 8, 16 through it) a candidate.
        net = Net()
        net.hear(ROOT, 0, 0)
        net.etx(ROOT, 1, 2)
        net.hear(B, 8, 0)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual((p.members(), p.rank), ((ROOT, B), 16))
        self.assertEqual(p.next_parent(ROOT, 20), ROOT)
        self.assertEqual((p.members(), p.drops), ((ROOT, B), 0))

    def _failing(self, until):
        # Beacons of the root every 500 ms up to until; R's frames to it fail
        # from 100 on, one failed sample per beacon interval, after its last
        # acknowledgement at 50.
        net = self.net
        net.table.acked(ROOT, True, 50)
        t = 100
        while t <= until:
            net.hear(ROOT, 0, t)
            net.table.acked(ROOT, False, t)
            t += 500

    def test_unreachable_root_left(self):
        # 4 consecutive failed samples by 1600, but kept until a silence
        # limit since its last acknowledgement (§9 reachability).
        silence = 5 * _PERIOD // 2
        self._failing(2100)
        self.assertEqual(self.net.table.get(ROOT).run, 4)
        self.now = 50 + silence - 1
        self._kept(self.p.next_parent(ROOT))
        self._left(self.p.next_parent(ROOT, 50 + silence))

    def test_unreachable_root_left_at_a_selection(self):
        # No exhausted attempt needed: the next selection (a beacon) leaves it.
        p = self.p
        self._failing(2600)
        events = p.recompute(2600)
        self.assertEqual(events, DETACHED | PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((p.members(), p.rank, p.drops), ((), RANK_INFINITE, 0))

    def test_reachable_root_kept_above_etx_4_its_cost_capped(self):
        # ETX 9 to the root, acknowledged within the silence limit: kept,
        # at a link cost of 32 (ETX 4), not 72 beyond the bound L + 56 = 64.
        p = self.p
        net = self.net
        net.etx(ROOT, 1, 9, 200)
        net.hear(ROOT, 0, 300)
        self.assertEqual(net.table.get(ROOT).etx(), 9 * 128)
        self.assertEqual(p.bound(), 64)
        events = p.recompute(300)
        self.assertEqual((p.members(), p.rank, p.cost, p.lowest), ((ROOT,), 32, 32, 8))
        self.assertEqual(events, RANK_CHANGED | RANK_INCREASED)
        self.now = 400
        self.assertEqual(p.next_parent(ROOT), ROOT)
        self.assertEqual((p.drops, p.detaches), (0, 0))

    def test_cap_only_towards_the_root(self):
        # The same ETX 9 on a relay link: ineligible (ETX > 4).
        net = Net()
        net.hear(A, 8, 0)
        net.etx(A, 1, 9, 0)
        p = Parents(net.table)
        p.recompute(10)
        self.assertEqual(p.members(), ())
        # At ETX <= 4 towards the root, the cost of its ETX (no cap).
        net.hear(ROOT, 0, 20)
        net.etx(ROOT, 1, 3, 20)
        p.recompute(30)
        self.assertEqual((p.members(), p.rank), ((ROOT,), 24))

    def test_silent_root_left(self):
        # Silent from 2.5 periods on (strict, §9).
        self._left(self.p.next_parent(ROOT, 5 * _PERIOD // 2))

    def test_root_of_another_version_left(self):
        self.net.hear(ROOT, 0, 50, version=1)  # not behind: not a candidate
        self._left(self.p.next_parent(ROOT, 100))

    def test_relay_parent_with_another_candidate_still_dropped(self):
        # A relay parent is kept only as the last candidate (KeepLast).
        net = Net()
        net.hear(A, 8, 0)
        net.hear(B, 10, 0)
        p = Parents(net.table)
        p.recompute(0)
        self.assertEqual(p.next_parent(A, 100), B)
        self.assertEqual((p.members(), p.drops, p.rank), ((B,), 1, 18))


class KeepLast(unittest.TestCase):
    """§12 upward failure, §9 eligibility: the preferred parent that is the
    node's last candidate is kept after exhausted attempts, and its link is
    judged by reachability, not ETX <= 4 (TLA+ `DODAG`, Exhaust =
    "keepboth"; the reachability limited to the last candidate, MP12)."""

    def setUp(self):
        # A hop-2 node: relay A (rank 8, ETX 1) its only candidate; rank 16,
        # L 16, bound 72.
        net = self.net = Net()
        net.hear(A, 8, 0)
        self.now = 0
        p = self.p = Parents(net.table, clock=lambda: self.now)
        p.recompute(0)
        p.events()
        self.assertEqual((p.members(), p.rank, p.bound()), ((A,), 16, 72))

    def _kept(self, hop):
        p = self.p
        self.assertEqual(hop, A)
        self.assertEqual((p.members(), p.drops, p.detaches), ((A,), 0, 0))
        self.assertTrue(self.net.table.get(A).parent)

    def test_last_candidate_kept_after_exhausted_attempts(self):
        self.now = 100
        self._kept(self.p.next_parent(A))
        self.assertEqual((self.p.rank, self.p.events()), (16, 0))
        self.now = 2 * _PERIOD  # again, while it stays a candidate
        self._kept(self.p.next_parent(A))

    def test_decided_at_each_exhaustion(self):
        # Kept, then B appears (rank 10, path cost 18, a gain below the switch
        # threshold): the next exhaustion drops A, the message goes to B.
        p = self.p
        self._kept(p.next_parent(A, 100))
        self.net.hear(B, 10, 300)
        p.recompute(300)
        self.assertEqual(p.members(), (A, B))
        self.assertEqual(p.next_parent(A, 400), B)
        self.assertEqual((p.members(), p.drops, p.rank), ((B,), 1, 18))
        # B, now the last candidate (A dropped until heard again): kept.
        self.assertEqual(p.next_parent(B, 500), B)
        self.assertEqual((p.members(), p.drops), ((B,), 1))

    def _failing(self, until):
        # A heard every 500 ms up to until; the node's frames to it fail from
        # 100 on, one failed sample per interval, after its last
        # acknowledgement at 50: ETX above 4 from the 4th failure.
        net = self.net
        net.table.acked(A, True, 50)
        t = 100
        while t <= until:
            net.hear(A, 8, t)
            net.table.acked(A, False, t)
            t += 500

    def test_eligible_above_etx_4_while_it_answers(self):
        # ETX 5 by 1600, 4 consecutive failed samples: kept, at a link cost
        # capped at 32, until a silence limit since its last acknowledgement.
        p = self.p
        silence = 5 * _PERIOD // 2
        self._failing(2100)
        n = self.net.table.get(A)
        self.assertEqual((n.etx(), n.run), (6 * 128, 4))
        p.recompute(50 + silence - 1)
        self.assertEqual((p.members(), p.rank, p.cost), ((A,), 40, 40))
        self._kept(p.next_parent(A, 50 + silence - 1))
        events = p.recompute(50 + silence)
        self.assertEqual(events, DETACHED | PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((p.members(), p.drops), ((), 0))

    def test_last_candidate_judged_by_reachability_not_etx(self):
        # ETX 2 but unreachable: left (a dead uplink is left without waiting
        # for its ETX to pass 4); with another candidate, judged by ETX: kept.
        p = self.p
        silence = 5 * _PERIOD // 2
        self.net.etx(A, 1, 1, 40)
        self._failing(2100)
        n = self.net.table.get(A)
        self.assertEqual((n.etx(), n.run), (2 * 128, 4))
        p.recompute(50 + silence - 1)
        self.assertEqual((p.members(), p.rank), ((A,), 24))
        self.assertTrue(p.recompute(50 + silence) & DETACHED)
        net = Net()
        net.hear(A, 8, 0)
        net.hear(B, 10, 0)
        q = Parents(net.table)
        q.recompute(0)
        net.etx(A, 1, 1, 40)
        net.table.acked(A, True, 50)
        for t in (100, 600, 1100, 1600, 2100):
            net.hear(A, 8, t)
            net.hear(B, 10, t)
            net.table.acked(A, False, t)
        q.recompute(50 + silence)
        self.assertEqual((q.members(), q.detaches), ((A, B), 0))

    def test_parent_taken_again_restarts_its_reachability(self):
        # A left unreachable at ETX 2; heard after the poison, taken again by
        # ETX <= 4: its probes restart (as a new entry), so the next
        # selection, A the last candidate, does not leave it at once.
        p = self.p
        silence = 5 * _PERIOD // 2
        self.net.etx(A, 1, 1, 40)
        self._failing(2100)
        self.assertTrue(p.recompute(50 + silence) & DETACHED)
        self.net.hear(A, 8, 2560)
        self.assertTrue(p.recompute(2560) & ATTACHED)
        n = self.net.table.get(A)
        self.assertEqual((p.members(), n.run, n.answered), ((A,), 0, 2560))
        self.net.hear(A, 8, 2570)
        self.assertEqual((p.recompute(2570), p.members(), p.detaches), (0, (A,), 1))

    def test_unreachable_last_candidate_dropped_at_an_exhaustion(self):
        p = self.p
        self._failing(2100)
        self.now = 50 + 5 * _PERIOD // 2
        self.assertEqual(p.next_parent(A), HOLD)
        self.assertEqual((p.members(), p.drops, p.rank), ((), 1, RANK_INFINITE))

    def test_reachable_last_candidate_kept_above_etx_4_its_cost_capped(self):
        # ETX 9, acknowledged within the silence limit: link cost 32 (path
        # cost 40), not 72 (path cost 80, beyond the bound 72).
        p = self.p
        self.net.etx(A, 1, 9, 200)
        self.net.hear(A, 8, 300)
        self.assertEqual(self.net.table.get(A).etx(), 9 * 128)
        events = p.recompute(300)
        self.assertEqual((p.members(), p.rank, p.cost, p.lowest), ((A,), 40, 40, 16))
        self.assertEqual(events, RANK_CHANGED | RANK_INCREASED)

    def test_relay_with_another_candidate_ineligible_above_etx_4(self):
        # MP12: B (path cost 18, ETX 1) a candidate: A's link at ETX 5 is
        # judged by ETX: A leaves, even if reachable.
        p = self.p
        self.net.hear(B, 10, 50)
        p.recompute(50)
        self.assertEqual(p.members(), (A, B))
        self.net.etx(A, 1, 5, 100)
        self.assertTrue(self.net.table.get(A).reachable(100, 5 * _PERIOD // 2))
        events = p.recompute(100)
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED)
        self.assertEqual((p.members(), p.rank), ((B,), 18))

    def test_old_parent_back_only_at_etx_4(self):
        # Once the node switched away from A (ETX 5), A is no longer the
        # preferred parent: B gone, A (reachable) is no candidate, the node
        # detaches; at ETX <= 4 it is one again.
        p = self.p
        self.net.hear(B, 10, 50)
        p.recompute(50)
        self.net.etx(A, 1, 5, 100)
        p.recompute(100)
        self.assertEqual(p.members(), (B,))
        # B silent since 50; A heard before its failures age out (at 2600).
        self.net.hear(A, 8, 2560)
        self.assertTrue(p.recompute(2560) & DETACHED)
        self.net.hear(A, 8, 2570)  # heard after the poison
        self.assertIsNone(p.candidate_cost(self.net.table.get(A), 2570))
        for _ in range(16):
            self.net.table.acked(A, True, 2570)
        self.assertEqual(p.candidate_cost(self.net.table.get(A), 2570), 16)


class Detach(unittest.TestCase):
    def test_hold_until_the_solicitation_then_neighbours_heard_after_it(self):
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
        self.assertFalse(p.solicited(1499))  # a solicitation within the hold ends nothing
        self.assertEqual(p.rank, RANK_INFINITE)
        # The hold's time is over: still nothing selected until the
        # solicitation goes on air (B, heard during the hold, may be a
        # child that lost the poison).
        self.assertEqual(p.recompute(1500), 0)
        self.assertEqual(p.recompute(3000), 0)
        self.assertEqual((p.rank, p.poisoned_at), (RANK_INFINITE, 1000))
        self.assertEqual(p.next_parent(A, 3000), HOLD)
        self.assertTrue(p.solicited(3100))
        self.assertEqual(p.poisoned_at, 3100)
        self.assertEqual(p.recompute(3100), 0)  # B heard before the solicitation
        net.hear(A, 8, 3200)
        net.hear(B, 16, 3200)
        events = p.recompute(3200)
        self.assertEqual((p.members(), p.rank), ((A,), 16))
        self.assertTrue(events & ATTACHED)
        self.assertIsNone(p.poisoned_at)
        self.assertFalse(p.solicited(3300))  # attached: nothing to end

    def test_solicitation_before_the_selection_ends_the_hold(self):
        # The solicitation may go on air in the step that ends the hold,
        # before any selection: it ends the hold all the same.
        net = Net()
        net.hear(A, 8, 900)
        p = Parents(net.table, hold_ms=500)
        p.recompute(900)
        p.detach(1000)
        net.hear(A, 8, 1400)
        self.assertTrue(p.solicited(1500))
        self.assertEqual(p.recompute(1500), 0)
        net.hear(A, 8, 1600)
        self.assertTrue(p.recompute(1600) & ATTACHED)
        self.assertEqual(p.preferred, A)

    def test_new_version_keeps_the_hold_and_its_rejoin_rule(self):
        # B, heard during the hold, may be a descendant that lost the
        # poison, whatever its version (TLA+ `Adopt = "fresh"`).
        net = Net()
        net.hear(A, 8, 900)
        p = Parents(net.table, hold_ms=500)
        p.recompute(900)
        p.detach(1000)
        net.hear(B, 8, 1100, version=1)
        self.assertEqual(p.on_new_version(1, 1100), 0)
        self.assertEqual((p.version, p.rank, p.hold_until), (1, RANK_INFINITE, 1500))
        p.recompute(1600)
        self.assertTrue(p.soliciting)
        self.assertEqual(p.on_new_version(2, 1650), 0)
        self.assertTrue(p.soliciting)
        self.assertTrue(p.solicited(1700))
        # Adopted after the solicitation: B (heard before it) is no
        # candidate, C (heard after it) is, at any rank.
        net.hear(B, 8, 1650, version=3)
        net.hear(C, 40, 1800, version=3)
        self.assertTrue(p.on_new_version(3, 1800) & ATTACHED)
        self.assertEqual((p.members(), p.rank, p.poisoned_at), ((C,), 48, None))

    def test_detach_without_route_has_no_rank_events(self):
        p = Parents(Net().table)
        self.assertEqual(p.detach(0), DETACHED)


class AnswerWindow(unittest.TestCase):
    """Every solicitation of a detached node opens an answer window: no
    selection until its close, then the best candidate heard after the
    solicitation that ended the hold (TLA+ `Rejoin = "window"`, RejoinBest)."""

    def detached(self):
        # Attached through A, detached at 1000; the hold (500 ms) ends at
        # the solicitation of 1500, which opens the window (300 ms).
        self.net = Net()
        self.net.hear(A, 8, 900)
        p = Parents(self.net.table, hold_ms=500, window_ms=300)
        p.recompute(900)
        p.detach(1000)
        self.assertIsNone(p.window_until)
        self.assertTrue(p.solicited(1500))
        self.assertEqual((p.poisoned_at, p.window_until), (1500, 1800))
        return p

    def test_best_candidate_heard_in_the_window_taken_at_its_close(self):
        p = self.detached()
        self.net.hear(D, 0, 1400)  # heard before the solicitation: no candidate
        self.net.hear(B, 24, 1550)  # first answer: path cost 32
        self.assertEqual(p.recompute(1550), 0)
        self.net.hear(C, 16, 1700)  # later and better by 8, less than the switch threshold
        self.assertEqual(p.recompute(1799), 0)
        self.assertEqual((p.preferred, p.rank), (None, RANK_INFINITE))
        self.assertEqual(p.next_parent(B, 1700), HOLD)  # a class 1 drop selects nothing either
        self.net.hear(B, 24, 1750)
        events = p.recompute(1800)
        self.assertTrue(events & ATTACHED)
        self.assertEqual((p.members(), p.rank, p.window_until), ((C,), 24, None))

    def test_empty_window_then_first_candidate_heard(self):
        p = self.detached()
        self.assertEqual(p.recompute(1800), 0)
        self.assertIsNone(p.window_until)
        self.net.hear(B, 24, 3500)  # A silent by now
        self.assertTrue(p.recompute(3500) & ATTACHED)
        self.assertEqual((p.preferred, p.rank), (B, 32))
        # A better candidate heard afterwards needs the switch threshold.
        self.net.hear(C, 16, 3550)
        p.recompute(3550)
        self.assertEqual((p.preferred, p.rank), (B, 32))

    def test_periodic_and_boot_solicitations_open_a_window(self):
        # At boot (no hold): only the window, no rejoin rule.
        net = Net()
        p = Parents(net.table, hold_ms=500, window_ms=300)
        net.hear(B, 24, 50)
        self.assertFalse(p.solicited(100))
        self.assertEqual((p.window_until, p.poisoned_at), (400, None))
        net.hear(C, 16, 200)
        self.assertEqual(p.recompute(399), 0)
        self.assertTrue(p.recompute(400) & ATTACHED)
        self.assertEqual(p.preferred, C)
        # Periodic, after a window closed empty: a new one; the rejoin rule
        # stays that of the hold's solicitation.
        p = self.detached()
        self.assertEqual(p.recompute(1800), 0)
        self.net.hear(D, 0, 1400)  # heard before the hold's solicitation
        self.assertFalse(p.solicited(2000))
        self.assertEqual((p.window_until, p.poisoned_at), (2300, 1500))
        self.net.hear(B, 24, 2100)
        self.assertEqual(p.recompute(2100), 0)
        self.net.hear(C, 16, 2200)
        self.assertTrue(p.recompute(2300) & ATTACHED)
        self.assertEqual((p.preferred, p.rank), (C, 24))

    def test_window_neither_opened_in_the_hold_nor_extended_nor_kept_by_a_detach(self):
        self.net = Net()
        self.net.hear(A, 8, 900)
        p = Parents(self.net.table, hold_ms=500, window_ms=300)
        p.recompute(900)
        self.assertFalse(p.solicited(950))  # attached (behind a newer version)
        self.assertIsNone(p.window_until)
        p.detach(1000)
        self.assertFalse(p.solicited(1200))  # in the hold
        self.assertIsNone(p.window_until)
        self.assertTrue(p.solicited(1500))
        self.assertFalse(p.solicited(1700))  # another version heard: not extended
        self.assertEqual(p.window_until, 1800)
        self.net.hear(A, 8, 1750)
        p.recompute(1800)
        self.assertEqual(p.preferred, A)
        p.detach(2000)
        self.assertEqual((p.window_until, p.hold_until), (None, 2500))

    def test_new_version_in_the_window_waits_for_its_close(self):
        # Selecting at the adoption would take the first answer of the new
        # version (TLA+ `Rejoin = "window_rejoin"` violates RejoinBest).
        p = self.detached()
        self.net.hear(B, 24, 1600, version=1)
        self.assertEqual(p.on_new_version(1, 1600), 0)
        self.assertEqual((p.version, p.rank, p.window_until, p.poisoned_at), (1, 255, 1800, 1500))
        self.net.hear(D, 0, 1400, version=1)  # heard before the solicitation: no candidate
        self.net.hear(C, 16, 1700, version=1)
        self.assertEqual(p.recompute(1700), 0)
        self.assertTrue(p.recompute(1800) & ATTACHED)
        self.assertEqual((p.preferred, p.rank), (C, 24))


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


class Behind(unittest.TestCase):
    """A node behind a newer version keeps its preferred parent once that
    parent has moved to it, at its own rank (§9; TLA+ `Behind = "keep"`)."""

    def setUp(self):
        # Version 1 through A (rank 16, ETX 1): rank 24.
        self.net = Net()
        self.p = Parents(self.net.table)
        self.p.on_new_version(1, 0)
        self.net.hear(A, 16, 0, version=1)
        self.p.recompute(0)
        self.assertEqual((self.p.preferred, self.p.rank), (A, 24))

    def cost(self, address, now):
        return self.p.candidate_cost(self.net.table.get(address), now)

    def test_preferred_parent_of_a_newer_version_kept_at_the_own_rank(self):
        self.net.hear(A, 100, 100, version=2)  # its rank in version 2: not compared
        self.p.behind = True
        self.assertEqual(self.cost(A, 100), 24)
        self.assertEqual(self.p.recompute(100), 0)
        self.assertEqual((self.p.members(), self.p.rank, self.p.lowest), ((A,), 24, 24))
        self.assertEqual(self.p.detaches, 0)

    def test_not_behind_the_parent_of_another_version_is_lost(self):
        self.net.hear(A, 16, 100, version=2)
        self.assertIsNone(self.cost(A, 100))
        self.assertTrue(self.p.recompute(100) & DETACHED)

    def test_only_the_preferred_parent_and_only_newer(self):
        self.p.behind = True
        self.net.hear(B, 4, 100, version=2)  # not the preferred parent
        self.assertIsNone(self.cost(B, 100))
        self.net.hear(A, 16, 100, version=0)  # older
        self.assertIsNone(self.cost(A, 100))
        self.net.hear(A, 16, 200, version=9)  # 8 apart: not comparable
        self.assertIsNone(self.cost(A, 200))
        self.net.hear(A, 16, 300, version=8)  # 7 ahead: newer
        self.assertEqual(self.cost(A, 300), 24)

    def test_poisoned_silent_or_dropped_parent_not_kept(self):
        self.p.behind = True
        self.net.hear(A, RANK_INFINITE, 100, version=2)
        self.assertIsNone(self.cost(A, 100))
        self.net.hear(A, 16, 200, version=2)
        self.assertIsNone(self.cost(A, 200 + 5 * _PERIOD // 2))
        self.assertEqual(self.p.next_parent(A, 300), A)  # the last candidate: kept
        self.net.hear(C, 20, 300, version=1)
        self.assertEqual(self.p.next_parent(A, 300), C)  # another one: dropped
        self.assertIsNone(self.cost(A, 300))
        self.assertEqual(self.p.next_parent(C, 300 + 5 * _PERIOD // 2), HOLD)  # silent
        self.assertEqual(self.p.rank, RANK_INFINITE)
        self.assertFalse(self.p.behind)  # cleared by the detach

    def test_better_candidate_of_the_own_version_switches(self):
        self.p.behind = True
        self.net.hear(A, 2, 100, version=2)
        self.net.hear(C, 2, 100, version=1)  # path cost 10: better by 14
        events = self.p.recompute(100)
        self.assertEqual(events, PARENT_CHANGED | RANK_CHANGED)
        # A, of another version, is no secondary member (rank 2 not compared).
        self.assertEqual((self.p.members(), self.p.rank), ((C,), 10))
        self.assertEqual(self.net.flagged(), [C])

    def test_secondary_members_of_the_own_version(self):
        self.p.behind = True
        self.net.hear(A, 30, 100, version=2)
        self.net.hear(C, 10, 100, version=1)  # path cost 18: better by 6 only
        self.p.recompute(100)
        self.assertEqual((self.p.members(), self.p.rank), ((A, C), 24))

    def test_new_version_clears_it(self):
        self.p.behind = True
        self.net.hear(A, 16, 100, version=2)
        self.p.on_new_version(2, 100)
        self.assertEqual((self.p.behind, self.p.preferred, self.p.rank), (False, A, 24))


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
    low = RANK_INFINITE if p.lowest is None else p.lowest

    def candidate(n, by_reach):
        # By reachability: fewer than 4 failed samples in a row, or an
        # acknowledgement within the silence limit (§9), its ETX capped at
        # 4; otherwise ETX <= 4.
        if by_reach:
            reach = n.run < 4 or now - n.answered < 5 * _PERIOD // 2
            eligible = n.margin_ok and n.m_count > 0 and reach
        else:
            eligible = n.margin_ok and n.m_count > 0 and n.etx() <= 4 * 128
        if n.version != p.version or not eligible:
            return None
        if n.rank >= low and n.address != p.preferred:
            return None
        if now - n.heard >= 5 * _PERIOD // 2:
            return None
        if p.poisoned_at is not None and n.heard <= p.poisoned_at:
            return None
        etx = min(n.etx(), 4 * 128) if by_reach else n.etx()
        cost = n.rank + max(8, int(8 * etx / 128 + 0.5))
        return (cost, -n.margin(), n.address, n.rank) if cost <= bound else None

    # The others first (the root by reachability, the rest by ETX), then the
    # preferred parent, by reachability if it is the root or no other is a
    # candidate.
    cands = []
    preferred = None
    for n in table.entries():
        if n.address == p.preferred:
            preferred = n
            continue
        key = candidate(n, n.address == ROOT)
        if key is not None:
            cands.append(key)
    if preferred is not None:
        key = candidate(preferred, preferred.address == ROOT or not cands)
        if key is not None:
            cands.append(key)
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
    low = min(low, rank)
    members = [chosen[2]] + [k[2] for k in top if k is not chosen and k[3] < low]
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
        if self.below(2):
            self.addresses.append(ROOT)  # its link judged by reachability
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
            self.net.table.acked(address, rng.chance(0.7), self.now)
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
            if m != p.preferred:
                self.assertTrue(n.rank < p.lowest, "member rank %d >= L %d" % (n.rank, p.lowest))
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
                        net.table.acked(address, False, 0)
                        net.table.acked(address, True, 0)
                p = Parents(net.table)
                p.recompute(0)
                results.append((p.rank, p.members()))
            self.assertEqual(results[0], results[1])
