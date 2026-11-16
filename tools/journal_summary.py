#!/usr/bin/env python3
"""Summary of a box's field journal, read back from the board (``make journal``).

    tools/journal_summary.py DIR

DIR holds the journal's segments (``NNNNNN.log``, ``sily.journal``). Runs
on CPython 3 and on the MicroPython unix port. One paragraph per boot (the
lines of one boot number, in order):

- the box, version, reset cause and crash count of its ``boot`` record;
- its span (first to last line) and how it ended: ``stop`` (Ctrl-C),
  ``fatal`` (with the error) or no end line (power cut, or read while
  running);
- the time from boot to the first attachment, the parent changes and
  detaches (events of the 1 Hz poll), the failures of own messages;
- per link of the minute records' neighbour rows: the margin (dB) min,
  mean and max, the last RSSI mean and frames;
- the counters since boot (totals of the last minute record, the non-zero
  ones), the battery (first, last, lowest mV) and the journal's own
  costs: longest write and sync (ms), items lost, errors;
- on the gateway, per origin of the reports received: presences,
  topologies, last battery;
- the test traffic (``sily.traffic``, the ``tt`` field of the minute
  records): on a node, its messages submitted, accepted, refused and
  failed (per reason: the codes of ``mesh.link`` refusals, negative, and
  of ``mesh.reliable`` failures); on the gateway, per node, the messages
  received against those expected, duplicates, reordered, late, stale,
  node boots (the last minute record's totals), and the latency above the
  best case over the minute records: the median and highest of the
  per-minute medians and maxima, with the samples and minutes counted.

Times are the boards' own (ms since the board's boot): no absolute time on
the boxes.
"""

import sys


def _parent(path):
    head = path.rsplit("/", 1)
    return head[0] if len(head) == 2 and head[0] else "."


sys.path.insert(0, _parent(_parent(__file__)) + "/src")
from sily import journal  # noqa: E402

COUNTER_GROUPS = ("link", "rt", "radio")


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
        "lines": len(run),
        "span_s": (last["t"] - first["t"]) / 1000,
        "end": last["k"] if last["k"] in ("stop", "fatal") else None,
        "error": last.get("err") if last["k"] == "fatal" else None,
    }
    t0 = first["t"]
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
    battery = [m["bat"] for m in minutes if m.get("bat") is not None]
    s["battery"] = (battery[0], battery[-1], min(battery)) if battery else None
    s["jw"] = max([m.get("jw", 0) for m in minutes] or [0])
    s["js"] = max([m.get("js", 0) for m in minutes] or [0])
    s["jq"] = minutes[-1].get("jq", 0) if minutes else 0
    s["je"] = minutes[-1].get("je", 0) if minutes else 0
    origins = {}
    for r in run:
        if r["k"] in ("rp", "rt", "ri"):
            o = origins.setdefault(r["o"], {"rp": 0, "rt": 0, "ri": 0, "bat": None})
            o[r["k"]] += 1
            if r["k"] == "rp" and r.get("bat") is not None:
                o["bat"] = r["bat"]
    s["origins"] = origins
    s["traffic"] = traffic(minutes)
    return s


def _median(values):
    values = sorted(values)
    return values[(len(values) - 1) // 2]


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
            node = nodes.setdefault(row[0], {"medians": [], "maxima": [], "samples": 0})
            node["row"] = row
            if row[9]:
                node["samples"] += row[9]
                node["medians"].append(row[10])
                node["maxima"].append(row[12])
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
    lost = t["lost"]
    if lost["mal"] or lost["q"] or lost["ovf"]:
        lines.append(
            "  test traffic lost to the gateway: %s malformed, %s queue overflows,"
            " %s beyond the node bound" % (lost["mal"], lost["q"], lost["ovf"])
        )
    return lines


def _duration(seconds):
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def report(s):
    """Return the lines of summary s."""
    lines = []
    b = s["record"]
    if b is not None:
        lines.append(
            "boot %s: %s %s (%s) at %s, version %s, reset %s, crashes %s"
            % (
                s["boot"],
                b.get("role"),
                b.get("name"),
                b.get("address"),
                "SF%s" % b.get("sf"),
                b.get("version"),
                b.get("reset"),
                b.get("crashes"),
            )
        )
    else:
        lines.append("boot %s: no boot record" % s["boot"])
    if s["end"] == "fatal":
        end = "fatal: %s" % s["error"]
    elif s["end"] == "stop":
        end = "stopped"
    else:
        end = "no end line (power cut, or read while running)"
    lines.append(
        "  span %s, %d lines, %d minute records; %s"
        % (_duration(s["span_s"]), s["lines"], s["minutes"], end)
    )
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
    if s["counters"]:
        items = ["%s %s" % (k, s["counters"][k]) for k in sorted(s["counters"])]
        lines.append("  counters since boot: " + ", ".join(items))
    if s["battery"] is not None:
        lines.append("  battery %d mV first, %d last, %d lowest" % s["battery"])
    lines.append(
        "  journal: longest write %s ms, sync %s ms; %s items lost, %s errors"
        % (s["jw"], s["js"], s["jq"], s["je"])
    )
    for origin in sorted(s["origins"]):
        o = s["origins"][origin]
        lines.append(
            "  from %s: %d presences, %d topologies, %d node information, battery %s mV"
            % (origin, o["rp"], o["rt"], o["ri"], o["bat"])
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
