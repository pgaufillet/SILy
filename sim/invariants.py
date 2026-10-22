"""Invariant checks of a running simulated mesh (MESH_DESIGN §2 properties).

A `Checker` observes a `sim.net.Network` whose boards run their routing
(`mesh.routing.Routing`): it samples every board on the scheduler every
``every_ms`` (reading their state, never changing it), observes the
medium's transmissions (`sim.medium.Medium.on_transmit`), and records a
`Violation` (time, check, nodes, detail) whenever an invariant fails. The
checks are a list of `Check` objects, so that later steps (downward
routing, class 2, key change) add theirs. Scenario events go through the
checker, which tells the checks: `Checker.switch_off` (a board dies),
`Checker.global_repair` (new DODAG version), `Checker.links_changed` (links
cut, restored or changed by the scenario). The network's observers
(`sim.net.Observer`, e.g. `sim.trace.Recorder`) are told of each violation
and scenario event too.

Bounds are computed from the network's parameters at the time of the check
(the root's DODAG information): presence period T, Imin, slot (time on air
of a presence frame, §8), silence limit S = 2.5 T (§9). Checks:

- `Loops` (MP11): no node stays in a preferred-parent cycle longer than
  `Loops.bound_ms`;
- `Attachment` (MP10): every node connected to the root attaches within
  the forming bound after start, and again within the repair bound after
  losing its route (`Attachment.bound_ms`);
- `VersionWave` (MP10, global repair): every connected node adopts a new
  DODAG version within `VersionWave.bound_ms`;
- `Delivery` (MP5): every class 1 message submitted through it is, by the
  end of the run, delivered to the root or reported failed to its sender;
- `DutyCycle` (MP9): no board transmits more than the duty-cycle budget in
  any window, measured from the medium's transmissions;
- `Resources` (MP13): tables and queues within their configured bounds.

Graph notions: a link is **usable** when it is up both ways, delivers at
least `USABLE` of the frames both ways without collisions (ETX ≤ 2, well
within the eligibility limit ETX ≤ 4 of §9, collisions taking part of the
rest) and has an SNR margin of at least 3 dB both ways (§9 eligibility);
a board is **connected** when usable links join it to the
root through boards switched on; a node is **routed** when its chain of
preferred parents reaches the root through boards switched on and links up
both ways, without cycle (a parent that died or a link cut breaks the
chain at once, before the node notices).
"""

from mesh.airtime import snr_limit_x10
from mesh.dodag import DESYNC_SLOTS, HOLD_IMIN, WINDOW_SLOTS
from mesh.frame import ROOT
from mesh.link import SENDERS
from mesh.neighbours import CAPACITY, MARGIN_LOW, silence_ms
from mesh.parents import SIZE as PARENT_SET
from mesh.ports import ERROR
from mesh.reliable import ACK_BATCH, DUPLICATES, decode_error
from mesh.txqueue import SIZE as QUEUE_SIZE

USABLE = 0.5  # delivery both ways without collisions for a usable link
OFF = 10**9  # off time of a board switched off for the rest of the run (ms)


class Violation:
    """An invariant that failed: time (ms), check name, nodes, detail (text)."""

    __slots__ = ("time", "check", "nodes", "detail")

    def __init__(self, time, check, nodes, detail):
        self.time = time
        self.check = check
        self.nodes = nodes
        self.detail = detail

    def __repr__(self):
        nodes = " ".join("%04x" % n for n in self.nodes)
        return "%9.1f s %s [%s] %s" % (self.time / 1000, self.check, nodes, self.detail)


class Check:
    """One invariant; subclasses override what they need.

    ``name``: the property (MP number); ``checker``: set by `bind`.
    """

    name = None

    def bind(self, checker):
        """Attach to checker, before the first sample."""
        self.checker = checker

    def sample(self, now):
        """Check the boards' state at now."""

    def transmitted(self, source, start, end):
        """A transmission of source on the medium over [start, end)."""

    def switched_off(self, board, now):
        """board is about to be switched off for the rest of the run."""

    def new_version(self, now):
        """The root issued a new DODAG version at now."""

    def finish(self, now):
        """End of the run (after a last sample)."""

    def stats(self):
        """Return a short text of what the check measured (summary)."""
        return ""

    def violate(self, now, nodes, detail):
        self.checker.violate(Violation(now, self.name, tuple(nodes), detail))


