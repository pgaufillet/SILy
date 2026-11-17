"""Journal records (sily.record): minute record, state events, reports, extension.

The minute record is read from real link engines with their routing on the
simulator (`sim.net`): a root and a node at SF7, presence period 10 s.
"""

import json
import unittest

from mesh.dodag import DodagInfo
from mesh.frame import RANK_INFINITE, ROOT
from mesh.neighbours import EXPIRY_PERIODS, NeighbourTable
from mesh.ports import NODE_INFO, PRESENCE, TOPOLOGY
from mesh.profile import EU_869
from mesh.radio import RX
from mesh.reports import NodeInfo, Presence
from mesh.routing import Routing
from sily import record
from sily.config import ADDRESS_MAX, DROP_MAX
from sily.drop import Drop
from sily.radio import link_source
from sily.tap import Tap
from sim.net import Network

KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
NODE = 5
OFF = 10**9  # switched off for the rest of the run


class Node:
    """The parts of a ``sily.stack.Stack`` the records read (no filter, no
    drop rates, no adapter)."""

    def __init__(self, board):
        self.engine = board.engine
        self.routing = board.routing
        self.filter = None
        self.drop = None
        self.adapter = None


def _routing(engine):
    return Routing(engine, EU_869, INFO)


def _attached():
    """Return (network, node board) once the node is attached under the root."""
    net = Network(1, KEY)
    root = net.add(ROOT, routing=_routing)
    node = net.add(NODE, routing=_routing)
    net.link_both(ROOT, NODE, snr=8)
    t = 0
    while not node.routing.attached:
        t += 1000
        if t > 60000:
            raise AssertionError("the node did not attach")
        net.run(t)
    net.run(t + 25000)  # a few presence periods: margins measured
    return net, root, node


def _minute(stack, now, tap=None):
    out = {}
    for piece in record.minute(stack, now, tap):
        out.update(piece)
    return out


