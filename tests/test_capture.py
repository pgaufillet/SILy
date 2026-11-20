"""A field capture as a trace (tools/capture.py), against the recorder.

A small simulated network (root - relay - far node, with routing and class
1 traffic) runs with a `sim.trace.Recorder` and a sniffer hearing every
box; the frames the sniffer received become a capture file as ``make
sniff`` writes it (the board's lines of ``sily.sniff``, stamped by
``tools/sniff.py``), its ticks about to wrap. The converter's ``tx``
records must be the recorder's for the same frames; its states, what
the frames tell; without the key or with another network's, nothing
opens; a payload carried in clear is reported.
"""

import io
import json
import unittest

from test_sily_stack import peer_frame, probe_frame

from mesh import kdf
from mesh import profile as profiles
from mesh.airtime import time_on_air_ms
from mesh.dodag import DodagInfo
from mesh.frame import ROOT
from mesh.profile import EU_869
from mesh.radio import RX
from mesh.routing import Routing
from mesh.security import AUTHENTICATION
from sily import config, sniff
from sim.net import Network
from sim.trace import FORMAT, Recorder
from tools import capture
from tools import sniff as host

NETWORK = {"password": "secret words", "network": "club", "iterations": 10, "sf": 7}
OTHER = dict(NETWORK, password="other words")
KEY = kdf.derive("secret words", "club", 10).frame_key
RELAY, FAR = 0x0011, 0x0022
BOXES = [
    ("gw.json", {"role": "gateway", "address": ROOT, "name": "gate"}),
    ("relay.json", {"role": "node", "address": RELAY, "name": "relay"}),
    ("far.json", {"role": "node", "address": FAR, "name": "far"}),
]
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
APP = 20
UTC0 = 1795000000000
HOST0 = UTC0 + 40  # the host's clock, 40 ms ahead
PERIOD = 1 << 30
TICKS0 = PERIOD - 20000  # the board's ticks wrap 20 s into the capture
SN = "sn"


def _routing(engine):
    return Routing(engine, EU_869, INFO)


def _settings(address=9):
    cfg, errors = config.board_config(
        NETWORK, {"role": "node", "address": address, "name": "spare"}
    )
    assert not errors, errors
    regional = profiles.get(cfg["profile"])
    return sniff.settings(cfg, regional, TICKS0, UTC0, PERIOD)


def _capture(heard, settings=None):
    """Return the capture file's lines of heard [(time ms, frame, rssi, snr)]."""
    s = settings if settings is not None else _settings()
    lines = [host.stamp(HOST0, "%s %s" % (sniff.SETTINGS, json.dumps(s)))]
    for t, frame, rssi, snr in heard:
        ticks = (TICKS0 + t) % PERIOD
        line = sniff.frame_line(ticks, UTC0 + t, rssi, snr, frame)
        lines.append(host.stamp(HOST0 + t + 3, line))
    return [line + "\n" for line in lines]


def _run():
    """Run the network; return (recorder's records, sniffer's receptions)."""
    net = Network(21, KEY)
    out = io.StringIO()
    rec = Recorder(net, out)
    for name in (ROOT, RELAY, FAR):
        net.add(name, routing=_routing)
    net.link_both(ROOT, RELAY)
    net.link_both(RELAY, FAR)
    sn = net.sniffer(SN)
    net.medium.link(ROOT, SN, rssi=-70, snr=8)
    net.medium.link(RELAY, SN, rssi=-85, snr=3)
    net.medium.link(FAR, SN, rssi=-101, snr=-4.5)
    heard = []
    sink = sn.port.sink

    def receive(event):
        sink(event)
        if event[0] == RX:
            heard.append((net.now, bytes(event[1]), event[2], event[3]))

    sn.port.sink = receive
    net.boards[ROOT].listen(APP)
    for i in range(8):
        net.at(40000 + 3000 * i, net.boards[FAR].submit, APP, 1, ROOT, b"punch %02d" % i)
    net.run(90000)
    rec.close()
    records = [json.loads(line) for line in out.getvalue().split("\n") if line]
    return records, heard


_cache = []


def _shared():
    if not _cache:
        _cache.append(_run())
    return _cache[0]


def _convert(lines, network=NETWORK, boxes=BOXES):
    known, errors = capture.load_boxes(network, boxes)
    assert not errors, errors
    cap = capture.parse(lines)
    return cap, known, capture.convert(cap, known)