class Checker:
    """Observes net (a `sim.net.Network`) with checks (a list of `Check`).

    every_ms: sampling period. `start` before running the network, `finish`
    after: it returns ``violations``, a list of `Violation` in time order.
    """

    def __init__(self, net, checks, every_ms=1000):
        self.net = net
        self.checks = list(checks)
        self.every_ms = every_ms
        self.violations = []
        self.root = None
        for b in net.boards.values():
            if b.address == ROOT:
                self.root = b
        if self.root is None:
            raise ValueError("no root")
        self._hops = None
        self._off = []
        self.started = None

    # --- Parameters (§8, §9) ---------------------------------------------------------

    @property
    def period_ms(self):
        """Presence period T of the root's DODAG information."""
        return self.root.routing.dodag.info.presence_s * 1000

    @property
    def imin_ms(self):
        return self.root.routing.dodag.imin_ms

    @property
    def slot_ms(self):
        return self.root.engine.slot_ms

    @property
    def silence_ms(self):
        return silence_ms(self.period_ms)

    @property
    def window_ms(self):
        """Answer window of a solicitation: Imin + 8 slots (§9 Trickle)."""
        return self.imin_ms + WINDOW_SLOTS * self.slot_ms

    # --- Running ------------------------------------------------------------------------

    def start(self):
        """Bind the checks, observe the medium and sample from now on."""
        self.started = self.net.now
        for c in self.checks:
            c.bind(self)
        self.net.medium.on_transmit = self._transmitted
        self.net.at(self.net.now, self._sample)

    def finish(self):
        """Last sample and end of the checks; return the violations in time order."""
        now = self.net.now
        for c in self.checks:
            c.sample(now)
        for c in self.checks:
            c.finish(now)
        self.net.medium.on_transmit = None
        self.violations.sort(key=lambda v: v.time)
        return self.violations

    def violate(self, violation):
        self.violations.append(violation)
        for o in self.net.observers:
            o.violated(violation)

    def _sample(self):
        now = self.net.now
        for c in self.checks:
            c.sample(now)
        self.net.at(now + self.every_ms, self._sample)

    def _transmitted(self, source, start, end):
        for c in self.checks:
            c.transmitted(source, start, end)

    # --- Scenario events ------------------------------------------------------------------

    def switch_off(self, name):
        """Switch board name off for the rest of the run (power loss, no reboot)."""
        board = self.net.boards[name]
        now = self.net.now
        for o in self.net.observers:
            o.scenario("switch-off", name)
        for c in self.checks:
            c.switched_off(board, now)
        self._off.append(name)
        board.power_loss(off_ms=OFF)
        self._hops = None

    def global_repair(self, info=None):
        """Root: issue the next DODAG version (new parameters info) now."""
        now = self.net.now
        dodag = self.root.routing.dodag
        dodag.set_version(dodag.version + 1, now, info)
        for o in self.net.observers:
            o.scenario("global-repair", dodag.version)
        self.root.node.wake()
        for c in self.checks:
            c.new_version(now)

    def links_changed(self):
        """The scenario changed links: connectivity is computed again."""
        for o in self.net.observers:
            o.scenario("links-changed", None)
        self._hops = None

    # --- Graph -----------------------------------------------------------------------------

    def alive(self):
        """Return the boards switched on (with an engine), the root included."""
        return [b for b in self.net.boards.values() if b.engine is not None]

    def nodes(self):
        """Return the boards switched on, the root excluded."""
        return [b for b in self.alive() if b is not self.root]

    def hops(self):
        """Return {name: hop count} of the boards connected to the root by usable links."""
        if self._hops is not None:
            return self._hops
        boards = self.net.boards
        on = set(b.name for b in self.alive())
        adj = {}
        medium = self.net.medium
        limit = snr_limit_x10(self.net.sf)
        for src, dst, link in medium.links():
            if src not in on or dst not in on or not link.up:
                continue
            back = medium.get_link(dst, src)
            if back is None or not back.up:
                continue
            if (1 - link.loss) * (1 - back.loss) < USABLE:
                continue
            if min(link.snr, back.snr) * 10 - limit < MARGIN_LOW:
                continue
            adj.setdefault(src, []).append(dst)
        hops = {}
        if self.root.name in on:
            hops[self.root.name] = 0
            frontier = [self.root.name]
            while frontier:
                nxt = []
                for n in frontier:
                    for m in adj.get(n, ()):
                        if m not in hops and m in boards:
                            hops[m] = hops[n] + 1
                            nxt.append(m)
                frontier = nxt
        self._hops = hops
        return hops

    def linked(self, a, b):
        """True if the links a -> b and b -> a exist and are up."""
        medium = self.net.medium
        for s, d in ((a, b), (b, a)):
            link = medium.get_link(s, d)
            if link is None or not link.up:
                return False
        return True

    def preferred(self, board):
        """Return the name of board's preferred parent, None (none, or board off)."""
        if board.engine is None or board is self.root:
            return None
        return board.routing.parents.preferred

    def chain(self, board):
        """Return the depth of board's chain of preferred parents to the root, None if broken."""
        boards = self.net.boards
        seen = set()
        cur = board
        depth = 0
        while cur is not self.root:
            if cur.name in seen:
                return None
            seen.add(cur.name)
            p = self.preferred(cur)
            if p is None:
                return None
            parent = boards.get(p)
            if parent is None or parent.engine is None or not self.linked(cur.name, p):
                return None
            cur = parent
            depth += 1
        return depth


