"""Range survey of a walking box against fixed ones (tools/survey.py).

Journals are written by ``sily.journal`` into directories under build/,
tracks in the form Garmin Connect exports. Positions are made up, on the
equator: a degree of latitude, or of longitude there, is `DEG_M` on the
survey's sphere.
"""

import math
import os
import unittest

from sily import journal, record, traffic, utc
from tools import survey

DIR = "build/test_survey"  # relative to the repository root, where the tests run
T0 = utc.days(2026, 11, 18) * utc.DAY_MS + 10 * 3600000  # 2026-11-18 10:00:00 UTC
DEG_M = 6371000 * math.pi / 180  # 111194.93 m
GATEWAY = (0.0, 0.0)
GARDEN = (0.0, 0.0009)  # 100.08 m east of the gateway
TRACK_10S = [(T0 + 10000 * k, 0.001 * k, 0.0) for k in range(7)]  # north, 11.1 m/s

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx creator="Garmin Connect" version="1.1"
  xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd"
  xmlns:ns3="http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
  xmlns="http://www.topografix.com/GPX/1/1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <metadata>
    <link href="connect.garmin.com"><text>Garmin Connect</text></link>
    <time>2026-11-18T09:59:58.000Z</time>
  </metadata>
  <trk>
    <name>Walk</name>
    <type>walking</type>
    <trkseg>
      <trkpt lat="0.000000000" lon="0.000000000">
        <ele>12.4</ele>
        <time>2026-11-18T10:00:00.000Z</time>
        <extensions>
          <ns3:TrackPointExtension><ns3:hr>92</ns3:hr></ns3:TrackPointExtension>
        </extensions>
      </trkpt>
      <trkpt lon='0.00025' lat='0.0002'>
        <ele>12.6</ele>
        <time>2026-11-18T10:00:10Z</time>
      </trkpt>
      <trkpt lat="-0.0001" lon="0.0005"><ele>12.0</ele><time>2026-11-18T10:00:20.5Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def _gpx(points):
    """Return a Garmin-like GPX document of points [(ms, lat, lon)]."""
    out = [GPX[: GPX.index("<trkseg>") + 8]]
    for ms, lat, lon in points:
        out.append(
            '<trkpt lat="%.9f" lon="%.9f"><ele>12.0</ele><time>%s</time>'
            "<extensions><ns3:TrackPointExtension><ns3:hr>95</ns3:hr>"
            "</ns3:TrackPointExtension></extensions></trkpt>" % (lat, lon, survey.iso(ms))
        )
    out.append("</trkseg></trk></gpx>")
    return "\n".join(out)


def _fresh(path):
    for name in ("build", DIR, path):
        try:
            os.mkdir(name)
        except OSError:
            pass
    for name in os.listdir(path):
        os.remove(path + "/" + name)


def _write(path, boot, lines):
    j = journal.Journal(path, boot, lambda: 0)
    j.open()
    for t, kind, fields in lines:
        j.write(t, kind, fields)
    j.close()


def _nb(address, frames, rssi=None, snr=None, last_margin=None, parent=0):
    # A neighbour row, columns `record.NEIGHBOUR_FIELDS`.
    return [address, 8, last_margin, last_margin, 128, 100, 1, parent, frames, -70, rssi, snr]


def _boot(address, name, role="node", ms=None):
    return {"role": role, "address": address, "name": name, "sf": 7, "utc": ms}


def _minute(rows, att=1, par=0, tt=None):
    fields = {"att": att, "par": par, "nb": rows}
    if tt is not None:
        fields["tt"] = tt
    return fields


def _tt(n, ok, fail=()):
    return {"n": n, "ok": ok, "ref": [], "fail": [list(pair) for pair in fail]}


def _received(*nodes):
    # A gateway's ``tt``: (node, received) rows, columns `traffic.FIELDS`.
    rows = [[node, 1, n, n + 3, 0, 0, 0, 0, 1, 0, None, None, None] for node, n in nodes]
    return {"o": rows, "mal": 0, "q": 0, "ovf": 0}


def _record(b, t, kind, fields=None):
    r = {"b": b, "t": t, "k": kind}
    r.update(fields or {})
    return r


