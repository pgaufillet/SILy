"""Large-network suite: routing invariants at the size of the traffic model.

Usage (MicroPython unix port, MICROPYPATH of the host tests; `make large`)::

    micropython tests/large.py [SEEDS]                      every run, in turn
    micropython tests/large.py run - SCENARIO TOPOLOGY SEED [NODES]
    micropython tests/large.py plan SEEDS                   the runs, one per line
    micropython tests/large.py run DIR SCENARIO TOPOLOGY SEED [NODES]
    micropython tests/large.py summary DIR [SECONDS]

Each run builds a network of NODES nodes (default 30) and the root (§15:
30 Nodes, one Gateway) on a seeded `sim.topologies` topology, runs their
link engines and routing at SF8 with the SILy *competition* periods
(§16–§17: presence 30 s, topology 300 s, root beacon every 15 s, Imin 32
slots = 3.0 s) and a class 1 load following §15, plays the scenario, and
checks the invariants of `sim.invariants` (MP11, MP10, MP5, MP9, MP13) all
along. A run passes when no invariant fails, except on an overload
topology (`OVERLOAD`, below). ``run -`` prints the full report of one run
(the reproduction of a failing run); ``plan``, ``run DIR`` and ``summary``
let `make large` run the plan in parallel processes, each writing its
report into DIR, then print the summary: the reports of the failing runs,
then one line (runs passed, among them the overload cases, with their
accepted losses summed).

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
MP10 at the network's depth (11.6 periods at 2 hops), every board routed
or, cut off, detached, and nothing still queued at the end; accepted and
counted, from the burst to that deadline: MP10 beyond its bounds
(attachment, version wave), messages refused or failed with an error. Its
result reads ``overload`` instead of ``ok``.
``run`` also takes the other generators of `sim.topologies.GENERATORS`
(``grid``; ``course``, the nominal orienteering course, at most 14 nodes),
outside the plan.

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

import os
import sys
import time

from mesh.dodag import default_info
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
    """One run: scenario on a topology of nodes nodes and the root, from seed."""

    def __init__(self, scenario, topology, seed, nodes=NODES):
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario %s" % scenario)
        self.scenario = scenario
        self.kind = topology
        self.seed = seed
        self.nodes = nodes
        event, burst, end = _timeline(nodes)
        self.event_ms = int(event * 60000)
        self.burst_ms = int(burst * 60000)
        self.end_ms = int(end * 60000)
        self.drain_ms = DRAIN_PERIODS * INFO.presence_s * 1000
        self.rate = PUNCHES_PER_HOUR  # changed before `build` by diagnostic scripts
        self.burst = BURST
        self.notes = []
        self.overload = topology in OVERLOAD
        self.recovery = None
        # End of the overload: the burst minute, or the scenario's event if later.
        self.after_ms = self.burst_ms + 60000
        if scenario in ("relay-death", "global-repair"):
            self.after_ms = max(self.after_ms, self.event_ms)

    @property
    def name(self):
        return "%s %s %d %d" % (self.scenario, self.kind, self.seed, self.nodes)

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


def _run(args):
    scenario, kind, seed = args[0], args[1], int(args[2])
    nodes = int(args[3]) if len(args) > 3 else NODES
    r = Run(scenario, kind, seed, nodes)
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
    on their criterion, with their accepted capacity losses summed.
    """
    failed = []
    overload = [0, 0, 0, 0]  # runs, MP10, refused, failed
    for lines in reports:
        result = lines[-1]
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
    for name in failed:
        print("  failed: %s" % name)
    return failed


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
    if len(argv) > 1 and argv[1] == "summary":
        reports = []
        for entry in sorted(os.listdir(argv[2])):
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