# --- MP11 ---------------------------------------------------------------------------------


class Loops(Check):
    """MP11: no node stays in a preferred-parent cycle longer than the bound.

    §2 MP11 and §9 (loop freedom): a loop forms only after two consecutive
    losses on a link and ends by a parent switch or when the ranks pass
    L + 56: 7 increases of one hop, each advertised after a Trickle reset
    within one Imin when idle ("about 7 Imin"). Bound: 7 (Imin + 8 slots),
    the 8 slots being an advertisement's access as in the answer window (§9:
    busy sense, backoff of at most 4 slots, 1.25 slots on air, the step).
    ``episodes`` counts the cycles seen (any length), ``longest`` the longest
    time a node spent in one (ms, as sampled).
    """

    name = "MP11"
    INCREASES = 7  # (L + 56 - L) / 8: hops of rank increase before the bound

    def bind(self, checker):
        super().bind(checker)
        self.since = {}  # name -> first sample in a cycle
        self.reported = set()
        self.episodes = 0
        self.longest = 0

    def bound_ms(self):
        c = self.checker
        return self.INCREASES * (c.imin_ms + WINDOW_SLOTS * c.slot_ms)

    def cycles(self):
        """Return the cycles of preferred parents among the boards switched on."""
        c = self.checker
        boards = c.net.boards
        state = {}  # name -> 1 on the current path, 2 done
        out = []
        for b in c.nodes():
            if b.name in state:
                continue
            path = []
            cur = b
            while cur is not None and cur.name not in state:
                state[cur.name] = 1
                path.append(cur.name)
                p = c.preferred(cur)
                cur = boards.get(p) if p is not None else None
                if cur is not None and cur.engine is None:
                    cur = None
            if cur is not None and state.get(cur.name) == 1:
                out.append(path[path.index(cur.name) :])
            for n in path:
                state[n] = 2
        return out

    def sample(self, now):
        looped = {}
        for cycle in self.cycles():
            for n in cycle:
                looped[n] = cycle
        for n in list(self.since):
            if n not in looped:
                self.longest = max(self.longest, now - self.since.pop(n))
                self.reported.discard(n)
        bound = self.bound_ms()
        for n, cycle in looped.items():
            since = self.since.get(n)
            if since is None:
                self.since[n] = now
                if n == min(cycle):
                    self.episodes += 1
                continue
            if now - since > bound and n not in self.reported:
                self.reported.update(cycle)
                boards = self.checker.net.boards
                ranks = " ".join("%04x:%d" % (m, boards[m].engine.rank) for m in cycle)
                self.violate(
                    now,
                    cycle,
                    "cycle for %.1f s > bound %.1f s (ranks %s)"
                    % ((now - since) / 1000, bound / 1000, ranks),
                )

    def finish(self, now):
        for since in self.since.values():
            self.longest = max(self.longest, now - since)

    def stats(self):
        return "cycles %d, longest %.1f s (bound %.1f s)" % (
            self.episodes,
            self.longest / 1000,
            self.bound_ms() / 1000,
        )


# --- MP10 ---------------------------------------------------------------------------------


