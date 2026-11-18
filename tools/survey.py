#!/usr/bin/env python3
"""Range survey: the links between a walking box and fixed ones, by distance.

    tools/survey.py --gpx TRACK.gpx --walker ADDRESS --at ADDRESS=LAT,LON
        [--at ...] [--band M] [--out DIR] JOURNAL_DIR...
    tools/survey.py --clocks JOURNAL_DIR...

Runs on CPython 3 and on the MicroPython unix port. ``--clocks`` prints
the clocks' section alone (below), without track. Inputs:

- JOURNAL_DIR: the field journal of one box as ``make journal`` copies it
  (``OUT/journal``); the box (address, name, role) is the one of its
  ``boot`` records. One is the walker's (``--walker``), the others boxes
  at fixed positions (``--at``, decimal degrees, read from a map); a fixed
  box without journal is still a source the walker hears.
- TRACK.gpx: the walker's track, a GPX file as Garmin Connect exports it:
  its points (``trkpt``: ``lat``, ``lon``, ``time`` in UTC with ``Z``,
  fractional seconds or not; a time without ``Z`` is refused).

Clocks (`boot_clock`): a box's anchors (``sily.utc``, as
``tools/journal_summary.py``: the ``boot`` record's ``utc``, the RTC's,
set by ``make configure`` or a page since the last power-on, and the
``clock`` lines, a phone's) place a line at ``utc + t - t_anchor`` by
the latest anchor at or before it, else by the boot's first anchor. A
node's boot also has the clock offsets the gateway journals each minute
(``sily.traffic``: ``offset_t``, ``offset_ms`` of the node's row; their
rows matched on the low 16 bits of the boot number): each gives a point
(`offsets`), the node's ``t`` and the UTC of the gateway's clock at
``offset_t + offset_ms`` by the gateway's anchors of that boot, an upper
bound of the node's UTC then (plus the fastest delivery's delay left:
the steps, sensing, waits and further hops beyond those the gateway
took off). Their lower envelope (`envelope`: at each point, the lower
convex hull of the points within `ENVELOPE_MS` / 2 of it) follows the
fastest deliveries and the clocks' drift (tens of ppm, steady over the
window; a change of rate bends the hull by at most the change times a
quarter of the window); a line's UTC is its ``t`` plus the envelope,
linear between its points and held beyond its first and last. Where a
node's boot has offsets they win over its own anchors: they follow its
clock minute by minute where an anchor is held by its crystal (about
90 ms an hour at 25 ppm) and they put every node on the gateway's clock,
so that the boxes' windows line up; its own anchors place the boots the
gateway did not hear, and the gateway's. The clocks' section (in
``survey.md``, and the whole output of ``--clocks``): per box and boot,
its placement (the minute records with offsets, their span and the
node's rate against the gateway's clock, positive when fast) and each anchor of its
own against the placement without it: a node's against its offsets, a
gateway's ``clock`` lines against its ``boot`` record's RTC alone
(difference: that placement minus the anchor, ms).

Windows: each ``min`` record covers the window from the previous minute
record of its boot (or the boot record) to its own ``t``; its UTC comes
from the clocks above. The walker's position at the window's middle is
interpolated linearly between the track's points. Left out and counted:
the records of a boot without anchor nor offsets (``no anchor``), or
whose ``boot`` record the journal's bound removed (the frames since boot
cannot be differenced), and windows whose middle falls outside the
track's span or between two points more than `GAP_MS` apart.

Rows (``survey.csv``, `COLUMNS`), one per (window, observer, source):
``down`` for each window of the walker and each fixed box (the walker
hearing it), ``up`` for each window of a fixed box (the box hearing the
walker). ``utc`` is the window's middle (ISO 8601), ``boot`` and ``t``
those of its record, ``lat`` and ``lon`` the walker's position then,
``distance_m`` from it to the fixed box (haversine on a sphere of radius
`EARTH_M`), ``moved_m`` the walker's path along the track over the
window. From the observer's neighbour row of the source
(``sily.record.NEIGHBOUR_FIELDS``): ``frames`` heard in the window (the
row's frames since boot minus those of the source's previous row in the
boot, 0 before its first), the RSSI and SNR means over them, the last
margin (empty without a frame in the window) and ``in_parents`` (the
source in the observer's parent set). A source absent from the record
(never heard, or expired from the neighbour table: unheard for 4 presence
periods) counts as heard 0 frames, ``in_parents`` empty; the tap counts
frames since boot across the table's expiry, so the frames of a source
that comes back count from its last row. ``attached`` and ``parent``: the
observer's state at the record. ``submitted``, ``accepted``, ``failed``,
on the walker's rows: its test messages in the window (``sily.traffic``,
deltas of its ``tt`` totals; failures counted when reported, after the
retries), the same on each of its rows. ``received``, on the gateway's
rows: the walker's test messages it received in its window
(``sily.traffic``: ``received`` of the walker's row in its ``tt``,
counted from that row in its previous record holding it in the boot, 0
before; 0 when the row is absent).

Summary (``survey.md``, also printed): the track, then per box the
windows, placed and left out per reason, and the journal lines skipped
(truncated or malformed, ``sily.journal.read``); the clocks' section;
then per fixed box and direction a table per distance band (``--band``,
50 m by default):
windows, share of windows with a frame, frames, RSSI and SNR means over
the frames, the lowest last margin; on the walker's tables, its test
messages and the share of windows relayed (a parent other than the
gateway, address 0) and detached; below each table, the farthest
distance with a frame; under the gateway's, the walker's test messages
by band: submitted (its windows), received (the gateway's) and the
delivery share. Both come from different boxes' windows: the latency
(seconds, against windows of 10 s) and the retries move a few messages
across windows and bands, a band's share possibly above 100 %. The
records give the parent, not the hop count: relayed means two hops or
more. Both files go into ``--out`` (the current directory by default;
created when missing, not its parents).
"""