class Track(unittest.TestCase):
    def test_garmin_points(self):
        # Namespaces, extensions and the metadata's time around the points;
        # both quote styles and orders of lat and lon; fractional seconds or not.
        self.assertEqual(
            survey.parse_gpx(GPX),
            [(T0, 0.0, 0.0), (T0 + 10000, 0.0002, 0.00025), (T0 + 20500, -0.0001, 0.0005)],
        )
        # Points out of order: sorted by time.
        points = [(T0 + 10000, 0.001, 0.0), (T0, 0.0, 0.0)]
        self.assertEqual(survey.parse_gpx(_gpx(points)), points[::-1])
        # An attribute's name, not the end of another one's.
        self.assertEqual(survey._attribute('<trkpt xlat="9" lat="1"', "lat"), "1")

    def test_time(self):
        self.assertEqual(survey.parse_time("2026-11-18T10:00:00Z"), T0)
        self.assertEqual(survey.parse_time("2026-11-18T10:00:01.5Z"), T0 + 1500)
        self.assertEqual(survey.parse_time("2026-11-18T10:00:01.123456Z"), T0 + 1123)
        self.assertEqual(survey.parse_time("2026-11-19T00:00:00.000Z"), T0 + 14 * 3600000)

    def test_zone_offset_refused(self):
        for text in ("2026-11-18T11:00:00+01:00", "2026-11-18T10:00:00.000"):
            with self.assertRaises(ValueError) as caught:
                survey.parse_time(text)
            self.assertEqual(str(caught.exception), "time %s: not in UTC (Z)" % text)
        bad = GPX.replace("10:00:10Z", "11:00:10.000+01:00")
        with self.assertRaises(ValueError) as caught:
            survey.parse_gpx(bad)
        self.assertTrue("not in UTC" in str(caught.exception), caught.exception)

    def test_malformed_points_refused(self):
        for old, new, message in (
            ("<time>2026-11-18T10:00:10Z</time>", "", "point 2: no time"),
            ("lat='0.0002'", "lat='north'", "point 2: lat north"),
            ('lon="0.0005"', "", "point 3: lon None"),
            (
                "<ele>12.0</ele><time>2026-11-18T10:00:20.5Z</time>\n      </trkpt>",
                "",
                "point 3: not closed",
            ),
        ):
            with self.assertRaises(ValueError) as caught:
                survey.parse_gpx(GPX.replace(old, new))
            self.assertEqual(str(caught.exception), message)

    def test_interpolation(self):
        track = [(0, 0.0, 0.0), (10000, 0.001, 0.002), (50000, 0.002, 0.002), (60000, 0.003, 0.0)]
        where, why = survey.position(track, 2500)
        self.assertEqual(why, None)
        self.assertAlmostEqual(where[0], 0.00025, places=12)
        self.assertAlmostEqual(where[1], 0.0005, places=12)
        self.assertEqual(survey.position(track, 10000), ((0.001, 0.002), None))
        self.assertEqual(survey.position(track, 60000), ((0.003, 0.0), None))
        where, _ = survey.position(track, 57500)
        self.assertAlmostEqual(where[1], 0.0005, places=12)
        # 40 s between the second and third points: no position inside.
        self.assertEqual(survey.position(track, 10001), (None, "gap"))
        self.assertEqual(survey.position(track, 49999), (None, "gap"))
        self.assertEqual(survey.position(track, 50000), ((0.002, 0.002), None))
        self.assertEqual(survey.position(track, -1), (None, "outside"))
        self.assertEqual(survey.position(track, 60001), (None, "outside"))
        # 30 s exactly is no gap.
        track = [(0, 0.0, 0.0), (30000, 0.003, 0.0)]
        where, why = survey.position(track, 10000)
        self.assertEqual(why, None)
        self.assertAlmostEqual(where[0], 0.001, places=12)

    def test_moved(self):
        # North 0.001 degree, back south: the path, not the displacement.
        track = [(0, 0.0, 0.0), (10000, 0.001, 0.0), (20000, 0.0, 0.0), (30000, 0.0, 0.001)]
        self.assertAlmostEqual(survey.moved(track, 5000, 15000), 0.001 * DEG_M, places=3)
        self.assertAlmostEqual(survey.moved(track, 0, 30000), 0.003 * DEG_M, places=3)
        # Clipped to the track's span.
        self.assertAlmostEqual(survey.moved(track, -9000, 10000), 0.001 * DEG_M, places=3)
        self.assertEqual(survey.moved(track, 31000, 40000), 0.0)

    def test_haversine(self):
        self.assertAlmostEqual(survey.distance((0.0, 0.0), (1.0, 0.0)), 111194.93, places=2)
        self.assertAlmostEqual(survey.distance((10.0, 3.0), (11.0, 3.0)), 111194.93, places=2)
        quarter = survey.distance((0.0, 0.0), (0.0, 90.0))
        self.assertAlmostEqual(quarter, 10007543.40, places=2)
        # 60 degrees north, a degree of longitude: the great circle, just
        # under the parallel's arc (cos 60 x 111194.93 = 55597.46 m).
        self.assertAlmostEqual(survey.distance((60.0, 5.0), (60.0, 6.0)), 55596.93, places=2)
        self.assertEqual(survey.distance((1.0, 2.0), (1.0, 2.0)), 0.0)


class Clock(unittest.TestCase):
    def test_between_and_before_anchors(self):
        marks = [(1000, T0), (61000, T0 + 60200)]
        self.assertEqual(survey.to_utc(marks, 30000), T0 + 29000)
        self.assertEqual(survey.to_utc(marks, 61000), T0 + 60200)
        self.assertEqual(survey.to_utc(marks, 70000), T0 + 69200)
        self.assertEqual(survey.to_utc(marks, 400), T0 - 600)  # before the first
        self.assertIsNone(survey.to_utc([], 400))

    def test_anchors_of_a_boot(self):
        run = [
            _record(1, 100, "boot", {"utc": T0}),
            _record(1, 5000, "clock", {"utc": T0 + 4930}),
            _record(1, 9000, "min"),
            _record(1, 9500, "clock", {"utc": None}),
        ]
        self.assertEqual(survey.anchors(run), [(100, T0), (5000, T0 + 4930)])
        run[0]["utc"] = None  # the RTC not set
        self.assertEqual(survey.anchors(run), [(5000, T0 + 4930)])

    def _board(self, runs):
        return {7: {"address": 7, "name": "box7", "role": "node", "runs": runs, "bad": 0}}

    def test_boot_placed_by_its_boot_record(self):
        track = TRACK_10S
        run = [
            _record(4, 500, "boot", {"utc": T0 + 2000}),
            _record(4, 10500, "min", {"nb": [_nb(3, 2)]}),
        ]
        rows, counts = survey.survey(self._board([run]), track, 3, {7: GARDEN})
        self.assertEqual(
            [(r["utc"], r["direction"], r["frames"]) for r in rows], [(T0 + 7000, "up", 2)]
        )
        self.assertAlmostEqual(rows[0]["lat"], 0.0007, places=12)
        self.assertEqual(counts[7]["placed"], 1)

    def test_boots_left_out(self):
        track = TRACK_10S
        no_anchor = [_record(5, 0, "boot", {"utc": None})]
        no_anchor += [_record(5, t, "min", {"nb": [_nb(3, 2)]}) for t in (10000, 20000)]
        # The boot record removed by the journal's bound, anchors kept.
        no_boot = [_record(6, 40000, "clock", {"utc": T0}), _record(6, 50000, "min")]
        safe = [_record(7, 800, "safe", {"why": []})]
        rows, counts = survey.survey(
            self._board([no_anchor, no_boot, safe]), track, 3, {7: GARDEN}
        )
        self.assertEqual(rows, [])
        self.assertEqual(
            counts[7],
            {
                "windows": 3,
                "placed": 0,
                "no boot record": 1,
                "no anchor": 2,
                "outside": 0,
                "gap": 0,
            },
        )


