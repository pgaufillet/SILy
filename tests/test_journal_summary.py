"""Summary of a journal read back from a box (tools/journal_summary.py).

The journal is written by ``sily.journal`` into a real directory under
build/: a node's boot that ends with a stop, then one cut by a power loss.
"""

import os
import unittest

from sily import journal
from tools import journal_summary

DIR = "build/test_journal_summary"  # relative to the repository root, where the tests run


def _minute(t, tf, margin, bat, jw, js, parent):
    rows = [[parent, 8, margin, margin, 140, 94, 3, 1, 12, -98, -97, 61]]
    return (
        t,
        "min",
        {
            "att": 1,
            "par": parent,
            "nb": rows,
            "link": {"tf": tf, "rf": 2 * tf, "crc": 0},
            "rt": {"sw": 1 if parent else 0},
            "bat": bat,
            "jw": jw,
            "js": js,
            "jq": 0,
            "je": 0,
        },
    )


def _write(boot, lines):
    j = journal.Journal(DIR, boot, lambda: 0)
    j.open()
    for t, kind, fields in lines:
        j.write(t, kind, fields)
    j.close()


class Summary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            os.mkdir("build")
        except OSError:
            pass
        try:
            for name in os.listdir(DIR):
                os.remove(DIR + "/" + name)
        except OSError:
            pass
        boot = {"role": "node", "address": 3, "name": "box3", "sf": 7, "version": "0.2"}
        boot.update(reset="power", crashes=0)
        _write(
            1,
            [
                (1000, "boot", boot),
                (13500, "ev", {"e": "attach", "par": 0, "rank": 8}),
                _minute(61000, 10, 85, 4010, 2, 30, 0),
                (70000, "ev", {"e": "parent", "par": 7, "was": 0, "rank": 16}),
                (90000, "ev", {"e": "fail", "dst": 0, "port": 20, "seq": 4, "why": 1}),
                _minute(121000, 25, 65, 3990, 5, 61, 7),
                (130000, "ev", {"e": "detach", "was": 7}),
                (131000, "stop", None),
            ],
        )
        _write(2, [(900, "boot", boot), (5900, "ev", {"e": "attach", "par": 0, "rank": 8})])
        path = DIR + "/" + journal.segment_name(2)
        with open(path, "ab") as f:
            f.write(b'{"b":2,"t":7000,"k":"m')  # cut by the power loss
        cls.records, cls.bad = journal.read(DIR)
        cls.runs = journal_summary.boots(cls.records)

    def test_one_summary_per_boot(self):
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(self.bad, 1)

    def test_attach_time_and_parent_changes(self):
        s = journal_summary.summarise(self.runs[0])
        self.assertEqual(s["attach_s"], 12.5)
        self.assertEqual(s["parents"], [0, 7])
        self.assertEqual((s["parent_changes"], s["detaches"], s["failures"]), (1, 1, 1))
        self.assertEqual((s["end"], s["span_s"]), ("stop", 130.0))

    def test_counters_links_battery_and_stalls(self):
        s = journal_summary.summarise(self.runs[0])
        self.assertEqual(s["counters"], {"link.tf": 25, "link.rf": 50, "rt.sw": 1})
        self.assertEqual(s["links"][0]["margins"], [8.5])
        self.assertEqual(s["links"][7]["margins"], [6.5])
        self.assertEqual(s["battery"], (4010, 3990, 3990))
        self.assertEqual((s["jw"], s["js"], s["minutes"]), (5, 61, 2))

    def test_power_cut_boot(self):
        s = journal_summary.summarise(self.runs[1])
        self.assertEqual((s["boot"], s["end"], s["attach_s"]), (2, None, 5.0))
        self.assertEqual(s["counters"], {})
        lines = journal_summary.report(s)
        self.assertTrue("no end line" in lines[1], lines)

    def test_report_lines(self):
        lines = journal_summary.report(journal_summary.summarise(self.runs[0]))
        self.assertEqual(
            lines[0], "boot 1: node box3 (3) at SF7, version 0.2, reset power, crashes 0"
        )
        self.assertTrue(lines[2].startswith("  attached after 0:00:12; parents 0 > 7"), lines[2])
        self.assertTrue(
            "  link 0: margin 8.5 / 8.5 / 8.5 dB (min/mean/max, 1), RSSI -97 dBm, 12 frames"
            in lines
        )


