"""DODAG version, DODAG information and repair timers, node side (MESH_DESIGN §9; §11 port 9).

**DODAG information** (port 9, link-local, class 0): empty = solicitation;
otherwise 8 B: flags 1 B (bit 0 topology report on change), presence period
2 B (s), topology period 2 B (s), Imin 1 B (slots), maximum rank increase
1 B, regional profile identifier 1 B (§4); little-endian. Its version is the
frame header's. `encode_info` / `decode_info`.

**Versions** (4 bits, RFC 1982 serial arithmetic): `compare_versions` says
whether a version is newer or older than another; versions 8 apart are not
comparable (None).

**`Dodag`**: the routing component of one board, plugged into its
`mesh.link.LinkEngine` through the extension points (``heard``, ``sent``,
``tick``, ``timer``: each wrapper calls the engine's previous method) and the
port 9 handler. It owns the board's `mesh.parents.Parents` and its
`mesh.trickle.Trickle`, and keeps the header fields ``engine.rank`` and
``engine.dodag_version`` up to date.

- **Selection**: the parent set is recomputed after every step in which a
  frame was heard (after the frame's messages, so that a DODAG information
  message of a new version is adopted first), when a member falls silent
  (the silence limit, `mesh.neighbours.silence_ms`: 2.5 presence periods),
  when a member's acknowledgement failures age out (its ETX may fall),
  when the detach hold ends and when the answer window closes.
- **Advertisements**: when the Trickle timer says so, a frame to every
  neighbour holding one DODAG information message (23 B), priority 1 (every
  Trickle advertisement follows a reset, §6); one still queued (the root
  beacon) takes the new one's place, raised to priority 1 if needed. Every
  frame sent counts as sent for suppression, every frame of the own version
  from a neighbour of finite rank as heard.
- **Trickle resets** (§9): adoption of a new version; a frame of an older
  version from a neighbour of finite rank; a solicitation (attached nodes
  and the root); an increase of the own rank; poisoning; a rank error
  (`rank_error`, data-path validation); the first parent after boot or
  detach. `reset` is the generic entry point. After a rank error the node
  also advertises at once, at most once per Imin: §9 wants its rank to
  reach the inconsistent sender within Imin, which a Trickle advertisement
  suppressed by k = 2 frames of the neighbourhood would not guarantee (in
  a loop, the frames of the loop itself suppress it).
- **Adoption**: an attached node adopts a newer version from the DODAG
  information of a neighbour of finite rank; a detached node adopts any
  other version, but only from a DODAG information heard after the
  solicitation that ends its hold (the rule of the rejoin: during the hold
  a descendant that lost the poison may still advertise, in an older
  version too); one that has adopted nothing since boot adopts any
  version, its own included. It takes the network-wide parameters
  (periods, Imin, maximum rank increase, flags), resets L
  (`Parents.on_new_version`, which keeps a detach hold and its rejoin
  rule) and its Trickle timer, the DODAG information owed (port 9 messages
  only suppress it); a node without route resets it when it attaches (in
  an answer window, below). A message with another regional profile
  identifier is not adopted and is counted (``foreign_profile``, MP22), as
  is a malformed one.
- **Across versions** (§9): a frame of a newer version from a neighbour of
  finite rank means the node is behind (`Parents.behind`): it keeps
  selecting in its own version, keeps its preferred parent once that parent
  has moved to the newer version (at its own rank) and forwards through it,
  and solicits with backoff until it adopts the version: at once (at most
  once per Imin), then after Imin, the gap doubling up to the presence
  period; a detach ends them (the hold's solicitation follows). A frame of
  an older version resets the Trickle timer: the solicitations and every
  other frame of a node behind make its newer neighbours answer within
  Imin.
- **Detach** (poisoning, §9 loop freedom): when no candidate remains, the
  node advertises rank 255 at once (Trickle reset), selects nothing for
  2 Imin (``Parents.hold_ms``), then stops its Trickle timer and sends a
  solicitation without selecting; the hold lasts until the solicitation
  goes on air (`Parents.solicited`, from ``sent``: a full queue or the duty
  cycle keep the node detached), and the node rejoins only through
  neighbours heard after it. The poison is sent once: a child that lost it
  hears the solicitation (rank 255) and detaches in turn.
  The poison and the solicitations leave after a random delay of at most
  `DESYNC_SLOTS` slots (the frame header says rank 255 at once): nodes
  that detach on one event (the children of a silent relay, which heard
  its last frame together) would otherwise send them together, and a
  neighbour that loses both poisons to the collision keeps a stale route
  through which they rejoin (a loop; §7 desynchronises relays the same
  way).
- **Solicitation**: a node without route solicits at boot after a random
  delay in [0, Imin], at the end of the detach hold, then once per presence
  period while it has no route; a node behind a newer version, with
  backoff (above).
- **Answer window**: a solicitation of a node without route (at boot, at
  the end of the hold, the periodic ones; `Parents.solicited`) opens a
  window of Imin + `WINDOW_SLOTS` slots from the time it goes on air
  (``Parents.window_ms``), during which the node selects nothing. The
  neighbours reset their Trickle timers at the end of the solicitation (at
  most one slot on air) and answer before Imin; an answer then waits at
  most one busy channel sense (a backoff of at most `mesh.link.CW_MIN`
  slots, §8) and takes at most 1.25 slots on air (23 B): 1 + 4 + 2 slots,
  and one more for the step. At its close the node takes the best
  candidate heard (lowest path cost, §9), or with none the first candidate
  heard afterwards (selection at each frame heard). A version adopted in
  the window keeps it (`Parents.on_new_version`); at boot, the first DODAG
  information is adopted at once (parameters), the parent selected at the
  close of the window if it is still open.
- **Root**: rank 0, never adopts. **Beacon** (§16): one DODAG information
  advertisement, priority 2, every presence period jittered by ±10 % (§7),
  the timer re-armed from each firing; it runs beside Trickle, whose resets
  (solicitation, older version, new version) send their priority-1
  advertisements without postponing or suppressing it, so that the root's
  frames are never further apart than 1.1 periods while beacons are not
  lost. `set_version` issues a version (and new parameters) and re-arms the
  beacon on the new period: the policy deciding when (global repair, §9)
  belongs to the root's later step.

``next_hop`` gives the preferred parent for upward messages (the link
engine's ``next_hop``, set by `mesh.routing.Routing`).

**Events**: every change of the parent set is applied from
`Parents.events`, the events accumulated since the last step included: a
parent dropped by class 1 (``engine.reroute``, `Parents.next_parent`, run by
the class 1 timers) updates the header rank, poisons or resets Trickle in
the same step. ``changed``, if set, is called with (events, now) after each
application (`mesh.routing`: presence report at once after a change of
preferred parent, queued upward messages redirected).

Readings where §9/§11 are silent: a node that has adopted nothing since
boot selects no parent (it learns the network's parameters first); a
detached node neither answers solicitations nor resets on older versions
(it has no route to advertise); frames of rank 255 are neither counted nor
compared (they carry no version, §9); a version 8 apart is ignored and
counted (``incomparable``); a DODAG information message with a presence
period or Imin of 0 is malformed.
"""