def _kind(records, kind):
    return [r for r in records if r["k"] == kind]


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.recorded, self.heard = _shared()
        self.cap, self.known, self.res = _convert(_capture(self.heard))
        self.records = self.res.records

    def test_tx_records_are_the_recorders(self):
        # The recorder's transmissions the sniffer received, in order.
        ends = {r["x"]: r for r in _kind(self.recorded, "end")}
        expected = []
        for r in _kind(self.recorded, "tx"):
            for k, (dst, rssi, snr) in enumerate(r["rx"]):
                if dst == SN and ends[r["i"]]["f"][k] == "ok":
                    expected.append((r, rssi, snr))
        mine = _kind(self.records, "tx")
        self.assertEqual(len(mine), len(self.heard))
        self.assertEqual(len(mine), len(expected))
        self.assertTrue(len(mine) > 40)
        apps = 0
        for m, (r, rssi, snr) in zip(mine, expected):
            self.assertEqual(m["rx"], [[capture.SNIFFER, rssi, snr]])
            want = dict(r)
            got = dict(m)
            for f in ("i", "rx"):
                del want[f]
                del got[f]
            self.assertEqual(got, want)
            apps += len([x for x in m["m"] if x["p"] == APP])
        self.assertTrue(apps >= 8)  # the class 1 traffic and its copies, decoded
        # Each tx has its end, at its end time, fate ok.
        by_index = {r["i"]: r for r in self.records}
        for e in _kind(self.records, "end"):
            tx = by_index[e["x"]]
            self.assertEqual((e["t"], e["n"], e["f"]), (tx["e"], tx["n"], ["ok"]))
        self.assertEqual(len(_kind(self.records, "end")), len(mine))

    def test_header_index_and_time_order(self):
        h = self.records[0]
        self.assertEqual((h["k"], h["i"], h["t"], h["fmt"], h["sf"]), ("hdr", 0, 0, FORMAT, 7))
        eu = profiles.get(EU_869)
        self.assertEqual(h["profile"], {"id": EU_869, "name": eu.name, "hz": eu.frequency_hz})
        self.assertEqual(h["air"]["slot_ms"], time_on_air_ms(17, 7))
        self.assertEqual((h["budget_ms"], h["window_ms"]), (eu.budget_ms(), eu.window_ms()))
        nodes = [(n["n"], n["kind"], n.get("a"), n.get("root")) for n in h["nodes"]]
        self.assertEqual(
            nodes,
            [
                (ROOT, "board", ROOT, True),
                (RELAY, "board", RELAY, False),
                (FAR, "board", FAR, False),
                (capture.SNIFFER, "sniffer", None, None),
            ],
        )
        self.assertTrue("2026-11-18" in h["label"] and "spare" in h["label"])
        self.assertEqual([r["i"] for r in self.records], list(range(len(self.records))))
        times = [r["t"] for r in self.records]
        self.assertEqual(times, sorted(times))
        self.assertEqual(self.records[-1]["k"], "eof")
        self.assertEqual(self.records[-1]["stats"][capture.SNIFFER][3], len(self.heard))
        self.assertEqual(self.res.shift, 0)
        self.assertEqual(self.res.utc0, UTC0)

    def test_states_are_what_the_frames_tell(self):
        mine = {}
        for s in _kind(self.records, "st"):
            mine[s["n"]] = s
        theirs = {}
        for s in _kind(self.recorded, "st"):
            theirs[s["n"]] = s
        self.assertEqual((mine[RELAY]["p"], mine[FAR]["p"]), (ROOT, RELAY))
        for n in (RELAY, FAR):
            self.assertEqual(
                (mine[n]["r"], mine[n]["v"], mine[n]["p"], mine[n]["at"]),
                (theirs[n]["r"], theirs[n]["v"], theirs[n]["p"], theirs[n]["at"]),
            )
        self.assertEqual((mine[ROOT]["r"], "p" in mine[ROOT]), (0, False))
        # Each st follows the tx that told it, at its start.
        by_index = {r["i"]: r for r in self.records}
        for s in _kind(self.records, "st"):
            before = by_index[s["i"] - 1]
            self.assertTrue(before["k"] in ("tx", "st") and before["t"] == s["t"])

    def test_summary_counts_and_no_plaintext(self):
        heard = len(self.heard)
        counts = self.res.counts
        self.assertEqual(sorted(counts), [ROOT, RELAY, FAR])
        self.assertEqual(sum(c[0] for c in counts.values()), heard)
        self.assertEqual(sum(c[1] for c in counts.values()), heard)  # all opened
        self.assertTrue(self.res.checked >= 8)
        self.assertEqual(self.res.found, [])
        text = "\n".join(capture.summary(self.cap, self.known, self.res))
        self.assertTrue("no plaintext found" in text)
        self.assertTrue("the same network" in text)
        self.assertTrue("relay (17, node)" in text)

    def test_trace_written_as_json_lines(self):
        out = io.StringIO()
        capture.write_trace(self.records, out)
        lines = out.getvalue().split("\n")
        self.assertEqual(lines[-1], "")
        back = [json.loads(line) for line in lines[:-1]]
        self.assertEqual(back, self.records)
        self.assertTrue(lines[0].startswith('{"k":"hdr","i":0,"t":0,'))


