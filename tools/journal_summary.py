#!/usr/bin/env python3
"""Summary of a box's field journal, read back from the board (``make journal``).

    tools/journal_summary.py DIR

DIR holds the journal's segments (``NNNNNN.log``, ``sily.journal``). Runs
on CPython 3 and on the MicroPython unix port. One paragraph per boot (the
lines of one boot number, in order):

- the box, version, build id (``none``: not deployed by ``make deploy``),
  reset cause (``watchdog``: the boot before hung), crash count and
  hardware watchdog timeout of its ``boot`` record; a boot in safe mode
  instead: its version, build and reasons (the ``safe`` line);
- its span (first to last line) and how it ended: ``stop`` (Ctrl-C),
  ``fatal`` (with the error) or no end line (power cut, a hang reset by
  the watchdog, or read while running);
- its clock (``sily.utc``): the UTC of its first line, by its first
  ``clock`` line or else by the ``boot`` record's ``utc`` (the RTC's), the
  ``clock`` lines and the largest departure between consecutive anchors
  (the ``boot`` record's then the ``clock`` lines: each anchor's UTC minus
  the one its predecessor predicts, ms; the boards' drift, a jump of the
  phone's clock, the RTC kept across a reset), or ``no clock``;
- the time from boot to the first attachment, the parent changes and
  detaches (events of the 1 Hz poll), the failures of own messages;
- the DODAG versions held and the root sequence (``ver``, ``seq`` of the
  minute records; those without a sequence left out: a gateway listening
  at boot reports version 0 before its first): the versions in turn (a
  change: the gateway issued one, as at each of its boots), the first and
  last sequence, and the longest time it stayed unchanged (on a node: no
  newer one from its parent, its gateway or its path silent);
- per link of the minute records' neighbour rows: the margin (dB) min,
  mean and max, the last RSSI mean and frames;
- the frames dropped on purpose per source (the box file's ``drop``,
  ``sily.drop``: ``drp`` of the last minute record), with their share of
  the source's frames (dropped and received) when it is a link above;
- the counters since boot (totals of the last minute record, the non-zero
  ones), the battery (first, last, lowest mV) and the journal's own
  costs: longest write and sync (ms), items lost, errors; the display's
  longest row write and page refresh (ms, ``sily.display``) and its
  failures;
- the access point's SSID (``boot`` record) and the web server's figures
  (``web`` of the minute records, ``sily.web``): requests, errors, longest
  stretch (ms), most stations joined; its failures at start (``ev wifi``);
- on the gateway, per origin of the reports received: presences,
  topologies, last battery; the turnaround of its immediate replies
  (``tr`` of the minute records, ``sily.turn``): the replies timed, the
  median of the per-minute medians, the worst minute's 90th percentile
  and the highest maximum (ms);
- the test traffic (``sily.traffic``, the ``tt`` field of the minute
  records): on a node, its messages submitted, accepted, refused and
  failed (per reason: the codes of ``mesh.link`` refusals, negative, and
  of ``mesh.reliable`` failures); on the gateway, per node, the messages
  received against those expected, duplicates, reordered, late, stale,
  node boots (the last minute record's totals), and the latency above the
  best case over the minute records: the median and highest of the
  per-minute medians and maxima, with the samples and minutes counted;
  the minutes with a clock offset (``offset_ms``), from which
  ``tools/survey.py`` places the node's journal in UTC.

Times are the boards' own (ms since the board's boot), but for the clock's
UTC: an analysis maps a line's ``t`` to UTC by the latest anchor before it,
or, for a node, through the gateway's clock offsets (``tools/survey.py``).
"""

import sys


def _parent(path):
    head = path.rsplit("/", 1)
    return head[0] if len(head) == 2 and head[0] else "."


sys.path.insert(0, _parent(_parent(__file__)) + "/src")
from sily import journal, utc  # noqa: E402

COUNTER_GROUPS = ("link", "rt", "radio")
OFFSET_MS = 14  # column of a node's row at the gateway (``sily.traffic.FIELDS``)


def boots(records):
    """Split records into runs of one boot number, in order: [[record, ...]]."""
    out = []
    current = None
    for r in records:
        if current is None or r.get("b") != current[0].get("b"):
            current = []
            out.append(current)
        current.append(r)
    return out


def _event(r, name):
    return r["k"] == "ev" and r.get("e") == name