from mesh.frame import BROADCAST, FIRST_HOP, LINK_LOCAL, RANK_INFINITE, UPWARD, Message
from mesh.neighbours import presence_ms, silence_ms
from mesh.parents import (
    ATTACHED,
    DETACHED,
    MAX_RANK_INCREASE,
    PARENT_CHANGED,
    RANK_INCREASED,
    VERSION_MASK,
    Parents,
)
from mesh.platform import jittered, randbelow
from mesh.ports import DODAG_INFO
from mesh.ports import priority as port_priority
from mesh.trickle import IMIN_SLOTS, URGENT, Trickle
from mesh.txqueue import accepted

INFO_SIZE = 8
DESYNC_SLOTS = 4  # random delay of the poison and solicitations (CW_MIN slots, §8)
TOPOLOGY_ON_CHANGE = 0x01  # flags bit 0: topology report on change (deployment, §17)
HOLD_IMIN = 2  # detach hold, in Imin (§9)
WINDOW_SLOTS = 8  # answer window beyond Imin: solicitation 1 + backoff 4 + answer 2 + step 1

# Topology period (s) per SF 7-12, SILy profile (§16).
TOPOLOGY_S = (180, 300, 600, 900, 3600, 3600)

_HALF = 8  # serial arithmetic on 4 bits: versions less than 8 apart compare


