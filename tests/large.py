"""Large-network suite: routing invariants at the size of the traffic model.

Usage (MicroPython unix port, MICROPYPATH of the host tests; `make large`)::

    micropython tests/large.py [SEEDS]                      every run, in turn
    micropython tests/large.py run - SCENARIO TOPOLOGY SEED [NODES [LOAD]]
    micropython tests/large.py plan SEEDS                   the runs, one per line
    micropython tests/large.py run DIR SCENARIO TOPOLOGY SEED [NODES [LOAD]]
    micropython tests/large.py summary DIR [SECONDS]
    micropython tests/large.py sweep-plan [OPTIONS]         a sweep's runs, one per line
    micropython tests/large.py record DIR SCENARIO TOPOLOGY SEED NODES [LOAD]
    micropython tests/large.py aggregate [--csv] DIR...

Each run builds a network of NODES nodes (default 30) and the root (§15:
30 Nodes, one Gateway) on a seeded `sim.topologies` topology, runs their
link engines and routing at SF8 with the SILy *competition* periods
(§16–§17: presence 30 s, topology 300 s, root beacon every 15 s, Imin 32
slots = 3.0 s) and a class 1 load following §15, plays the scenario, and
checks the invariants of `sim.invariants` (MP11, MP10, MP5, MP9, MP13) all
along, and counts the false floats (`sim.invariants.Floats`: losses of
grounding without a scenario event, reported, never a failure). A run
passes when no invariant fails, except on an overload topology
(`OVERLOAD`, below). ``run -`` prints the full report of one run
(the reproduction of a failing run); ``plan``, ``run DIR`` and ``summary``
let `make large` run the plan in parallel processes, each writing its
report into DIR, then print the summary: the reports of the failing runs,
then one line (runs passed, among them the overload cases, with their
accepted losses summed) and the class 1 duplicates and false floats summed
over all runs.

Plan: every scenario × topology (`SCENARIOS`, `TOPOLOGIES`) at 30 nodes on
seeds 1 to SEEDS, plus one 64-node run per scenario on the worst-case star
(the relay's neighbour table holds 64 entries, its full size, the others
63; every message through one relay: the MP13 bounds), seed 1. The star
comes in three variants: ``star``, the root hidden from all but the relay,
``star-domain``, the root also heard and hearing every board on links too
weak to be parent links (one collision domain, §15), and ``star2b``, the
dimensioning case: two relays next to the root, each with its half of the
Nodes, the halves hidden from each other and from the root.

Overload (``star``, at 30 and 64 nodes): the hidden-root star with a
single relay is at its capacity limit at the §15 load (hidden
terminals drown the root's frames at the relay during the burst). Its runs
pass on a criterion of their own, `sim.invariants.Recovery` and
`sim.invariants.overload`: required, no silent loss (MP5), the safety
checks (MP11, MP9, MP13), and the recovery once the overload has ended
(the burst minute, or the scenario's event if later): by a deadline of
3.6 presence periods (a parent link left, §9) plus the repair bound of
MP10 at the network's depth (11.6 periods at 2 hops), every board not
routed since that end routed or, cut off, detached (a board routed again
since and without a route at the deadline is in an ordinary repair,
judged by MP10, after the deadline too: counted, not a failure), and
nothing still queued at the end; accepted and
counted, from the burst to that deadline: MP10 beyond its bounds
(attachment, version wave), messages refused or failed with an error. Its
result reads ``overload`` instead of ``ok``.
``run`` also takes the other generators of `sim.topologies.GENERATORS`
(``grid``; ``course``, the nominal orienteering course, at most 14 nodes),
outside the plan.

Loads (`LOADS`, LOAD of ``run``; the plan's: ``s15``): ``s15``, the §15
load below (steady rate and burst); ``steady``, the same rate without the
burst; ``idle``, no class 1 traffic (routing and presence only). Only
``s15`` makes the ``star`` runs overload cases: at the other loads they
are judged on the ordinary invariants. A run's name (and its ``make
large RUN``) names its load unless it is ``s15``.

Sweeps, failure rates over many seeds instead of a verdict on a few:
``sweep-plan`` prints one line per run, SCENARIO TOPOLOGY SEED NODES LOAD,
the largest networks first (options ``--seeds A-B``, default 1-100;
``--scenarios``, ``--topologies``, ``--loads``: comma-separated names,
default the plan's scenarios and topologies and ``s15``; ``--nodes N[,M]``,
default 30). ``record DIR ...`` plays one run unless DIR already holds
its record (a sweep resumes), writes NAME.json (`result`: violations by
check, the strict verdict (no violation at all, overload runs too),
delivery counts, failures split by their origin's state, false floats,
relay and leaf detaches, passive and late acknowledgements, duplicates
before the copy went on air, class 1 upward attempts to a hop other than
the next hop and their messages, recovery details (among them the boards
without a route at its deadline, failing or in repair, and those with
messages still queued at the end), wall time, and the ``SWEEP_VARIANT``
and ``SWEEP_REV`` of the environment), the report NAME.txt only when the
run fails, and prints the result line.
``aggregate DIR...`` groups the records by variant, load, scenario,
topology and nodes and prints, for each per-run indicator (`INDICATORS`:
failed, any violation, silent loss, duplicate delivered, false float,
relay detach, the same connected and without scenario event, failed
with its origin connected, recovery failed, the root alone with messages
queued at the end, an attempt to a hop other than the next hop; then
each check violated), the runs k of n, the rate and its 95 % Wilson
interval (`wilson`); for the per-message counts (`TOTALS`) their totals
and means per run; then the failing runs (``--csv``: one row per group
and indicator). A record written before a field existed counts as not
applicable for the indicators and totals of that field.
``summary`` reads only the NAME.txt reports.

Traffic (§15, scaled to the run): 1000 punches per hour over the whole
network (each node submits every 30 × 3.6 s on average at 30 nodes,
interval drawn uniformly in [0.5, 1.5] of the mean), from the first minute
until the drain; one burst of 100 punches within a minute (mass start)
from nodes drawn at random. A punch is a class 1 message of 10 B to the
root, a frame of 27 B (§16). No message is submitted during the last
`DRAIN_PERIODS` presence periods, so that each one is delivered or failed
by the end (MP5).

Scenarios (`TIMELINE`: the event at 6 minutes of 15 at 30 nodes, at 4 of
10 at 64 nodes):

- ``forming``: the boards start together; nothing else happens;
- ``relay-death``: the relay that forwarded the most messages is switched
  off for good (on the star, the only relay: the others are cut off; on
  ``star2b``, one of the two: its half is cut off, its messages failed
  with an error or exempt, MP5);
- ``global-repair``: the root issues a new DODAG version;
- ``lossy``: from the start, `LOSSY_SHARE` of the links lose `LOSS` of
  their frames both ways (drawn per link).
"""

