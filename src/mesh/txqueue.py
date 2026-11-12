"""Transmit queue: priority, fairness, aggregation (MESH_DESIGN §6, §7, §12).

One queue per board holds the messages waiting for the channel, own and
forwarded, each with its next hop (link destination, `frame.BROADCAST` for
every neighbour).

**Order** (§7): priority first (from the port, `mesh.ports`), strictly;
within a priority, round robin by origin, one message per origin and per
round, the node's own messages counting as one origin (`LOCAL`) whatever
their port; age within an origin. Origins take turns in a fixed cyclic order
(their index in the origin table), starting after the origin served last at
that priority, so that a frame or an allowance too small for every queued
message shares its capacity equally between origins, whatever their volume.

**Admission** (§7, §12 queues and resources): `offer` returns the outcome
for the message's class and origin. The queue holds ``size`` messages
(64 by default) plus `RESERVE` (3) entries kept for the node's own control
messages: its DODAG information (port 9: one advertisement, one
solicitation, `mesh.dodag` queues at most one of each) and its own
presence (port 1, mode `FIRST_HOP`: one, the reporter replaces it by
newer), which neither fill the queue nor are refused by the quota. A node
whose queue is full of held class 1 messages, or of its own errors, still
solicits and rejoins (otherwise it stays detached until their holding
expires, and its subtree's holdings expire meanwhile); a relay whose queue
is full of forwarded traffic still queues its presence, the probe of the
link to its parent (§9: otherwise nothing tests that link, and the root
receives no sample of it, for as long as the queue stays full). The
reserve guarantees admission, not a turn: under due priority-1 traffic the
presence leaves as a passenger of a frame to the parent. The node's own
acknowledgements (port 3) are not subject to the quota either (but to a
full queue: their number is not bounded), nor is a message offered with
``exempt``: an error taking the entry of the message it reports
(`mesh.reliable`), which occupies no more room than that message did.
Neither control message is any origin's traffic.

- `REPLACED`: class 0 replace by newer (per port, chosen by the caller): the
  newer message takes the place, and the age, of a queued class 0 message of
  the same origin and port (the newest one). With ``keep`` (chosen by the
  caller: the presence reports, each carrying its own flags and
  application extension), a message held for a bundle is not replaced:
  the newer one is queued beside it (`ACCEPTED`, or a refusal or eviction
  as any message). Held: deferred (every queued message of that origin
  and port then takes the same deferral, the earlier time, so that they
  leave together, oldest first), or deferred once and not yet on air (its
  deferral ended, the frame that carries it waits for the channel) while
  fewer than `KEEP` (2) of that origin and port are queued (beyond, under
  congestion, the newest is replaced as before);
- `OVER_QUOTA` refusal: the queue holds more than half its size and the
  origin already holds `quota` messages;
- `FULL` refusal: no free entry. A forwarded class 0 message then drops the
  forwarded class 0 message of lowest priority, oldest first, provided it is
  not more urgent than the newcomer (`EVICTED`, ``victim`` set; otherwise
  the newcomer is the one dropped).

A refusal is `REFUSED` for a local submission (failure reported to the
application, MP5), `DROPPED` for a forwarded class 0 message, `UNACKED` for a
forwarded class 1 or 2 message (not acknowledged: its sender keeps it and
retries, MP5 back-pressure). Refusals are counted per class; ``overflow`` is
set by any refusal or eviction, for the presence flag *queue overflow* (the
reporter clears it). The caller counts refusals per origin (§13).

**Deferral** (§7 bundling of forwarded reports, §16): a message offered
with ``until`` (a time, ms) is *deferred*: until then it does not start a
frame of its own but joins, as a passenger, any frame packed for its next
hop (after the messages that are due, which it never displaces). A relay
defers the reports of its children until its own next presence, so that
they leave in one bundle per period (`mesh.routing`). Only while the queue
is *lightly loaded*: at most half its size in messages, deferred, due and
waiting ones alike (the reserve's excluded; the level beyond which the
quota applies: a message outside the reserve is refused or evicted only
beyond it). A message
that would take the queue beyond is not deferred, and every deferred entry
is then due at once: under load the frames to the next hop are full anyway
and carry the reports as they come (opportunistic aggregation), and
deferred ones would hold entries the load needs. The deferral also ends at
``until``; and when a class 0 replace by newer puts a message not deferred
in the place of a deferred one (two deferred ones keep the earlier time),
or queues one beside it (``keep``: they all become due).

**Packing** (§7 opportunistic aggregation, §6 mixed frames): `pack` takes
the head of the queue (the first message, in queue order, that is due and
fits a frame on its own), then every other due message bound to the same
next hop, in queue order, then the deferred ones bound to it, in queue order,
while the frame stays within the frame limit and within the allowance of
the least urgent priority it carries (`DutyCycle.admit`). Messages that
can share a header (`frame.same_group`) are arranged consecutively, so that
`frame.encode` bundles them; the frame size is tracked exactly as `encode`
computes it. Round robin resumes after the origins of the due messages
served (deferred passengers do not move it).

**Commit and retries**: `pack` changes nothing; it returns a `Selection`
(one outstanding at a time: the board transmits one frame at a time). The
caller encodes and seals it, then calls `commit` when the frame goes on air;
dropping the selection instead aborts (nothing to undo). `commit` removes
the class 0 messages and keeps the class 1 and 2 messages as *waiting* for an
acknowledgement: they keep their entry (quota and size) and their age, are
not packed again, and count one more attempt. The reliability layer then
calls `remove` (acknowledged, or failed) or `retry` (timeout: packed again,
possibly towards another next hop with fresh attempts); `hold` makes a
queued message wait without an attempt (held during repair, §12). Entries
are designated by their slot (``slot`` after `offer`) and checked against
their message, so that a stale handle never touches a reused slot.
`redirect` gives the messages not waiting a new next hop (routing, after a
change of preferred parent: queued upward messages follow it, deferred
ones keeping their deferral); `select`
lists them, waiting ones included on demand (routing, after a detach: they
are held or dropped; class 1, the copies a node holds); `promote` raises an
entry's priority.

Bounded memory: every array is allocated at construction; queueing and
packing allocate nothing per message beyond the message itself (one list of
messages per packed frame).
"""