def _offset_row(node, boot, offset_t, offset_ms):
    # A gateway's ``tt`` row of node with a clock offset, columns `traffic.FIELDS`.
    return [node, boot, 1, 1, 0, 0, 0, 0, 1, 1, 0, 0, 0, offset_t, offset_ms]


def _box(address, name, role, runs):
    return {"address": address, "name": name, "role": role, "runs": runs, "bad": 0}


G0 = 100000  # the gateway's clock when the node boots
STEP = 120000  # the node's messages: one per 2 minutes of the gateway's clock
STEPS = 31  # an hour
JITTER = (0, 7, 3, 0, 12, 5, 0, 30, 2)  # ms above the fastest delivery, in turn
PAGE_K = 5  # the node's page opened at its k-th message


def _node_t(k):
    # The node's clock at its k-th message: 25 ppm fast (3 ms per 2 minutes).
    return STEP * k + 3 * k


def _rate_runs(page_ms=30):
    """A gateway set at boot by the host (UTC T0 at t 0) and a node booted at
    its G0 whose clock runs 25 ppm fast; the fastest delivery's remaining
    delay 4 ms, plus JITTER. The node's page opened at its PAGE_K-th
    message, the phone's time page_ms behind (the poll's delay)."""
    gateway = [_record(2, 0, "boot", {"utc": T0})]
    for k in range(STEPS):
        offset_ms = G0 + 4 + JITTER[k % len(JITTER)] - 3 * k
        tt = {"o": [_offset_row(3, 1, _node_t(k), offset_ms)], "mal": 0, "q": 0, "ovf": 0}
        gateway.append(_record(2, G0 + STEP * k + 30000, "min", {"tt": tt}))
    node = [_record(1, 0, "boot", {"utc": None})]
    node.append(_record(1, _node_t(PAGE_K), "clock", {"utc": T0 + G0 + STEP * PAGE_K - page_ms}))
    for k in range(STEPS):
        node.append(_record(1, _node_t(k) + 50000, "min", {"nb": []}))
    return {
        0: _box(0, "base", "gateway", [gateway]),
        3: _box(3, "box3", "node", [node]),
    }