import json
import math
import os
import sys
import time

from mesh.dodag import default_info
from mesh.frame import ROOT
from mesh.profile import EU_869
from mesh.routing import Routing
from sim import invariants, topologies
from sim.net import Network
from sim.prng import XorShift32

KEY = bytes(range(16))
SF = 8
INFO = default_info(SF, EU_869)  # competition periods at SF8 (§16, §17)
APP = 20  # punches: high-priority application port (§6)
PUNCHES_PER_HOUR = 1000  # §15 peak through the busiest relay
BURST = 100  # punches within a minute (§15 mass start)
TRAFFIC_START_MS = 60000
DRAIN_PERIODS = 5  # presence periods without submission at the end (MP5)
LOSSY_SHARE = 0.3  # share of the links made lossy (scenario lossy)
LOSS = (0.1, 0.25)  # their loss, drawn uniformly, both ways
TRAFFIC_LABEL = (1 << 20) + 1  # derive() labels of the suite's own generators
LOSS_LABEL = (1 << 20) + 2

# Loads: name -> (punches per hour, burst, overload case on the OVERLOAD topologies).
LOADS = {
    "s15": (PUNCHES_PER_HOUR, BURST, True),  # §15: the steady rate and the mass start
    "steady": (PUNCHES_PER_HOUR, 0, False),  # the same rate without the burst
    "idle": (0, 0, False),  # no class 1 traffic: routing and presence only
}
DEFAULT_LOAD = "s15"

