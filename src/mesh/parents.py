"""Rank, parent set and preferred parent (MESH_DESIGN §9; MP11, MP12).

One `Parents` per board, reading its `mesh.neighbours.NeighbourTable`.
Integer arithmetic; ranks are the 1-byte header values (0 = root, 255 =
infinite: no route or poisoned).

- **Link cost** = round(8 × ETX) (`MIN_HOP` = 8 per hop of ETX 1, RPL
  MinHopRankIncrease), 8 to 32 on an eligible link (on a link judged by
  reachability rather than ETX <= 4, below, the ETX capped at 4,
  `Neighbour.link_etx`); **path cost** through a neighbour = its rank +
  link cost; above 254 the neighbour is not usable.
- **Root sequence** (§9, 8 bits, serial arithmetic: `sequence_newer`,
  sequences less than 128 apart compared): the node holds the sequence of
  its preferred parent (``sequence``). Each neighbour's is the last one
  its frames carried (``Neighbour.sequence``, recorded by the routing
  step from every frame heard, overheard ones included). When the
  selection keeps or takes a preferred parent whose sequence is newer
  than the node's, the node takes it (``sequence_at``: the time it
  advanced) and **restarts L** at its current rank. **L and the sequence
  are kept across a detach**: only a newer sequence restarts L. A
  sequence not advanced for `FORGET_PERIODS` (50) presence periods is
  forgotten with L (at most 111 root increments meanwhile, below the 128
  of serial arithmetic): without a sequence (``sequence`` None: also
  before the first one) every sequence heard is newer, so the next one is
  taken at any rank. A preferred parent whose frame carries the floating
  flag (``Neighbour.floating``) gives no sequence: a floating neighbour's
  sequence grounds no one. Adopting a DODAG version restarts L and takes the
  sequence of the information adopted (`on_new_version`): sequences are
  compared within a version. ``carried``: the sequence the node's frames
  carry, the last one held (kept when forgotten).
- **Grounding** (§9): a node is **ungrounded** (``grounded`` False) while
  detached (from boot to its first grounding, and from a detach on), once
  attached its sequence has not advanced for **G** = 2.5 + h presence
  periods, h = min(8, ceil(rank / 8)) at its advertised rank
  (`ground_ms`, `float_at`), or at once when its preferred parent's frame
  carries the floating flag. It is grounded again only when it takes a
  newer sequence from a preferred parent that is not floating (the
  selection above): a rejoin through a parent at the node's sequence
  leaves it ungrounded until that parent's next frame with a newer one.
  A node never grounded since boot is also grounded by a preferred parent
  that is not floating and holds the node's sequence (the one of the
  DODAG information it adopted since boot, less than G ago: no older
  sequence of its own to mistake for a fresh one).
  ``ungrounded_at``: the moment it lost grounding (the detach, the moment
  G ran out, the selection that read the floating parent), kept until it
  is grounded again (a detach while ungrounded does not move it); None
  while grounded and before the first grounding since boot. The routing
  step sets the floating flag of the node's frames while it is ungrounded
  and holds its upward class 1/2 messages (`mesh.reliable`, §12). The root
  is always grounded.
- **Candidates**: neighbours of the node's DODAG version that are
  **feasible** (Babel, RFC 8966): a sequence **newer** than the node's, at
  any rank (path cost within 254: the newer sequence restarts L), or the
  same sequence and a rank strictly lower than **L**, the lowest rank the
  node has advertised with that sequence (never above its advertised
  rank; kept by a node without route); never an older sequence, nor an
  unknown one while the node holds one; not floating (its last frame
  without the floating flag, §9). The preferred parent is one at any
  rank (**rank following**, RPL: the node follows its preferred parent's
  rank increase within the bound below; its sequence is never older than
  the node's), heard within the silence limit
  (`mesh.neighbours.silence_ms`: 2.5 presence periods, strict), through an
  eligible link (`Neighbour.eligible`: margin, and ETX <= 4, or instead
  reachability for the link to the root and for the preferred parent's
  while it is the **last candidate**, no other neighbour being a
  candidate, drops included: the other candidates do not depend on the
  preferred parent, so they are found first with the ordinary rule; once
  the node has switched away from it, the old parent needs ETX <= 4
  again; a relay taken as the preferred parent, and the root taken again,
  have their reachability restarted, `NeighbourTable.restart_reach`, so
  that a link taken again by ETX <= 4 after it was left unreachable, a
  link back after a cut included, and the root taken again once its
  failures aged out, get their probes before they are judged), whose path
  cost is at
  most the
  **bound** min(254, L + max_increase), max_increase 56 by default
  (MaxRankIncrease, carried by the DODAG information, §9), 254 for a
  neighbour of newer sequence. Testing against L rather than the
  advertised rank keeps out a neighbour between L and a rank increase not
  yet heard by the node's descendants: it may be one of them, and taking
  it would close a cycle (DUAL's feasibility condition). A descendant
  holds a sequence no newer than the node's (taken from it) and, with the
  same one, a rank above every rank the node advertised with it: it is
  never feasible, whatever was lost, across a detach too (TLA+ `DODAG`,
  ``Seq = "babel"``). A neighbour dropped after failed attempts
  (`next_parent`) is no candidate until a new frame of it is heard; after
  a detach, only neighbours heard after the solicitation that ends its
  hold are.
- **Kept after exhausted attempts** (`next_parent`, §12): the root, and
  the preferred parent when it is the node's last candidate, are not
  dropped after failed attempts while they are candidates apart from
  drops (heard within the silence limit, eligible link, of the node's
  version, within the bound): the message gets fresh attempts towards
  them. Under the test against L the root is a root child's only
  candidate, and a relay often a hop-2 node's: a drop would be a detach,
  its subtree following, after mere collisions at the parent (hidden
  terminals). Only the silence limit or the link's ineligibility (margin,
  or unreachable: `UNREACH_RUN` consecutive failed samples and a silence
  limit without acknowledgement of the parent, `mesh.neighbours`) removes
  them (TLA+ `DODAG`, Exhaust = "keepboth", Elig = "reachall", which
  judges every link by reachability; the code limits it to the last
  candidate, MESH_DESIGN §9, MP12). The keep is decided at each
  exhaustion, never remembered: a relay parent with another candidate
  is dropped, as is any other hop.
- **Behind a newer version** (``behind``, set by the Trickle step when a
  frame of a newer version is heard, its DODAG information included when
  the node does not adopt it; cleared by `on_new_version` and the
  detach): the preferred parent stays a candidate once it has moved to a
  newer version (finite rank, under the same silence, drop and link
  rules), at the node's own rank as path cost (ranks of different versions
  are not compared), until the node adopts the version (§9 DODAG version).
  The other candidates keep the node's version; the preferred parent of
  another version is never a secondary member.
- **Adoption condition** (`adoptable`, §9): an attached node adopts a
  newer version only if a neighbour would be a candidate in it, as the
  selection of `on_new_version` judges them; otherwise it would detach
  (the sender's link not eligible, the sender floating, its own parent not
  yet heard in that version), and it stays behind instead; its preferred
  parent's information is adopted all the same (`mesh.dodag`).
- **Parent set**: the `SIZE` (3) candidates of lowest path cost, ordered by
  path cost, then larger margin (§9 tie-break), then lower address (so that
  the result does not depend on the table's slot order). **Preferred
  parent**: kept while it is in the set and no candidate's path cost is
  lower by `SWITCH_THRESHOLD` (12) or more; otherwise the first one. The set
  is listed preferred parent first, then by that order.
- **Advertised rank** (``rank``) = path cost through the preferred parent,
  updated when it differs by `RANK_HYSTERESIS` (4) or more, or when the
  preferred parent changes (a followed increase is at least one link cost,
  8); L follows its decreases (and restarts with a newer sequence, above).
  Members other than the preferred parent whose rank is not strictly lower
  than L, so updated, or whose sequence is older than the node's, so
  updated, leave the set (rank rule), so that the set holds no member the
  next selection would refuse.
- **Detach**: when no candidate remains (bound exceeded included), the node
  advertises 255 at once, clears the set (L and the sequence kept), and
  selects nothing for ``hold_ms`` (2 Imin, set by the Trickle step; 0 by
  default), then until its solicitation goes on air (`solicited`,
  ``soliciting`` meanwhile); it then rejoins through feasible neighbours
  heard after that solicitation (without hold: after the poison).
  **Answer window**: every
  solicitation of a detached node (the one ending the hold, the periodic
  ones without route, the one at boot) opens a window of ``window_ms``
  (Imin and a margin, set by the Trickle step; 0 by default) during which
  it selects nothing; at its close it takes the lowest path cost among the
  candidates heard meanwhile (no switch threshold: it has no preferred
  parent), and if none was heard, the first candidate heard afterwards.
  A child that lost the poison hears the solicitation (rank 255) and
  detaches in turn; one that lost both is no candidate either, by
  feasibility (above). A loop formed otherwise is not broken by the rank
  rule, each member following the other's increase: it ends by a switch
  or when a member's path cost passes the bound (§9).
- **Parent flags**: the set's members are flagged in the neighbour table
  (`NeighbourTable.set_parent`), so that eviction spares them.

Entry points for the later steps: `recompute` (after frames are heard, at
each presence period, on a timer, at the close of the answer window),
`next_parent` / `reroute` (the link engine's ``reroute``: class 1 attempts
exhausted on a parent, §12), `detach`, `solicited` and `on_new_version`
(Trickle and DODAG version, §9). Each returns event flags
(`PARENT_CHANGED`, `RANK_CHANGED`, `RANK_INCREASED`, `DETACHED`,
`ATTACHED`, `GROUNDED`, `UNGROUNDED`), also accumulated until read by
`events`: the caller copies ``rank``, ``version`` and the grounding into
the frame header (``LinkEngine.rank``, ``dodag_version``, ``floating``),
resets Trickle, sends its presence at once after a parent change and calls
``Reliable.route_changed`` and ``Reliable.grounding``. G gets no timer
here: the caller runs `recompute` at `float_at`.

Readings where §9 is silent: a preferred parent outside the 3 lowest
path costs has left the set even when no candidate beats it by 12; the
bound L + 56 makes a candidate unusable (as a path cost above 254 does),
the node detaching when none remains; a dropped parent stays excluded
until heard again; the root keeps rank 0 and selects nothing; a
solicitation sent during the hold's 2 Imin (another version heard) does
not end it, nor opens a window; a new version does not end the hold or
the window nor lift its rejoin rule (its parents are taken at any rank,
among the neighbours heard after the solicitation, at the window's close);
a solicitation sent while the window is open (another version heard) does
not extend it; the sequence is taken at the selection that reads it (the
step of the parent's frame), the preferred parent kept or new; a neighbour
whose sequence is unknown (no frame carrying one heard since its entry
was made or its version changed) is not feasible while the node holds a
sequence, and a node without one compares an unknown sequence as the same
(the rank rule alone); a sequence 128 apart from the node's is neither
newer nor the same (not feasible); forgetting is checked at each
selection; the members other than the preferred parent keep the rank
rule (below L), a newer sequence above L not making one; G is read at the
advertised rank of the moment (a rank change moves it); a node grounded
and ungrounded within one selection (G ran out just before the parent's
newer sequence was read) reports both events and ends grounded; the
floating preferred parent stays followed (§9), no switch is made to leave
it; adopting a new version keeps the grounding as it was (the sequence
taken there restarts G, an ungrounded node stays so until an advance) and
the preferred parent's following: floating, it is still taken in the new
version (a node adopting from its floating parent would otherwise detach
with its subtree), as `adoptable` counts it; in the new version the
preferred parent is no longer the last candidate judged by reachability
(the selection starts without one): `adoptable` judges its link by
ETX <= 4, as that selection does (its own information is adopted all
the same: the node selects afresh rather than stay behind it);
at boot, a parent at the node's sequence grounds it (above: otherwise a
node would wait for its parent's next advance before being a candidate
itself, and the DODAG would form one presence period per hop).
"""

