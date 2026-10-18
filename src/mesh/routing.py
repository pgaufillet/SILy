"""Upward routing of one board: DODAG, forwarding, validation, reports (MESH_DESIGN §9, §11, §16).

`Routing` plugs into a `mesh.link.LinkEngine` (one per boot) and builds:

- its `mesh.dodag.Dodag` (version, Trickle, parent set) and, on a node, its
  `mesh.reports.Reporter` (presence, topology, node information);
- the engine's ``next_hop`` (preferred parent for upward messages) and
  ``reroute`` (`Parents.reroute`: after class 1 attempts are exhausted on a
  parent, the next parent of the set, local repair without exchange, §1,
  §9);
- the engine's ``forward``, ``expand`` and ``repeated`` extension points.

**Upward forwarding** (§1, §9): an upward message goes to the preferred
parent. After a change of preferred parent, the queued upward messages not
waiting for an acknowledgement follow the new parent
(`mesh.txqueue.TxQueue.redirect`), and the presence report leaves at once.
After a detach they do not go to the former parent: class 1/2 ones are held,
those waiting for an acknowledgement included (not retried towards a
parent of rank 255; §12 holding during repair, resumed on the next parent),
class 0 ones are dropped (counted as ``no_route`` by the link).
Forwarded presence, topology and node information reports replace an older
report of the same origin still queued (class 0 replace by newer, §12): the
relay aggregates the reports of its subtree into the frames it sends anyway
(§7, §16; bundling by `mesh.txqueue` and `mesh.frame`).

**First-hop compression** (§11): a node's own reports leave in mode 6; the
node that receives them rewrites them in mode 1 (origin = link source) and
restores the presence report's parent field (itself), so that relays forward
and the root decodes the mode 1 form only.

**Data-path validation** (§9 loop freedom; CTP, RPL §11.2): an upward
message received from a sender of the same DODAG version whose rank is not
higher than this node's (finite) rank reveals an inconsistency: the node
calls `Dodag.rank_error` (counted, Trickle reset, presence flag), drops a
class 0 message (``rank_dropped``) and forwards a class 1/2 one, unless the
duplicate cache already holds it and the node still holds a copy: then it
has looped and is dropped and counted (``loops``; the link acknowledges it,
so its sender stops). An upward message whose origin is this node has come
back, a loop (counted): dropped (taken, so acknowledged) while the node
still holds its copy or the message is class 0, otherwise forwarded again
(custody, §12: no copy would remain). Across versions, ranks are not
compared (§9).

**Root**: the reports (ports 1, 2, 5) are decoded and handed to
``on_report(port, origin, report, header)`` (`mesh.reports.Presence`, the
topology entries, `mesh.reports.NodeInfo`): the root's graph (§10, §13) is
built on it; malformed ones are counted.

Readings where §9/§11 are silent: the first-hop presence is restored by
whichever node receives it (relay or root); an own message that comes back
is a loop even without rank inconsistency; a rank error is counted per
message (a bundle from an inconsistent sender counts each of its
elements); P2P messages are not validated here (their upward leg is routed
with the downward step).
"""

from mesh.dodag import Dodag
from mesh.frame import FIRST_HOP, RANK_INFINITE, UPWARD
from mesh.link import DELIVERED, NOT_TAKEN
from mesh.parents import PARENT_CHANGED
from mesh.ports import NODE_INFO, PRESENCE, TOPOLOGY
from mesh.reliable import identity
from mesh.reports import (
    Reporter,
    decode_node_info,
    decode_presence,
    decode_topology,
    restore_parent,
)

_REPORTS = (PRESENCE, TOPOLOGY, NODE_INFO)


def _upward(msg):
    return msg.mode == UPWARD or msg.mode == FIRST_HOP


class Counters:
    """Routing counters since boot (§13; rank errors are counted by `Dodag`)."""

    def __init__(self):
        self.loops = 0  # upward messages that came back: own, or inconsistent duplicates
        self.rank_dropped = 0  # class 0 messages dropped on a rank inconsistency
        self.redirected = 0  # queued upward messages sent to a new preferred parent
        self.reports = 0  # root: reports received and decoded
        self.malformed = 0  # root: reports that cannot be decoded