import math
import os
import sys


def _parent(path):
    head = path.rsplit("/", 1)
    return head[0] if len(head) == 2 and head[0] else "."


sys.path.insert(0, _parent(_parent(__file__)) + "/src")
from sily import journal, utc  # noqa: E402

USAGE = (
    "usage: survey.py --gpx TRACK.gpx --walker ADDRESS --at ADDRESS=LAT,LON [--at ...]"
    " [--band M] [--out DIR] JOURNAL_DIR...\n"
    "       survey.py --clocks JOURNAL_DIR..."
)
BAND_M = 50
GAP_MS = 30000  # track points further apart: no position between them
EARTH_M = 6371000.0
GATEWAY = 0  # the root's address (``sily.config``)

# Columns of a neighbour row (``sily.record.NEIGHBOUR_FIELDS``).
LAST_MARGIN = 3
PARENT = 7
FRAMES = 8
RSSI_MEAN = 10
SNR_MEAN = 11
# Columns of a node's row at the gateway (``sily.traffic.FIELDS``).
RECEIVED = 2
NODE_BOOT = 1
OFFSET_T = 13
OFFSET_MS = 14
ENVELOPE_MS = 1800000  # window of the clock offsets' lower envelope (30 min)

REASONS = ("no boot record", "no anchor", "outside", "gap")
COLUMNS = (
    "utc",
    "boot",
    "t",
    "direction",
    "observer",
    "source",
    "lat",
    "lon",
    "distance_m",
    "moved_m",
    "frames",
    "rssi_dbm",
    "snr_db",
    "last_margin_db",
    "in_parents",
    "attached",
    "parent",
    "submitted",
    "accepted",
    "failed",
    "received",
)
DECIMALS = {"lat": 6, "lon": 6, "distance_m": 0, "moved_m": 0, "snr_db": 1, "last_margin_db": 1}


# --- Track ------------------------------------------------------------------------------


def parse_time(text):
    """Return the ms since the Unix epoch of ``YYYY-MM-DDThh:mm:ss[.fff]Z``."""
    if not text.endswith("Z"):
        raise ValueError("time %s: not in UTC (Z)" % text)
    ymd, _, clock = text[:-1].partition("T")
    hms, _, fraction = clock.partition(".")
    try:
        year, month, day = [int(x) for x in ymd.split("-")]
        h, m, s = [int(x) for x in hms.split(":")]
        ms = int((fraction + "00")[:3]) if fraction else 0
    except ValueError:
        raise ValueError("time %s: not YYYY-MM-DDThh:mm:ss[.fff]Z" % text) from None
    return utc.days(year, month, day) * utc.DAY_MS + (h * 3600 + m * 60 + s) * 1000 + ms


def _attribute(tag, name):
    """Return the value of attribute name of a start tag, None when absent."""
    i = 0
    while True:
        i = tag.find(name + "=", i)
        if i < 0:
            return None
        if tag[i - 1] in " \t\r\n":
            break
        i += 1
    i += len(name) + 1
    quote = tag[i : i + 1]
    end = tag.find(quote, i + 1) if quote in ("'", '"') else -1
    return tag[i + 1 : end] if end > 0 else None