from mesh.frame import FIRST_HOP, RANK_INFINITE, RANK_ROOT, ROOT, UPWARD
from mesh.neighbours import ETX_ONE, silence_ms
from mesh.reliable import HOLD

MIN_HOP = 8  # rank increase of one hop of ETX 1 (RPL MinHopRankIncrease)
MAX_COST = 254  # highest usable path cost (255 is infinite)
MAX_RANK_INCREASE = 56  # 7 perfect hops (Contiki-NG's MaxRankIncrease)
SIZE = 3  # parent set members
SWITCH_THRESHOLD = 12  # path cost gain to switch preferred parent (1.5 hops)
RANK_HYSTERESIS = 4  # advertised rank change to advertise it (half a hop)
VERSION_MASK = 0x0F  # 4-bit DODAG version
_HALF = 8  # serial arithmetic on 4 bits: versions less than 8 apart compare
SEQUENCE_MASK = 0xFF  # 8-bit root sequence (§9)
_SEQ_HALF = 128  # serial arithmetic on 8 bits: sequences less than 128 apart compare
FORGET_PERIODS = 50  # presence periods without advance: sequence and L forgotten
GROUND_HOPS = 8  # G = 2.5 + min(GROUND_HOPS, ceil(rank / 8)) presence periods (§9)

