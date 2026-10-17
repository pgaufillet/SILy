"""Rank, parent set and preferred parent (MESH_DESIGN §9; MP11, MP12).

One `Parents` per board, reading its `mesh.neighbours.NeighbourTable`.
Integer arithmetic; ranks are the 1-byte header values (0 = root, 255 =
infinite: no route or poisoned).

- **Link cost** = round(8 × ETX) (`MIN_HOP` = 8 per hop of ETX 1, RPL
  MinHopRankIncrease), 8 to 32 on an eligible link; **path cost** through a
  neighbour = its rank + link cost; above 254 the neighbour is not usable.
- **Candidates**: neighbours of the node's DODAG version, of rank strictly
  lower than the node's advertised rank (any finite rank without route),
  heard within the last `SILENCE_PERIODS` (2) presence periods, through an
  eligible link (`Neighbour.eligible`), whose path cost is at most the
  **bound** min(254, L + max_increase): L is the lowest rank advertised in
  this DODAG version, max_increase 56 by default (MaxRankIncrease, carried
  by the DODAG information, §9). A neighbour dropped after failed attempts
  (`next_parent`) is no candidate until a new frame of it is heard; after a
  detach, only neighbours heard after the poison are.
- **Parent set**: the `SIZE` (3) candidates of lowest path cost, ordered by
  path cost, then larger margin (§9 tie-break), then lower address (so that
  the result does not depend on the table's slot order). **Preferred
  parent**: kept while it is in the set and no candidate's path cost is
  lower by `SWITCH_THRESHOLD` (12) or more; otherwise the first one. The set
  is listed preferred parent first, then by that order.
- **Advertised rank** (``rank``) = path cost through the preferred parent,
  updated when it differs by `RANK_HYSTERESIS` (4) or more, or when the
  preferred parent changes. Members whose rank is not strictly lower than
  the new advertised rank leave the set (rank rule).
- **Detach**: when no candidate remains (bound exceeded included), the node
  advertises 255 at once, clears L and the set, and selects nothing for
  ``hold_ms`` (2 Imin, set by the Trickle step; 0 by default); it then
  rejoins through neighbours heard after the poison, at any rank.
- **Parent flags**: the set's members are flagged in the neighbour table
  (`NeighbourTable.set_parent`), so that eviction spares them.

Entry points for the later steps: `recompute` (after frames are heard, at
each presence period, on a timer), `next_parent` / `reroute` (the link
engine's ``reroute``: class 1 attempts exhausted on a parent, §12), `detach`
and `on_new_version` (Trickle and DODAG version, §9). Each returns event
flags (`PARENT_CHANGED`, `RANK_CHANGED`, `RANK_INCREASED`, `DETACHED`,
`ATTACHED`), also accumulated until read by `events`: the caller copies
``rank`` and ``version`` into the frame header (``LinkEngine.rank``,
``dodag_version``), resets Trickle, sends its presence at once after a
parent change and calls ``Reliable.route_changed``.

Readings where §9 is silent: the node's rank compared with candidates is
the advertised one; a preferred parent outside the 3 lowest path costs has
left the set even when no candidate beats it by 12; the bound L + 56 makes a
candidate unusable (as a path cost above 254 does), the node detaching when
none remains; a dropped parent stays excluded until heard again; the root
keeps rank 0 and selects nothing.
"""

from mesh.frame import FIRST_HOP, RANK_INFINITE, RANK_ROOT, UPWARD
from mesh.neighbours import ETX_ONE
from mesh.reliable import HOLD

MIN_HOP = 8  # rank increase of one hop of ETX 1 (RPL MinHopRankIncrease)
MAX_COST = 254  # highest usable path cost (255 is infinite)
MAX_RANK_INCREASE = 56  # 7 perfect hops (Contiki-NG's MaxRankIncrease)
SIZE = 3  # parent set members
SWITCH_THRESHOLD = 12  # path cost gain to switch preferred parent (1.5 hops)
RANK_HYSTERESIS = 4  # advertised rank change to advertise it (half a hop)
SILENCE_PERIODS = 2  # presence periods without a frame: no longer a candidate
VERSION_MASK = 0x0F  # 4-bit DODAG version

