"""Topologies of large simulated networks: the hand-placed course."""

import unittest

from sim import topologies
from sim.net import Network

_KEY = bytes(range(16))
_BOARDS = 15  # the root and the 14 Nodes of the course


def _neighbours(t):
    out = dict((n, set()) for n in t.names)
    for a, b, _, _ in t.links:
        out[a].add(b)
        out[b].add(a)
    return out


class Course(unittest.TestCase):
    def setUp(self):
        self.t = topologies.make("course", _BOARDS, 1)

    def test_every_board_reached_within_three_hops(self):
        self.assertEqual(len(self.t.names), _BOARDS)
        self.assertEqual(sorted(self.t.hops), sorted(self.t.names))
        self.assertEqual(self.t.hops[self.t.names[0]], 0)
        self.assertTrue(2 <= self.t.depth <= 3, self.t.depth)

    def test_no_single_relay(self):
        first = [n for n in self.t.names if self.t.hops[n] == 1]
        self.assertTrue(len(first) >= 3, first)

    def test_two_possible_parents_beyond_hop_one(self):
        adj = _neighbours(self.t)
        for n in self.t.names:
            h = self.t.hops[n]
            if h >= 2:
                closer = [m for m in adj[n] if self.t.hops[m] == h - 1]
                self.assertTrue(len(closer) >= 2, (hex(n), closer))

    def test_hidden_terminals(self):
        # Nodes out of each other's range, beyond the root's.
        adj = _neighbours(self.t)
        nodes = self.t.names[1:]
        hidden = [(a, b) for a in nodes for b in nodes if a < b and b not in adj[a]]
        self.assertTrue(hidden)
        # Among them, Nodes at hop 1 hearing the root but not each other.
        first = [n for n in nodes if self.t.hops[n] == 1]
        self.assertTrue(any(b not in adj[a] for a in first for b in first if a < b))

    def test_positions_in_the_unit_square(self):
        self.assertEqual(sorted(self.t.positions), sorted(self.t.names))
        for x, y in self.t.positions.values():
            self.assertTrue(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0, (x, y))

    def test_links_from_the_distance(self):
        # One range: linked when closer than it, weaker with the distance.
        p = self.t.positions
        linked = {}
        for a, b, rssi, snr in self.t.links:
            linked[(a, b)] = (rssi, snr)
        for i, a in enumerate(self.t.names):
            for b in self.t.names[i + 1 :]:
                d = ((p[a][0] - p[b][0]) ** 2 + (p[a][1] - p[b][1]) ** 2) ** 0.5
                self.assertEqual((a, b) in linked, d < topologies.COURSE_RANGE, (a, b))
                if (a, b) in linked:
                    rssi, snr = linked[(a, b)]
                    self.assertTrue(-100 <= rssi <= -70 and 0 <= snr <= 10, (rssi, snr))

    def test_links_once_per_pair_applied_both_ways(self):
        pairs = [frozenset((a, b)) for a, b, _, _ in self.t.links]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertTrue(all(len(p) == 2 for p in pairs))
        net = Network(1, _KEY)
        for name in self.t.names:
            net.add(name)
        self.t.apply(net)
        self.assertEqual(len(net.medium.links()), 2 * len(self.t.links))
        for a, b, rssi, snr in self.t.links:
            for src, dst in ((a, b), (b, a)):
                link = net.medium.get_link(src, dst)
                self.assertEqual((link.rssi, link.snr, link.loss), (rssi, snr, 0.0))

    def test_deterministic(self):
        self.assertEqual(topologies.make("course", _BOARDS, 2).links, self.t.links)

    def test_smaller_courses_keep_two_parents(self):
        for n in range(2, _BOARDS):
            t = topologies.course(n)
            adj = _neighbours(t)
            self.assertEqual(len(t.hops), n)
            self.assertEqual(t.links, [x for x in self.t.links if x[1] in t.names])
            for m in t.names:
                h = t.hops[m]
                if h >= 2:
                    self.assertTrue(sum(1 for k in adj[m] if t.hops[k] == h - 1) >= 2, (n, m))

    def test_larger_course_refused(self):
        with self.assertRaises(ValueError):
            topologies.course(_BOARDS + 1)