SCENARIOS = ("forming", "relay-death", "global-repair", "lossy")
TOPOLOGIES = ("domain", "star", "star-domain", "star2b", "geometric")
OVERLOAD = ("star",)  # overload cases: the recovery criterion (`invariants.Recovery`)
NODES = 30
LARGE_NODES = 64
SHOWN = 8  # violations listed per check in a report (``run -`` lists all)
LARGE_TOPOLOGY = "star"

# Timeline (minutes) per network size: event, burst, end.
TIMELINE = {NODES: (6, 4, 15), LARGE_NODES: (4, 2.5, 10)}


def _timeline(nodes):
    if nodes in TIMELINE:
        return TIMELINE[nodes]
    return TIMELINE[NODES]


class Run:
    """One run: scenario on a topology of nodes nodes and the root, from seed, at load."""

    def __init__(self, scenario, topology, seed, nodes=NODES, load=DEFAULT_LOAD):
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario %s" % scenario)
        if load not in LOADS:
            raise ValueError("unknown load %s" % load)
        self.scenario = scenario
        self.kind = topology
        self.seed = seed
        self.nodes = nodes
        self.load = load
        event, burst, end = _timeline(nodes)
        self.event_ms = int(event * 60000)
        self.burst_ms = int(burst * 60000)
        self.end_ms = int(end * 60000)
        self.drain_ms = DRAIN_PERIODS * INFO.presence_s * 1000
        # rate and burst: changed before `build` by diagnostic scripts
        self.rate, self.burst, overload = LOADS[load]
        self.notes = []
        self.overload = overload and topology in OVERLOAD
        self.recovery = None
        # End of the overload: the burst minute, or the scenario's event if later.
        self.after_ms = self.burst_ms + 60000
        if scenario in ("relay-death", "global-repair"):
            self.after_ms = max(self.after_ms, self.event_ms)

    @property
    def name(self):
        """SCENARIO TOPOLOGY SEED NODES, then LOAD unless the default one."""
        name = "%s %s %d %d" % (self.scenario, self.kind, self.seed, self.nodes)
        if self.load != DEFAULT_LOAD:
            name += " " + self.load
        return name

    def build(self):
        self.topology = topologies.make(self.kind, self.nodes + 1, self.seed)
        net = Network(self.seed, KEY, sf=SF)
        self.net = net

        def make(engine):
            return Routing(engine, EU_869, INFO)

        for name in self.topology.names:
            net.add(name, routing=make)
        losses = None
        if self.scenario == "lossy":
            losses = self._losses()
        self.topology.apply(net, losses)
        self.root = net.boards[self.topology.names[0]]
        self.root.listen(APP)
        checks = invariants.standard(APP, self.drain_ms)
        if self.overload:
            self.recovery = invariants.Recovery(self.after_ms)
            checks.append(self.recovery)
        self.delivery = [c for c in checks if isinstance(c, invariants.Delivery)][0]
        self.checker = invariants.Checker(net, checks)
        self.checker.start()
        self._traffic()
        if self.scenario == "relay-death":
            net.at(self.event_ms, self._kill)
        elif self.scenario == "global-repair":
            net.at(self.event_ms, self.checker.global_repair)

    def _losses(self):
        rng = XorShift32(self.seed).derive(LOSS_LABEL)
        out = {}
        for a, b, _, _ in self.topology.links:
            if rng.chance(LOSSY_SHARE):
                lo, hi = LOSS
                out[(a, b)] = lo + (hi - lo) * rng.next32() / 4294967296
        self.notes.append("lossy links %d of %d" % (len(out), len(self.topology.links)))
        return out

    def _traffic(self):
        rng = XorShift32(self.seed).derive(TRAFFIC_LABEL)
        net = self.net
        nodes = [net.boards[n] for n in self.topology.names[1:]]
        mean = len(nodes) * 3600000 // self.rate if self.rate else None
        stop = self.end_ms - self.drain_ms

        def punch(board):
            if board.engine is not None:
                self.delivery.submit(board)

        def periodic(board):
            punch(board)
            t = net.now + mean // 2 + rng.next32() % mean
            if t < stop:
                net.at(t, periodic, board)

        for b in nodes:
            if mean is None:
                break
            t = TRAFFIC_START_MS + rng.next32() % mean
            if t < stop:
                net.at(t, periodic, b)
        for _ in range(self.burst):
            t = self.burst_ms + rng.next32() % 60000
            net.at(t, punch, nodes[rng.next32() % len(nodes)])

    def _kill(self):
        best = None
        for b in self.checker.nodes():
            f = b.engine.counters.forwarded
            if best is None or f > best[0] or (f == best[0] and b.name < best[1].name):
                best = (f, b)
        f, board = best
        self.notes.append(
            "switched off %04x at %.1f s (forwarded %d)" % (board.name, self.net.now / 1000, f)
        )
        self.checker.switch_off(board.name)

    def run(self):
        """Build, run and check; return the violations."""
        start = time.ticks_ms()
        self.build()
        self.net.run(self.end_ms)
        self.violations = self.checker.finish()
        self.wall_ms = time.ticks_diff(time.ticks_ms(), start)
        self.judge()
        return self.violations

    def judge(self):
        """Split the violations into ``failing`` and ``accepted`` (overload cases).

        A full case fails on any violation; an overload case on those
        `invariants.overload` does not accept in its window (from the burst
        to the recovery deadline), whose refused and failed messages are
        counted (``refused``, ``failed``).
        """
        self.accepted, self.refused, self.failed = [], 0, 0
        self.failing = self.violations
        if not self.overload:
            return
        end = self.recovery.deadline
        if end is None:
            end = self.end_ms
        self.window = (self.burst_ms, end)
        self.failing, self.accepted, self.refused, self.failed = invariants.overload(
            self.violations, self.burst_ms, end, self.delivery.outcomes
        )

    def report(self, limit=SHOWN):
        """Return the lines of the run's report, the result line last.

        limit: violations listed per check (None: all).
        """
        t = self.topology
        degrees = t.degrees()
        lines = [
            "== %s: %d links, depth %d, mean degree %.1f; %s"
            % (
                self.name,
                len(t.links),
                t.depth,
                sum(degrees.values()) / len(degrees),
                "; ".join(self.notes) or "no event",
            )
        ]
        for c in self.checker.checks:
            s = c.stats()
            if s:
                lines.append("   %s %s: %s" % (c.name, type(c).__name__, s))
        _listed(lines, "VIOLATION", self.failing, limit)
        _listed(lines, "ACCEPTED", self.accepted, limit)
        if self.failing:
            status = "FAIL"
        elif self.overload:
            status = "overload"
        else:
            status = "ok"
        accepted = ""
        if self.overload:
            accepted = ", accepted in %.1f-%.1f s: %d MP10, %d refused, %d failed" % (
                self.window[0] / 1000,
                self.window[1] / 1000,
                len(self.accepted),
                self.refused,
                self.failed,
            )
        lines.append(
            "RESULT %s %s: %d violations%s, %.1f s"
            % (status, self.name, len(self.failing), accepted, self.wall_ms / 1000)
        )
        return lines