class Minute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net, cls.root, cls.board = _attached()

    def test_state_of_an_attached_node(self):
        rec = _minute(Node(self.board), self.net.now)
        engine = self.board.engine
        self.assertEqual((rec["att"], rec["par"], rec["gnd"]), (1, ROOT, 1))
        self.assertEqual(rec["rank"], engine.rank)
        self.assertTrue(rec["rank"] < RANK_INFINITE)
        self.assertEqual(rec["mem"], [ROOT])
        self.assertEqual(rec["ver"], engine.dodag_version)
        self.assertIsNotNone(rec["seq"])  # carried from the root
        self.assertEqual(rec["seq"], self.board.routing.dodag.sequence)
        self.assertEqual(rec["link"]["tf"], engine.counters.tx_frames)
        self.assertTrue(rec["link"]["rf"] > 0)
        self.assertEqual(rec["rt"]["pr"], self.board.routing.reporter.counters.presence)
        self.assertEqual(rec["rt"]["sw"], self.board.routing.parents.switches)
        self.assertEqual(len(rec["link"]), len(record.LINK))

    def test_root_among_the_neighbours_with_the_table_margin(self):
        now = self.net.now
        rec = _minute(Node(self.board), now)
        rows = [row for row in rec["nb"] if row[0] == ROOT]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        entry = self.board.engine.neighbours.get(ROOT)
        self.assertEqual(len(row), len(record.NEIGHBOUR_FIELDS))
        self.assertIsNotNone(entry.margin())
        self.assertEqual(row[2], entry.margin())
        self.assertEqual(row[3], entry.last_margin())
        self.assertEqual(row[4], entry.etx())
        self.assertEqual(row[6], (now - entry.heard) // 1000)
        self.assertEqual(row[7], 1)  # in the parent set
        self.assertEqual(rec["foreign"], [0, 0])

    def test_root_record(self):
        rec = _minute(Node(self.root), self.net.now)
        self.assertEqual((rec["att"], rec["par"], rec["rank"]), (1, None, 0))
        self.assertIsNotNone(rec["seq"])  # its own, advanced at each beacon
        self.assertEqual(rec["seq"], self.root.routing.dodag.sequence)
        self.assertEqual([row[0] for row in rec["nb"]], [NODE])
        self.assertTrue(rec["rt"]["rcv"] > 0)  # the node's reports
        self.assertFalse("pr" in rec["rt"])  # the root sends none

    def test_neighbour_older_than_four_periods_left_out(self):
        table = self.board.engine.neighbours
        heard = table.get(ROOT).heard
        limit = EXPIRY_PERIODS * table.period_ms
        rows, _ = record.neighbours(table, heard + limit)
        self.assertEqual([r[0] for r in rows], [ROOT])
        rows, _ = record.neighbours(table, heard + limit + 1)
        self.assertEqual(rows, [])
        self.assertIsNotNone(table.get(ROOT))  # the table itself unchanged

    def test_record_is_json(self):
        rec = _minute(Node(self.board), self.net.now)
        self.assertEqual(json.loads(json.dumps(rec)), rec)

    def test_drop_counts_of_the_sources_with_a_rate(self):
        node = Node(self.board)
        self.assertFalse("drp" in _minute(node, self.net.now))  # no rates: no field
        node.drop = Drop(Sink(), {9: 100, ROOT: 100, 4: 0}, link_source, lambda: 0)
        node.drop.sink = lambda event: None
        for source in (9, 9, 4, 3, ROOT, 9):
            node.drop._on_event((RX, bytes((0, 0, 0, source, 0)), -90, 1.0))
        rec = _minute(node, self.net.now)
        self.assertEqual(rec["drp"], [[ROOT, 1], [4, 0], [9, 3]])
        self.assertEqual(json.loads(json.dumps(rec)), rec)


class Sequence(unittest.TestCase):
    def test_none_before_the_first(self):
        # A root listening at boot (no version given, as the gateway's) and a
        # node apart: neither holds a root sequence yet; the root's ver 0 is
        # no version.
        net = Network(1, KEY)
        root = net.add(ROOT, routing=lambda e: Routing(e, EU_869, INFO, version=None))
        node = net.add(NODE, routing=_routing)
        net.run(30000)
        self.assertFalse(root.routing.dodag.configured)
        for board in (root, node):
            rec = _minute(Node(board), net.now)
            self.assertEqual((rec["ver"], rec["seq"]), (0, None), board.engine.address)

    def test_advances_with_the_root_beacons(self):
        net, root, board = _attached()
        before = _minute(Node(board), net.now)["seq"]
        net.run(net.now + 2 * INFO.presence_s * 1000)  # beacons every half period
        after = _minute(Node(board), net.now)
        self.assertNotEqual(after["seq"], before)
        self.assertEqual(after["seq"], _minute(Node(root), net.now)["seq"])

    def test_static_routes(self):
        net = Network(1, KEY)
        board = net.add(NODE)
        net.run(1000)
        self.assertIsNone(board.routing)
        self.assertIsNone(_minute(Node(board), net.now)["seq"])


class Neighbours(unittest.TestCase):
    def _table(self, n):
        table = NeighbourTable(7)
        for a in range(1, n + 1):
            table.heard(1000 + a, 5, 255, 3, -120, -15.75, 0)
        return table

    def test_tap_joined_to_the_table_and_foreign_summed(self):
        table = self._table(2)
        tap = Tap(Sink())
        tap.sink = lambda event: None
        for source, rssi in ((1001, -90), (1001, -94), (77, -100), (78, -100), (78, -101)):
            tap._on_event((RX, bytes((0, 0, 0, source & 0xFF, source >> 8)), rssi, 2.5))
        rows, foreign = record.neighbours(table, 1000, tap)
        by = {row[0]: row for row in rows}
        self.assertEqual(by[1001][8:], [2, -94, -92, 25])
        self.assertEqual(by[1002][8:], [0, None, None, None])
        self.assertEqual(foreign, [2, 3])

    def test_64_neighbours_stay_within_a_segment(self):
        table = self._table(64)
        tap = Tap(Sink())
        tap.sink = lambda event: None
        for a in range(1001, 1065):
            for _ in range(3):
                tap._on_event((RX, bytes((0, 0, 0, a & 0xFF, a >> 8)), -120, -15.75))
        rows, _ = record.neighbours(table, 99000, tap)  # ages of 99 s
        self.assertEqual(len(rows), 64)
        # A whole minute record (the node of the simulator, radio and
        # application figures and the most drop rates added) fits the
        # smallest segment (journal.segment_kb 4): no record needs a segment
        # of its own.
        net, _, board = _attached()
        rec = _minute(Node(board), net.now)
        rec["nb"] = rows
        rec["radio"] = {key: 999999 for key, _ in record.RADIO}
        rec.update(noise=-110.5, bat=4200, heap=8000000, jw=999, js=999, jq=999, je=999)
        rec.update(b=99999, t=86400000, k="min", fil=99999, tov=99999)
        rec["drp"] = [[ADDRESS_MAX - a, 9999999] for a in range(DROP_MAX)]
        line = json.dumps(rec, separators=(",", ":"))
        self.assertTrue(len(line) < 4096, len(line))


class Sink:
    sink = None

    def transmit(self, frame):
        pass

    def sense(self):
        pass


class Transitions(unittest.TestCase):
    def test_scripted_states(self):
        attached = (1, ROOT, 8, 1, 0, 0, 0)
        self.assertEqual(
            record.transitions(record.DETACHED, attached),
            [
                ("attach", {"par": ROOT, "rank": 8, "sw": 0, "det": 0, "flt": 0}),
                ("grounded", {"sw": 0, "det": 0, "flt": 0}),
            ],
        )
        moved = (1, 7, 16, 1, 1, 0, 0)
        self.assertEqual(
            record.transitions(attached, moved),
            [("parent", {"par": 7, "was": ROOT, "rank": 16, "sw": 1, "det": 0, "flt": 0})],
        )
        lower = (1, 7, 24, 1, 1, 0, 0)
        self.assertEqual(
            record.transitions(moved, lower),
            [("rank", {"rank": 24, "was": 16, "sw": 1, "det": 0, "flt": 0})],
        )
        gone = (0, None, RANK_INFINITE, 0, 1, 1, 0)
        self.assertEqual(
            record.transitions(lower, gone),
            [
                ("detach", {"was": 7, "sw": 1, "det": 1, "flt": 0}),
                ("ungrounded", {"sw": 1, "det": 1, "flt": 0}),
            ],
        )

    def test_change_undone_between_polls_shows_in_the_counters_only(self):
        before = (1, ROOT, 8, 1, 0, 0, 0)
        after = (1, ROOT, 8, 1, 0, 1, 0)  # detached and back between two polls
        self.assertEqual(record.transitions(before, after), [])
        self.assertNotEqual(before, after)

    def test_detach_when_the_root_leaves(self):
        net, root, board = _attached()
        before = record.state(board.engine, board.routing)
        self.assertEqual(before[:2], (1, ROOT))
        root.power_loss(off_ms=OFF)
        t = net.now
        while board.routing.attached:
            t += 5000
            if t > net.now + 600000:
                raise AssertionError("the node stayed attached")
            net.run(t)
        after = record.state(board.engine, board.routing)
        events = record.transitions(before, after)
        self.assertEqual(events[0][0], "detach")
        self.assertEqual(events[0][1]["was"], ROOT)
        self.assertTrue(events[0][1]["det"] >= 1)


class Extension(unittest.TestCase):
    def test_battery_within_20_mv(self):
        for mv in range(3000, 4261, 7):
            data = record.encode_extension(mv, 2)
            self.assertEqual(len(data), 1)
            back, mode = record.decode_extension(data)
            self.assertTrue(abs(back - mv) <= 10, (mv, back))
            self.assertEqual(mode, 2)

    def test_clamped(self):
        self.assertEqual(record.decode_extension(record.encode_extension(2500)), (3000, 0))
        self.assertEqual(record.decode_extension(record.encode_extension(5100)), (4260, 0))
        self.assertEqual(record.decode_extension(record.encode_extension(None)), (3000, 0))

    def test_mode_kept(self):
        for mode in range(4):
            self.assertEqual(
                record.decode_extension(record.encode_extension(4100, mode)), (4100, mode)
            )

    def test_not_one_byte(self):
        self.assertIsNone(record.decode_extension(b""))
        self.assertIsNone(record.decode_extension(b"\x01\x02"))


class Reports(unittest.TestCase):
    def test_presence_with_the_box_extension(self):
        p = Presence(0x01, record.encode_extension(3920, 1), parent=7)
        self.assertEqual(
            record.report(PRESENCE, 9, p),
            ("rp", {"o": 9, "par": 7, "fl": 1, "bat": 3920, "mode": 1}),
        )

    def test_presence_with_another_extension_and_tails(self):
        p = Presence(0, b"\xab\xcd", parent=7, tails=((20, 3, 5),))
        kind, fields = record.report(PRESENCE, 9, p)
        self.assertEqual(kind, "rp")
        self.assertEqual((fields["x"], fields["tl"]), ("abcd", [[20, 3, 5]]))

    def test_topology(self):
        entries = [(0, 12, True, 94), (3, -2, False, 40)]
        self.assertEqual(
            record.report(TOPOLOGY, 9, entries),
            ("rt", {"o": 9, "e": [[0, 12, 1, 94], [3, -2, 0, 40]]}),
        )

    def test_node_information(self):
        info = NodeInfo(b"\x02\x00\x00\xab\xcd\xef", 0x0102, "box9")
        kind, fields = record.report(NODE_INFO, 9, info)
        self.assertEqual(kind, "ri")
        self.assertEqual(
            fields,
            {"o": 9, "mac": "020000abcdef", "fw": 0x0102, "name": "box9", "pv": info.protocol},
        )