class Offsets(unittest.TestCase):
    """A node's clock placed by the gateway's offsets (``sily.traffic``)."""

    def test_rows_of_a_node_boot(self):
        tt1 = {
            "o": [
                _offset_row(3, 1, 40000, 25000),
                _offset_row(3, 2, 41000, 9000),  # another boot of the node
                _offset_row(5, 1, 42000, 8000),  # another node
                _offset_row(3, 1, None, None),  # no sample in the period
                _offset_row(3, 1, 0, 0)[:13],  # written before the offsets
            ]
        }
        tt2 = {"o": [_offset_row(3, 1, 100000, 25010)]}
        run = [
            _record(2, 0, "boot", {"utc": T0}),
            _record(2, 60000, "min", {"tt": tt1}),
            # The gateway's page, after the node's submission (its t 100000)
            # and before the reception: the gateway's anchor at the latter.
            _record(2, 110000, "clock", {"utc": T0 + 110100}),
            _record(2, 120000, "min", {"tt": tt2}),
        ]
        unanchored = [_record(3, 0, "boot", {"utc": None}), _record(3, 60000, "min", {"tt": tt2})]
        gateway = _box(0, "base", "gateway", [run, unanchored])
        expected = [(40000, T0 + 25000), (100000, T0 + 100 + 25010)]
        self.assertEqual(survey.offsets(gateway, 3, 1), expected)
        self.assertEqual(survey.offsets(gateway, 3, 65537), expected)  # 16 bits on the air
        self.assertEqual(survey.offsets(gateway, 3, 3), [])

    def test_steady_rate_through_the_fastest(self):
        boards = _rate_runs()
        points = survey.offsets(boards[0], 3, 1)
        env = survey.envelope(points)
        # On the fastest deliveries' line whatever the jitter of the others.
        self.assertEqual(env, [(_node_t(k), T0 + G0 + 4 - 3 * k) for k in range(STEPS)])
        self.assertEqual("%.2f" % survey._rate(env), "25.00")

    def test_window_bounds_a_rate_change(self):
        # 50 ppm (3 ms a minute) for 40 minutes, then none: the hull of the
        # whole run would cut 60 ms below at the change, the window 22.5.
        points = [(60000 * k, 3 * min(k, 40)) for k in range(81)]
        env = survey.envelope(points)
        for (t, e), (_, d) in zip(env, points):
            self.assertTrue(d - 23 <= e <= d, (t, e, d))
        self.assertEqual(env[40], (2400000, 97))
        self.assertEqual(survey.envelope(points, 10**9)[40], (2400000, 60))

    def test_same_time_keeps_the_lowest(self):
        env = survey.envelope([(0, 10), (60000, 5), (60000, 9), (120000, 10)])
        self.assertEqual(env, [(0, 10), (60000, 5), (120000, 10)])

    def test_between_and_beyond_the_points(self):
        line = [(0, 10), (60000, 16), (120000, 4)]
        self.assertEqual(survey._line(line, -5000), 10)
        self.assertEqual(survey._line(line, 30000), 13)
        self.assertEqual(survey._line(line, 90000), 10)
        self.assertEqual(survey._line(line, 500000), 4)

    def test_gateway_offsets_win(self):
        boards = _rate_runs()
        node = boards[3]
        clock = survey.boot_clock(node, node["runs"][0], boards[0])
        self.assertEqual(clock.source, "gateway")
        # Placed 4 ms late (the fastest delivery's delay left), whatever its page.
        for k in (0, PAGE_K, STEPS - 1):
            self.assertEqual(clock.utc(_node_t(k)), T0 + G0 + STEP * k + 4)
        # Held beyond the last point: the node's clock counted from it.
        last = STEP * (STEPS - 1) + 4
        self.assertEqual(clock.utc(_node_t(STEPS - 1) + 180003), T0 + G0 + last + 180003)
        own = survey.boot_clock(node, node["runs"][0], None)
        self.assertEqual(own.source, "anchors")
        self.assertEqual(own.utc(_node_t(PAGE_K)), T0 + G0 + STEP * PAGE_K - 30)
        gw = survey.boot_clock(boards[0], boards[0]["runs"][0], boards[0])
        self.assertEqual((gw.source, gw.utc(1000)), ("anchors", T0 + 1000))
        lost = [_record(4, 0, "boot", {"utc": None}), _record(4, 60000, "min")]
        self.assertIsNone(survey.boot_clock(node, lost, boards[0]).source)

    def test_page_against_the_gateway(self):
        boards = _rate_runs(page_ms=30)
        reference, rows = survey.differences(boards[3], boards[3]["runs"][0], boards[0])
        page_t = _node_t(PAGE_K)
        self.assertEqual(reference, "gateway")
        self.assertEqual(
            rows,
            [(page_t, "clock", T0 + G0 + STEP * PAGE_K - 30, T0 + G0 + STEP * PAGE_K + 4)],
        )
        # The gateway: its page against its boot record's RTC (the host's).
        run = boards[0]["runs"][0] + [_record(2, 3600000, "clock", {"utc": T0 + 3600090})]
        self.assertEqual(
            survey.differences(boards[0], run, boards[0]),
            ("boot", [(3600000, "clock", T0 + 3600090, T0 + 3600000)]),
        )

    def test_clock_lines(self):
        lines = survey.clock_lines(_rate_runs(page_ms=30))
        self.assertEqual(
            lines,
            [
                "",
                "base (0), boot 2: by its own anchors (1).",
                "",
                "box3 (3), boot 1: by the gateway's clock offsets, 31 minute records over"
                " 1:00:00, rate +25.0 ppm.",
                "",
                "| t (s) | anchor | its UTC | by the gateway's offsets | difference (ms) |",
                "|---|---|---|---|---|",
                "| 600.015 | clock | 2026-11-18 10:11:39.970 | 2026-11-18 10:11:40.004 | +34 |",
            ],
        )

    def test_survey_places_a_node_without_anchor(self):
        boards = _rate_runs()
        boards[3]["runs"][0] = [r for r in boards[3]["runs"][0] if r["k"] != "clock"]
        track = [(T0 + G0 + 10000 * k, 0.00001 * k, 0.0) for k in range(400)]
        rows, counts = survey.survey(boards, track, 3, {0: GATEWAY})
        self.assertEqual((counts[3]["placed"], counts[3]["no anchor"]), (STEPS, 0))
        down = [r for r in rows if r["direction"] == "down"]
        # The windows' middles at node t 25000 and 110001: 24999.4 and
        # 109998.3 ms of the gateway's clock, placed 4 ms late (rounded down).
        self.assertEqual(down[0]["utc"], T0 + G0 + 25003)
        self.assertEqual(down[1]["utc"], T0 + G0 + 110002)