def _listed(lines, label, violations, limit):
    # Append the violations, at most limit per check (None: all).
    shown = {}
    for v in violations:
        n = shown.get(v.check, 0)
        shown[v.check] = n + 1
        if limit is None or n < limit:
            lines.append("   %s %r" % (label, v))
    for check in sorted(shown):
        if limit is not None and shown[check] > limit:
            lines.append("   ... %d more %s %s" % (shown[check] - limit, check, label.lower()))


def plan(seeds):
    """Return the runs of the suite as (scenario, topology, seed, nodes)."""
    out = []
    for seed in range(1, seeds + 1):
        for scenario in SCENARIOS:
            for kind in TOPOLOGIES:
                out.append((scenario, kind, seed, NODES))
    for scenario in SCENARIOS:
        out.append((scenario, LARGE_TOPOLOGY, 1, LARGE_NODES))
    return out


def _args(args):
    # (scenario, topology, seed, nodes, load) of SCENARIO TOPOLOGY SEED [NODES [LOAD]].
    nodes = int(args[3]) if len(args) > 3 else NODES
    load = args[4] if len(args) > 4 else DEFAULT_LOAD
    return args[0], args[1], int(args[2]), nodes, load


def _run(args):
    r = Run(*_args(args))
    r.run()
    return r