class WithoutTheKey(unittest.TestCase):
    def test_no_network_file_nothing_opens(self):
        _, heard = _shared()
        cap, known, res = _convert(_capture(heard), network=None)
        self.assertIsNone(known.key)
        self.assertEqual(known.names[RELAY], ("relay", "node"))
        txs = _kind(res.records, "tx")
        self.assertEqual(len(txs), len(heard))
        self.assertTrue(all(t["err"] == AUTHENTICATION and "m" not in t for t in txs))
        self.assertTrue(all(c[0] == c[2] for c in res.counts.values()))
        self.assertEqual(_kind(res.records, "st"), [])
        text = "\n".join(capture.summary(cap, known, res))
        self.assertTrue("key: none" in text)

    def test_another_networks_key_opens_nothing(self):
        _, heard = _shared()
        cap, known, res = _convert(_capture(heard), network=OTHER)
        self.assertNotEqual(known.key, KEY)
        self.assertTrue(all(c[1] == 0 and c[2] == c[0] for c in res.counts.values()))
        self.assertEqual(res.checked, 0)
        text = "\n".join(capture.summary(cap, known, res))
        self.assertTrue("another network" in text)


class Plaintext(unittest.TestCase):
    def test_payload_in_clear_reported(self):
        # Its payload repeats the clear header's destination and source:
        # found in the raw frame, at byte 1.
        leak = peer_frame(RELAY, ROOT, b"\x00\x00\x11\x00", key=KEY)
        fine = peer_frame(FAR, ROOT, b"punch 07", key=KEY)
        heard = [(1000, fine, -80, 5.0), (3000, leak, -80, 5.0)]
        cap, known, res = _convert(_capture(heard))
        self.assertEqual(res.checked, 2)
        self.assertEqual(len(res.found), 1)
        t, n, port, length, at = res.found[0]
        self.assertEqual((n, port, length, at), (RELAY, 20, 4, 1))
        tx = _kind(res.records, "tx")[1]
        self.assertEqual(t, tx["t"])
        text = "\n".join(capture.summary(cap, known, res))
        self.assertTrue("1 FOUND" in text and "PLAINTEXT" in text)

    def test_short_payloads_not_judged(self):
        short = peer_frame(RELAY, ROOT, b"\x00\x00\x11", key=KEY)
        cap, known, res = _convert(_capture([(1000, short, -80, 5.0)]))
        self.assertEqual((res.checked, res.short, res.found), (0, 1, []))


class States(unittest.TestCase):
    def test_parent_of_a_first_hop_presence_rank_tells_attachment(self):
        # A box without route (rank 255) still sends its presence first to
        # a parent: the parent is known, the box detached.
        frame = probe_frame(RELAY, key=KEY)
        _, _, res = _convert(_capture([(1000, frame, -80, 5.0)]))
        st = _kind(res.records, "st")
        self.assertEqual(len(st), 1)
        self.assertEqual(
            (st[0]["n"], st[0]["r"], st[0]["p"], st[0]["at"]), (RELAY, 255, ROOT, False)
        )


class Lines(unittest.TestCase):
    """The capture file: ticks unwrapped, other and cut lines skipped,
    a frame begun before the sniffer's start, one run per capture."""

    def test_odd_lines(self):
        frame = peer_frame(RELAY, ROOT, b"punch 01", key=KEY)
        lines = _capture([(5, frame, -80, 5.0), (25000, frame, -81, 4.75)])
        lines.insert(1, "1795000000100 MicroPython v1.27.0 on 2025-12-09\n")
        lines.append("1795000025100 F 1234 1795000025000 -80 5.0 4a0\n")  # cut at Ctrl-C
        lines.append(
            "1795000025200 C %d 1795000025100 -119 -12.25\n" % ((TICKS0 + 25100) % PERIOD)
        )
        lines.append('1795000026000 END {"frames": 3, "crc": 1, "crashes": 0, "skip": 0}\n')
        cap = capture.parse(lines)
        self.assertEqual((cap.other, cap.malformed, len(cap.crc)), (1, 1, 1))
        self.assertEqual(capture.parse(lines[:-1] + ['1795000026000 END {"fra\n']).malformed, 2)
        self.assertEqual([f[0] for f in cap.frames], [5, 25000])  # across the wrap
        self.assertEqual(cap.crc[0][:2], (25100, 1795000025100))
        self.assertEqual(cap.end["skip"], 0)
        known, _ = capture.load_boxes(NETWORK, BOXES)
        res = capture.convert(cap, known)
        toa = time_on_air_ms(len(frame), 7)
        self.assertEqual(res.shift, toa - 5)  # the first frame began before the start
        tx = _kind(res.records, "tx")
        self.assertEqual([(t["t"], t["e"]) for t in tx], [(0, toa), (25000 - 5, 25000 + toa - 5)])
        self.assertEqual(res.utc0, UTC0 - (toa - 5))
        self.assertEqual(res.records[-1]["t"], 25100 + toa - 5)  # the CRC error's time
        text = "\n".join(capture.summary(cap, known, res))
        self.assertTrue(
            "CRC errors: 1" in text and "its next reset starts the application" in text
        )
        self.assertTrue("by the board: 3 frames, 1 CRC errors; in the capture: 2, 1" in text)

    def test_one_run_per_capture(self):
        lines = _capture([]) + _capture([])
        with self.assertRaises(ValueError):
            capture.parse(lines)
        with self.assertRaises(ValueError):
            capture.parse(["1795000000000 MicroPython\n"])


if __name__ == "__main__":
    unittest.main()