class Attachment(Check):
    """MP10: every connected node is routed within the forming or repair bound.

    Bounds per node, h being its hop count from the root over usable links
    (§9 does not state a forming bound nor a bound for several hops; derived
    from its mechanisms, tolerating one lost frame per hop as the silence
    limit does):

    - per hop, once a neighbour has a route: S + W, W = Imin + 8 slots the
      answer window. The node, detached, takes the best candidate heard in
      the window of a solicitation (answered within Imin, §9), or the first
      one heard afterwards; it solicits once per presence period while it
      has no route. One lost exchange costs at most the next solicitation
      (T + 4 slots of delay) or the next frame of the neighbour (within the
      silence limit S = 2.5 T, which tolerates one loss): S + W covers both;
    - forming, from start: Imin (boot solicitation delay) + h (S + W);
    - repair, from the time the node's chain breaks: S (an idle node
      detects a silent parent at the silence limit, §9 "switch, idle") +
      2 Imin + 8 slots (detach hold, random delays of the poison and the
      solicitation) + h (S + W). The §9 table's "detach + rejoin" figure is
      the no-loss case of one hop.

    ``slowest`` records the longest time a node took to get a route, per
    cause ('start', 'repair'), as sampled.
    """

    name = "MP10"
    START = "start"
    REPAIR = "repair"

    def bind(self, checker):
        super().bind(checker)
        self.since = {}  # name -> (time the route was lost, cause)
        self.routed = set()  # names routed at least once
        self.reported = set()
        self.slowest = {self.START: 0, self.REPAIR: 0}
        self.pending = []
        self.depth = 0  # largest hop count seen

    def bound_ms(self, hops, cause):
        c = self.checker
        per_hop = c.silence_ms + c.window_ms
        if cause == self.START:
            return c.imin_ms + hops * per_hop
        hold = HOLD_IMIN * c.imin_ms + 2 * DESYNC_SLOTS * c.slot_ms
        return c.silence_ms + hold + hops * per_hop

    def sample(self, now):
        c = self.checker
        hops = c.hops()
        for b in c.nodes():
            n = b.name
            h = hops.get(n)
            if h is not None and h > self.depth:
                self.depth = h
            depth = c.chain(b)
            if depth is not None:
                lost = self.since.pop(n, None)
                if lost is not None:
                    cause = lost[1]
                    self.slowest[cause] = max(self.slowest[cause], now - lost[0])
                self.routed.add(n)
                self.reported.discard(n)
                continue
            if h is None:
                self.since.pop(n, None)  # cut off: no route expected
                self.reported.discard(n)
                continue
            lost = self.since.get(n)
            if lost is None:
                cause = self.REPAIR if n in self.routed else self.START
                lost = (c.started if cause == self.START else now, cause)
                self.since[n] = lost
            bound = self.bound_ms(h, lost[1])
            if now - lost[0] > bound and n not in self.reported:
                self.reported.add(n)
                self.violate(
                    now,
                    (n,),
                    "no route %.1f s after %s at %.1f s, %d hops, bound %.1f s "
                    "(preferred %s, rank %d)"
                    % (
                        (now - lost[0]) / 1000,
                        lost[1],
                        lost[0] / 1000,
                        h,
                        bound / 1000,
                        _name(c.preferred(b)),
                        b.engine.rank,
                    ),
                )

    def finish(self, now):
        # Nodes still without route within their bound: inconclusive, listed.
        hops = self.checker.hops()
        for n, (t, cause) in sorted(self.since.items()):
            if n not in self.reported and n in hops:
                self.pending.append((n, cause, now - t))

    def stats(self):
        depth = self.depth
        out = "forming %.1f s (bound %.1f s at %d hops), repair %.1f s" % (
            self.slowest[self.START] / 1000,
            self.bound_ms(depth, self.START) / 1000,
            depth,
            self.slowest[self.REPAIR] / 1000,
        )
        if self.pending:
            out += ", without route at the end (within bound): %s" % " ".join(
                "%04x" % n for n, _, _ in self.pending
            )
        return out