def _command(name):
    return 'make large RUN="%s"' % name


def _accepted(result):
    # (MP10, refused, failed) accepted in an overload RESULT line.
    tail = result.split(": ", 2)[-1]
    words = tail.replace(",", "").split()
    return int(words[0]), int(words[2]), int(words[4])


def summary(reports, seconds=None):
    """Print the failing reports and the summary line; return the failing run names.

    The line counts the runs passed, among them the overload cases passed
    on their criterion, with their accepted capacity losses summed, the
    class 1 duplicates delivered in all runs (`sim.invariants.Delivery`)
    and their false floats (`sim.invariants.Floats`), the overload topologies
    apart (their detaches under overload are expected).
    """
    failed = []
    overload = [0, 0, 0, 0]  # runs, MP10, refused, failed
    duplicates = [0, 0]  # copies delivered again, runs with any
    # false floats, by G, runs with any, node-s ungrounded: other topologies, overload ones
    floats = ([0, 0, 0, 0.0], [0, 0, 0, 0.0])
    for lines in reports:
        n = _duplicates(lines)
        duplicates[0] += n
        duplicates[1] += 1 if n else 0
        result = lines[-1]
        f = floats[result.split()[3] in OVERLOAD]  # RESULT status scenario topology ...
        n, by_g, ungrounded = _floats(lines)
        f[0] += n
        f[1] += by_g
        f[2] += 1 if n else 0
        f[3] += ungrounded
        status = result.split(" ", 2)[1]
        if status == "overload":
            overload[0] += 1
            for i, n in enumerate(_accepted(result)):
                overload[i + 1] += n
        elif status != "ok":
            for line in lines:
                print(line)
            name = result.split(" ", 2)[2].split(":")[0]
            print("   reproduce: %s" % _command(name))
            failed.append(name)
    wall = "" if seconds is None else ", %d s" % seconds
    print(
        "large: %d of %d runs passed (%d as overload: accepted %d MP10, %d refused, "
        "%d failed)%s" % ((len(reports) - len(failed), len(reports)) + tuple(overload) + (wall,))
    )
    print("large: %d class 1 duplicates delivered, in %d runs" % tuple(duplicates))
    print(
        "large: %d false floats (G ran out %d), in %d runs, %.1f node-s ungrounded; "
        "overload topologies %d (%d), in %d runs, %.1f node-s"
        % (tuple(floats[0]) + tuple(floats[1]))
    )
    for name in failed:
        print("  failed: %s" % name)
    return failed


def _duplicates(lines):
    # Class 1 duplicates delivered in a report (its MP5 Delivery line), 0 if none.
    for line in lines:
        if " MP5 Delivery: " in line and " duplicates delivered" in line:
            return int(line.split("; ")[-1].split()[0])
    return 0


def _floats(lines):
    # (false floats, of them G ran out, node-s ungrounded) of a report's
    # grounding line, zeros if none.
    for line in lines:
        if " grounding Floats: false floats " in line:
            words = line.split(": ", 1)[1]
            for c in "(),":
                words = words.replace(c, " ")
            words = words.split()
            return int(words[2]), int(words[6]), float(words[7])
    return 0, 0, 0.0


# --- Sweeps -------------------------------------------------------------------------------

SWEEP_SEEDS = (1, 100)
Z95 = 1.959963984540054  # two-sided 95 % normal quantile


def sweep_plan(
    seeds=SWEEP_SEEDS,
    scenarios=SCENARIOS,
    topologies=TOPOLOGIES,
    nodes=(NODES,),
    loads=(DEFAULT_LOAD,),
):
    """Return the runs of a sweep as (scenario, topology, seed, nodes, load), largest first.

    seeds: (first, last), both included.
    """
    out = []
    for n in sorted(nodes, reverse=True):
        for seed in range(seeds[0], seeds[1] + 1):
            for load in loads:
                for scenario in scenarios:
                    for kind in topologies:
                        out.append((scenario, kind, seed, n, load))
    return out


def _names(text, known, what):
    # The comma-separated names of text, each one of known.
    out = tuple(text.split(","))
    for name in out:
        if name not in known:
            raise ValueError("unknown %s %s" % (what, name))
    return out