def parse_gpx(text):
    """Return the track points [(ms, lat, lon)] of a GPX document, in time order."""
    points = []
    i = 0
    while True:
        i = text.find("<trkpt", i)
        if i < 0:
            break
        head = text.find(">", i)
        end = text.find("</trkpt>", i)
        n = len(points) + 1
        if head < 0 or end < 0:
            raise ValueError("point %d: not closed" % n)
        tag = text[i:head]
        body = text[head + 1 : end]
        values = []
        for name in ("lat", "lon"):
            value = _attribute(tag, name)
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                raise ValueError("point %d: %s %s" % (n, name, value)) from None
        start = body.find("<time>")
        stop = body.find("</time>")
        if start < 0 or stop < start:
            raise ValueError("point %d: no time" % n)
        points.append((parse_time(body[start + 6 : stop].strip()), values[0], values[1]))
        i = end
    points.sort(key=lambda p: p[0])
    return points


def distance(p, q):
    """Return the distance (m) between (lat, lon) p and q (haversine)."""
    lat1 = math.radians(p[0])
    lat2 = math.radians(q[0])
    a = math.sin((lat2 - lat1) / 2) ** 2
    a += math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(q[1] - p[1]) / 2) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(a)))


def _index(track, ms):
    # Index of the last point at or before ms, within the track's span.
    lo = 0
    hi = len(track) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if track[mid][0] <= ms:
            lo = mid
        else:
            hi = mid
    return hi if track[hi][0] <= ms else lo


def _at(track, i, ms):
    # Position at ms between points i and i + 1; point i at or before it, or
    # when it is the last (the track's ends hold beyond them).
    p = track[i]
    if i + 1 >= len(track) or ms <= p[0]:
        return p[1], p[2]
    q = track[i + 1]
    f = (ms - p[0]) / (q[0] - p[0])
    return p[1] + f * (q[1] - p[1]), p[2] + f * (q[2] - p[2])


def position(track, ms):
    """Return ((lat, lon), None) of the walker at ms, else (None, reason):
    ``outside`` the track's span, ``gap`` between points more than `GAP_MS`
    apart."""
    if ms < track[0][0] or ms > track[-1][0]:
        return None, "outside"
    i = _index(track, ms)
    if ms > track[i][0] and track[i + 1][0] - track[i][0] > GAP_MS:
        return None, "gap"
    return _at(track, i, ms), None


def moved(track, a, b):
    """Return the walker's path (m) along the track from a to b (ms), within its span."""
    i = _index(track, a)
    j = _index(track, b)
    points = [_at(track, i, a)] + [(p[1], p[2]) for p in track[i + 1 : j + 1]]
    points.append(_at(track, j, b))
    return sum(distance(points[k], points[k + 1]) for k in range(len(points) - 1))


def gaps(track):
    """Return the number of intervals between points longer than `GAP_MS`."""
    return len([k for k in range(1, len(track)) if track[k][0] - track[k - 1][0] > GAP_MS])


# --- Journals ---------------------------------------------------------------------------


def boots(records):
    """Split records into runs of one boot number, in order: [[record, ...]]."""
    out = []
    for r in records:
        if not out or r.get("b") != out[-1][0].get("b"):
            out.append([])
        out[-1].append(r)
    return out


def anchors(run):
    """Return the UTC anchors [(t, utc)] of the records of one boot, in order."""
    out = []
    for r in run:
        if r["k"] in ("boot", "clock") and r.get("utc") is not None:
            out.append((r["t"], r["utc"]))
    return out


def to_utc(marks, t):
    """Return the UTC (ms) of t by anchors marks: the latest at or before t,
    else the first; None without one."""
    if not marks:
        return None
    t_anchor, ms = marks[0]
    for mark in marks:
        if mark[0] > t:
            break
        t_anchor, ms = mark
    return ms + t - t_anchor


def gateway_of(boards):
    """Return the gateway's box among boards (`board`), None without one."""
    b = boards.get(GATEWAY)
    return b if b is not None and b["role"] == "gateway" else None


def offsets(gateway, address, boot):
    """Return the clock points [(t, d)] of boot number boot of node address
    from the gateway's journal (`board`), sorted.

    One per row of the node's boot (the low 16 bits of its number) with an
    offset in the gateway's minute records (``sily.traffic``): t its
    ``offset_t`` (the node's clock), d the UTC of the gateway's clock at
    ``offset_t + offset_ms`` (by the anchors of that gateway boot, `to_utc`)
    minus t: t + d is the node's UTC at t, late by what is left of the
    fastest delivery's delay. A gateway boot without anchor gives none.
    """
    points = []
    for run in gateway["runs"]:
        marks = anchors(run)
        if not marks:
            continue
        for r in run:
            tt = r.get("tt") if r["k"] == "min" else None
            if not tt or "o" not in tt:
                continue
            for row in tt["o"]:
                if row[0] != address or len(row) <= OFFSET_MS or row[OFFSET_MS] is None:
                    continue
                if row[NODE_BOOT] != boot & 0xFFFF:
                    continue
                t = row[OFFSET_T]
                points.append((t, to_utc(marks, t + row[OFFSET_MS]) - t))
    points.sort()
    return points