class Routing:
    """Routing component of one board (see the module docstring).

    engine: its `LinkEngine` (the root if its address is the root's);
    profile, info, version: as `Dodag`; node_info, extension, tails,
    next_key, entries: as `mesh.reports.Reporter` (nodes only).

    Read: ``dodag``, ``parents``, ``reporter`` (None on the root),
    ``counters``, ``attached``; set ``on_report`` on the root.
    """

    def __init__(
        self,
        engine,
        profile,
        info=None,
        version=0,
        node_info=None,
        extension=None,
        tails=None,
        next_key=None,
        entries=6,
    ):
        self.engine = engine
        self.dodag = Dodag(engine, profile, info, version)
        self.parents = self.dodag.parents
        self.counters = Counters()
        self.on_report = None
        engine.next_hop = self.dodag.next_hop
        engine.reroute = self.parents.reroute
        self._base_forward = engine.forward
        self._base_expand = engine.expand
        self._base_repeated = engine.repeated
        engine.forward = self._forward
        engine.expand = self._expand
        engine.repeated = self._repeated
        self.dodag.changed = self._changed
        if engine.is_root:
            self.reporter = None
            for port in _REPORTS:
                engine.bind_mesh(port, self._on_report)
        else:
            self.reporter = Reporter(
                engine, self.dodag, node_info, extension, tails, next_key, entries
            )

    @property
    def attached(self):
        """True on the root and on a node with a preferred parent."""
        return self.dodag.attached

    def inconsistent(self, header):
        """Return True if an upward message in a frame with header reveals a rank error."""
        e = self.engine
        return (
            not e.is_root
            and e.rank != RANK_INFINITE
            and header.dodag_version == e.dodag_version
            and header.rank <= e.rank
        )

    # --- Link engine hooks -----------------------------------------------------------

    def _expand(self, msg, header):
        if msg.mode == FIRST_HOP and msg.port == PRESENCE:
            msg.payload = restore_parent(msg.payload, self.engine.address)
        self._base_expand(msg, header)

    def _forward(self, msg, header, now):
        if msg.mode != UPWARD:
            return self._base_forward(msg, header, now)
        if self.inconsistent(header):
            self.dodag.rank_error(now)
            if not msg.cls:
                self.counters.rank_dropped += 1
                return NOT_TAKEN
        if msg.origin == self.engine.address:
            # Back from a loop: dropped while still held (custody, §12),
            # otherwise taken again.
            self.counters.loops += 1
            key = identity(msg, header.source, None)
            if not msg.cls or self.engine.reliable.holding(key, header.source):
                return DELIVERED
        return self._base_forward(msg, header, now, not msg.cls and msg.port in _REPORTS)

    def _repeated(self, msg, header, now):
        self._base_repeated(msg, header, now)
        if msg.mode == UPWARD and self.inconsistent(header):
            self.dodag.rank_error(now)
            self.counters.loops += 1

    def _changed(self, events, now):
        if events & PARENT_CHANGED:
            e = self.engine
            hop = self.parents.preferred
            if hop is not None:
                self.counters.redirected += e.queue.redirect(_upward, hop)
            else:
                # Waiting copies too: not retried towards the former parent.
                for slot, msg in e.queue.select(_upward, True):
                    if msg.cls:
                        e.reliable.hold(slot, msg, now)
                    else:
                        e.queue.remove(slot, msg)
                        e.counters.no_route += 1
        if self.reporter is not None:
            self.reporter.changed(events, now)

    # --- Root -------------------------------------------------------------------------

    def _on_report(self, origin, msg, header):
        port = msg.port
        try:
            if msg.mode != UPWARD:
                raise ValueError("not upward")
            if port == PRESENCE:
                report = decode_presence(msg.payload)
            elif port == TOPOLOGY:
                report = decode_topology(msg.payload)
            else:
                report = decode_node_info(msg.payload)
        except ValueError:
            self.counters.malformed += 1
            return
        self.counters.reports += 1
        if self.on_report is not None:
            self.on_report(port, origin, report, header)
