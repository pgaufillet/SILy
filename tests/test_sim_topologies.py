"""Topologies of large simulated networks: the hand-placed course, the star domain, star2b."""

import unittest

from mesh.dodag import DodagInfo
from mesh.frame import ROOT
from mesh.neighbours import NeighbourTable
from mesh.profile import EU_869
from mesh.routing import Routing
from sim import topologies
from sim.net import Network

_KEY = bytes(range(16))
_BOARDS = 15  # the root and the 14 Nodes of the course
_STAR = 30  # the root, the relay and 28 Nodes behind it
_SF = 8  # the large-network suite's


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


class StarSplit(unittest.TestCase):
    """star2b: two relays next to the root, each with its own half of the Nodes."""

    def setUp(self):
        self.t = topologies.make("star2b", _STAR + 1, 1)  # the suite's 30 Nodes and the root
        self.adj = _neighbours(self.t)
        root, a, b = self.t.names[:3]
        self.root, self.relays = root, (a, b)
        self.halves = [set(n for n in self.t.names[3:] if r in self.adj[n]) for r in (a, b)]

    def test_relays_hear_the_root_and_each_other(self):
        a, b = self.relays
        self.assertEqual(self.adj[self.root], {a, b})
        self.assertIn(b, self.adj[a])
        self.assertEqual(self.t.hops[a], 1)
        self.assertEqual(self.t.hops[b], 1)

    def test_halves_disjoint_and_cover_the_nodes(self):
        h1, h2 = self.halves
        self.assertEqual(h1 & h2, set())
        self.assertEqual(h1 | h2, set(self.t.names[3:]))
        self.assertEqual((len(h1), len(h2)), (14, 14))
        self.assertEqual(sorted(h1), self.t.names[3:17])
        odd = topologies.relay_star_split(8)  # 5 Nodes: the first half the larger
        self.assertEqual(len(_neighbours(odd)[odd.names[1]] - {odd.names[0], odd.names[2]}), 3)

    def test_each_node_hears_its_relay_and_its_half_only(self):
        for relay, half in zip(self.relays, self.halves):
            for n in half:
                self.assertEqual(self.adj[n], (half - {n}) | {relay}, hex(n))
                self.assertEqual(self.t.hops[n], 2, hex(n))
            self.assertEqual(self.adj[relay], half | {self.root} | (set(self.relays) - {relay}))

    def test_links_like_the_star(self):
        self.assertTrue(all((rssi, snr) == (-80, 10) for _, _, rssi, snr in self.t.links))
        pairs = [frozenset((a, b)) for a, b, _, _ in self.t.links]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(self.t.depth, 2)


class StarDomain(unittest.TestCase):
    def setUp(self):
        self.t = topologies.make("star-domain", _STAR, 1)
        self.star = topologies.make("star", _STAR, 1)

    def test_star_links_kept(self):
        self.assertEqual(self.t.names, self.star.names)
        self.assertEqual(self.t.links[: len(self.star.links)], self.star.links)

    def test_root_linked_to_every_board(self):
        root, relay = self.t.names[0], self.t.names[1]
        adj = _neighbours(self.t)
        self.assertEqual(adj[root], set(self.t.names[1:]))
        extra = self.t.links[len(self.star.links) :]
        self.assertEqual(sorted(b for _, b, _, _ in extra), self.t.names[2:])
        for a, b, rssi, snr in extra:
            self.assertEqual(a, root)
            self.assertNotEqual(b, relay)
            self.assertEqual((rssi, snr), (topologies.WEAK_RSSI, topologies.WEAK_SNR))

    def test_root_links_below_the_eligibility_margin(self):
        # Ineligible from the first frame and after 8, at the suite's SF and
        # at SF7; the star's links are eligible.
        for sf in (7, _SF):
            table = NeighbourTable(sf, 10000)
            for counter in range(1, 9):
                rssi, snr = topologies.WEAK_RSSI, topologies.WEAK_SNR
                weak = table.heard(ROOT, counter, 0, 0, rssi, snr, counter)
                self.assertFalse(weak.eligible(), (sf, counter))
            _, _, rssi, snr = self.star.links[0]
            self.assertTrue(table.heard(0x0101, 1, 256, 0, rssi, snr, 9).eligible(), sf)

    def test_nodes_never_choose_the_root(self):
        # A small star domain forming at SF8, fast periods: the root heard by
        # every Node, never their preferred parent; every Node under the relay.
        info = DodagInfo(0, 10, 60, 32, 56, EU_869)
        t = topologies.relay_star_domain(6)
        net = Network(1, _KEY, sf=_SF)
        for name in t.names:
            net.add(name, routing=lambda engine: Routing(engine, EU_869, info))
        t.apply(net)
        root, relay = t.names[0], t.names[1]
        chosen = {}  # Node -> first time the root was its preferred parent

        def sample():
            for name in t.names[2:]:
                if net.boards[name].routing.parents.preferred == root:
                    chosen.setdefault(name, net.now)
            if net.now < 60000:
                net.at(net.now + 250, sample)

        net.at(0, sample)
        net.run(60000)
        self.assertEqual(chosen, {})
        self.assertEqual(net.boards[relay].routing.parents.preferred, root)
        for name in t.names[2:]:
            b = net.boards[name]
            self.assertEqual(b.routing.parents.preferred, relay, hex(name))
            self.assertIsNotNone(b.engine.neighbours.get(root), hex(name))
            self.assertIsNotNone(net.boards[root].engine.neighbours.get(name), hex(name))