def _hull(points):
    """Return the lower convex hull of points [(t, d)] (sorted, distinct t)."""
    out = []
    for p in points:
        while len(out) >= 2:
            o = out[-2]
            a = out[-1]
            if (a[0] - o[0]) * (p[1] - o[1]) - (a[1] - o[1]) * (p[0] - o[0]) > 0:
                break  # a below the chord from o to p
            out.pop()
        out.append(p)
    return out


def _line(points, t):
    """Return d at t of the polyline points [(t, d)] (sorted): linear between
    them, held beyond its ends (floor of the interpolation, ms)."""
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    lo = 0
    hi = len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    a = points[lo]
    b = points[hi]
    return a[1] + (b[1] - a[1]) * (t - a[0]) // (b[0] - a[0])


def envelope(points, window_ms=ENVELOPE_MS):
    """Return the lower envelope [(t, d)] of clock points [(t, d)] (`offsets`).

    At each point's t, the lower convex hull of the points within
    window_ms / 2 of it, at that t: below every point of the window, on the
    straight line through the lowest ones (the fastest deliveries) when the
    clocks drift at a steady rate. The window bounds the error of a rate
    that changes (temperature). Of points with the same t, the lowest.
    """
    pts = []
    for p in points:
        if pts and pts[-1][0] == p[0]:
            continue  # sorted: the lowest d of a t first
        pts.append(p)
    half = window_ms // 2
    out = []
    lo = 0
    hi = 0
    for p in pts:
        while pts[lo][0] < p[0] - half:
            lo += 1
        while hi < len(pts) and pts[hi][0] <= p[0] + half:
            hi += 1
        out.append((p[0], _line(_hull(pts[lo:hi]), p[0])))
    return out


class BootClock:
    """The UTC of the lines of one boot of a box (`boot_clock`): `utc`.

    source: ``gateway`` (the gateway's clock offsets, `envelope`: held
    beyond its first and last points), ``anchors`` (the box's own, `to_utc`)
    or None (not placed); marks: the box's anchors; env: the envelope, None
    without offsets.
    """

    def __init__(self, marks, env):
        self.marks = marks
        self.env = env
        if env:
            self.source = "gateway"
        elif marks:
            self.source = "anchors"
        else:
            self.source = None

    def utc(self, t):
        """Return the UTC (ms) of t, None when not placed."""
        if self.env:
            return t + _line(self.env, t)
        return to_utc(self.marks, t)


def boot_clock(box, run, gateway):
    """Return the `BootClock` of run, a boot of box (`board`); gateway: the
    gateway's box or None. The gateway's offsets of a node's boot win over
    its own anchors."""
    env = None
    if gateway is not None and box["role"] != "gateway" and run:
        points = offsets(gateway, box["address"], run[0]["b"])
        if points:
            env = envelope(points)
    return BootClock(anchors(run), env)


def windows(run):
    """Return the windows of the minute records of one boot, its boot record
    first: [(record, start t, links, traffic, received)].

    links: {source: (frames in the window, RSSI mean, SNR mean, last margin,
    parent flag)} of the record's neighbour rows, frames counted from the
    source's previous row in the boot (0 before its first); traffic:
    (submitted, accepted, failed) in the window, None without ``tt``;
    received: {node: test messages received in the window} of a gateway's
    ``tt`` rows, counted from the node's previous row in the boot (0
    before its first).
    """
    start = run[0]["t"]
    seen = {}  # source -> frames since boot at its last row
    got = {}  # node -> messages received since boot at its last row
    totals = (0, 0, 0)
    out = []
    for r in run:
        if r["k"] != "min":
            continue
        links = {}
        for row in r.get("nb") or ():
            frames = row[FRAMES] or 0
            links[row[0]] = (
                frames - seen.get(row[0], 0),
                row[RSSI_MEAN],
                row[SNR_MEAN],
                row[LAST_MARGIN],
                row[PARENT],
            )
            seen[row[0]] = frames
        traffic = None
        tt = r.get("tt")
        if tt and "n" in tt:  # a node's own totals
            now = (tt["n"], tt["ok"], sum([pair[1] for pair in tt["fail"]]))
            traffic = (now[0] - totals[0], now[1] - totals[1], now[2] - totals[2])
            totals = now
        received = {}
        if tt and "o" in tt:  # the gateway's rows per node
            for row in tt["o"]:
                received[row[0]] = row[RECEIVED] - got.get(row[0], 0)
                got[row[0]] = row[RECEIVED]
        out.append((r, start, links, traffic, received))
        start = r["t"]
    return out