from mesh import frame
from mesh.ports import ACK, DODAG_INFO, PRESENCE, PRIORITY_REPAIR
from mesh.ports import priority as port_priority

SIZE = 64  # default transmit queue size (§12)
RESERVE = 3  # entries beyond it for the node's own DODAG information (port 9) and presence
QUOTA = 16  # messages per origin once the queue is more than half full (§7)
MAX_SIZE = 254

LOCAL = -1  # origin of the node's own messages

# Outcomes of `offer`.
ACCEPTED = 0  # queued
REPLACED = 1  # replaced an older class 0 message of the same origin and port
EVICTED = 2  # queued after dropping a forwarded class 0 message (``victim``)
REFUSED = 3  # local submission refused: failure to the application
DROPPED = 4  # forwarded class 0 message dropped
UNACKED = 5  # forwarded class 1/2 message not accepted: not acknowledged

# Reasons of a refusal or eviction (``reason``).
FULL = 1
OVER_QUOTA = 2

_NIL = 255
_END = 0x3FFFFFFF  # sorts after every key
_WAIT = 0x01
_LOCAL = 0x02
_CONTROL = 0x04  # the node's own DODAG information or presence: in the reserve
_HELD = 0x08  # deferred once (`offer` until): kept beside a newer one (keep)
KEEP = 2  # with keep: messages of one origin and port kept once due (deferred: all)


def accepted(outcome):
    """Return True if an outcome of `offer` means the message was queued."""
    return outcome <= EVICTED


def _header_bytes(count, last):
    # Control and port, then length and count (bundle), or length unless last.
    if count > 1:
        return 4
    return 2 if last else 3


