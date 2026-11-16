"""The gateway's view of the mesh (sily.nodes): reports in, one dict per node out.

`Nodes` is fed decoded reports as ``mesh.routing.Routing.on_report`` gives
them, with injected times (ms); presence period 30 s.
"""

import json
import unittest

from mesh.ports import NODE_INFO, PRESENCE, TOPOLOGY
from mesh.reports import FIRST_REPORT, MARGIN_UNKNOWN, RANK_ERRORS, NodeInfo, Presence
from sily import record
from sily.nodes import CAPACITY, FIELDS, Nodes

P = 30000  # ms
MAC = b"\x01\x02\x03\x04\x05\x06"


def _presence(nodes, t, origin, parent, flags=0, battery=3920, mode=1, extension=None):
    if extension is None:
        extension = record.encode_extension(battery, mode)
    nodes.update(t, PRESENCE, origin, Presence(flags, extension, parent=parent))


def _chain(pairs, t=1000):
    nodes = Nodes(P // 1000)
    for origin, parent in pairs:
        _presence(nodes, t, origin, parent)
    return nodes


class Reports(unittest.TestCase):
    def test_presence(self):
        nodes = Nodes(30)
        _presence(nodes, 1000, 5, 0, flags=RANK_ERRORS, battery=3920, mode=1)
        _presence(nodes, 31000, 5, 0, flags=0, battery=3900, mode=2)
        row = nodes.row(5, 34000)
        self.assertEqual((row["address"], row["parent"], row["presences"]), (5, 0, 2))
        self.assertEqual((row["battery"], row["mode"], row["flags"]), (3900, "competition", 0))
        self.assertEqual((row["age_s"], row["heard_s"]), (3, 3))
        self.assertEqual((row["parent_changes"], row["parent_age_s"]), (0, None))
        self.assertEqual((row["restarts"], row["name"], row["firmware"]), (0, None, None))
        self.assertEqual((row["topology_age_s"], row["links"]), (None, []))
        _presence(nodes, 32000, 5, 0, flags=RANK_ERRORS)
        self.assertEqual(nodes.row(5, 32000)["flags"], RANK_ERRORS)

    def test_parent_change_counted_and_its_age_reset(self):
        nodes = Nodes(30)
        _presence(nodes, 1000, 5, 0)
        _presence(nodes, 11000, 5, 2)
        row = nodes.row(5, 14000)
        self.assertEqual((row["parent"], row["parent_changes"], row["parent_age_s"]), (2, 1, 3))
        _presence(nodes, 20000, 5, 2)  # same parent
        self.assertEqual(nodes.row(5, 21000)["parent_changes"], 1)
        _presence(nodes, 30000, 5, 0)
        row = nodes.row(5, 31000)
        self.assertEqual((row["parent"], row["parent_changes"], row["parent_age_s"]), (0, 2, 1))

    def test_first_report_counts_restarts(self):
        nodes = Nodes(30)
        _presence(nodes, 1000, 5, 0, flags=FIRST_REPORT)
        _presence(nodes, 2000, 5, 0)
        _presence(nodes, 3000, 5, 0, flags=FIRST_REPORT | RANK_ERRORS)
        row = nodes.row(5, 3000)
        self.assertEqual((row["presences"], row["restarts"]), (3, 2))

    def test_extension_other_than_one_byte_gives_no_battery(self):
        nodes = Nodes(30)
        _presence(nodes, 1000, 5, 0, battery=4000, mode=1)
        self.assertEqual(nodes.row(5, 1000)["battery"], 4000)
        for extension in (b"", b"\x10\x20"):
            _presence(nodes, 2000, 5, 0, extension=extension)
            row = nodes.row(5, 2000)
            self.assertEqual((row["battery"], row["mode"]), (None, None), extension)

    def test_topology_replaces_links(self):
        nodes = Nodes(30)
        nodes.update(1000, TOPOLOGY, 5, [(0, 12, True, 98), (7, MARGIN_UNKNOWN, False, 0)])
        row = nodes.row(5, 3000)
        self.assertEqual(row["links"], [[0, 12, 1, 98], [7, -128, 0, 0]])
        self.assertEqual(row["topology_age_s"], 2)
        nodes.update(4000, TOPOLOGY, 5, [(9, -3, True, 100)])
        row = nodes.row(5, 4000)
        self.assertEqual((row["links"], row["topology_age_s"]), ([[9, -3, 1, 100]], 0))
        nodes.update(5000, TOPOLOGY, 5, [])
        self.assertEqual(nodes.row(5, 5000)["links"], [])

    def test_node_information(self):
        nodes = Nodes(30)
        nodes.update(1000, NODE_INFO, 5, NodeInfo(MAC, 0x0003, "relay1"))
        row = nodes.row(5, 1000)
        self.assertEqual((row["name"], row["firmware"]), ("relay1", "0.3"))
        nodes.update(2000, NODE_INFO, 5, NodeInfo(MAC, 0x0102, "relay2"))
        row = nodes.row(5, 2000)
        self.assertEqual((row["name"], row["firmware"]), ("relay2", "1.2"))
        self.assertEqual((row["presences"], row["parent"], row["age_s"]), (0, None, None))

    def test_bound(self):
        nodes = Nodes(30)
        for origin in range(1, CAPACITY + 1):
            _presence(nodes, 1000, origin, 0)
        self.assertEqual((len(nodes), nodes.beyond), (CAPACITY, 0))
        _presence(nodes, 2000, CAPACITY + 1, 0)
        nodes.update(2000, NODE_INFO, CAPACITY + 1, NodeInfo(MAC, 3, "late"))
        self.assertEqual((len(nodes), nodes.beyond), (CAPACITY, 2))
        _presence(nodes, 3000, 1, 0)  # an existing node still updated
        self.assertEqual(nodes.row(1, 3000)["presences"], 2)
        self.assertEqual(nodes.beyond, 2)
        rows = list(nodes.rows(lambda: 3000))
        self.assertEqual([r["address"] for r in rows], list(range(1, CAPACITY + 1)))


class State(unittest.TestCase):
    def test_limits(self):
        nodes = _chain([(5, 0)], t=1000)
        for age, state in (
            (0, "ok"),
            (P * 5 // 2, "ok"),
            (P * 5 // 2 + 1, "late"),
            (P * 9 // 2, "late"),
            (P * 9 // 2 + 1, "silent"),
        ):
            self.assertEqual(nodes.state(5, 1000 + age), state, age)
            self.assertEqual(nodes.row(5, 1000 + age)["state"], state, age)

    def test_period_scales_the_limits(self):
        nodes = Nodes(60)
        _presence(nodes, 0, 5, 0)
        self.assertEqual(nodes.state(5, 150000), "ok")
        self.assertEqual(nodes.state(5, 150001), "late")

    def test_from_the_last_presence(self):
        nodes = _chain([(5, 0)], t=0)
        nodes.update(P * 5, TOPOLOGY, 5, [])  # a later report of another kind
        self.assertEqual(nodes.state(5, P * 5), "silent")
        self.assertEqual(nodes.row(5, P * 5)["heard_s"], 0)
        _presence(nodes, P * 6, 5, 0)
        self.assertEqual(nodes.state(5, P * 6), "ok")

    def test_before_any_presence_from_the_last_report(self):
        nodes = Nodes(30)
        nodes.update(0, NODE_INFO, 5, NodeInfo(MAC, 3, "n5"))
        self.assertEqual(nodes.state(5, P * 5 // 2), "ok")
        self.assertEqual(nodes.state(5, P * 9 // 2 + 1), "silent")


class Hops(unittest.TestCase):
    def test_chain(self):
        nodes = _chain([(1, 0), (3, 1), (4, 3)])
        self.assertEqual([nodes.hops(a) for a in (1, 3, 4)], [1, 2, 3])
        self.assertEqual(nodes.row(3, 1000)["hops"], 2)

    def test_unknown_parent(self):
        nodes = _chain([(3, 9), (4, 3)])
        self.assertEqual((nodes.hops(3), nodes.hops(4)), (None, None))
        nodes.update(1000, TOPOLOGY, 9, [])  # heard, parent not known yet
        self.assertEqual((nodes.hops(9), nodes.hops(3)), (None, None))
        _presence(nodes, 2000, 9, 0)
        self.assertEqual((nodes.hops(9), nodes.hops(3), nodes.hops(4)), (1, 2, 3))

    def test_loop(self):
        nodes = _chain([(1, 0), (6, 7), (7, 6), (8, 7)])
        self.assertEqual([nodes.hops(a) for a in (1, 6, 7, 8)], [1, None, None, None])
        self.assertEqual(nodes.row(6, 1000)["hops"], None)

    def test_self_loop(self):
        nodes = _chain([(1, 0), (2, 2)])
        self.assertEqual((nodes.hops(1), nodes.hops(2)), (1, None))


class Rows(unittest.TestCase):
    def test_one_dict_per_node_by_address(self):
        nodes = _chain([(9, 0), (2, 9), (5, 0)])
        nodes.update(1000, NODE_INFO, 2, NodeInfo(MAC, 3, "two"))
        nodes.update(1000, TOPOLOGY, 2, [(9, 4, True, 90)])
        rows = list(nodes.rows(lambda: 4000))
        self.assertEqual([r["address"] for r in rows], [2, 5, 9])
        for r in rows:
            self.assertEqual(sorted(r), sorted(FIELDS))
        self.assertEqual(json.loads(json.dumps(rows)), rows)
        self.assertEqual(
            (rows[0]["name"], rows[0]["hops"], rows[0]["links"]), ("two", 2, [[9, 4, 1, 90]])
        )

    def test_one_at_a_time_with_the_clock_of_each(self):
        nodes = _chain([(1, 0), (2, 0)], t=0)
        times = [5000, 9000]
        rows = nodes.rows(lambda: times.pop(0))
        first = next(rows)
        self.assertEqual((first["address"], first["age_s"]), (1, 5))
        _presence(nodes, 6000, 3, 0)  # added between two rows: next call
        _presence(nodes, 6000, 2, 1)
        second = next(rows)
        self.assertEqual((second["address"], second["age_s"], second["hops"]), (2, 3, 2))
        self.assertEqual(list(rows), [])
        self.assertEqual([r["address"] for r in nodes.rows(lambda: 7000)], [1, 2, 3])