class VersionWave(Check):
    """MP10 (global repair): connected nodes adopt a new version within the bound.

    §9 (repair times, global repair wave): a hop whose exchange loses n
    frames takes at most (2^(n+1) - 1) Imin, without detach. Bound per hop,
    tolerating two lost frames: 7 Imin + 8 slots (an advertisement's access,
    as in the answer window); per node: that times its depth (hops of its
    chain of preferred parents when the version is issued, else its hop
    count over usable links). ``slowest``: the longest adoption time.
    """

    name = "MP10"
    LOSSES = 2  # lost frames tolerated per hop

    def bind(self, checker):
        super().bind(checker)
        self.issued = None  # (time, version)
        self.depth = {}
        self.adopted = {}
        self.reported = set()
        self.slowest = 0
        self.waves = 0

    def hop_ms(self):
        c = self.checker
        return ((1 << (self.LOSSES + 1)) - 1) * c.imin_ms + WINDOW_SLOTS * c.slot_ms

    def new_version(self, now):
        c = self.checker
        self.issued = (now, c.root.routing.dodag.version)
        self.waves += 1
        hops = c.hops()
        self.depth = {}
        self.adopted = {}
        self.reported = set()
        for b in c.nodes():
            d = c.chain(b)
            self.depth[b.name] = d if d is not None else hops.get(b.name)

    def sample(self, now):
        if self.issued is None:
            return
        t0, version = self.issued
        c = self.checker
        hops = c.hops()
        for b in c.nodes():
            n = b.name
            if n in self.adopted:
                continue
            if b.routing.dodag.version == version:
                self.adopted[n] = now - t0
                self.slowest = max(self.slowest, now - t0)
                continue
            if n not in hops or n in self.reported:
                continue
            depth = self.depth.get(n) or hops[n]
            bound = depth * self.hop_ms()
            if now - t0 > bound:
                self.reported.add(n)
                self.violate(
                    now,
                    (n,),
                    "version %d not adopted %.1f s after issue, depth %d, bound %.1f s "
                    "(version %d, preferred %s, rank %d)"
                    % (
                        version,
                        (now - t0) / 1000,
                        depth,
                        bound / 1000,
                        b.routing.dodag.version,
                        _name(c.preferred(b)),
                        b.engine.rank,
                    ),
                )

    def stats(self):
        if not self.waves:
            return ""
        return "wave %.1f s (%.1f Imin; bound per hop %.1f s)" % (
            self.slowest / 1000,
            self.slowest / self.checker.imin_ms,
            self.hop_ms() / 1000,
        )


# --- MP5 ----------------------------------------------------------------------------------