class Selection:
    """Messages chosen by `TxQueue.pack` for one frame.

    dest: link destination (next hop or `frame.BROADCAST`); messages: the
    list to pass to `frame.encode`, compatible messages consecutive; size:
    frame length in bytes, header and tag included; toa_ms: its time on air
    (the caller charges it, `DutyCycle.start`); priority: least urgent
    priority carried (the allowance checked); count: number of messages.
    """

    def __init__(self, size):
        self.dest = None
        self.messages = None
        self.size = 0
        self.toa_ms = 0
        self.priority = 0
        self.count = 0
        self.slots = bytearray(size)  # slot of each message, in frame order
        self._gid = bytearray(size)  # group of each message, in selection order
        self._sel = bytearray(size)  # slots in selection order
        self._rep = bytearray(size)  # slot of the first message of each group
        self._gcount = bytearray(size)  # messages per group
        self._order = [_END] * size
        self._last = bytearray(4)  # origin index served last, per priority


class TxQueue:
    """Transmit queue of one board.

    size: messages (64 by default); reserve: entries beyond them for the
    node's own DODAG information and presence (module docstring); ``entries``: size +
    reserve, at most 254, the slots (0 to entries - 1); quota: messages an
    origin may hold once the queue holds more than size // 2.
    """

    def __init__(self, size=SIZE, quota=QUOTA, reserve=RESERVE):
        entries = size + reserve
        if size < 2 or reserve < 0 or entries > MAX_SIZE or quota < 1:
            raise ValueError("bad queue size")
        self.size = size
        self.entries = entries
        self.quota = quota
        self._count = 0
        self.control = 0  # entries of the reserve in use
        # Entries (slots).
        self._msg = [None] * entries
        self._hop = [0] * entries
        self._prio = bytearray(entries)
        self._flags = bytearray(entries)
        self._attempts = bytearray(entries)
        self._oidx = bytearray(entries)
        self._due = [0] * entries  # deferred until (ms), 0: due
        self.deferred = 0  # entries deferred
        # Age order: doubly linked list of the entries, oldest first; free
        # slots are chained through _next.
        self._prev = bytearray(entries)
        self._next = bytearray(range(1, entries + 1))
        self._next[entries - 1] = _NIL
        self._free = 0
        self._head = _NIL
        self._tail = _NIL
        # Origins with queued messages: key (address or LOCAL), messages held.
        self._okey = [None] * entries
        self._ocount = bytearray(entries)
        # Round robin: origin index served last, per priority (index 1–3).
        self._last = bytearray((entries - 1,) * 4)
        # Packing scratch.
        self._keys = [_END] * entries
        self._hkeys = [_END] * entries  # deferred entries
        self._rounds = bytearray(3 * entries)
        self._hrounds = bytearray(3 * entries)
        self._zero = bytes(3 * entries)
        self._sel = Selection(entries)
        # Reporting.
        self.refusals = [0, 0, 0]  # per class
        self.evictions = 0
        self.overflow = False
        self.reason = 0
        self.victim = None
        self.victim_origin = None
        self.slot = None

    def __len__(self):
        return self._count

    def messages(self):
        """Return the number of queued messages, the reserve's excluded (at most size)."""
        return self._count - self.control

    def holding(self, origin):
        """Return the number of messages queued for origin (address or `LOCAL`)."""
        o = self._origin(origin)
        return 0 if o == _NIL else self._ocount[o]

    # --- Admission ---------------------------------------------------------

    def offer(
        self,
        msg,
        hop,
        local=False,
        trickle_reset=False,
        replace=False,
        exempt=False,
        until=None,
        keep=False,
    ):
        """Queue msg for next hop hop; return the outcome (`ACCEPTED`...).

        local: the node's own message (one origin, `LOCAL`), else forwarded
        (origin: msg.origin, the root for messages without origin); a local
        port 9 message, or its own first-hop presence (port 1, mode
        `FIRST_HOP`), goes into the reserve, a local port 3 one is not
        subject to the quota (module docstring); trickle_reset: port 9
        priority (`mesh.ports.priority`); replace: class 0 replace by newer
        for this port; exempt: admitted beyond the origin's quota (an error
        taking the entry of the message it reports); until: deferred until
        this time (ms) if the queue is lightly loaded (module docstring;
        None: due at once); keep: with replace, a message held for a bundle
        is not replaced, the newer one is queued beside it (module
        docstring). On a refusal or eviction, ``reason``
        is `FULL` or `OVER_QUOTA`; on `EVICTED`, ``victim`` and
        ``victim_origin`` give the message dropped. ``slot`` is the entry of
        the message once queued (or of the message it replaced), None after a
        refusal. Raises ValueError for a malformed message, a reserved port or
        a message no frame can carry (a bug of the caller: submissions are
        checked before, MP6).
        """
        p = port_priority(msg.port, trickle_reset)
        frame.check_message(msg)
        if frame.OVERHEAD + 2 + frame.element_size(msg) > frame.MAX_FRAME:
            raise ValueError("message too long")
        if local:
            key = LOCAL
        else:
            key = msg.origin if msg.origin is not None else frame.ROOT
        self.reason = 0
        self.victim = None
        self.victim_origin = None
        self.slot = None
        held = _NIL
        if replace and msg.cls == 0:
            i = self._older(key, msg.port)
            if i != _NIL and keep and self._held(i):
                held = i
            elif i != _NIL:
                self._msg[i] = msg
                self._hop[i] = hop
                self._prio[i] = p
                self.slot = i
                due = self._due[i]
                if due and not until:
                    self._release(i)
                elif due and until < due:
                    self._due[i] = until
                return REPLACED
        port = msg.port
        control = local and (
            port == DODAG_INFO or (port == PRESENCE and msg.mode == frame.FIRST_HOP)
        )
        if control:
            if self.control == self.entries - self.size:
                return self._refuse(msg, local, FULL)  # more than the reserve
            self._insert(msg, hop, p, key, local, _CONTROL)
            return ACCEPTED
        o = self._origin(key)
        count = self._count - self.control
        if local and msg.port == ACK:
            exempt = True  # the node's acknowledgements: no origin's traffic
        if o != _NIL and not exempt and count > self.size // 2:
            if self._ocount[o] >= self.quota:
                return self._refuse(msg, local, OVER_QUOTA)
        outcome = ACCEPTED
        if count == self.size:
            v = _NIL if local or msg.cls else self._eviction(p)
            if v == _NIL:
                return self._refuse(msg, local, FULL)
            self.victim = self._msg[v]
            self.victim_origin = self._okey[self._oidx[v]]
            self._remove(v)
            self.evictions += 1
            self.overflow = True
            self.reason = FULL
            outcome = EVICTED
        if until and not self._light(1):
            until = None  # loaded: due at once
        if held != _NIL and self._msg[held] is not None and self._due[held]:
            # Kept beside the deferred one(s) of its origin and port: one
            # deferral for all (the earlier time; none if it is due).
            if until and until > self._due[held]:
                until = self._due[held]
            self._defer_all(held, until)
        self._insert(msg, hop, p, key, local)
        if until:
            self._flags[self.slot] |= _HELD
            self._due[self.slot] = until
            self.deferred += 1
        elif self.deferred and not self._light():
            self.release()
        return outcome

    def _light(self, more=0):
        # Lightly loaded (deferral, module docstring): at most half the size
        # in messages, deferred, due and waiting ones alike, more included.
        return self._count - self.control + more <= self.size // 2

    def release(self, now=None):
        """End the deferral of every deferred entry (now: of those deferred until now at most)."""
        if not self.deferred:
            return
        due = self._due
        for i in range(self.entries):
            if due[i] and (now is None or due[i] <= now):
                self._release(i)

    def _release(self, i):
        self._due[i] = 0
        self.deferred -= 1

    def next_due(self):
        """Return the earliest end of deferral of an entry not waiting, or None."""
        if not self.deferred:
            return None
        t = None
        i = self._head
        while i != _NIL:
            d = self._due[i]
            if d and not self._flags[i] & _WAIT and (t is None or d < t):
                t = d
            i = self._next[i]
        return t

    def _refuse(self, msg, local, reason):
        self.reason = reason
        self.refusals[msg.cls] += 1
        self.overflow = True
        if local:
            return REFUSED
        return DROPPED if msg.cls == 0 else UNACKED

    def _origin(self, key):
        okey = self._okey
        return okey.index(key) if key in okey else _NIL

    def _older(self, key, port):
        # Newest queued class 0 message of origin key on port, or _NIL.
        o = self._origin(key)
        if o == _NIL:
            return _NIL
        i = self._tail
        while i != _NIL:
            m = self._msg[i]
            if self._oidx[i] == o and m.port == port and m.cls == 0:
                return i
            i = self._prev[i]
        return _NIL

    def _held(self, i):
        # Entry i (newest of its origin and port) is held for a bundle (keep).
        if self._due[i]:
            return True
        if not self._flags[i] & _HELD:
            return False
        o = self._oidx[i]
        port = self._msg[i].port
        n = 0
        j = self._head
        while j != _NIL:
            m = self._msg[j]
            if self._oidx[j] == o and m.port == port and m.cls == 0:
                n += 1
            j = self._next[j]
        return n < KEEP

    def _defer_all(self, held, until):
        # The deferred class 0 messages of held's origin and port: deferred
        # until until (ms), or due if None.
        o = self._oidx[held]
        port = self._msg[held].port
        i = self._head
        while i != _NIL:
            m = self._msg[i]
            if self._due[i] and self._oidx[i] == o and m.port == port and m.cls == 0:
                if until:
                    self._due[i] = until
                else:
                    self._release(i)
            i = self._next[i]

    def _eviction(self, p):
        # Forwarded class 0 entry of the lowest priority (>= p), oldest first.
        best = _NIL
        worst = p - 1
        i = self._head
        while i != _NIL:
            if not self._flags[i] & _LOCAL and self._msg[i].cls == 0 and self._prio[i] > worst:
                best = i
                worst = self._prio[i]
            i = self._next[i]
        return best

    def _insert(self, msg, hop, p, key, local, control=0):
        i = self._free
        self._free = self._next[i]
        o = self._origin(key)
        if o == _NIL:
            o = self._okey.index(None)
            self._okey[o] = key
        self._ocount[o] += 1
        self._msg[i] = msg
        self._hop[i] = hop
        self._prio[i] = p
        self._flags[i] = (_LOCAL if local else 0) | control
        if control:
            self.control += 1
        self._attempts[i] = 0
        self._oidx[i] = o
        self._prev[i] = self._tail
        self._next[i] = _NIL
        if self._tail == _NIL:
            self._head = i
        else:
            self._next[self._tail] = i
        self._tail = i
        self._count += 1
        self.slot = i

    def _remove(self, i):
        prev = self._prev[i]
        nxt = self._next[i]
        if prev == _NIL:
            self._head = nxt
        else:
            self._next[prev] = nxt
        if nxt == _NIL:
            self._tail = prev
        else:
            self._prev[nxt] = prev
        o = self._oidx[i]
        self._ocount[o] -= 1
        if not self._ocount[o]:
            self._okey[o] = None
        if self._flags[i] & _CONTROL:
            self.control -= 1
        if self._due[i]:
            self._release(i)
        self._msg[i] = None
        self._flags[i] = 0
        self._next[i] = self._free
        self._free = i
        self._count -= 1

    # --- Packing -------------------------------------------------------------

    def pack(self, now, dutycycle, limit, toa_ms):
        """Choose the messages of the next frame; return a `Selection` or None.

        now: time (ms) for the duty-cycle window; dutycycle: the board's
        `DutyCycle`; limit: frame limit in bytes (`frame.frame_limit`);
        toa_ms: function giving the time on air (ms) of a frame length at the
        current spreading factor. None if no queued message is due and fits
        a frame now. The queue is unchanged until `commit`, but for the
        deferrals that ended by now.
        """
        size = self.entries
        keys = self._keys
        hkeys = self._hkeys
        rounds = self._rounds
        rounds[:] = self._zero
        last = self._last
        prio = self._prio
        oidx = self._oidx
        flags = self._flags
        nxt = self._next
        due = self._due
        deferred = self.deferred
        if deferred:
            self.release(now)
            deferred = self.deferred
            hrounds = self._hrounds
            hrounds[:] = self._zero
        # Service order key: priority, round (rank within its origin, by age),
        # cyclic distance of the origin from the one served last, slot. Due
        # and deferred entries apart: the deferred ones only join a frame.
        n = 0
        hn = 0
        i = self._head
        while i != _NIL:
            if not flags[i] & _WAIT:
                p = prio[i]
                o = oidx[i]
                r = (p - 1) * size + o
                k = ((o - last[p] - 1) % size) << 8 | i
                if deferred and due[i]:
                    hkeys[hn] = p << 24 | hrounds[r] << 16 | k
                    hrounds[r] += 1
                    hn += 1
                else:
                    keys[n] = p << 24 | rounds[r] << 16 | k
                    rounds[r] += 1
                    n += 1
            i = nxt[i]
        if not n:
            return None
        for j in range(n, size):
            keys[j] = _END
        keys.sort()
        if hn:
            for j in range(hn, size):
                hkeys[j] = _END
            hkeys.sort()

        sel = self._sel
        chosen = sel._sel
        gid = sel._gid
        rep = sel._rep
        gcount = sel._gcount
        sel_last = sel._last
        for p in range(4):
            sel_last[p] = _NIL
        msgs = self._msg
        hops = self._hop
        same_group = frame.same_group
        hop = None
        fprio = PRIORITY_REPAIR
        total = frame.OVERHEAD
        toa = 0
        groups = 0
        count = 0
        for j in range(n + hn):
            if j < n:
                k = keys[j]
            elif hop is None:
                break  # deferred messages never start a frame
            else:
                k = hkeys[j - n]  # passengers, after the due messages
            i = k & 0xFF
            if hop is not None and hops[i] != hop:
                continue
            m = msgs[i]
            g = 0
            while g < groups and not same_group(msgs[rep[g]], m):
                g += 1
            grow = frame.element_size(m)
            if g < groups:
                c = gcount[g]
                if c == 255:
                    continue
                is_last = g == groups - 1
                grow += _header_bytes(c + 1, is_last) - _header_bytes(c, is_last)
            else:
                grow += 2 + (1 if groups and gcount[groups - 1] == 1 else 0)
            s = total + grow
            if s > limit:
                continue
            p = k >> 24
            t = toa_ms(s)
            newp = dutycycle.admit(now, fprio, p, t)
            if not newp:
                continue
            if g == groups:
                rep[g] = i
                gcount[g] = 0
                groups += 1
            gcount[g] += 1
            chosen[count] = i
            gid[count] = g
            count += 1
            total = s
            toa = t
            fprio = newp
            if hop is None:
                hop = hops[i]
            if j < n:
                sel_last[p] = oidx[i]
        if not count:
            return None

        # Frame order: groups in order of their first message, then
        # selection order within a group.
        order = sel._order
        for j in range(count):
            order[j] = gid[j] << 8 | j
        for j in range(count, size):
            order[j] = _END
        order.sort()
        messages = []
        for j in range(count):
            i = chosen[order[j] & 0xFF]
            sel.slots[j] = i
            messages.append(msgs[i])
        sel.dest = hop
        sel.messages = messages
        sel.size = total
        sel.toa_ms = toa
        sel.priority = fprio
        sel.count = count
        return sel

    # --- Commit and reliability ----------------------------------------------

    def commit(self, sel):
        """Record that the frame of sel went on air (call at TX start).

        Class 0 messages leave the queue; class 1 and 2 messages wait for an
        acknowledgement (`remove`, `retry`) with one more attempt. Round robin
        resumes after the origins just served. Entries changed since `pack`
        (replaced, removed) are left as they are.
        """
        for j in range(sel.count):
            i = sel.slots[j]
            m = sel.messages[j]
            if self._msg[i] is not m or self._flags[i] & _WAIT:
                continue
            if m.cls == 0:
                self._remove(i)
            else:
                self._flags[i] |= _WAIT
                if self._attempts[i] < 255:
                    self._attempts[i] += 1
        for p in range(1, 4):
            if sel._last[p] != _NIL:
                self._last[p] = sel._last[p]
        sel.count = 0

    def waiting(self, slot, msg):
        """Return True if msg, in slot, waits for an acknowledgement."""
        return self._msg[slot] is msg and bool(self._flags[slot] & _WAIT)

    def attempts(self, slot, msg):
        """Return the transmissions of msg in slot since queued or retargeted."""
        return self._attempts[slot] if self._msg[slot] is msg else 0

    def retry(self, slot, msg, hop=None, fresh=False):
        """Make waiting msg in slot eligible again; return False if stale.

        hop: a new next hop (e.g. the next parent): attempts restart from 0,
        as they do with fresh (a held message resumed, §12). The message keeps
        its age, hence its place in its origin's turn.
        """
        if self._msg[slot] is not msg:
            return False
        self._flags[slot] &= ~_WAIT
        if hop is not None and hop != self._hop[slot]:
            self._hop[slot] = hop
            fresh = True
        if fresh:
            self._attempts[slot] = 0
        return True

    def select(self, test, waiting=False):
        """Return [(slot, msg)] of the entries not waiting for which test(msg) is true.

        waiting: the waiting entries (sent, or held) too.
        """
        out = []
        i = self._head
        while i != _NIL:
            if (waiting or not self._flags[i] & _WAIT) and test(self._msg[i]):
                out.append((i, self._msg[i]))
            i = self._next[i]
        return out

    def redirect(self, test, hop):
        """Send the entries not waiting for which test(msg) is true to hop; return their number.

        Waiting entries (sent, or held) keep their next hop: the reliability
        layer retries or resumes them. A redirected entry restarts its
        attempts and keeps its age.
        """
        n = 0
        i = self._head
        while i != _NIL:
            if not self._flags[i] & _WAIT and self._hop[i] != hop and test(self._msg[i]):
                self._hop[i] = hop
                self._attempts[i] = 0
                n += 1
            i = self._next[i]
        return n

    def promote(self, slot, msg, priority):
        """Raise msg in slot to priority if it is more urgent (lower); False if stale.

        The entry keeps its age; used for an advertisement merged with a more
        urgent one (`mesh.dodag`).
        """
        if self._msg[slot] is not msg:
            return False
        if priority < self._prio[slot]:
            self._prio[slot] = priority
        return True

    def hold(self, slot, msg):
        """Make msg in slot wait without an attempt (held, §12); False if stale.

        It keeps its entry and is not packed until `retry`.
        """
        if self._msg[slot] is not msg:
            return False
        self._flags[slot] |= _WAIT
        return True

    def holds(self, slot, msg):
        """Return True if slot holds msg (waiting or not)."""
        return self._msg[slot] is msg

    def remove(self, slot, msg):
        """Remove msg from slot (acknowledged, or failed); False if stale."""
        if self._msg[slot] is not msg:
            return False
        self._remove(slot)
        return True

    # --- Inspection and dropping ---------------------------------------------

    def ready(self, now=None):
        """Return the number of queued messages not waiting for an acknowledgement.

        now: only those due by then (not deferred beyond it).
        """
        n = 0
        due = self._due
        i = self._head
        while i != _NIL:
            if not self._flags[i] & _WAIT and (now is None or not due[i] or due[i] <= now):
                n += 1
            i = self._next[i]
        return n

    def drop(self, test, sel=None):
        """Remove the entries for which test(msg, priority, local) is true.

        Every entry is tested, waiting ones included, oldest first; sel: test
        only the messages of this selection (from the last `pack`, before its
        commit). Returns the number of entries removed. Used by the link
        engine for messages it gives up (duty-cycle budget, busy channel).
        """
        n = 0
        if sel is not None:
            for j in range(sel.count):
                i = sel.slots[j]
                m = sel.messages[j]
                if self._msg[i] is m and test(m, self._prio[i], bool(self._flags[i] & _LOCAL)):
                    self._remove(i)
                    n += 1
            return n
        i = self._head
        while i != _NIL:
            nxt = self._next[i]
            if test(self._msg[i], self._prio[i], bool(self._flags[i] & _LOCAL)):
                self._remove(i)
                n += 1
            i = nxt
        return n