def summarise(run):
    """Return the summary (a dict) of the records of one boot."""
    first = run[0]
    last = run[-1]
    boot = first if first["k"] == "boot" else None
    s = {
        "boot": first.get("b"),
        "record": boot,
        "safe": first if first["k"] == "safe" else None,
        "lines": len(run),
        "span_s": (last["t"] - first["t"]) / 1000,
        "end": last["k"] if last["k"] in ("stop", "fatal") else None,
        "error": last.get("err") if last["k"] == "fatal" else None,
    }
    t0 = first["t"]
    s["clock"] = clock(run, boot)
    attach = [r for r in run if _event(r, "attach")]
    s["attach_s"] = (attach[0]["t"] - t0) / 1000 if attach else None
    parents = []
    if attach:
        parents.append(attach[0].get("par"))
    for r in run:
        if _event(r, "parent"):
            parents.append(r.get("par"))
    s["parents"] = parents
    s["parent_changes"] = len([r for r in run if _event(r, "parent")])
    s["detaches"] = len([r for r in run if _event(r, "detach")])
    s["failures"] = len([r for r in run if _event(r, "fail")])
    minutes = [r for r in run if r["k"] == "min"]
    s["minutes"] = len(minutes)
    s["dodag"] = dodag(minutes)
    links = {}
    for m in minutes:
        for row in m.get("nb", ()):
            link = links.setdefault(row[0], {"margins": [], "rssi": None, "frames": 0})
            if row[2] is not None:
                link["margins"].append(row[2] / 10)
            if len(row) > 10 and row[10] is not None:
                link["rssi"] = row[10]
            if len(row) > 8 and row[8]:
                link["frames"] = row[8]
    s["links"] = links
    totals = {}
    if minutes:
        final = minutes[-1]
        for group in COUNTER_GROUPS:
            for key, value in (final.get(group) or {}).items():
                if value:
                    totals[group + "." + key] = value
    s["counters"] = totals
    s["drops"] = minutes[-1].get("drp") if minutes else None
    battery = [m["bat"] for m in minutes if m.get("bat") is not None]
    s["battery"] = (battery[0], battery[-1], min(battery)) if battery else None
    s["jw"] = max([m.get("jw", 0) for m in minutes] or [0])
    s["js"] = max([m.get("js", 0) for m in minutes] or [0])
    s["jq"] = minutes[-1].get("jq", 0) if minutes else 0
    s["je"] = minutes[-1].get("je", 0) if minutes else 0
    shown = [m for m in minutes if "ow" in m]
    s["display"] = (max(m["ow"] for m in shown), max(m["or"] for m in shown)) if shown else None
    s["display_errors"] = [r.get("err") for r in run if _event(r, "display")]
    s["ap"] = None if boot is None else boot.get("ap")
    s["web"] = web(minutes)
    s["wifi_errors"] = [r.get("err") for r in run if _event(r, "wifi")]
    origins = {}
    for r in run:
        if r["k"] in ("rp", "rt", "ri"):
            o = origins.setdefault(r["o"], {"rp": 0, "rt": 0, "ri": 0, "bat": None})
            o[r["k"]] += 1
            if r["k"] == "rp" and r.get("bat") is not None:
                o["bat"] = r["bat"]
    s["origins"] = origins
    s["turnaround"] = turnaround(minutes)
    s["traffic"] = traffic(minutes)
    return s


def clock(run, boot):
    """Return (UTC of the first line, ``clock`` or ``boot`` its anchor, clock
    lines, largest departure or None; ms) of the records of one boot and its
    boot record, None without an anchor."""
    lines = [r for r in run if r["k"] == "clock" and r.get("utc") is not None]
    anchors = [(r["t"], r["utc"]) for r in lines]
    if boot is not None and boot.get("utc") is not None:
        anchors.insert(0, (boot["t"], boot["utc"]))
    if not anchors:
        return None
    largest = None
    for i in range(1, len(anchors)):
        t, ms = anchors[i - 1]
        departure = anchors[i][1] - (ms + anchors[i][0] - t)
        if largest is None or abs(departure) > abs(largest):
            largest = departure
    t, ms = (lines[0]["t"], lines[0]["utc"]) if lines else anchors[0]
    return ms - (t - run[0]["t"]), "clock" if lines else "boot", len(lines), largest


def dodag(minutes):
    """Return (versions, first sequence, last sequence, longest unchanged s) of
    the minute records holding a root sequence, None without one."""
    versions = []
    first = last = None
    since = None  # time of the first record of the current (version, sequence)
    longest = 0
    for m in minutes:
        seq = m.get("seq")
        if seq is None:
            continue
        ver = m.get("ver")
        if not versions or versions[-1] != ver:
            versions.append(ver)
            since = m["t"]
        elif seq != last:
            since = m["t"]
        if first is None:
            first = seq
        last = seq
        longest = max(longest, m["t"] - since)
    if first is None:
        return None
    return versions, first, last, longest / 1000