class Delivery(Check):
    """MP5: no silent class 1 loss; messages go through `submit`.

    Each message is a class 1 message to the root on port, its payload
    unique (`payload`). At the end of the run, every message submitted
    before the end minus drain_ms must be: delivered to the root (its
    inbox; the root listens on port), refused at submission, or reported
    failed to its sender (``Board.failures``). Not counted (§2 MP5, §12
    exceptions): the messages of a board switched off (the sender is gone)
    and those a switched-off relay held a copy of when it died, unless they
    arrive anyway (a relay rebooting after acknowledging; here it never
    comes back); and those failed by a relay cut off from the root while
    their origin is cut off too (no usable path to the root from either
    when the relay fails it: the error cannot travel, D13 in the TLA+
    `DODAG_line_partition_MP5Live`), counted ``partition``.

    Until downward routing exists (§10), the root cannot forward to the
    origin an error about a message failed by a relay (port 4): the root
    holds it without route and drops it. Such a message, whose error
    reached the root, is counted apart (``at_root``), not as a violation:
    the failure is explicit, and reaching the sender is the downward step's
    to check.

    For the diagnosis of a violation, the failures of forwarded messages
    by relays are recorded (``relayed``: payload -> (time, relay, reason,
    cut off)),
    by wrapping each board's ``Reliable.fail`` (the wrapper calls it
    unchanged).
    """

    name = "MP5"

    def __init__(self, port, drain_ms, size=10):
        self.port = port
        self.drain_ms = drain_ms
        self.size = size
        self.sent = []  # (time, board name, sequence or refusal, payload)
        self.root_errors = set()  # (origin, sequence) of the errors heard by the root
        self.lost_with = {}  # payload -> relay switched off holding it
        self.off = set()
        self.counts = {}
        self.relayed = {}

    def bind(self, checker):
        super().bind(checker)
        for b in checker.alive():
            if b is not checker.root:
                self._watch(b)
        engine = checker.root.engine
        reliable = engine.reliable

        def on_error(origin, msg, header):
            try:
                _, _, failed, _, port, seq = decode_error(bytes(msg.payload))
            except ValueError:
                failed = None
            if failed is not None and port == self.port:
                self.root_errors.add((failed, seq))
            reliable.on_error(origin, msg, header)

        engine.bind_mesh(ERROR, on_error)

    def _watch(self, board):
        reliable = board.engine.reliable
        fail = reliable.fail

        def watched(slot, msg, reason, hop, now):
            if msg.port == self.port and msg.origin is not None:
                hops = self.checker.hops()
                cut = board.name not in hops and msg.origin not in hops
                self.relayed[bytes(msg.payload)] = (now, board.name, reason, cut)
            return fail(slot, msg, reason, hop, now)

        reliable.fail = watched

    def payload(self, board):
        """Return a unique payload of `size` bytes (origin 2 B, number 4 B, padding)."""
        n = len(self.sent)
        p = bytearray(self.size)
        a = board.address
        p[0] = a >> 8
        p[1] = a & 0xFF
        for i in range(4):
            p[2 + i] = (n >> (24 - 8 * i)) & 0xFF
        return bytes(p)

    def submit(self, board):
        """Submit one class 1 message from board to the root; return its sequence or refusal."""
        payload = self.payload(board)
        result = board.submit(self.port, 1, ROOT, payload)
        self.sent.append((self.checker.net.now, board.name, result, payload))
        return result

    def switched_off(self, board, now):
        self.off.add(board.name)
        if board.engine is None:
            return
        for _, msg in board.engine.queue.select(lambda m: m.cls and m.port == self.port, True):
            self.lost_with[bytes(msg.payload)] = board.name

    def _copies(self, payload):
        out = []
        for b in self.checker.alive():
            for _, msg in b.engine.queue.select(lambda m: m.port == self.port, True):
                if bytes(msg.payload) == payload:
                    out.append(b.name)
        return out

    def finish(self, now):
        c = self.checker
        delivered = set(r[3] for r in c.root.inbox if r[2] == self.port)
        failed = {}
        for b in c.net.boards.values():
            failed[b.name] = set(f[3] for f in b.failures if f[2] == self.port)
        counts = {
            "submitted": 0,
            "delivered": 0,
            "refused": 0,
            "failed": 0,
            "at_root": 0,
            "exempt": 0,
            "partition": 0,
            "late": 0,
            "lost": 0,
        }
        for t, name, seq, payload in self.sent:
            if t > now - self.drain_ms:
                counts["late"] += 1
                continue
            counts["submitted"] += 1
            if payload in delivered:
                counts["delivered"] += 1
            elif seq < 0:
                counts["refused"] += 1
            elif seq in failed[name]:
                counts["failed"] += 1
            elif (name, seq) in self.root_errors:
                counts["at_root"] += 1
            elif name in self.off or payload in self.lost_with:
                counts["exempt"] += 1
            elif payload in self.relayed and self.relayed[payload][3]:
                counts["partition"] += 1
            else:
                counts["lost"] += 1
                where = "copies at: %s" % (
                    " ".join("%04x" % n for n in self._copies(payload)) or "none"
                )
                relayed = self.relayed.get(payload)
                if relayed is not None:
                    where += "; failed by %04x at %.1f s (reason %d), error lost" % (
                        relayed[1],
                        relayed[0] / 1000,
                        relayed[2],
                    )
                self.violate(
                    now,
                    (name,),
                    "class 1 message %s seq %d submitted at %.1f s neither delivered nor "
                    "failed (%s)" % (_hex(payload), seq, t / 1000, where),
                )
        self.counts = counts

    def stats(self):
        k = self.counts
        if not k:
            return ""
        return (
            "class 1: %(submitted)d submitted, %(delivered)d delivered, %(failed)d failed, "
            "%(refused)d refused, %(at_root)d failed at a relay (error at the root), "
            "%(exempt)d exempt, %(partition)d failed in a partition, %(lost)d silent" % k
        )


# --- MP9 ----------------------------------------------------------------------------------


