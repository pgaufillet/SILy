#!/usr/bin/env python3
"""Range survey: the links between a walking box and fixed ones, by distance.

    tools/survey.py --gpx TRACK.gpx --walker ADDRESS --at ADDRESS=LAT,LON
        [--at ...] [--band M] [--out DIR] JOURNAL_DIR...

Runs on CPython 3 and on the MicroPython unix port. Inputs:

- JOURNAL_DIR: the field journal of one box as ``make journal`` copies it
  (``OUT/journal``); the box (address, name, role) is the one of its
  ``boot`` records. One is the walker's (``--walker``), the others boxes
  at fixed positions (``--at``, decimal degrees, read from a map); a fixed
  box without journal is still a source the walker hears.
- TRACK.gpx: the walker's track, a GPX file as Garmin Connect exports it:
  its points (``trkpt``: ``lat``, ``lon``, ``time`` in UTC with ``Z``,
  fractional seconds or not; a time without ``Z`` is refused).

Windows: each ``min`` record covers the window from the previous minute
record of its boot (or the boot record) to its own ``t``. Its UTC comes
from the box's own anchors (``sily.utc``, as ``tools/journal_summary.py``:
the ``boot`` record's ``utc`` and the ``clock`` lines): a line's UTC is
``utc + t - t_anchor`` by the latest anchor at or before it, else by the
boot's first anchor. The walker's position at the window's middle is
interpolated linearly between the track's points. Left out and counted:
the records of a boot without anchor, or whose ``boot`` record the
journal's bound removed (the frames since boot cannot be differenced),
and windows whose middle falls outside the track's span or between two
points more than `GAP_MS` apart.

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
(truncated or malformed, ``sily.journal.read``); then per fixed box and
direction a table per distance band (``--band``, 50 m by default):
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
    " [--band M] [--out DIR] JOURNAL_DIR..."
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
RECEIVED = 2  # of a node's row at the gateway (``sily.traffic.FIELDS``)

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
            marks = anchors(run)
            gateway = boards[address]["role"] == "gateway"
            for r, start, links, traffic, received in windows(run):
                if not marks:
                    count["no anchor"] += 1
                    continue
                middle = to_utc(marks, (start + r["t"]) // 2)
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
                    "moved_m": moved(track, to_utc(marks, start), to_utc(marks, r["t"])),
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
    opts = {"--at": {}, "--band": BAND_M, "--out": "."}
    dirs = []
    i = 0
    while i < len(args):
        a = args[i]
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
    boards = {}
    for directory in dirs:
        b = board(directory)
        address = b["address"]
        if address in boards:
            raise ValueError("%s: a second journal of %s" % (directory, address))
        if address != opts["--walker"] and address not in opts["--at"]:
            raise ValueError("%s: box %s neither --walker nor --at" % (directory, address))
        boards[address] = b
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