def sweep_options(args):
    """Return the keyword arguments of `sweep_plan` from the options args."""
    out = {}
    i = 0
    while i < len(args):
        if i + 1 >= len(args):
            raise ValueError("%s without value" % args[i])
        option, value = args[i], args[i + 1]
        if option == "--seeds":
            first, _, last = value.partition("-")
            out["seeds"] = (int(first), int(last or first))
        elif option == "--scenarios":
            out["scenarios"] = _names(value, SCENARIOS, "scenario")
        elif option == "--topologies":
            out["topologies"] = _names(value, topologies.GENERATORS, "topology")
        elif option == "--nodes":
            out["nodes"] = tuple(int(n) for n in value.split(","))
        elif option == "--loads":
            out["loads"] = _names(value, LOADS, "load")
        else:
            raise ValueError("unknown option %s" % option)
        i += 2
    return out


def record_name(scenario, kind, seed, nodes, load):
    """Return the base name of a run's record files (load always named)."""
    return "%s_%s_%d_%d_%s" % (scenario, kind, seed, nodes, load)


def _exists(path):
    try:
        os.stat(path)
    except OSError:
        return False
    return True


def _check(run, cls):
    for c in run.checker.checks:
        if isinstance(c, cls):
            return c
    return None


def _by_check(violations):
    out = {}
    for v in violations:
        out[v.check] = out.get(v.check, 0) + 1
    return out


def result(r):
    """Return the record of run r (judged): a dict, JSON-ready."""
    floats = _check(r, invariants.Floats)
    detaches = _check(r, invariants.Detaches)
    acks = _check(r, invariants.Acknowledgements)
    if r.failing:
        status = "FAIL"
    elif r.overload:
        status = "overload"
    else:
        status = "ok"
    recovery = None
    if r.recovery is not None:
        c = r.recovery
        recovery = {
            "after_s": c.after_ms / 1000,
            "deadline_s": None if c.deadline is None else c.deadline / 1000,
            "checked_s": None if c.checked is None else c.checked / 1000,
            "depth": c.depth,
            "routed": c.routed,
            "detached": c.detached,
            "unrouted": c.unrouted,
            "repairing": c.repairing,
            "queued": c.copies,
            "nodes": ["%04x" % n for n in sorted(c.nodes)],
            "violations": sum(1 for v in r.violations if v.check == c.name),
        }
    accepted = None
    if r.overload:
        accepted = {"mp10": len(r.accepted), "refused": r.refused, "failed": r.failed}
    return {
        "variant": os.getenv("SWEEP_VARIANT") or "",
        "rev": os.getenv("SWEEP_REV") or "",
        "scenario": r.scenario,
        "topology": r.kind,
        "seed": r.seed,
        "nodes": r.nodes,
        "load": r.load,
        "status": status,
        "strict": not r.violations,
        "violations": _by_check(r.violations),
        "failing": _by_check(r.failing),
        "delivery": dict(r.delivery.counts),
        "failures": dict(r.delivery.failures),
        "floats": {
            "count": floats.count,
            "by_g": floats.by_g,
            "ungrounded_s": floats.ungrounded_ms / 1000,
        },
        "detaches": {
            "relay": detaches.relay,
            "relay_false": detaches.relay_false,
            "leaf": detaches.leaf,
        },
        "acks": {
            "passive": acks.passive,
            "late": acks.late,
            "duplicates": acks.duplicates,
            "spurious": acks.spurious,
            "stale_hop": acks.stale_hop,
            "stale_hop_messages": len(acks.stale_messages),
        },
        "recovery": recovery,
        "accepted": accepted,
        "wall_s": r.wall_ms / 1000,
    }


def record(directory, args, play=_run):
    """Play one run of a sweep into directory, unless its record exists; return 0.

    Writes NAME.json (`result`, one line), and NAME.txt (the report) when
    the run fails; prints the result line.
    """
    base = "%s/%s" % (directory, record_name(*_args(args)))
    if _exists(base + ".json"):
        print("exists %s.json" % base)
        return 0
    r = play(args)
    lines = r.report()
    if r.failing:
        with open(base + ".txt", "w") as f:
            f.write("\n".join(lines) + "\n")
    with open(base + ".json.tmp", "w") as f:
        f.write(json.dumps(result(r)) + "\n")
    os.rename(base + ".json.tmp", base + ".json")
    print(lines[-1])
    return 0