class Windows(unittest.TestCase):
    def test_columns(self):
        fields = record.NEIGHBOUR_FIELDS
        self.assertEqual(
            (
                survey.LAST_MARGIN,
                survey.PARENT,
                survey.FRAMES,
                survey.RSSI_MEAN,
                survey.SNR_MEAN,
            ),
            tuple(
                fields.index(name)
                for name in ("last_margin", "parent", "frames", "rssi_mean", "snr_mean")
            ),
        )
        self.assertEqual(survey.RECEIVED, traffic.FIELDS.index("received"))

    def test_frames_in_each_window(self):
        run = [
            _record(1, 0, "boot"),
            _record(
                1, 10000, "min", {"nb": [_nb(0, 4, -60, 95, 300, 1), _nb(7, 2, -80, 50, 150)]}
            ),
            _record(1, 20000, "min", {"nb": [_nb(0, 9, -63, 90, 290, 1)]}),  # 7 expired
            _record(
                1, 30000, "min", {"nb": [_nb(0, 9, None, None, 290, 1), _nb(7, 6, -79, 55, 160)]}
            ),
        ]
        out = survey.windows(run)
        self.assertEqual(
            [(w[0]["t"], w[1]) for w in out], [(10000, 0), (20000, 10000), (30000, 20000)]
        )
        self.assertEqual(out[0][2], {0: (4, -60, 95, 300, 1), 7: (2, -80, 50, 150, 0)})
        self.assertEqual(out[1][2], {0: (5, -63, 90, 290, 1)})
        # Back after its expiry: counted from its last row (the tap counts since boot).
        self.assertEqual(out[2][2], {0: (0, None, None, 290, 1), 7: (4, -79, 55, 160, 0)})

    def test_traffic_deltas(self):
        run = [
            _record(1, 0, "boot"),
            _record(1, 10000, "min", {"tt": _tt(1, 1)}),
            _record(1, 20000, "min"),  # no figures in that record
            _record(1, 30000, "min", {"tt": _tt(3, 2, [(3, 1)])}),
            _record(1, 40000, "min", {"tt": _tt(5, 4, [(3, 1), (5, 2)])}),
            _record(1, 50000, "min", {"tt": {"o": [], "mal": 0, "q": 0, "ovf": 0}}),  # a gateway's
        ]
        traffic = [w[3] for w in survey.windows(run)]
        self.assertEqual(traffic, [(1, 1, 0), None, (2, 1, 1), (2, 2, 2), None])

    def test_received_by_the_gateway(self):
        run = [
            _record(2, 0, "boot", {"role": "gateway"}),
            _record(2, 10000, "min", {"tt": _received((3, 1), (5, 4))}),
            _record(2, 20000, "min", {"tt": _received((5, 6))}),  # nothing yet from 3
            _record(2, 30000, "min", {"tt": _received((3, 4), (5, 6))}),
            _record(2, 40000, "min"),  # nothing came since boot
        ]
        received = [w[4] for w in survey.windows(run)]
        # From the node's previous row in the boot, not the previous record.
        self.assertEqual(received, [{3: 1, 5: 4}, {5: 2}, {3: 3, 5: 0}, {}])

    def test_nothing_heard(self):
        # The walker hears the gateway in its first window, nothing then: a
        # row with no frame (its last margin and means not of that window),
        # then no row.
        track = TRACK_10S
        run = [
            _record(1, 0, "boot", {"utc": T0}),
            _record(1, 10000, "min", {"nb": [_nb(0, 4, -60, 95, 300, 1)]}),
            _record(1, 20000, "min", {"nb": [_nb(0, 4, None, None, 300, 1)]}),
            _record(1, 30000, "min", {"nb": []}),
        ]
        boards = {3: {"address": 3, "name": "box3", "role": "node", "runs": [run], "bad": 0}}
        rows, _ = survey.survey(boards, track, 3, {0: GATEWAY})
        keys = ("frames", "rssi_dbm", "snr_db", "last_margin_db", "in_parents")
        self.assertEqual(
            [tuple(r[k] for k in keys) for r in rows],
            [(4, -60, 9.5, 30.0, 1), (0, None, None, None, 1), (0, None, None, None, None)],
        )


HEAD = "| band (m) | windows | heard | frames | RSSI (dBm) | SNR (dB) | lowest margin (dB) |"


class Bands(unittest.TestCase):
    def _row(self, metres, frames, rssi, snr, margin, traffic=None, att=1, par=0, received=None):
        row = {"distance_m": metres, "frames": frames, "rssi_dbm": rssi, "snr_db": snr}
        row.update(last_margin_db=margin, attached=att, parent=par, received=received)
        row["submitted"], row["accepted"], row["failed"] = traffic or (None, None, None)
        return row

    def test_delivery(self):
        # The walker's messages submitted in its windows (down rows) against
        # those the gateway received in its own (up rows), by band.
        down = [
            self._row(10.0, 1, -60, 9.5, 31.0, (2, 2, 0)),
            self._row(40.0, 0, None, None, None, (1, 1, 0)),
            self._row(60.0, 0, None, None, None),  # no test traffic
            self._row(110.0, 0, None, None, None, (2, 1, 0)),
        ]
        up = [
            self._row(20.0, 1, -60, 9.5, 31.0, received=1),
            self._row(30.0, 0, None, None, None, received=1),
            self._row(70.0, 1, -60, 9.5, 31.0, received=1),
            self._row(160.0, 0, None, None, None, received=0),
        ]
        self.assertEqual(
            survey.delivery(down, up, 50),
            [
                "| band (m) | submitted | received | delivery |",
                "|---|---|---|---|",
                "| 0-50 | 3 | 2 | 67 % |",
                "| 50-100 | - | 1 | - |",
                "| 100-150 | 2 | - | - |",
                "| 150-200 | - | 0 | - |",
            ],
        )

    def test_tables(self):
        rows = [
            self._row(10.0, 4, -60, 9.5, 31.0, (1, 1, 0)),
            self._row(49.9, 2, -66, 8.0, 18.5, (1, 1, 0), par=7),
            self._row(50.0, 0, None, None, None, (2, 1, 1), att=0, par=None),
            self._row(120.4, 1, -90, -2.5, 2.0, (1, 0, 0), par=7),
            self._row(130.0, 0, None, None, None, (1, 1, 0)),
            self._row(140.0, 0, None, None, None, (1, 1, 0)),
        ]
        self.assertEqual(
            survey.table(rows, 50, True),
            [
                HEAD + " submitted | accepted | failed | relayed | detached |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|",
                # RSSI and SNR weighted by frames: (4 x -60 + 2 x -66) / 6.
                "| 0-50 | 2 | 100 % | 6 | -62.0 | 9.0 | 18.5 | 2 | 2 | 0 | 50 % | 0 % |",
                "| 50-100 | 1 | 0 % | 0 | - | - | - | 2 | 1 | 1 | 0 % | 100 % |",
                "| 100-150 | 3 | 33 % | 1 | -90.0 | -2.5 | 2.0 | 3 | 2 | 0 | 33 % | 0 % |",
                "",
                "Farthest frame heard: 120 m.",
            ],
        )
        up = [self._row(10.0, 0, None, None, None), self._row(60.0, 0, None, None, None)]
        self.assertEqual(
            survey.table(up, 100, False),
            [
                HEAD,
                "|---|---|---|---|---|---|---|",
                "| 0-100 | 2 | 0 % | 0 | - | - | - |",
                "",
                "No frame heard.",
            ],
        )