def compare_versions(a, b):
    """Compare 4-bit DODAG versions: 1 if a is newer than b, -1 if older, 0 if equal.

    None when they are 8 apart (not comparable, RFC 1982).
    """
    d = (a - b) & VERSION_MASK
    if d == 0:
        return 0
    if d < _HALF:
        return 1
    if d > _HALF:
        return -1
    return None


class DodagInfo:
    """Network-wide parameters of a DODAG version (§9, port 9 payload).

    flags (bit 0 `TOPOLOGY_ON_CHANGE`); presence_s, topology_s: periods
    (s); imin_slots: Imin (slots); max_increase: maximum rank increase;
    profile: regional profile identifier (§4).
    """

    def __init__(self, flags, presence_s, topology_s, imin_slots, max_increase, profile):
        self.flags = flags
        self.presence_s = presence_s
        self.topology_s = topology_s
        self.imin_slots = imin_slots
        self.max_increase = max_increase
        self.profile = profile

    def _fields(self):
        return (
            self.flags,
            self.presence_s,
            self.topology_s,
            self.imin_slots,
            self.max_increase,
            self.profile,
        )

    def __eq__(self, other):
        return isinstance(other, DodagInfo) and self._fields() == other._fields()

    def __repr__(self):
        return "DodagInfo(%d, %d, %d, %d, %d, %d)" % self._fields()


def default_info(sf, profile):
    """Return the parameters of the SILy profile at sf (§16) for regional profile id profile."""
    return DodagInfo(
        0, presence_ms(sf) // 1000, TOPOLOGY_S[sf - 7], IMIN_SLOTS, MAX_RANK_INCREASE, profile
    )


def encode_info(info, buf=None):
    """Return the 8-byte payload of info (written into buf, a bytearray, if given).

    Raises ValueError for a field out of range.
    """
    for value, high in (
        (info.flags, 0xFF),
        (info.presence_s, 0xFFFF),
        (info.topology_s, 0xFFFF),
        (info.imin_slots, 0xFF),
        (info.max_increase, 0xFF),
        (info.profile, 0xFF),
    ):
        if type(value) is not int or value < 0 or value > high:
            raise ValueError("field out of range")
    if buf is None:
        buf = bytearray(INFO_SIZE)
    buf[0] = info.flags
    buf[1] = info.presence_s & 0xFF
    buf[2] = info.presence_s >> 8
    buf[3] = info.topology_s & 0xFF
    buf[4] = info.topology_s >> 8
    buf[5] = info.imin_slots
    buf[6] = info.max_increase
    buf[7] = info.profile
    return buf


def decode_info(payload):
    """Return the `DodagInfo` of a port 9 payload, None for a solicitation (empty).

    Raises ValueError for another length, a presence period or Imin of 0.
    """
    n = len(payload)
    if n == 0:
        return None
    if n != INFO_SIZE:
        raise ValueError("bad length")
    info = DodagInfo(
        payload[0],
        payload[1] | payload[2] << 8,
        payload[3] | payload[4] << 8,
        payload[5],
        payload[6],
        payload[7],
    )
    if not info.presence_s or not info.imin_slots:
        raise ValueError("bad parameters")
    return info


class Counters:
    """DODAG counters since boot (§13 local observability; MP22)."""

    def __init__(self):
        self.adverts = 0  # advertisements queued (poison included)
        self.solicits = 0  # solicitations queued
        self.solicited = 0  # solicitations received
        self.adoptions = 0  # versions adopted (first one after boot included)
        self.poisons = 0  # detaches advertised (rank 255)
        self.older = 0  # frames of an older version from a neighbour of finite rank
        self.newer = 0  # frames of a newer version from a neighbour of finite rank
        self.incomparable = 0  # frames of a version 8 apart
        self.rank_errors = 0  # reported by data-path validation
        self.foreign_profile = 0  # DODAG information of another regional profile (MP22)
        self.malformed = 0  # DODAG information that cannot be decoded