def wilson(k, n, z=Z95):
    """Return the Wilson score interval (low, high) of k successes in n trials."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    z2 = z * z
    d = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / d
    low = 0.0 if k == 0 else centre - half
    high = 1.0 if k == n else centre + half
    return low, high


def _field(record, part, name):
    # record[part][name], None when absent (a record written before the field existed).
    d = record.get(part)
    return None if d is None else d.get(name)


def _positive(n):
    return None if n is None else n > 0


def _root_queue_only(record):
    # Only the root holding messages at the end of an overload run (None: not one,
    # or a record without the field).
    nodes = _field(record, "recovery", "nodes")
    return None if nodes is None else nodes == ["%04x" % ROOT]


# Per-run indicators: name, test(record) -> True, False, or None (not applicable).
INDICATORS = (
    ("failed", lambda r: r["status"] == "FAIL"),
    ("violation", lambda r: not r["strict"]),  # any, overload runs included
    ("silent_loss", lambda r: r["delivery"].get("lost", 0) > 0),
    ("duplicate", lambda r: r["delivery"].get("duplicates", 0) > 0),
    ("false_float", lambda r: r["floats"]["count"] > 0),
    ("relay_detach", lambda r: r["detaches"]["relay"] > 0),
    ("relay_detach_false", lambda r: r["detaches"]["relay_false"] > 0),  # connected, no event
    ("failed_connected", lambda r: r["failures"]["connected"] > 0),
    (
        "recovery_failed",
        lambda r: None if r["recovery"] is None else r["recovery"]["violations"] > 0,
    ),
    ("root_queue_only", _root_queue_only),  # recovery failed on the root's queue alone
    ("stale_hop", lambda r: _positive(_field(r, "acks", "stale_hop"))),
)

# Per-message totals: name, count(record) (None: not in the record, the run not counted).
TOTALS = (
    ("late_acks", lambda r: r["acks"]["late"]),
    ("passive_acks", lambda r: r["acks"]["passive"]),
    ("spurious_retries", lambda r: r["acks"]["spurious"]),
    ("failed_class1", lambda r: r["delivery"].get("failed", 0)),
    ("failed_class1_connected", lambda r: r["failures"]["connected"]),
    ("false_floats", lambda r: r["floats"]["count"]),
    ("relay_detaches", lambda r: r["detaches"]["relay"]),
    ("stale_hop_attempts", lambda r: _field(r, "acks", "stale_hop")),
    ("stale_hop_messages", lambda r: _field(r, "acks", "stale_hop_messages")),
)


def load_records(directories):
    """Return the records (dicts) of the NAME.json files of directories."""
    out = []
    for d in directories:
        for entry in sorted(os.listdir(d)):
            if entry.endswith(".json"):
                with open("%s/%s" % (d, entry)) as f:
                    out.append(json.loads(f.read()))
    return out


def aggregate(records):
    """Group records by (variant, load, scenario, topology, nodes); return the groups.

    Each group: a dict with ``key``, ``runs``, ``revs`` (sorted), ``wall_s``
    (mean), ``indicators`` [(name, k, n)] (`INDICATORS`, then ``MPx`` for
    each check with a violation in the group: runs with any), ``totals``
    [(name, total, runs)] and ``failing`` (run names, as ``make large RUN``).
    """
    groups = {}
    for r in records:
        key = (r["variant"], r["load"], r["scenario"], r["topology"], r["nodes"])
        groups.setdefault(key, []).append(r)
    out = []
    for key in sorted(groups):
        runs = groups[key]
        indicators = []
        for name, test in INDICATORS:
            k = n = 0
            for r in runs:
                v = test(r)
                if v is not None:
                    n += 1
                    k += 1 if v else 0
            indicators.append((name, k, n))
        checks = set()
        for r in runs:
            checks.update(r["violations"])
        for check in sorted(checks):
            k = sum(1 for r in runs if r["violations"].get(check))
            indicators.append((check, k, len(runs)))
        totals = []
        for name, count in TOTALS:
            counts = [n for n in (count(r) for r in runs) if n is not None]
            totals.append((name, sum(counts), len(counts)))
        failing = []
        for r in runs:
            if r["status"] == "FAIL":
                run = Run(r["scenario"], r["topology"], r["seed"], r["nodes"], r["load"])
                failing.append(run.name)
        out.append(
            {
                "key": key,
                "runs": len(runs),
                "revs": sorted(set(r["rev"] for r in runs)),
                "wall_s": sum(r["wall_s"] for r in runs) / len(runs),
                "indicators": indicators,
                "totals": totals,
                "failing": sorted(failing),
            }
        )
    return out


def print_aggregate(groups, csv=False):
    """Print the groups of `aggregate`: a text table, or CSV rows."""
    if csv:
        print("variant,load,scenario,topology,nodes,indicator,k,n,rate,low,high")
    for g in groups:
        key = "%s,%s,%s,%s,%d" % g["key"]
        if not csv:
            print(
                "== %s %s %s %s %d: %d runs, rev %s, %.1f s per run"
                % (g["key"] + (g["runs"], " ".join(g["revs"]) or "-", g["wall_s"]))
            )
        for name, k, n in g["indicators"]:
            if not n:
                continue
            lo, hi = wilson(k, n)
            if csv:
                print("%s,%s,%d,%d,%.5f,%.5f,%.5f" % (key, name, k, n, k / n, lo, hi))
            else:
                print(
                    "   %-18s %4d/%-4d %6.2f %%  [%.2f, %.2f] %%"
                    % (name, k, n, 100 * k / n, 100 * lo, 100 * hi)
                )
        if csv:
            for name, total, n in g["totals"]:
                if n:
                    print("%s,%s,%d,%d,%.5f,," % (key, name, total, n, total / n))
            continue
        print(
            "   totals: "
            + ", ".join("%s %d (%.2f/run)" % (name, t, t / n) for name, t, n in g["totals"] if n)
        )
        for name in g["failing"]:
            print("   failed: %s" % _command(name))


def main(argv):
    if len(argv) > 1 and argv[1] == "plan":
        for p in plan(int(argv[2])):
            print("%s %s %d %d" % p)
        return 0
    if len(argv) > 1 and argv[1] == "run":
        r = _run(argv[3:])
        lines = r.report(None if argv[2] == "-" else SHOWN)
        if argv[2] == "-":
            for line in lines:
                print(line)
            return 1 if r.failing else 0
        name = "%s/%s.txt" % (argv[2], r.name.replace(" ", "_"))
        with open(name, "w") as f:
            f.write("\n".join(lines) + "\n")
        print(lines[-1])
        return 0
    if len(argv) > 1 and argv[1] == "sweep-plan":
        try:
            options = sweep_options(argv[2:])
        except ValueError as e:
            print("sweep-plan: %s" % e)
            return 2
        for p in sweep_plan(**options):
            print("%s %s %d %d %s" % p)
        return 0
    if len(argv) > 1 and argv[1] == "record":
        return record(argv[2], argv[3:])
    if len(argv) > 1 and argv[1] == "aggregate":
        csv = "--csv" in argv[2:]
        records = load_records([a for a in argv[2:] if a != "--csv"])
        print_aggregate(aggregate(records), csv)
        return 0 if records else 1
    if len(argv) > 1 and argv[1] == "summary":
        reports = []
        for entry in sorted(os.listdir(argv[2])):
            if not entry.endswith(".txt"):
                continue
            with open("%s/%s" % (argv[2], entry)) as f:
                reports.append(f.read().rstrip("\n").split("\n"))
        seconds = int(argv[3]) if len(argv) > 3 else None
        return 1 if summary(reports, seconds) else 0
    seeds = int(argv[1]) if len(argv) > 1 else 1
    start = time.ticks_ms()
    reports = []
    for p in plan(seeds):
        r = _run(p)
        lines = r.report()
        print(lines[-1])
        reports.append(lines)
    seconds = time.ticks_diff(time.ticks_ms(), start) // 1000
    return 1 if summary(reports, seconds) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