def web(minutes):
    """Return (requests, errors, longest stretch ms, most stations) of the
    minute records' ``web``, None without one."""
    rows = [m["web"] for m in minutes if m.get("web")]
    if not rows:
        return None
    stations = [row[6] for row in rows if len(row) > 6 and row[6] is not None]
    return (
        rows[-1][0],
        rows[-1][1],
        max(row[5] for row in rows),
        max(stations) if stations else None,
    )


def _median(values):
    values = sorted(values)
    return values[(len(values) - 1) // 2]


def turnaround(minutes):
    """Return (replies, minutes, median of the medians, worst p90, highest
    maximum; 0.1 ms) of the minute records' ``tr``, None without one."""
    rows = [m["tr"] for m in minutes if m.get("tr")]
    if not rows:
        return None
    return (
        sum(row[0] for row in rows),
        len(rows),
        _median([row[1] for row in rows]),
        max(row[2] for row in rows),
        max(row[3] for row in rows),
    )


def traffic(minutes):
    """Return the test traffic's summary of the minute records, None without one."""
    with_tt = [m["tt"] for m in minutes if m.get("tt")]
    if not with_tt:
        return None
    final = with_tt[-1]
    if "o" not in final:  # a node's own counts
        return {"node": final}
    nodes = {}
    for tt in with_tt:
        for row in tt["o"]:
            node = nodes.setdefault(
                row[0], {"medians": [], "maxima": [], "samples": 0, "offsets": 0}
            )
            node["row"] = row
            if row[9]:
                node["samples"] += row[9]
                node["medians"].append(row[10])
                node["maxima"].append(row[12])
            if len(row) > OFFSET_MS and row[OFFSET_MS] is not None:
                node["offsets"] += 1
    lost = {"mal": final.get("mal", 0), "q": final.get("q", 0), "ovf": final.get("ovf", 0)}
    return {"gateway": nodes, "lost": lost}


def _reasons(pairs):
    return ", ".join("%s x%s" % (reason, n) for reason, n in pairs) or "none"


def _traffic_lines(t):
    if t is None:
        return []
    if "node" in t:
        n = t["node"]
        return [
            "  test traffic: %s submitted, %s accepted, refused: %s; failed: %s"
            % (n["n"], n["ok"], _reasons(n["ref"]), _reasons(n["fail"]))
        ]
    lines = []
    for address in sorted(t["gateway"]):
        node = t["gateway"][address]
        row = node["row"]
        rate = "%.1f %%" % (100 * row[2] / row[3]) if row[3] else "-"
        lines.append(
            "  test traffic from %s: %s/%s received (%s), %s duplicates, %s reordered,"
            " %s late, %s stale, %s boots" % ((address, row[2], row[3], rate) + tuple(row[4:9]))
        )
        if node["medians"]:
            lines.append(
                "    latency above the best case: minute medians %d ms median, %d highest;"
                " minute maxima %d ms median, %d highest; %d samples in %d minutes"
                % (
                    _median(node["medians"]),
                    max(node["medians"]),
                    _median(node["maxima"]),
                    max(node["maxima"]),
                    node["samples"],
                    len(node["medians"]),
                )
            )
        if node["offsets"]:
            lines.append(
                "    clock offset to the gateway in %d minutes (tools/survey.py --clocks"
                " places the node's journal by them)" % node["offsets"]
            )
    lost = t["lost"]
    if lost["mal"] or lost["q"] or lost["ovf"]:
        lines.append(
            "  test traffic lost to the gateway: %s malformed, %s queue overflows,"
            " %s beyond the node bound" % (lost["mal"], lost["q"], lost["ovf"])
        )
    return lines


def _drops(rows, links):
    items = []
    for address, dropped in rows:
        item = "from %s %d" % (address, dropped)
        link = links.get(address)
        if link is not None and dropped + link["frames"]:
            item += " (%.1f %%)" % (100 * dropped / (dropped + link["frames"]))
        items.append(item)
    return ", ".join(items)


def _clock_line(c):
    if c is None:
        return "  no clock"
    first, source, count, largest = c
    line = "  clock: first line at %s UTC by the %s, %d clock lines" % (
        utc.text(first),
        "first clock line" if source == "clock" else "boot record's RTC",
        count,
    )
    if largest is not None:
        line += ", largest departure %+d ms" % largest
    return line


def _duration(seconds):
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def _build(record):
    if "build" not in record:  # a journal written before the field
        return ""
    return ", build %s" % (record["build"] or "none")


def report(s):
    """Return the lines of summary s."""
    safe = s["safe"]
    if safe is not None:
        lines = [
            "boot %s: safe mode, version %s%s" % (s["boot"], safe.get("version"), _build(safe))
        ]
        return lines + ["  safe: %s" % reason for reason in safe.get("why", ())]
    lines = []
    b = s["record"]
    if b is not None:
        line = "boot %s: %s %s (%s) at %s, version %s%s, reset %s, crashes %s" % (
            s["boot"],
            b.get("role"),
            b.get("name"),
            b.get("address"),
            "SF%s" % b.get("sf"),
            b.get("version"),
            _build(b),
            b.get("reset"),
            b.get("crashes"),
        )
        if b.get("watchdog_s") is not None:
            line += ", watchdog %s s" % b["watchdog_s"]
        lines.append(line)
    else:
        lines.append("boot %s: no boot record" % s["boot"])
    if s["end"] == "fatal":
        end = "fatal: %s" % s["error"]
    elif s["end"] == "stop":
        end = "stopped"
    else:
        end = "no end line (power cut, hang, or read while running)"
    lines.append(
        "  span %s, %d lines, %d minute records; %s"
        % (_duration(s["span_s"]), s["lines"], s["minutes"], end)
    )
    lines.append(_clock_line(s["clock"]))
    attached = "never attached"
    if s["attach_s"] is not None:
        attached = "attached after " + _duration(s["attach_s"])
    lines.append(
        "  %s; parents %s (%d changes), %d detaches, %d failures"
        % (
            attached,
            " > ".join(str(p) for p in s["parents"]) or "none",
            s["parent_changes"],
            s["detaches"],
            s["failures"],
        )
    )
    if s["dodag"] is not None:
        versions, first, last, longest = s["dodag"]
        lines.append(
            "  DODAG version %s; root sequence %s first, %s last, unchanged for %s at most"
            % (" > ".join(str(v) for v in versions), first, last, _duration(longest))
        )
    for address in sorted(s["links"]):
        link = s["links"][address]
        m = link["margins"]
        if m:
            margin = "margin %.1f / %.1f / %.1f dB (min/mean/max, %d)" % (
                min(m),
                sum(m) / len(m),
                max(m),
                len(m),
            )
        else:
            margin = "margin unknown"
        lines.append(
            "  link %s: %s, RSSI %s dBm, %d frames"
            % (address, margin, link["rssi"], link["frames"])
        )
    if s["drops"]:
        lines.append("  dropped on purpose: " + _drops(s["drops"], s["links"]))
    if s["counters"]:
        items = ["%s %s" % (k, s["counters"][k]) for k in sorted(s["counters"])]
        lines.append("  counters since boot: " + ", ".join(items))
    if s["battery"] is not None:
        lines.append("  battery %d mV first, %d last, %d lowest" % s["battery"])
    lines.append(
        "  journal: longest write %s ms, sync %s ms; %s items lost, %s errors"
        % (s["jw"], s["js"], s["jq"], s["je"])
    )
    if s["display"] is not None:
        lines.append("  display: longest row write %s ms, refresh %s ms" % s["display"])
    for error in s["display_errors"]:
        lines.append("  display failed: %s" % error)
    if s["ap"] is not None or s["web"] is not None:
        figures = "no web figures"
        if s["web"] is not None:
            figures = (
                "%s requests, %s errors, longest stretch %s ms, at most %s stations" % s["web"]
            )
        lines.append("  access point %s: %s" % (s["ap"], figures))
    for error in s["wifi_errors"]:
        lines.append("  wifi failed: %s" % error)
    for origin in sorted(s["origins"]):
        o = s["origins"][origin]
        lines.append(
            "  from %s: %d presences, %d topologies, %d node information, battery %s mV"
            % (origin, o["rp"], o["rt"], o["ri"], o["bat"])
        )
    if s["turnaround"] is not None:
        replies, minutes, median, p90, top = s["turnaround"]
        lines.append(
            "  reply turnaround: %d replies in %d minutes; median of the minute medians"
            " %.1f ms, worst minute p90 %.1f ms, max %.1f ms"
            % (replies, minutes, median / 10, p90 / 10, top / 10)
        )
    lines.extend(_traffic_lines(s["traffic"]))
    return lines


def main(argv):
    if len(argv) != 2:
        print("usage: journal_summary.py DIR")
        return 2
    records, bad = journal.read(argv[1])
    if not records:
        print("%s: no journal line" % argv[1])
        return 1
    for run in boots(records):
        for line in report(summarise(run)):
            print(line)
    if bad:
        print("%d lines skipped (truncated or malformed)" % bad)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