# Event flags.
PARENT_CHANGED = 1  # preferred parent changed (presence report at once)
RANK_CHANGED = 2  # advertised rank changed
RANK_INCREASED = 4  # advertised rank increased, poisoning included (Trickle reset)
DETACHED = 8  # poisoned: rank 255 (Trickle reset)
ATTACHED = 16  # first parent after boot or detach (Trickle reset)


def link_cost(etx):
    """Return round(8 × ETX) of an ETX in units of 1/ETX_ONE, at least 8."""
    cost = (etx * MIN_HOP + ETX_ONE // 2) // ETX_ONE
    return cost if cost > MIN_HOP else MIN_HOP


def path_cost(rank, etx):
    """Return the path cost through a neighbour of rank, over a link of etx."""
    return rank + link_cost(etx)


def _insert(best, key):
    # Keep best sorted and at most SIZE long.
    i = len(best)
    while i and key < best[i - 1]:
        i -= 1
    if i < SIZE:
        best.insert(i, key)
        if len(best) > SIZE:
            best.pop()


class Parents:
    """Rank and parent set of one board (see the module docstring).

    table: its `NeighbourTable`; root: True on the root (rank 0, no parent);
    max_increase: MaxRankIncrease; hold_ms: selection pause after a detach;
    clock: function() -> now (ms) used by `reroute`, which receives no time
    (None: the time of the last `recompute`).

    Read: ``rank`` (advertised), ``version``, ``lowest`` (L, None after a
    detach or a new version), ``preferred`` (address or None), ``cost``
    (path cost through it at the last selection), `members`; counters
    ``switches``, ``detaches``, ``drops``.
    """

    def __init__(self, table, root=False, max_increase=MAX_RANK_INCREASE, hold_ms=0, clock=None):
        self.table = table
        self.root = root
        self.max_increase = max_increase
        self.hold_ms = hold_ms
        self.clock = clock
        self.version = 0
        self.rank = RANK_ROOT if root else RANK_INFINITE
        self.lowest = RANK_ROOT if root else None
        self.preferred = None
        self.cost = None
        self.poisoned_at = None  # time of the last detach until attached again
        self.hold_until = None
        self.now = None
        self.switches = 0
        self.detaches = 0
        self.drops = 0
        self._members = []
        self._dropped = {}  # address -> frame counter when dropped
        self._events = 0

    def members(self):
        """Return the parent set's addresses, preferred parent first."""
        return tuple(self._members)

    def events(self):
        """Return the event flags accumulated since the last call, and clear them."""
        events = self._events
        self._events = 0
        return events

    def bound(self):
        """Return the highest usable path cost: min(254, L + max_increase)."""
        if self.lowest is None:
            return MAX_COST
        bound = self.lowest + self.max_increase
        return bound if bound < MAX_COST else MAX_COST

    def candidate_cost(self, n, now):
        """Return the path cost through neighbour entry n if it is a candidate, else None."""
        if n.version != self.version or n.rank >= self.rank:
            return None
        if now - n.heard >= SILENCE_PERIODS * self.table.period_ms:
            return None
        if self.poisoned_at is not None and n.heard <= self.poisoned_at:
            return None
        if self._dropped.get(n.address, -1) == n.counter:
            return None
        if not n.eligible():
            return None
        cost = n.rank + link_cost(n.etx())
        return cost if cost <= self.bound() else None

    # --- Entry points --------------------------------------------------------------

    def recompute(self, now):
        """Select the parent set and the advertised rank at now; return the events."""
        if self.root:
            return 0
        events = self._select(now)
        self._events |= events
        return events

    def detach(self, now):
        """Poison: advertise rank 255 at once, forget L and the set; return the events."""
        if self.root:
            return 0
        events = self._detach(now)
        self._events |= events
        return events

    def on_new_version(self, version, now):
        """Adopt DODAG version (4 bits) and select again, L restarted; return the events.

        Only parents of the new version are taken, at any rank; without one
        the node detaches. The root only records the version it issued.
        """
        version &= VERSION_MASK
        if self.root:
            self.version = version
            return 0
        old = self.rank
        self.version = version
        self._set_members([])
        self.cost = None
        self.rank = RANK_INFINITE
        self.lowest = None
        self.poisoned_at = None
        self.hold_until = None
        self._dropped.clear()
        events = self._select(now)
        if self.rank == RANK_INFINITE:
            events = 0
            if old != RANK_INFINITE:
                self.rank = old
                events = self._detach(now)
        elif old != RANK_INFINITE:
            events &= ~(ATTACHED | RANK_CHANGED | RANK_INCREASED)
            if self.rank != old:
                events |= RANK_CHANGED
            if self.rank > old:
                events |= RANK_INCREASED
        self._events |= events
        return events

    def next_parent(self, hop, now=None):
        """Drop hop after exhausted attempts; return the new preferred parent or `HOLD`.

        hop stays out of the candidates until a new frame of it is heard;
        the set is selected again at now (default: ``clock()``, else the
        time of the last `recompute`), which may detach the node.
        """
        if self.root:
            return None
        n = self.table.get(hop)
        if n is not None:
            self._dropped[hop] = n.counter
            self.drops += 1
        if now is None:
            now = self.clock() if self.clock is not None else self.now
        if now is not None:
            self.recompute(now)
        p = self.preferred
        return HOLD if p is None or p == hop else p

    def reroute(self, msg, hop):
        """``LinkEngine.reroute``: `next_parent` for upward messages, None otherwise."""
        if msg.mode == UPWARD or msg.mode == FIRST_HOP:
            return self.next_parent(hop)
        return None

    # --- Selection -------------------------------------------------------------------

    def _select(self, now):
        self.now = now
        table = self.table
        table.expire(now)
        self._prune()
        if self.hold_until is not None:
            if now < self.hold_until:
                return 0
            self.hold_until = None
        best = []  # (path cost, -margin, address, rank), lowest first
        for n in table.entries():
            cost = self.candidate_cost(n, now)
            if cost is not None:
                _insert(best, (cost, -n.margin(), n.address, n.rank))
        if not best:
            if self.rank != RANK_INFINITE:
                return self._detach(now)
            return 0
        old_pref = self.preferred
        old_rank = self.rank
        chosen = best[0]
        if old_pref is not None:
            for key in best:
                if key[2] == old_pref:
                    if key[0] - best[0][0] < SWITCH_THRESHOLD:
                        chosen = key
                    break
        cost = chosen[0]
        events = 0
        if chosen[2] != old_pref:
            events |= PARENT_CHANGED
            if old_pref is not None:
                self.switches += 1
            self.rank = cost
        elif cost - old_rank >= RANK_HYSTERESIS or old_rank - cost >= RANK_HYSTERESIS:
            self.rank = cost
        rank = self.rank
        if rank != old_rank:
            events |= RANK_CHANGED
            if rank > old_rank:
                events |= RANK_INCREASED
        if old_rank == RANK_INFINITE:
            events |= ATTACHED
            self.poisoned_at = None
        if self.lowest is None or rank < self.lowest:
            self.lowest = rank
        self.cost = cost
        members = [chosen[2]]
        for key in best:
            if key is not chosen and key[3] < rank:
                members.append(key[2])
        self._set_members(members)
        return events

    def _detach(self, now):
        was = self.rank
        self._set_members([])
        self.cost = None
        self.rank = RANK_INFINITE
        self.lowest = None
        self.poisoned_at = now
        self.hold_until = now + self.hold_ms if self.hold_ms else None
        self.detaches += 1
        events = DETACHED
        if was != RANK_INFINITE:
            events |= PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED
        return events

    def _set_members(self, members):
        table = self.table
        for a in self._members:
            if a not in members:
                table.set_parent(a, False)
        for a in members:
            table.set_parent(a, True)
        self._members = members
        self.preferred = members[0] if members else None

    def _prune(self):
        # Forget the drops of neighbours gone or heard again (bounded by the table).
        dropped = self._dropped
        if not dropped:
            return
        for a in list(dropped):
            n = self.table.get(a)
            if n is None or n.counter != dropped[a]:
                del dropped[a]