GATEWAY_DIR = DIR + "_gateway"


def _clear(directory):
    try:
        os.mkdir("build")
    except OSError:
        pass
    try:
        for name in os.listdir(directory):
            os.remove(directory + "/" + name)
    except OSError:
        pass


def _row(address, received, expected, latency, boots=1):
    return [address, boots, received, expected, 1, 2, 0, 0, boots] + latency


class Traffic(unittest.TestCase):
    """Test traffic: a node's own counts, the gateway's figures per node."""

    @classmethod
    def setUpClass(cls):
        _clear(GATEWAY_DIR)
        boot = {"role": "gateway", "address": 0, "name": "base", "sf": 7, "version": "0.2"}
        minutes = [
            [_row(3, 5, 6, [6, 100, 900, 900]), _row(4, 6, 6, [6, 0, 50, 50])],
            [_row(3, 11, 12, [6, 300, 2000, 2000]), _row(4, 12, 12, [0, None, None, None])],
            [_row(3, 17, 18, [6, 200, 500, 700]), _row(4, 15, 18, [6, 10, 80, 80], 2)],
        ]
        lines = [(0, "boot", boot)]
        for i, rows in enumerate(minutes):
            tt = {"o": rows, "mal": 0, "q": 0, "ovf": 0}
            lines.append((60000 * (i + 1), "min", {"tt": tt}))
        lines[-1][2]["tt"]["mal"] = 2
        j = journal.Journal(GATEWAY_DIR, 1, lambda: 0)
        j.open()
        for t, kind, fields in lines:
            j.write(t, kind, fields)
        j.close()
        records, _ = journal.read(GATEWAY_DIR)
        cls.gateway = journal_summary.summarise(journal_summary.boots(records)[0])

    def test_gateway_figures_per_node(self):
        t = self.gateway["traffic"]["gateway"]
        self.assertEqual(sorted(t), [3, 4])
        self.assertEqual(t[3]["row"][2:4], [17, 18])
        self.assertEqual(
            (t[3]["medians"], t[3]["maxima"], t[3]["samples"]),
            ([100, 300, 200], [900, 2000, 700], 18),
        )
        self.assertEqual((t[4]["medians"], t[4]["samples"]), ([0, 10], 12))
        self.assertEqual(t[4]["row"][8], 2)

    def test_gateway_lines(self):
        lines = journal_summary.report(self.gateway)
        self.assertTrue(
            "  test traffic from 3: 17/18 received (94.4 %), 1 duplicates, 2 reordered,"
            " 0 late, 0 stale, 1 boots" in lines,
            lines,
        )
        self.assertTrue(
            "    latency above the best case: minute medians 200 ms median, 300 highest;"
            " minute maxima 900 ms median, 2000 highest; 18 samples in 3 minutes" in lines,
            lines,
        )
        self.assertTrue("from 4: 15/18 received (83.3 %)" in lines[-3], lines)
        self.assertTrue(lines[-1].startswith("  test traffic lost to the gateway: 2 malformed"))

    def test_node_counts(self):
        tt = {"n": 40, "ok": 38, "ref": [[-7, 2]], "fail": [[3, 1]]}
        s = journal_summary.summarise([{"b": 1, "t": 0, "k": "min", "tt": tt}])
        self.assertEqual(s["traffic"], {"node": tt})
        lines = journal_summary.report(s)
        self.assertEqual(
            lines[-1], "  test traffic: 40 submitted, 38 accepted, refused: -7 x2; failed: 3 x1"
        )

    def test_no_traffic_no_line(self):
        s = journal_summary.summarise([{"b": 1, "t": 0, "k": "min"}])
        self.assertIsNone(s["traffic"])
        self.assertFalse(any("test traffic" in line for line in journal_summary.report(s)))