def board(directory):
    """Return the box of a journal directory: {address, name, role, runs, bad}."""
    try:
        records, bad = journal.read(directory)
    except OSError:
        raise ValueError("%s: no journal" % directory) from None
    found = [r for r in records if r["k"] == "boot"]
    if not found:
        raise ValueError("%s: no boot record" % directory)
    for r in found:
        if r.get("address") != found[0].get("address"):
            raise ValueError("%s: boot records of several addresses" % directory)
    last = found[-1]
    return {
        "address": last.get("address"),
        "name": last.get("name"),
        "role": last.get("role"),
        "runs": boots(records),
        "bad": bad,
    }


# --- Survey -----------------------------------------------------------------------------


def _row(base, direction, observer, source, link, metres):
    row = dict(base)
    row["direction"] = direction
    row["observer"] = observer
    row["source"] = source
    row["distance_m"] = metres
    row["frames"] = 0
    for key in ("rssi_dbm", "snr_db", "last_margin_db", "in_parents"):
        row[key] = None
    if link is not None:
        frames, rssi, snr, margin, parent = link
        row["frames"] = frames
        row["rssi_dbm"] = rssi
        row["snr_db"] = None if snr is None else snr / 10
        if frames and margin is not None:
            row["last_margin_db"] = margin / 10
        row["in_parents"] = parent
    return row