# Event flags.
PARENT_CHANGED = 1  # preferred parent changed (presence report at once)
RANK_CHANGED = 2  # advertised rank changed
RANK_INCREASED = 4  # advertised rank increased, poisoning included (Trickle reset)
DETACHED = 8  # poisoned: rank 255 (Trickle reset)
ATTACHED = 16  # first parent after boot or detach (Trickle reset)
GROUNDED = 32  # grounded again: a newer sequence from a parent not floating
UNGROUNDED = 64  # grounding lost: detach, G without advance, floating parent


def link_cost(etx):
    """Return round(8 × ETX) of an ETX in units of 1/ETX_ONE, at least 8."""
    cost = (etx * MIN_HOP + ETX_ONE // 2) // ETX_ONE
    return cost if cost > MIN_HOP else MIN_HOP


def sequence_newer(a, b):
    """Return True if root sequence a is newer than b (8-bit serial arithmetic, §9).

    None is no sequence: every sequence is newer than None, None is newer
    than none; sequences 128 apart are not comparable (False both ways).
    """
    if a is None:
        return False
    if b is None:
        return True
    return 0 < (a - b) & SEQUENCE_MASK < _SEQ_HALF


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
    max_increase: MaxRankIncrease; hold_ms: selection pause after a detach
    (then until `solicited`); window_ms: answer window after a solicitation
    of a detached node;
    clock: function() -> now (ms) used by `reroute`, which receives no time
    (None: the time of the last `recompute`).

    Read: ``rank`` (advertised), ``version``, ``lowest`` (L, None after a
    new version or forgotten), ``sequence`` (root sequence held, None
    before the first or forgotten), ``sequence_at`` (time it last
    advanced), ``carried`` (the one the frames carry), ``grounded``,
    ``ungrounded_at``, ``preferred`` (address or None), ``cost``
    (path cost through it at the last selection), `members`,
    ``hold_until``, ``soliciting``, ``poisoned_at``, ``window_until``;
    counters ``switches``, ``detaches``, ``drops``, ``floats`` (losses
    of grounding). Read and set:
    ``behind`` (a newer version heard, not adopted yet).
    """

    def __init__(
        self, table, root=False, max_increase=MAX_RANK_INCREASE, hold_ms=0, window_ms=0, clock=None
    ):
        self.table = table
        self.root = root
        self.max_increase = max_increase
        self.hold_ms = hold_ms
        self.window_ms = window_ms
        self.clock = clock
        self.version = 0
        self.rank = RANK_ROOT if root else RANK_INFINITE
        self.lowest = RANK_ROOT if root else None
        self.sequence = None  # root sequence held (of the preferred parent)
        self.sequence_at = None  # time it last advanced
        self.carried = None  # the one the frames carry: the last held
        self.grounded = root  # §9: a node is ungrounded from boot to its first grounding
        self.ungrounded_at = None  # moment grounding was lost (None: grounded, or since boot)
        self.preferred = None
        self.cost = None
        self.poisoned_at = None  # last detach, then the solicitation ending its hold
        self.hold_until = None  # end of the detach hold's 2 Imin
        self.soliciting = False  # hold over, waiting for the solicitation to go on air
        self.window_until = None  # close of the answer window of a detached node
        self.behind = False  # a newer version heard, not adopted yet
        self.now = None
        self.switches = 0
        self.detaches = 0
        self.drops = 0
        self.floats = 0
        self._members = []
        self._dropped = {}  # address -> frame counter when dropped
        self._kept = None  # preferred parent before a version adopted: followed if floating
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

    def candidate_cost(self, n, now, drops=True, new=None):
        """Return the path cost through neighbour entry n if it is a candidate, else None.

        The preferred parent is one at any rank (its increase is followed,
        within the bound), the others when feasible: a newer sequence at any
        rank (path cost within 254), or the same and a rank below L (any
        finite rank once L is restarted), within the bound, and not
        floating. While the node is behind, the preferred parent moved to a
        newer version is one at the node's own rank. drops: False to ignore
        a drop of n after failed attempts (`next_parent`; the other
        neighbours' drops still count). The preferred parent's link is
        judged by reachability alone, without ETX <= 4, when it is the last
        candidate (`last`). new: (version, sequence) to judge n as the
        selection of `on_new_version` adopting them would (`adoptable`): no
        preferred parent, the current one followed if floating, L restarted,
        drops cleared; None for the current state.
        """
        address = n.address
        if new is None:
            version = self.version
            seq = self.sequence
            lowest = self.lowest
            preferred = self.preferred
            kept = self._kept
        else:
            version, seq = new
            lowest = preferred = None
            kept = self.preferred
            drops = False
        if n.floating and address != preferred and address != kept:
            return None  # a floating neighbour is no candidate, the preferred parent followed
        across = n.version != version
        newer = False
        if across:
            if not (self.behind and address == preferred and n.rank != RANK_INFINITE):
                return None
            if not 0 < (n.version - version) & VERSION_MASK < _HALF:
                return None  # not newer
        else:
            newer = sequence_newer(n.sequence, seq)
            if address != preferred and not newer:
                # Feasibility: the same sequence and a rank below L.
                if n.sequence != seq:
                    return None  # older, unknown or not comparable
                if lowest is not None and n.rank >= lowest:
                    return None
        silence = silence_ms(self.table.period_ms)
        if now - n.heard >= silence:
            return None
        if self.poisoned_at is not None and n.heard <= self.poisoned_at:
            return None
        if drops and self._dropped.get(address, -1) == n.counter:
            return None
        eligible = n.eligible(now, silence)
        reach = False
        if address == preferred and address != ROOT:
            # The last candidate's link is judged by reachability instead of
            # ETX <= 4 (the others looked up only when the two disagree).
            if n.eligible(now, silence, True) != eligible and self.last(address, now):
                eligible = not eligible
                reach = True
        if not eligible:
            return None
        if across:
            return self.rank
        cost = n.rank + link_cost(n.link_etx(reach))
        return cost if cost <= (MAX_COST if newer or lowest is None else self.bound()) else None

    def ground_ms(self):
        """Return G (ms): 2.5 + min(8, ceil(rank / 8)) presence periods at the advertised rank."""
        h = (self.rank + MIN_HOP - 1) // MIN_HOP
        if h > GROUND_HOPS:
            h = GROUND_HOPS
        return (5 + 2 * h) * self.table.period_ms // 2

    def float_at(self):
        """Return the time a grounded attached node becomes ungrounded without advance, else None.

        G after the last advance of its sequence (`ground_ms`); None on the
        root, on a node ungrounded or detached.
        """
        if self.root or not self.grounded or self.rank == RANK_INFINITE:
            return None
        at = self.sequence_at
        if at is None:
            return self.now  # no sequence held: nothing comes down (not reached: G < 50)
        return at + self.ground_ms()

    def last(self, address, now):
        """Return True if no neighbour but address is a candidate at now (drops included)."""
        for m in self.table.entries():
            if m.address != address and self.candidate_cost(m, now) is not None:
                return False
        return True

    # --- Entry points --------------------------------------------------------------

    def recompute(self, now):
        """Select the parent set and the advertised rank at now; return the events."""
        if self.root:
            return 0
        events = 0
        due = self.float_at()
        if due is not None and now >= due:
            events = self._unground(due)  # G without advance
        events |= self._select(now)
        self._events |= events
        return events

    def detach(self, now):
        """Poison: advertise rank 255 at once, forget the set (L kept); return the events."""
        if self.root:
            return 0
        events = self._detach(now)
        self._events |= events
        return events

    def solicited(self, now):
        """A solicitation went on air at now: it ends the detach hold once its time is over.

        Only neighbours heard after now are candidates from then on (§9:
        rejoin through neighbours heard after the solicitation). A
        solicitation of a detached node outside the hold opens the answer
        window, unless one is open. Returns True if it ended the hold.
        """
        if self.rank != RANK_INFINITE:
            return False  # attached (behind a newer version), or the root
        if self.hold_until is not None and now < self.hold_until:
            return False
        if self.window_until is None or now >= self.window_until:
            self.window_until = now + self.window_ms
        if not self.soliciting and self.hold_until is None:
            return False  # at boot, or periodic without route
        self.soliciting = False
        self.hold_until = None
        self.poisoned_at = now
        return True

    def adoptable(self, version, sequence, now):
        """Return True if `on_new_version` (version, sequence) at now would find a candidate.

        Read only: a neighbour of that version, feasible at that sequence at
        any rank (path cost within 254), heard within the silence limit,
        through an eligible link, not floating unless it is the preferred
        parent (`candidate_cost` with new). An attached node adopts a
        version only then (`mesh.dodag`, §9): it would otherwise detach.
        """
        new = (version & VERSION_MASK, sequence)
        for n in self.table.entries():
            if self.candidate_cost(n, now, False, new) is not None:
                return True
        return False

    def on_new_version(self, version, now, sequence=None):
        """Adopt DODAG version (4 bits) and select again, L restarted; return the events.

        sequence: the root sequence of the DODAG information adopted, taken
        (None: no sequence held). Only parents of the new version are
        taken, at any rank at that sequence (or a newer one); without one
        the node detaches (an attached node adopts only when `adoptable`
        says one exists: `mesh.dodag`). A detach hold goes on, and after it
        only neighbours heard after the solicitation are taken. The root
        only records the version it issued.
        """
        version &= VERSION_MASK
        if self.root:
            self.version = version
            return 0
        old = self.rank
        self._kept = self.preferred
        self.version = version
        self._set_members([])
        self.cost = None
        self.rank = RANK_INFINITE
        self.lowest = None
        self.sequence = sequence
        self.sequence_at = None if sequence is None else now
        if sequence is not None:
            self.carried = sequence
        self.behind = False
        self._dropped.clear()
        events = self._select(now)
        self._kept = None
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
        """Drop hop after exhausted attempts; return the new preferred parent, `HOLD` or hop.

        The root, and the preferred parent when it is the last candidate,
        are kept (returned, unchanged set: fresh attempts) while they are
        candidates apart from drops (module docstring); decided now, never
        remembered. Any other hop is dropped: it stays out of the
        candidates until a new frame of it is heard, and the set is selected
        again at now (default: ``clock()``, else the time of the last
        `recompute`), which may detach the node.
        """
        if self.root:
            return None
        if now is None:
            now = self.clock() if self.clock is not None else self.now
        n = self.table.get(hop)
        if n is not None and now is not None and (hop == ROOT or hop == self.preferred):
            if self.candidate_cost(n, now, False) is not None:
                if hop == ROOT or self.last(hop, now):
                    return hop
        if n is not None:
            self._dropped[hop] = n.counter
            self.drops += 1
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
        events = 0
        at = self.sequence_at
        if at is not None and now - at >= FORGET_PERIODS * table.period_ms:
            self.sequence = None  # forgotten with L: the next one taken at any rank
            self.sequence_at = None
            self.lowest = None
        if self.hold_until is not None:
            if now < self.hold_until:
                return events
            self.hold_until = None
            self.soliciting = True
        if self.soliciting:
            return events  # until `solicited`
        if self.window_until is not None:
            if now < self.window_until:
                return events  # answers still to come
            self.window_until = None
        best = []  # (path cost, -margin, address, rank, sequence, floating), lowest first
        for n in table.entries():
            cost = self.candidate_cost(n, now)
            if cost is not None:
                # A preferred parent of another version: never a secondary
                # member, its sequence never taken.
                rank = n.rank if n.version == self.version else RANK_INFINITE
                _insert(best, (cost, -n.margin(), n.address, rank, n.sequence, n.floating))
        if not best:
            if self.rank != RANK_INFINITE:
                return events | self._detach(now)
            return events
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
        if chosen[2] != old_pref:
            events |= PARENT_CHANGED
            if old_pref is not None:
                self.switches += 1
            self.rank = cost
            # A relay taken by ETX <= 4 may be the last candidate at the next
            # selection, judged by reachability, and the root always is: the
            # probes start now.
            table.restart_reach(chosen[2], now)
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
        lowest = self.lowest
        seq = chosen[4]
        floating = chosen[5]
        if chosen[3] != RANK_INFINITE and not floating and sequence_newer(seq, self.sequence):
            # The preferred parent's newer sequence: taken, L restarted, the
            # node grounded (§9).
            self.sequence = self.carried = seq
            self.sequence_at = now
            lowest = self.lowest = rank
            if not self.grounded:
                events |= self._ground()
        else:
            if lowest is None or rank < lowest:
                lowest = self.lowest = rank
            if not self.grounded and self.ungrounded_at is None and not floating:
                # Never grounded since boot: a parent that is not floating
                # and holds the node's sequence (adopted from a DODAG
                # information since boot, less than G ago) grounds it.
                at = self.sequence_at
                if chosen[3] != RANK_INFINITE and seq is not None and seq == self.sequence:
                    if at is not None and now - at < self.ground_ms():
                        events |= self._ground()
        if floating and self.grounded:
            events |= self._unground(now)  # the preferred parent floats: at once
        self.cost = cost
        seq = self.sequence
        members = [chosen[2]]
        for key in best:
            if key is not chosen and key[3] < lowest:
                if key[4] == seq or sequence_newer(key[4], seq):
                    members.append(key[2])
        self._set_members(members)
        return events

    def _detach(self, now):
        was = self.rank
        self._set_members([])
        self.cost = None
        self.rank = RANK_INFINITE
        self.poisoned_at = now  # L and the sequence kept: only a newer one restarts L
        self.hold_until = now + self.hold_ms if self.hold_ms else None
        self.soliciting = False
        self.window_until = None
        self.behind = False
        self.detaches += 1
        events = DETACHED
        if was != RANK_INFINITE:
            events |= PARENT_CHANGED | RANK_CHANGED | RANK_INCREASED
        if self.grounded:
            events |= self._unground(now)
        return events

    def _unground(self, at):
        self.grounded = False
        self.ungrounded_at = at
        self.floats += 1
        return UNGROUNDED

    def _ground(self):
        self.grounded = True
        self.ungrounded_at = None
        return GROUNDED

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