WALKER_DIR = DIR + "/walker"
GARDEN_DIR = DIR + "/garden"
GATEWAY_DIR = DIR + "/gateway"
OUT_DIR = DIR + "/out"
TRACK = DIR + "/track.gpx"
CLI_DIR = DIR + "/cli"
CLI_WALKER = DIR + "/cli_walker"
CLI_GATEWAY = DIR + "/cli_gateway"
CLI_MIXED = DIR + "/cli_mixed"
AT = ["--at", "0=0.0,0.0", "--at", "7=0.0,0.0009"]


class Survey(unittest.TestCase):
    """A walk north from the gateway (0) at 2.22 m/s, the garden box (7) 100 m
    east of it; the walker is 3."""

    @classmethod
    def setUpClass(cls):
        for path in (WALKER_DIR, GARDEN_DIR, GATEWAY_DIR, OUT_DIR):
            _fresh(path)
        os.rmdir(OUT_DIR)  # created by the survey
        track = [(T0 + 10000 * k, 0.0002 * k, 0.0) for k in range(7)]
        with open(TRACK, "w") as f:
            f.write(_gpx(track))
        # The walker: an earlier boot whose boot record was removed, the
        # boot of the walk (its clock 500 ms behind the phone from t 35 s),
        # then one without an anchor.
        _write(WALKER_DIR, 0, [(50000, "min", _minute([]))])
        _write(
            WALKER_DIR,
            1,
            [
                (0, "boot", _boot(3, "box3")),
                (
                    10000,
                    "min",
                    _minute([_nb(0, 4, -61, 95, 310, 1), _nb(7, 2, -81, 50, 140)], tt=_tt(1, 1)),
                ),
                (20000, "min", _minute([_nb(0, 9, -63, 90, 290, 1)], tt=_tt(2, 2))),
                (25000, "clock", {"utc": T0 + 25000}),
                (
                    30000,
                    "min",
                    _minute(
                        [_nb(0, 9, None, None, 290, 1), _nb(7, 6, -79, 55, 160, 1)],
                        par=7,
                        tt=_tt(3, 2, [(3, 1)]),
                    ),
                ),
                (35000, "clock", {"utc": T0 + 35500}),
                (
                    40000,
                    "min",
                    _minute(
                        [_nb(0, 12, -70, 40, 120), _nb(7, 6, None, None, 160, 1)],
                        att=0,
                        par=None,
                        tt=_tt(4, 3, [(3, 1)]),
                    ),
                ),
                (90000, "min", _minute([_nb(0, 12)], tt=_tt(9, 8, [(3, 1)]))),  # after the track
            ],
        )
        _write(WALKER_DIR, 2, [(0, "boot", _boot(3, "box3")), (10000, "min", _minute([]))])
        # The garden box, placed by its boot record's RTC.
        _write(
            GARDEN_DIR,
            4,
            [
                (500, "boot", _boot(7, "box7", ms=T0 - 1500)),
                (12000, "min", _minute([_nb(3, 3, -81, 48, 150), _nb(0, 40, -70, 70, 200, 1)])),
                (22000, "min", _minute([_nb(0, 48, -70, 70, 200, 1)])),
                (32000, "min", _minute([_nb(3, 7, -84, 30, 120), _nb(0, 56, -70, 70, 200, 1)])),
            ],
        )
        with open(GARDEN_DIR + "/" + journal.segment_name(1), "ab") as f:
            f.write(b'{"b":4,"t":40000,"k":"m')  # cut by a power loss
        # The gateway: its first window before its first anchor.
        _write(
            GATEWAY_DIR,
            2,
            [
                (0, "boot", _boot(0, "base", "gateway")),
                (10000, "min", _minute([_nb(3, 5, -58, 98, 320)], par=None, tt=_received((5, 4)))),
                (15000, "clock", {"utc": T0 + 15000}),
                (
                    20000,
                    "min",
                    _minute([_nb(3, 11, -62, 92, 300)], par=None, tt=_received((3, 2), (5, 5))),
                ),
            ],
        )
        cls.argv = ["survey.py", "--gpx", TRACK, "--walker", "3"] + AT
        cls.argv += ["--out", OUT_DIR, WALKER_DIR, GARDEN_DIR, GATEWAY_DIR]
        opts, dirs = survey.options(cls.argv[1:])
        cls.track, cls.boards = survey.load(opts, dirs)
        cls.rows, cls.counts = survey.survey(cls.boards, cls.track, 3, opts["--at"])

    def _rows(self, direction, observer, source):
        return [
            r
            for r in self.rows
            if (r["direction"], r["observer"], r["source"]) == (direction, observer, source)
        ]

    def test_boxes(self):
        self.assertEqual(sorted(self.boards), [0, 3, 7])
        self.assertEqual((self.boards[0]["name"], self.boards[0]["role"]), ("base", "gateway"))
        self.assertEqual(len(self.track), 7)

    def test_records_left_out(self):
        self.assertEqual(
            self.counts[3],
            {
                "windows": 7,
                "placed": 4,
                "no boot record": 1,
                "no anchor": 1,
                "outside": 1,
                "gap": 0,
            },
        )
        self.assertEqual((self.counts[7]["placed"], self.counts[0]["placed"]), (3, 2))

    def test_down(self):
        rows = self._rows("down", 3, 0)
        # Window middles 5, 15, 25 s by the first anchor, 35 s by the second.
        self.assertEqual([r["utc"] - T0 for r in rows], [5000, 15000, 25000, 35500])
        for r, lat in zip(rows, (0.0001, 0.0003, 0.0005, 0.00071)):
            self.assertAlmostEqual(r["lat"], lat, places=12)
            self.assertAlmostEqual(r["distance_m"], lat * DEG_M, places=3)
        self.assertEqual([r["frames"] for r in rows], [4, 5, 0, 3])
        self.assertEqual([r["last_margin_db"] for r in rows], [31.0, 29.0, None, 12.0])
        self.assertEqual(
            [(r["submitted"], r["accepted"], r["failed"]) for r in rows],
            [(1, 1, 0), (1, 1, 0), (1, 0, 1), (1, 1, 0)],
        )
        self.assertEqual(
            [(r["attached"], r["parent"]) for r in rows], [(1, 0), (1, 0), (1, 7), (0, None)]
        )
        garden = self._rows("down", 3, 7)
        self.assertEqual([r["frames"] for r in garden], [2, 0, 4, 0])
        self.assertEqual([r["in_parents"] for r in garden], [0, None, 1, 1])
        for r in garden:
            self.assertAlmostEqual(
                r["distance_m"], math.sqrt(r["lat"] ** 2 + 0.0009**2) * DEG_M, delta=0.01
            )
        # The walker's path over the window from 30 s (first anchor) to 40.5 s.
        self.assertAlmostEqual(rows[3]["moved_m"], 0.00021 * DEG_M, places=3)

    def test_up(self):
        garden = self._rows("up", 7, 3)
        self.assertEqual([r["utc"] - T0 for r in garden], [4250, 15000, 25000])
        self.assertEqual([r["frames"] for r in garden], [3, 0, 4])
        self.assertEqual([r["rssi_dbm"] for r in garden], [-81, None, -84])
        self.assertAlmostEqual(garden[0]["lat"], 0.000085, places=12)
        self.assertAlmostEqual(
            garden[0]["distance_m"], math.sqrt(0.000085**2 + 0.0009**2) * DEG_M, delta=0.01
        )
        # Its first window starts before the track: the path from the track's start.
        self.assertAlmostEqual(garden[0]["moved_m"], 0.0002 * DEG_M, places=3)
        self.assertEqual(garden[0]["submitted"], None)
        gateway = self._rows("up", 0, 3)
        self.assertEqual([r["utc"] - T0 for r in gateway], [5000, 15000])
        self.assertEqual([(r["frames"], r["snr_db"]) for r in gateway], [(5, 9.8), (6, 9.2)])
        self.assertAlmostEqual(gateway[1]["distance_m"], 0.0003 * DEG_M, places=3)
        # The walker's messages received: none yet in its first window.
        self.assertEqual([r["received"] for r in gateway], [0, 2])
        self.assertEqual([r["received"] for r in garden], [None, None, None])  # a node
        self.assertEqual(len(self.rows), 13)

    def test_command_line(self):
        out = []
        self.assertEqual(survey.main(self.argv, out.append), 0)
        with open(OUT_DIR + "/survey.md") as f:
            self.assertEqual(f.read(), "\n".join(out) + "\n")
        self.assertEqual(out[0], "# Range survey")
        self.assertEqual(
            out[2],
            "Track: 7 points, 2026-11-18 10:00:00.000 to 2026-11-18 10:01:00.000 UTC,"
            " 0 gaps over 30 s.",
        )
        self.assertTrue("| box3 (3) | 7 | 4 | 1 | 1 | 1 | 0 |" in out, out)
        head = out.index("## base (0) heard by the walker (down)")
        self.assertEqual(
            out[head + 4 : head + 8],
            [
                "| 0-50 | 2 | 100 % | 9 | -62.1 | 9.2 | 29.0 | 2 | 2 | 0 | 0 % | 0 % |",
                "| 50-100 | 2 | 50 % | 3 | -70.0 | 4.0 | 12.0 | 2 | 1 | 1 | 50 % | 50 % |",
                "",
                "Farthest frame heard: 79 m.",
            ],
        )
        self.assertTrue("## The walker heard by box7 (7) (up)" in out, out)
        head = out.index("Test messages of the walker:")
        self.assertEqual(
            out[head + 2 : head + 8],
            [
                "| band (m) | submitted | received | delivery |",
                "|---|---|---|---|",
                "| 0-50 | 2 | 2 | 100 % |",
                "| 50-100 | 2 | - | - |",
                "",
                "## box7 (7) heard by the walker (down)",
            ],
        )
        self.assertEqual(out.count("Test messages of the walker:"), 1)  # not under the node's
        self.assertTrue("1 journal lines skipped (truncated or malformed)." in out, out)
        with open(OUT_DIR + "/survey.csv") as f:
            lines = f.read().split("\n")
        self.assertEqual(lines[0], ",".join(survey.COLUMNS))
        self.assertEqual(len(lines), 15)  # 13 rows, the final newline
        self.assertEqual(
            lines[1],
            "2026-11-18T10:00:04.250Z,4,12000,up,7,3,0.000085,0.000000,101,22,3,-81,4.8,15.0,0,1,0,,,,",
        )
        self.assertEqual(
            lines[2],
            "2026-11-18T10:00:05.000Z,1,10000,down,3,0,0.000100,0.000000,11,22,4,-61,9.5,31.0,1,1,0,1,1,0,",
        )
        self.assertEqual(
            lines[4],
            "2026-11-18T10:00:05.000Z,2,10000,up,0,3,0.000100,0.000000,11,22,5,-58,9.8,32.0,0,1,,,,,0",
        )

    def test_cells(self):
        self.assertEqual(survey.cell(None, "frames"), "")
        self.assertEqual(survey.cell(T0 + 1500, "utc"), "2026-11-18T10:00:01.500Z")
        self.assertEqual(survey.cell(-2.5, "snr_db"), "-2.5")
        self.assertEqual(survey.cell(48.1234567, "lat"), "48.123457")
        self.assertEqual(survey.cell(78.948, "distance_m"), "79")
        self.assertEqual(survey.cell('a "b", c'), '"a ""b"", c"')
        self.assertEqual(survey.cell("down"), "down")