class Dodag:
    """Routing state of one board, plugged into its link engine (see the module docstring).

    engine: its `mesh.link.LinkEngine` (root if its address is the root's);
    profile: own regional profile identifier (§4); info: `DodagInfo` the
    root issues, or a node's parameters until it adopts the network's
    (`default_info` of the engine's SF by default); version: the root's
    first DODAG version; parents: its `Parents` (built on the engine's
    neighbour table by default).

    Read: ``info``, ``version``, ``configured`` (a version adopted since
    boot; always True on the root), ``attached``, ``imin_ms``,
    ``topology_ms``, ``parents``, ``trickle``, ``counters``.
    """

    def __init__(self, engine, profile, info=None, version=0, parents=None):
        self.engine = engine
        self.root = engine.is_root
        self.profile = profile
        self.counters = Counters()
        if info is None:
            info = default_info(engine.sf, profile)
        if parents is None:
            parents = Parents(engine.neighbours, root=self.root, clock=self._clock)
        self.parents = parents
        self.trickle = Trickle(1, 1, engine.random)
        self._buf = bytearray(INFO_SIZE)  # payload of every advertisement, updated in place
        self._apply_info(info)
        self.version = version & VERSION_MASK if self.root else 0
        self.configured = self.root
        parents.on_new_version(self.version, 0)
        engine.dodag_version = self.version
        engine.rank = parents.rank
        self._started = False
        self._dirty = False  # a frame was heard: select again in this step's tick
        self._check_at = None  # a member falls silent or its failures age out
        self._beacon_at = None  # root: next beacon
        self._solicit_at = None  # next solicitation of a node without route
        self._poison_at = None  # poison advertisement due
        self._ask_at = None  # solicitation for the DODAG information of another version
        self._asked = None  # time of the last one (at most one per Imin)
        self._ask_gap = None  # behind a newer version: gap to the next one (backoff)
        self._advert = None  # (slot, message) of the last advertisement queued
        self._rank_advert = None  # time of the last advertisement after a rank error
        self._solicit = None  # ... and of the last solicitation
        self._info_owed = False  # a version adopted: Trickle reset with its information owed
        self.changed = None  # function(events, now) after events are applied
        # Extension points: each wrapper calls the engine's previous method.
        self._base_heard = engine.heard
        self._base_sent = engine.sent
        self._base_tick = engine.tick
        self._base_timer = engine.timer
        engine.heard = self._heard
        engine.sent = self._sent
        engine.tick = self._tick
        engine.timer = self._timer
        engine.bind_mesh(DODAG_INFO, self._on_info)

    def _clock(self):
        return self.engine.now

    @property
    def attached(self):
        """True on the root and on a node with a preferred parent."""
        return self.root or self.parents.rank != RANK_INFINITE

    def _apply_info(self, info):
        encode_info(info, self._buf)
        self.info = info
        engine = self.engine
        self.imin_ms = info.imin_slots * engine.slot_ms
        self.topology_ms = info.topology_s * 1000
        period = info.presence_s * 1000
        engine.neighbours.set_period(period)
        self.trickle.configure(self.imin_ms, period)
        self.parents.hold_ms = HOLD_IMIN * self.imin_ms
        self.parents.window_ms = self.imin_ms + WINDOW_SLOTS * engine.slot_ms
        self.parents.max_increase = info.max_increase

    # --- Entry points ------------------------------------------------------------------

    def next_hop(self, msg):
        """Return the preferred parent for an upward message, else None."""
        if msg.mode == UPWARD or msg.mode == FIRST_HOP:
            return self.parents.preferred
        return None

    def reset(self, now, need_info=False):
        """Reset the Trickle timer (an event of §9) if the node has a route."""
        if self.attached:
            self.trickle.reset(now, need_info)

    def rank_error(self, now):
        """Data-path validation found a rank inconsistency (§9): count it, reset Trickle.

        The node advertises at once (at most once per Imin), so that its rank
        reaches the sender within Imin whatever the suppression.
        """
        self.counters.rank_errors += 1
        self.reset(now)
        if not self.attached:
            return
        last = self._rank_advert
        if last is None or now >= last + self.imin_ms:
            self._rank_advert = now
            self._advertise(True)

    def set_version(self, version, now, info=None):
        """Root: issue DODAG version (and new parameters info); Trickle reset."""
        if not self.root:
            raise ValueError("not the root")
        if info is not None:
            self._apply_info(info)
        self.version = version & VERSION_MASK
        self.engine.dodag_version = self.version
        self.parents.on_new_version(self.version, now)
        self.trickle.reset(now, True)
        self._beacon_at = now + jittered(self.engine.neighbours.period_ms, self.engine.random)

    # --- Link engine hooks ---------------------------------------------------------------

    def _heard(self, header, rssi, snr, now):
        n = self._base_heard(header, rssi, snr, now)
        if n is None:
            return n
        self._dirty = True  # a poison too: its children select again
        if header.rank == RANK_INFINITE:
            return n
        c = self.counters
        cmp = compare_versions(header.dodag_version, self.version)
        if not self.attached or not self.configured:
            if cmp != 0 and self.configured:
                self._ask(now)  # a detached node adopts another version
            return n
        if cmp == 0:
            self.trickle.heard()
        elif cmp is None:
            c.incomparable += 1
        elif cmp < 0:
            c.older += 1
            self.trickle.reset(now, True)
        else:
            c.newer += 1
            p = self.parents
            if not self.root and not p.behind:
                p.behind = True  # solicit with backoff until adopted
                self._ask_gap = self.imin_ms
                self._ask(now)
        return n

    def _ask(self, now):
        # Solicit in this step's tick, at most once per Imin.
        if self._ask_at is None:
            t = now
            if self._asked is not None and self._asked + self.imin_ms > now:
                t = self._asked + self.imin_ms
            self._ask_at = t

    def _sent(self, sel, counter, now):
        self._base_sent(sel, counter, now)
        info = False
        for m in sel.messages:
            if m.port == DODAG_INFO:
                if len(m.payload):
                    info = True
                elif not self.root:
                    self.parents.solicited(now)  # ends the detach hold, opens the window
                    self._watch()  # its close
        self.trickle.transmitted(info)

    def _on_info(self, origin, msg, header):
        now = self.engine.now
        c = self.counters
        payload = msg.payload
        if not len(payload):
            c.solicited += 1
            self.reset(now, True)
            return
        try:
            info = decode_info(payload)
        except ValueError:
            c.malformed += 1
            return
        if info.profile != self.profile:
            c.foreign_profile += 1
            return
        if header.rank == RANK_INFINITE:
            return
        version = header.dodag_version
        cmp = compare_versions(version, self.version)
        if not self.root:
            if self.attached:
                adopt = cmp == 1
            else:  # another version, heard after the solicitation ending the hold
                p = self.parents
                adopt = cmp != 0 and p.hold_until is None and not p.soliciting
            if adopt or not self.configured:
                self._adopt(version, info, now)
                return
        if cmp == 0 and self.attached:
            self.trickle.heard(True)

    def _tick(self, now):
        if not self._started:
            self._start(now)
        if not self.root:
            self._routing(now)
        elif self._beacon_at is not None and now >= self._beacon_at:
            self._beacon_at = now + jittered(self.engine.neighbours.period_ms, self.engine.random)
            self._advertise(False)
        due = self.trickle.poll(now)
        if due:
            self._advertise(due == URGENT)
        self._base_tick(now)
        # Parent dropped by class 1 during the timers (reroute): apply now.
        events = self.parents.events()
        if events:
            self._apply(events, now)

    def _timer(self):
        t = self._base_timer()
        for u in (
            self.trickle.timer(),
            self._beacon_at,
            self._check_at,
            self._solicit_at,
            self._poison_at,
            self._ask_at,
        ):
            if u is not None and (t is None or u < t):
                t = u
        return t

    # --- Node side -------------------------------------------------------------------------

    def _start(self, now):
        self._started = True
        if self.root:
            self.trickle.reset(now, True)
            self._beacon_at = now + jittered(self.engine.neighbours.period_ms, self.engine.random)
        else:
            self._solicit_at = now + randbelow(self.engine.random, self.imin_ms + 1)

    def _desync(self):
        return randbelow(self.engine.random, DESYNC_SLOTS * self.engine.slot_ms + 1)

    def _routing(self, now):
        if self._poison_at is not None and now >= self._poison_at:
            self._poison_at = None
            if not self.attached:
                self._advertise(True)
        p = self.parents
        if self._ask_at is not None and now >= self._ask_at:
            self._ask_at = None
            self._asked = now
            self._send_solicitation()
            if p.behind:  # again after the gap, doubled up to the presence period
                gap = self._ask_gap
                self._ask_at = now + gap
                period = self.engine.neighbours.period_ms
                self._ask_gap = 2 * gap if 2 * gap < period else period
        due = self._dirty or (self._check_at is not None and now >= self._check_at)
        if p.hold_until is not None and now >= p.hold_until:
            due = True
        if p.window_until is not None and now >= p.window_until:
            due = True
        if due and self.configured:
            self._dirty = False
            p.recompute(now)
            self._apply(p.events(), now)
        if not self.attached and self._solicit_at is not None and now >= self._solicit_at:
            if p.hold_until is None or now >= p.hold_until:
                self.trickle.stop()
                self._send_solicitation()
                self._solicit_at = now + self.engine.neighbours.period_ms + self._desync()

    def _apply(self, events, now):
        p = self.parents
        if events & DETACHED:
            self._info_owed = False
            self._poison(now)
        else:
            self.engine.rank = p.rank
            if events & ATTACHED:
                self._solicit_at = None
            if self._info_owed and self.attached:
                self._info_owed = False
                self.trickle.reset(now, True)
            elif events & (ATTACHED | RANK_INCREASED):
                self.trickle.reset(now)
        if events & PARENT_CHANGED:
            self.engine.reliable.route_changed(now)
        self._watch()
        if self.changed is not None and events:
            self.changed(events, now)

    def _watch(self):
        # Next time a member of the parent set falls silent or its failures
        # age out (same limit, `NeighbourTable.expire`), the hold ends or the
        # answer window closes (no selection before the first adoption).
        p = self.parents
        t = p.hold_until
        w = p.window_until
        if w is not None and self.configured and (t is None or w < t):
            t = w
        limit = silence_ms(self.engine.neighbours.period_ms)
        for a in p.members():
            n = self.engine.neighbours.get(a)
            if n is None:
                continue
            for u in (n.heard, n.fail_at):
                if u is not None and (t is None or u + limit < t):
                    t = u + limit
        self._check_at = t

    def _poison(self, now):
        self.engine.rank = RANK_INFINITE
        self.counters.poisons += 1
        self._ask_at = None  # no longer behind: the hold's solicitation follows
        self.trickle.reset(now)
        self._poison_at = now + self._desync()
        hold = self.parents.hold_until
        self._solicit_at = (now if hold is None else hold) + self._desync()

    def _adopt(self, version, info, now):
        self._apply_info(info)
        self.version = version
        self.engine.dodag_version = version
        self.configured = True
        self.counters.adoptions += 1
        self._ask_at = None
        self._dirty = False
        self.parents.on_new_version(version, now)
        self._info_owed = True  # at once, or once attached (answer window)
        self._apply(self.parents.events(), now)

    # --- Sending -----------------------------------------------------------------------------

    def _queued(self, entry):
        return entry is not None and self.engine.queue.holds(entry[0], entry[1])

    def _advertise(self, urgent):
        """Queue an advertisement, unless one is still queued (raised to urgent if needed)."""
        entry = self._advert
        if self._queued(entry):
            if urgent:  # a queued root beacon (priority 2) takes priority 1
                self.engine.queue.promote(entry[0], entry[1], port_priority(DODAG_INFO, True))
            return
        msg = Message(LINK_LOCAL, 0, DODAG_INFO, self._buf)
        if self._offer(msg, urgent):
            self._advert = (self.engine.queue.slot, msg)
            self.counters.adverts += 1

    def _send_solicitation(self):
        if self._queued(self._solicit):
            return
        msg = Message(LINK_LOCAL, 0, DODAG_INFO, b"")
        if self._offer(msg, True):
            self._solicit = (self.engine.queue.slot, msg)
            self.counters.solicits += 1

    def _offer(self, msg, urgent):
        return accepted(self.engine.send(msg, BROADCAST, trickle_reset=urgent))
