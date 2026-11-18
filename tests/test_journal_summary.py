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

    def test_display(self):
        self.assertIsNone(journal_summary.summarise(self.runs[0])["display"])
        run = list(self.runs[0])
        stop = run.pop()
        for t, ow, o_r in ((181000, 3, 25), (241000, 4, 21)):
            run.append({"b": 1, "t": t, "k": "min", "ow": ow, "or": o_r})
        run.append({"b": 1, "t": 250000, "k": "ev", "e": "display", "err": "OSError(5,)"})
        run.append(stop)
        s = journal_summary.summarise(run)
        self.assertEqual((s["display"], s["display_errors"]), ((4, 25), ["OSError(5,)"]))
        lines = journal_summary.report(s)
        self.assertTrue("  display: longest row write 4 ms, refresh 25 ms" in lines, lines)
        self.assertTrue("  display failed: OSError(5,)" in lines, lines)

    def test_web(self):
        s = journal_summary.summarise(self.runs[0])  # no access point
        self.assertEqual((s["ap"], s["web"], s["wifi_errors"]), (None, None, []))
        self.assertFalse(any("access point" in line for line in journal_summary.report(s)))
        boot = {"b": 3, "t": 0, "k": "boot", "role": "gateway", "ap": "SILy-base"}
        run = [boot]
        for t, web in ((60000, [3, 0, 1, 0, 2, 7, 1]), (120000, [9, 1, 1, 0, 5, 4, 2])):
            run.append({"b": 3, "t": t, "k": "min", "web": web})
        run.append({"b": 3, "t": 150000, "k": "min"})  # the server down
        s = journal_summary.summarise(run)
        self.assertEqual((s["ap"], s["web"]), ("SILy-base", (9, 1, 7, 2)))
        lines = journal_summary.report(s)
        self.assertTrue(
            "  access point SILy-base: 9 requests, 1 errors, longest stretch 7 ms,"
            " at most 2 stations" in lines,
            lines,
        )

    def test_boot_after_a_watchdog_reset(self):
        boot = {"b": 6, "t": 0, "k": "boot", "role": "node", "address": 3, "name": "box3"}
        boot.update(sf=7, version="0.2", reset="watchdog", crashes=1, watchdog_s=20)
        lines = journal_summary.report(journal_summary.summarise([boot]))
        self.assertEqual(
            lines[0],
            "boot 6: node box3 (3) at SF7, version 0.2, reset watchdog, crashes 1, watchdog 20 s",
        )

    def test_build_per_boot(self):
        boot = {"b": 8, "t": 0, "k": "boot", "role": "node", "address": 3, "name": "box3"}
        boot.update(sf=7, version="0.2", build="a961409-dirty", reset="power", crashes=0)
        head = "boot 8: node box3 (3) at SF7, version 0.2, build "
        lines = journal_summary.report(journal_summary.summarise([boot]))
        self.assertEqual(lines[0], head + "a961409-dirty, reset power, crashes 0")
        boot["build"] = None  # not deployed by make deploy
        lines = journal_summary.report(journal_summary.summarise([boot]))
        self.assertEqual(lines[0], head + "none, reset power, crashes 0")

    def test_safe_mode_boot(self):
        why = ["crashes: 3 without 10 min of stable run", "sf: out of range 7 to 12"]
        safe = {"b": 9, "t": 812, "k": "safe", "why": why, "version": "0.2", "build": "a961409"}
        s = journal_summary.summarise([safe])
        self.assertEqual(
            journal_summary.report(s),
            [
                "boot 9: safe mode, version 0.2, build a961409",
                "  safe: crashes: 3 without 10 min of stable run",
                "  safe: sf: out of range 7 to 12",
            ],
        )

    def test_dodag_versions_and_root_sequence(self):
        s = journal_summary.summarise(self.runs[0])  # minute records without them
        self.assertIsNone(s["dodag"])
        self.assertFalse(any("DODAG" in line for line in journal_summary.report(s)))
        # A node: no sequence before its first, then version 2; the gateway
        # restarted (version 3), then silent for 2 minutes.
        run = [{"b": 1, "t": 0, "k": "boot", "role": "node"}]
        for t, ver, seq in (
            (60000, 0, None),
            (120000, 2, 40),
            (180000, 2, 44),
            (240000, 3, 45),
            (300000, 3, 45),
            (360000, 3, 45),
            (420000, 3, 49),
        ):
            run.append({"b": 1, "t": t, "k": "min", "ver": ver, "seq": seq})
        s = journal_summary.summarise(run)
        self.assertEqual(s["dodag"], ([2, 3], 40, 49, 120.0))
        lines = journal_summary.report(s)
        self.assertTrue(
            "  DODAG version 2 > 3; root sequence 40 first, 49 last,"
            " unchanged for 0:02:00 at most" in lines,
            lines,
        )

    def test_gateway_listening_at_boot(self):
        # Version 0 until its first version, without a sequence: not a version.
        run = [{"b": 2, "t": 0, "k": "boot", "role": "gateway"}]
        for t, ver, seq in ((60000, 0, None), (120000, 5, 7), (180000, 5, 11)):
            run.append({"b": 2, "t": t, "k": "min", "ver": ver, "seq": seq})
        s = journal_summary.summarise(run)
        self.assertEqual(s["dodag"], ([5], 7, 11, 0.0))
        lines = journal_summary.report(s)
        self.assertTrue(
            "  DODAG version 5; root sequence 7 first, 11 last, unchanged for 0:00:00 at most"
            in lines,
            lines,
        )

    def test_wifi_failure(self):
        boot = {"b": 4, "t": 0, "k": "boot", "role": "node", "ap": "SILy-box3"}
        failed = {"b": 4, "t": 40, "k": "ev", "e": "wifi", "err": "OSError(5,)"}
        s = journal_summary.summarise([boot, failed, {"b": 4, "t": 60000, "k": "min"}])
        self.assertEqual((s["web"], s["wifi_errors"]), (None, ["OSError(5,)"]))
        lines = journal_summary.report(s)
        self.assertEqual(
            lines[-2:], ["  access point SILy-box3: no web figures", "  wifi failed: OSError(5,)"]
        )

    def test_reply_turnaround(self):
        s = journal_summary.summarise(self.runs[0])  # a node: none
        self.assertIsNone(s["turnaround"])
        self.assertFalse(any("turnaround" in line for line in journal_summary.report(s)))
        run = [{"b": 5, "t": 0, "k": "boot", "role": "gateway"}]
        for t, tr in ((60000, [12, 213, 231, 380]), (120000, [30, 220, 450, 490])):
            run.append({"b": 5, "t": t, "k": "min", "tr": tr})
        run.append({"b": 5, "t": 180000, "k": "min"})  # no reply in that minute
        run.append({"b": 5, "t": 240000, "k": "min", "tr": [5, 190, 200, 210]})
        s = journal_summary.summarise(run)
        self.assertEqual(s["turnaround"], (47, 3, 213, 450, 490))
        lines = journal_summary.report(s)
        self.assertTrue(
            "  reply turnaround: 47 replies in 3 minutes; median of the minute medians"
            " 21.3 ms, worst minute p90 45.0 ms, max 49.0 ms" in lines,
            lines,
        )

    def test_drops(self):
        s = journal_summary.summarise(self.runs[0])  # no drop rates
        self.assertIsNone(s["drops"])
        self.assertFalse(any("dropped" in line for line in journal_summary.report(s)))
        run = [{"b": 7, "t": 0, "k": "boot", "role": "node"}]
        for t, received, drp in ((60000, 70, [[0, 25], [9, 0]]), (120000, 140, [[0, 60], [9, 2]])):
            row = [0, 8, 85, 85, 140, 94, 3, 1, received, -98, -97, 61]
            run.append({"b": 7, "t": t, "k": "min", "nb": [row], "drp": drp})
        s = journal_summary.summarise(run)
        self.assertEqual(s["drops"], [[0, 60], [9, 2]])  # totals of the last record
        lines = journal_summary.report(s)
        # 60 dropped of 200 frames of 0 (140 received); 9 is no link.
        self.assertTrue("  dropped on purpose: from 0 60 (30.0 %), from 9 2" in lines, lines)

    def test_clock(self):
        self.assertIsNone(journal_summary.summarise(self.runs[0])["clock"])
        now = 1794929400000  # 2026-11-17 15:30:00 UTC
        boot = {"b": 4, "t": 800, "k": "boot", "role": "node", "utc": now - 60000}
        run = [boot]
        for t, ms in ((20800, now - 40030), (620800, now + 559990), (700800, now + 3600000)):
            run.append({"b": 4, "t": t, "k": "clock", "utc": ms})
        run.append({"b": 4, "t": 900000, "k": "stop"})
        # The RTC 30 ms ahead of the phone at boot, the board's clock 20 ms
        # slow over 10 min, then the phone's clock moved on by an hour.
        s = journal_summary.summarise(run)
        self.assertEqual(s["clock"], (now - 60030, "clock", 3, 2960010))
        lines = journal_summary.report(s)
        self.assertEqual(
            lines[2],
            "  clock: first line at 2026-11-17 15:28:59.970 UTC by the first clock line,"
            " 3 clock lines, largest departure +2960010 ms",
        )
        run.pop(3)
        self.assertEqual(journal_summary.summarise(run)["clock"], (now - 60030, "clock", 2, -30))
        # The boot record's RTC alone.
        s = journal_summary.summarise([boot, {"b": 4, "t": 60800, "k": "min"}])
        self.assertEqual(s["clock"], (now - 60000, "boot", 0, None))
        self.assertEqual(
            journal_summary.report(s)[2],
            "  clock: first line at 2026-11-17 15:29:00.000 UTC by the boot record's RTC,"
            " 0 clock lines",
        )
        # A boot record without the field (an older journal), clock lines alone.
        s = journal_summary.summarise([{"b": 5, "t": 0, "k": "boot"}, run[1], run[2]])
        self.assertEqual(s["clock"], (now - 60830, "clock", 2, 20))

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
        self.assertEqual(lines[2], "  no clock")
        self.assertTrue(lines[3].startswith("  attached after 0:00:12; parents 0 > 7"), lines[3])
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
            [_row(3, 5, 6, [6, 100, 900, 900]), _row(4, 6, 6, [6, 0, 50, 50, 1000, 900])],
            [
                _row(3, 11, 12, [6, 300, 2000, 2000]),
                _row(4, 12, 12, [0, None, None, None, None, None]),
            ],
            [_row(3, 17, 18, [6, 200, 500, 700]), _row(4, 15, 18, [6, 10, 80, 80, 7000, 901], 2)],
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
        # Rows written before the clock offsets (3), and with them (4).
        self.assertEqual((t[3]["offsets"], t[4]["offsets"]), (0, 2))

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
        self.assertTrue("from 4: 15/18 received (83.3 %)" in lines[-4], lines)
        self.assertEqual(
            lines[-2],
            "    clock offset to the gateway in 2 minutes (tools/survey.py --clocks places"
            " the node's journal by them)",
        )
        self.assertEqual(sum([1 for line in lines if "clock offset" in line]), 1)  # not node 3
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