class DutyCycle(Check):
    """MP9: no board transmits more than its budget in any duty-cycle window.

    §6: a charge counts for at least one window W (60 bins of 60 s plus the
    current bin, in the EU profile) and a transmission starts only if the
    window's use plus its time on air fits the allowance (at most the budget,
    90 % of the profile's limit: 324 s per hour in the EU profile). Hence,
    for every transmission starting at t, the transmissions of its board
    starting in [t - W, t] last at most the budget in total. Measured from
    the medium's transmit times, not from the cores' accounting. W and the
    budget are the network's (`sim.net.Network` ``window_ms``,
    ``budget_ms``), the profile's by default. A board that loses power
    restarts with an empty window (§6, accepted): not supported here (no
    reboot in these runs). ``busiest``: (time on air in a window, board).
    """

    name = "MP9"

    def bind(self, checker):
        super().bind(checker)
        net = checker.net
        self.window = net.window_ms if net.window_ms else net.profile.window_ms()
        self.budget = net.budget_ms if net.budget_ms else net.profile.budget_ms()
        self.recent = {}  # source -> [starts, durations, first index, total]
        self.reported = set()
        self.busiest = (0, None)

    def transmitted(self, source, start, end):
        r = self.recent.get(source)
        if r is None:
            r = [[], [], 0, 0]
            self.recent[source] = r
        starts, durations, first, total = r
        while first < len(starts) and starts[first] < start - self.window:
            total -= durations[first]
            first += 1
        if first > 64:  # drop the expired entries now and then
            del starts[:first]
            del durations[:first]
            first = 0
        starts.append(start)
        durations.append(end - start)
        total += end - start
        r[2] = first
        r[3] = total
        if total > self.busiest[0]:
            self.busiest = (total, source)
        if total > self.budget and source not in self.reported:
            self.reported.add(source)
            self.violate(
                start,
                (source,),
                "%d ms on air in the window of %d s before this transmission > budget %d ms"
                % (total, self.window // 1000, self.budget),
            )

    def stats(self):
        total, source = self.busiest
        return "busiest %s: %.1f s on air in a window (budget %.1f s)" % (
            _name(source),
            total / 1000,
            self.budget / 1000,
        )


# --- MP13 ---------------------------------------------------------------------------------


class Resources(Check):
    """MP13: tables and queues within their bounds (§12 queues and resources, §9).

    Bounds (defaults: the cores' configured sizes): neighbour table 64
    entries (§9), parent set 3 (§9), duplicate cache 128 (§12), transmit
    queue 64 messages (§12), per-address counters 32 (§13), root
    acknowledgement batch 8 entries (§16). ``highest``: the largest value
    seen of each.
    """

    name = "MP13"

    def __init__(
        self,
        neighbours=CAPACITY,
        parents=PARENT_SET,
        duplicates=DUPLICATES,
        queue=QUEUE_SIZE,
        senders=SENDERS,
        batch=ACK_BATCH,
    ):
        self.bounds = {
            "neighbours": neighbours,
            "parents": parents,
            "duplicates": duplicates,
            "queue": queue,
            "senders": senders,
            "batch": batch,
        }
        self.highest = dict((k, 0) for k in self.bounds)

    def bind(self, checker):
        super().bind(checker)
        self.reported = set()

    def sample(self, now):
        for b in self.checker.alive():
            e = b.engine
            sizes = {
                "neighbours": len(e.neighbours),
                "parents": len(b.routing.parents.members()),
                "duplicates": len(e.reliable.duplicates),
                "queue": len(e.queue),
                "senders": len(e.senders),
                "batch": len(e.reliable._batch),  # root batch (no accessor)
            }
            for k, v in sizes.items():
                if v > self.highest[k]:
                    self.highest[k] = v
                if v > self.bounds[k] and (b.name, k) not in self.reported:
                    self.reported.add((b.name, k))
                    self.violate(now, (b.name,), "%s %d > bound %d" % (k, v, self.bounds[k]))

    def stats(self):
        return "highest " + ", ".join(
            "%s %d/%d" % (k, self.highest[k], self.bounds[k]) for k in sorted(self.bounds)
        )


def standard(port, drain_ms):
    """Return the checks of the large-network suite: MP11, MP10, MP5, MP9, MP13."""
    return [
        Loops(),
        Attachment(),
        VersionWave(),
        Delivery(port, drain_ms),
        DutyCycle(),
        Resources(),
    ]


def _name(address):
    return "-" if address is None else "%04x" % address


def _hex(data):
    return "".join("%02x" % c for c in data)