def survey(boards, track, walker, at):
    """Return (rows, counts) of the survey.

    boards: {address: `board`}; track: `parse_gpx`; walker: its address;
    at: {address: (lat, lon)} of the fixed boxes. rows: dicts of `COLUMNS`
    (``utc`` in ms) in UTC order; counts: {address: {"windows", "placed",
    and each of `REASONS`: records}}.
    """
    rows = []
    counts = {}
    gateway_box = gateway_of(boards)
    for address in sorted(boards):
        count = {"windows": 0, "placed": 0}
        for reason in REASONS:
            count[reason] = 0
        counts[address] = count
        for run in boards[address]["runs"]:
            minutes = len([r for r in run if r["k"] == "min"])
            count["windows"] += minutes
            if run[0]["k"] != "boot":
                count["no boot record"] += minutes
                continue
            clock = boot_clock(boards[address], run, gateway_box)
            gateway = boards[address]["role"] == "gateway"
            for r, start, links, traffic, received in windows(run):
                if clock.source is None:
                    count["no anchor"] += 1
                    continue
                middle = clock.utc((start + r["t"]) // 2)
                where, why = position(track, middle)
                if where is None:
                    count[why] += 1
                    continue
                count["placed"] += 1
                base = {
                    "utc": middle,
                    "boot": r["b"],
                    "t": r["t"],
                    "lat": where[0],
                    "lon": where[1],
                    "moved_m": moved(track, clock.utc(start), clock.utc(r["t"])),
                    "attached": r.get("att"),
                    "parent": r.get("par"),
                    "submitted": None,
                    "accepted": None,
                    "failed": None,
                    "received": None,
                }
                if address != walker:
                    if gateway:
                        base["received"] = received.get(walker, 0)
                    metres = distance(where, at[address])
                    rows.append(_row(base, "up", address, walker, links.get(walker), metres))
                    continue
                if traffic is not None:
                    base["submitted"], base["accepted"], base["failed"] = traffic
                for source in sorted(at):
                    metres = distance(where, at[source])
                    rows.append(_row(base, "down", walker, source, links.get(source), metres))
    rows.sort(key=lambda row: (row["utc"], row["direction"], row["observer"], row["source"]))
    return rows, counts


def bands(rows, band_m):
    """Return {band: figures} of rows (band k: distances from k * band_m to
    (k + 1) * band_m)."""
    out = {}
    for row in rows:
        k = int(row["distance_m"] // band_m)
        f = out.get(k)
        if f is None:
            f = {
                "windows": 0,
                "heard": 0,
                "frames": 0,
                "weight": 0,  # frames with RSSI and SNR means
                "rssi": 0,
                "snr": 0,
                "margin": None,
                "traffic": None,
                "relayed": 0,
                "detached": 0,
                "received": None,
            }
            out[k] = f
        f["windows"] += 1
        n = row["frames"]
        if n:
            f["heard"] += 1
            f["frames"] += n
            if row["rssi_dbm"] is not None and row["snr_db"] is not None:
                f["weight"] += n
                f["rssi"] += n * row["rssi_dbm"]
                f["snr"] += n * row["snr_db"]
            margin = row["last_margin_db"]
            if margin is not None and (f["margin"] is None or margin < f["margin"]):
                f["margin"] = margin
        if row["submitted"] is not None:
            t = f["traffic"] or [0, 0, 0]
            t[0] += row["submitted"]
            t[1] += row["accepted"]
            t[2] += row["failed"]
            f["traffic"] = t
        if row["received"] is not None:
            f["received"] = (f["received"] or 0) + row["received"]
        if not row["attached"]:
            f["detached"] += 1
        elif row["parent"] is not None and row["parent"] != GATEWAY:
            f["relayed"] += 1
    return out


def _percent(n, total):
    return "%d %%" % ((200 * n + total) // (2 * total))


def _decimal(value):
    return "-" if value is None else "%.1f" % value


def table(rows, band_m, walker_side):
    """Return the lines of the band table of rows (one link and direction);
    walker_side: with the walker's test messages and parents."""
    head = "| band (m) | windows | heard | frames | RSSI (dBm) | SNR (dB) | lowest margin (dB) |"
    if walker_side:
        head += " submitted | accepted | failed | relayed | detached |"
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    figures = bands(rows, band_m)
    for k in sorted(figures):
        f = figures[k]
        w = f["weight"]
        cells = [
            "%d-%d" % (k * band_m, (k + 1) * band_m),
            "%d" % f["windows"],
            _percent(f["heard"], f["windows"]),
            "%d" % f["frames"],
            _decimal(f["rssi"] / w if w else None),
            _decimal(f["snr"] / w if w else None),
            _decimal(f["margin"]),
        ]
        if walker_side:
            t = f["traffic"]
            cells.extend(["-", "-", "-"] if t is None else ["%d" % n for n in t])
            cells.append(_percent(f["relayed"], f["windows"]))
            cells.append(_percent(f["detached"], f["windows"]))
        lines.append("| " + " | ".join(cells) + " |")
    heard = [row["distance_m"] for row in rows if row["frames"]]
    lines.append("")
    lines.append("Farthest frame heard: %.0f m." % max(heard) if heard else "No frame heard.")
    return lines


def delivery(down, up, band_m):
    """Return the lines of the walker's test messages by band: submitted in
    its windows (down rows) against received in the gateway's (up rows)."""
    sent = bands(down, band_m)
    got = bands(up, band_m)
    lines = ["| band (m) | submitted | received | delivery |", "|---|---|---|---|"]
    for k in sorted(set(sent) | set(got)):
        n = sent[k]["traffic"][0] if k in sent and sent[k]["traffic"] else None
        r = got[k]["received"] if k in got else None
        cells = ["%d-%d" % (k * band_m, (k + 1) * band_m)]
        cells.extend(["-" if v is None else "%d" % v for v in (n, r)])
        cells.append(_percent(r, n) if n and r is not None else "-")
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _label(address, boards):
    b = boards.get(address)
    return "%s (%s)" % (b["name"], address) if b is not None else "%s" % address


def differences(box, run, gateway):
    """Return (reference, [(t, kind, anchor UTC, reference UTC)]) of a boot:
    each anchor of its own against its placement without it, None and []
    without one.

    A node placed by the gateway's offsets: each anchor (``boot`` record's
    RTC, ``clock`` lines: a phone's) against them. Otherwise a ``boot``
    record's RTC (a gateway's, set by ``make configure``): each ``clock``
    line against it alone.
    """
    clock = boot_clock(box, run, gateway)
    marks = []
    for r in run:
        if r["k"] in ("boot", "clock") and r.get("utc") is not None:
            marks.append((r["t"], r["k"], r["utc"]))
    if clock.env:
        return "gateway", [(t, k, ms, clock.utc(t)) for t, k, ms in marks]
    if marks and marks[0][1] == "boot":
        base = [(marks[0][0], marks[0][2])]
        return "boot", [(t, k, ms, to_utc(base, t)) for t, k, ms in marks[1:]]
    return None, []


def _rate(env):
    """Return the rate (ppm) of a node's clock against the gateway's from its
    envelope: positive, fast; None over less than a minute."""
    span = env[-1][0] - env[0][0]
    if span < 60000:
        return None
    return -(env[-1][1] - env[0][1]) * 1000000 / span


def clock_lines(boards):
    """Return the lines (Markdown) of the boxes' clocks: per box and boot, its
    placement (`boot_clock`) and its anchors against the placement without
    them (`differences`)."""
    gateway = gateway_of(boards)
    lines = []
    for address in sorted(boards):
        box = boards[address]
        for run in box["runs"]:
            if run[0]["k"] == "safe":
                continue
            clock = boot_clock(box, run, gateway)
            head = "%s, boot %s: " % (_label(address, boards), run[0]["b"])
            if clock.source == "gateway":
                env = clock.env
                rate = _rate(env)
                head += "by the gateway's clock offsets, %d minute records over %s, %s" % (
                    len(env),
                    _span(env[-1][0] - env[0][0]),
                    "rate -" if rate is None else "rate %+.1f ppm" % rate,
                )
            elif clock.source == "anchors":
                head += "by its own anchors (%d)" % len(clock.marks)
            else:
                head += "not placed"
            lines.extend(["", head + "."])
            reference, rows = differences(box, run, gateway)
            if not rows:
                continue
            against = "the gateway's offsets" if reference == "gateway" else "the boot's RTC"
            lines.extend(
                [
                    "",
                    "| t (s) | anchor | its UTC | by %s | difference (ms) |" % against,
                    "|---|---|---|---|---|",
                ]
            )
            for t, kind, ms, placed in rows:
                lines.append(
                    "| %.3f | %s | %s | %s | %+d |"
                    % (t / 1000, kind, utc.text(ms), utc.text(placed), placed - ms)
                )
    return lines


def _span(ms):
    s = ms // 1000
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def summary(rows, counts, boards, track, walker, at, band_m):
    """Return the lines of the survey's summary (Markdown)."""
    lines = ["# Range survey", ""]
    if track:
        lines.append(
            "Track: %d points, %s to %s UTC, %d gaps over %d s."
            % (
                len(track),
                utc.text(track[0][0]),
                utc.text(track[-1][0]),
                gaps(track),
                GAP_MS // 1000,
            )
        )
    fixed = ["%s at %.6f, %.6f" % ((_label(a, boards),) + at[a]) for a in sorted(at)]
    lines.append("Walker %s; fixed: %s." % (_label(walker, boards), "; ".join(fixed)))
    lines.append(
        "Bands of %d m; RSSI and SNR: means over the frames heard; lowest margin: of the"
        " last frames of the windows." % band_m
    )
    lines.append("")
    lines.append(
        "| box | windows | placed | no boot record | no anchor | outside the track | gap |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for address in sorted(counts):
        c = counts[address]
        cells = [_label(address, boards), c["windows"], c["placed"]] + [c[r] for r in REASONS]
        lines.append("| " + " | ".join(["%s" % cell for cell in cells]) + " |")
    bad = sum([b["bad"] for b in boards.values()])
    if bad:
        lines.extend(["", "%d journal lines skipped (truncated or malformed)." % bad])
    lines.extend(["", "## Clocks"])
    lines.extend(clock_lines(boards))
    for source in sorted(at):
        for direction in ("down", "up"):
            if direction == "down":
                title = "%s heard by the walker (down)"
                subset = [r for r in rows if r["direction"] == "down" and r["source"] == source]
            else:
                if source not in boards:
                    continue  # no journal of that box
                title = "The walker heard by %s (up)"
                subset = [r for r in rows if r["direction"] == "up" and r["observer"] == source]
            lines.extend(["", "## " + title % _label(source, boards), ""])
            if subset:
                lines.extend(table(subset, band_m, direction == "down"))
            else:
                lines.append("No window placed.")
            if direction == "up" and boards[source]["role"] == "gateway":
                down = [r for r in rows if r["direction"] == "down" and r["source"] == source]
                lines.extend(["", "Test messages of the walker:", ""])
                lines.extend(delivery(down, subset, band_m))
    return lines


# --- Output -----------------------------------------------------------------------------


def iso(ms):
    """Return ms since the Unix epoch as ``YYYY-MM-DDThh:mm:ss.mmmZ``."""
    dt = utc.to_datetime(ms)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
        dt[0],
        dt[1],
        dt[2],
        dt[4],
        dt[5],
        dt[6],
        dt[7] // 1000,
    )


def cell(value, column=None):
    """Return value as a CSV field of column (quoted only when needed)."""
    if value is None:
        return ""
    if column == "utc":
        return iso(value)
    if column in DECIMALS:
        return ("%." + str(DECIMALS[column]) + "f") % value
    text = "%s" % value
    if "," in text or '"' in text or "\n" in text:
        text = '"' + text.replace('"', '""') + '"'
    return text


def csv_lines(rows):
    """Return the lines of ``survey.csv``: the header, one per row."""
    lines = [",".join(COLUMNS)]
    for row in rows:
        lines.append(",".join([cell(row[c], c) for c in COLUMNS]))
    return lines


def _write(path, lines):
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# --- Command line -----------------------------------------------------------------------


def _position(text):
    address, _, rest = text.partition("=")
    try:
        lat, lon = [float(x) for x in rest.split(",")]
        address = int(address)
    except ValueError:
        raise ValueError("--at %s: not ADDRESS=LAT,LON" % text) from None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("--at %s: out of range" % text)
    return address, (lat, lon)


def options(args):
    """Return (options, journal directories) of the arguments; ValueError on a mistake."""
    opts = {"--at": {}, "--band": BAND_M, "--out": ".", "--clocks": False}
    dirs = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--clocks":
            opts[a] = True
            i += 1
            continue
        if a in ("--gpx", "--walker", "--at", "--band", "--out"):
            if i + 1 >= len(args):
                raise ValueError("%s without value" % a)
            value = args[i + 1]
            i += 2
            if a == "--at":
                address, where = _position(value)
                if address in opts["--at"]:
                    raise ValueError("--at %d given twice" % address)
                opts["--at"][address] = where
            elif a in ("--walker", "--band"):
                try:
                    opts[a] = int(value)
                except ValueError:
                    raise ValueError("%s %s: not an integer" % (a, value)) from None
            else:
                opts[a] = value
            continue
        if a.startswith("--"):
            raise ValueError("unknown option %s" % a)
        dirs.append(a)
        i += 1
    if opts["--clocks"]:
        track_options = ("--gpx", "--walker")
        if [o for o in track_options if o in opts] or opts["--at"] or len(args) > len(dirs) + 1:
            raise ValueError("--clocks takes journal directories only")
        if not dirs:
            raise ValueError("no journal directory")
        return opts, dirs
    for required in ("--gpx", "--walker"):
        if required not in opts:
            raise ValueError("no %s" % required)
    if not opts["--at"]:
        raise ValueError("no --at")
    if opts["--walker"] in opts["--at"]:
        raise ValueError("--at %d: the walker has no fixed position" % opts["--walker"])
    if opts["--band"] < 1:
        raise ValueError("--band %d below 1 m" % opts["--band"])
    if not dirs:
        raise ValueError("no journal directory")
    return opts, dirs


def load_boards(dirs):
    """Return {address: `board`} of journal directories; ValueError on a mistake."""
    boards = {}
    for directory in dirs:
        b = board(directory)
        address = b["address"]
        if address in boards:
            raise ValueError("%s: a second journal of %s" % (directory, address))
        b["directory"] = directory
        boards[address] = b
    return boards


def load(opts, dirs):
    """Return (track, boards) of the command line's inputs; ValueError on a mistake."""
    try:
        with open(opts["--gpx"]) as f:
            text = f.read()
    except OSError:
        raise ValueError("%s: cannot read" % opts["--gpx"]) from None
    track = parse_gpx(text)
    if not track:
        raise ValueError("%s: no track point" % opts["--gpx"])
    boards = load_boards(dirs)
    for address in sorted(boards):
        if address != opts["--walker"] and address not in opts["--at"]:
            raise ValueError(
                "%s: box %s neither --walker nor --at" % (boards[address]["directory"], address)
            )
    if opts["--walker"] not in boards:
        raise ValueError("no journal of the walker %d" % opts["--walker"])
    return track, boards


def main(argv, echo=print):
    try:
        opts, dirs = options(argv[1:])
    except ValueError as e:
        echo("survey: %s" % e)
        echo(USAGE)
        return 2
    if opts["--clocks"]:
        try:
            boards = load_boards(dirs)
        except ValueError as e:
            echo("survey: %s" % e)
            return 1
        for line in ["# Clocks"] + clock_lines(boards):
            echo(line)
        return 0
    try:
        track, boards = load(opts, dirs)
    except ValueError as e:
        echo("survey: %s" % e)
        return 1
    walker = opts["--walker"]
    rows, counts = survey(boards, track, walker, opts["--at"])
    lines = summary(rows, counts, boards, track, walker, opts["--at"], opts["--band"])
    out = opts["--out"]
    try:
        os.mkdir(out)
    except OSError:
        pass  # exists
    try:
        _write(out + "/survey.csv", csv_lines(rows))
        _write(out + "/survey.md", lines)
    except OSError:
        echo("survey: %s: cannot write" % out)
        return 1
    for line in lines:
        echo(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