class CommandLine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _fresh(CLI_DIR)
        with open(CLI_DIR + "/offset.gpx", "w") as f:
            f.write(GPX.replace("10:00:10Z", "11:00:10+01:00"))
        with open(CLI_DIR + "/track.gpx", "w") as f:
            f.write(GPX)
        for path, address, name in ((CLI_WALKER, 3, "box3"), (CLI_GATEWAY, 0, "base")):
            _fresh(path)
            _write(path, 1, [(0, "boot", _boot(address, name))])
        _fresh(CLI_MIXED)
        _write(CLI_MIXED, 1, [(0, "boot", _boot(3, "box3"))])
        _write(CLI_MIXED, 2, [(0, "boot", _boot(5, "box5"))])

    def _main(self, args):
        out = []
        status = survey.main(["survey.py"] + args, out.append)
        return status, out[0]

    def test_clocks(self):
        out = []
        status = survey.main(["survey.py", "--clocks", CLI_WALKER, CLI_GATEWAY], out.append)
        self.assertEqual(status, 0)
        self.assertEqual(
            out,
            [
                "# Clocks",
                "",
                "base (0), boot 1: not placed.",
                "",
                "box3 (3), boot 1: not placed.",
            ],
        )
        only = "--clocks takes journal directories only"
        for args, message in (
            (["--clocks"], "no journal directory"),
            (["--clocks", "--gpx", "t.gpx", CLI_WALKER], only),
            (["--clocks", "--walker", "3", CLI_WALKER], only),
            (["--clocks", "--at", "0=0.0,0.0", CLI_WALKER], only),
            (["--clocks", "--band", "20", CLI_WALKER], only),
        ):
            self.assertEqual(self._main(args), (2, "survey: " + message), args)
        self.assertEqual(
            self._main(["--clocks", CLI_WALKER, CLI_WALKER]),
            (1, "survey: " + CLI_WALKER + ": a second journal of 3"),
        )

    def test_usage_errors(self):
        base = ["--gpx", "t.gpx", "--walker", "3", "--at", "0=0.0,0.0", "j"]
        for args, message in (
            (base[2:], "no --gpx"),
            (base[:2] + base[4:], "no --walker"),
            (base[:4] + base[6:], "no --at"),
            (base[:-1], "no journal directory"),
            (base + ["--fast"], "unknown option --fast"),
            (base + ["--band"], "--band without value"),
            (base + ["--band", "0"], "--band 0 below 1 m"),
            (base + ["--walker", "three"], "--walker three: not an integer"),
            (base + ["--at", "7=0.0"], "--at 7=0.0: not ADDRESS=LAT,LON"),
            (base + ["--at", "7=91.0,0.0"], "--at 7=91.0,0.0: out of range"),
            (base + ["--at", "0=1.0,1.0"], "--at 0 given twice"),
            (base + ["--at", "3=1.0,1.0"], "--at 3: the walker has no fixed position"),
        ):
            self.assertEqual(self._main(args), (2, "survey: " + message), args)

    def test_input_errors(self):
        gpx = ["--gpx", CLI_DIR + "/track.gpx"] + AT
        out = ["--out", CLI_DIR + "/none/out"]  # cannot be created
        for args, message in (
            (
                ["--gpx", CLI_DIR + "/none.gpx"] + AT + [CLI_WALKER],
                CLI_DIR + "/none.gpx: cannot read",
            ),
            (
                ["--gpx", CLI_DIR + "/offset.gpx"] + AT + [CLI_WALKER],
                "time 2026-11-18T11:00:10+01:00: not in UTC (Z)",
            ),
            (gpx + [CLI_DIR + "/none"], CLI_DIR + "/none: no journal"),
            (gpx + [CLI_DIR], CLI_DIR + ": no boot record"),
            (gpx + [CLI_GATEWAY], "no journal of the walker 3"),
            (gpx + [CLI_WALKER, CLI_WALKER], CLI_WALKER + ": a second journal of 3"),
            (gpx + [CLI_MIXED], CLI_MIXED + ": boot records of several addresses"),
            (gpx + out + [CLI_WALKER], CLI_DIR + "/none/out: cannot write"),
            (
                ["--gpx", CLI_DIR + "/track.gpx", "--at", "7=0.0,0.0009", CLI_WALKER, CLI_GATEWAY],
                CLI_GATEWAY + ": box 0 neither --walker nor --at",
            ),
        ):
            args = ["--walker", "3"] + args
            if "--out" not in args:
                args += ["--out", DIR + "/cli_out"]  # never the current directory
            self.assertEqual(self._main(args), (1, "survey: " + message), args)
