"""Class 1: hop-by-hop reliability (MESH_DESIGN §12 class 1; §9; §11 ports 3, 4; MP5, MP15).

One `Reliable` per `mesh.link.LinkEngine` (``engine.reliable``), plugged into
its extension points (`sent`, `overheard`, `tick`, `timer`), its forwarding
(`owe`, `hold`) and its receive path (duplicate cache, `acknowledge`); it
handles mesh ports 3 (acknowledgement) and 4 (error). Class 2 runs over it at
every hop (§12).

**Sender** (own and forwarded class 1/2 messages, any hop): a message sent in
a frame waits in the transmit queue (`mesh.txqueue`: *waiting*) until the
next hop acknowledges it:

- **passive acknowledgement**: the node overhears the next hop forward the
  message, identified by origin, destination, port and sequence (§11,
  `identity`);
- **explicit acknowledgement** (port 3): an entry (link source = this node,
  low 16 bits of the frame counter) acknowledges every class 1/2 message of
  that frame; only the frame's link destination may send it.

Timeout (§9, §12): from the end of the frame, the time on air of a frame of
the frame limit (255 B) + 6 slots; towards the root, its batching delay
(20 times the time on air of an 8-entry acknowledgement frame: at most 5 %
of the channel, §16) + that frame + 6 slots. The next hop's p_ack
(`NeighbourTable.acked`, §9) gets **one sample per frame** (next hop, frame
counter low 16 bits), whatever the number of class 1/2 messages the frame
carries: a success at the first acknowledgement of one of them (one
explicit entry acknowledges them all; a passive one proves the frame was
received), a failure when the last of them still pending times out without
any acknowledged. Towards the root, a failure of a frame that ended within
one batching delay of the frame of the last failed sample towards the root
gives no sample: one lost batch acknowledges several frames of a relay and
is one loss, not one per frame (the attempts of one message are a root
timeout apart, longer than the batching delay, so each still fails once).
After `ATTEMPTS` (3) transmissions to one next hop, the injected
``engine.reroute(msg, hop)`` ("next hop after failure"; routing's parent
set later) returns another next hop (fresh attempts), `HOLD` (no route for
now: held) or None (no alternative: the message fails, reason 1).

**Holding during repair** (MP15): a class 1/2 message without next hop
(``engine.next_hop`` returns None, or `HOLD` above) keeps its queue entry
for `HOLD_PERIODS` (2) presence periods from the first time it is held; the
route is queried again every `POLL_SLOTS` slots (Imin) and at once on
`route_changed`. Then it fails with reason 3 (no route).

**Failure** of a message: an own message is reported to the application
(``engine.failed``: destination, port, sequence, reason); for a forwarded
one an error (port 4, class 1) goes to the root, which forwards it to the
message's origin (the root sends it directly); an error about an error is
never generated, only counted (``errors_dropped``).

**Receiver** (`mesh.link.LinkEngine._receive`): every class 1/2 message
received is looked up in the duplicate cache (`DUPLICATES` = 128 entries,
oldest evicted, an entry ignored after `DUPLICATE_PERIODS` = 2 presence
periods). **Custody** (§12): a duplicate is dropped at its destination, and
elsewhere only while the node holds a copy (`holding`: queued, waiting or
held); a node that has handed it over takes it again as a new arrival
(it came back through a loop, or its sender missed the acknowledgement:
the destination filters the extra copy). The frame gets one explicit
acknowledgement entry when it carries a duplicate (dropped or taken
again), a message delivered here or a message held; the root batches up
to `ACK_BATCH` (8) entries per frame for at most its batching delay, any
other node sends at once. A forwarded message
queued normally is acknowledged passively when the relay forwards it; if it
has not gone on air within half a timeout (queue, duty cycle, busy channel),
the relay acknowledges it explicitly (`owe`). A frame with a class 1/2
message the relay refused (queue full or quota) gets no explicit
acknowledgement at all, since the entry would cover the refused message too.

Readings where §11/§12 are silent: a copy waiting for the acknowledgement
of the node it comes back from is not held (that node took it; its
acknowledgement, not atomic with the reception, may still release the
copy: with both the copy and the returned message dropped, none would
remain); the sample of a frame whose messages
end differently (acknowledged if any was: the frame was received; a
message removed from the queue while pending, by a redirect or a hold,
leaves the frame to its other messages); the identity of a link-local message
(`identity`), no explicit acknowledgement for a frame with a refused
message (above), errors of a relay without route held like any class 1
message (`Reliable.fail`), a held message's holding counted from the first
time it is held, across resumptions. Class 1 alone cannot survive a relay
reboot after the relay acknowledged (explicitly, or by forwarding a frame
then lost): class 2 recovers it (§12).

Bounded memory: per-slot state arrays of the queue size, a 128-entry
duplicate cache, an 8-entry batch.
"""

from mesh import frame
from mesh.frame import BROADCAST, DOWNWARD, FIRST_HOP, LINK_LOCAL, P2P, ROOT, UPWARD, Message
from mesh.ports import ACK, ERROR
from mesh.txqueue import accepted

ATTEMPTS = 3  # transmissions per next hop (§12)
ACK_BATCH = 8  # entries per root acknowledgement frame (§16)
ACK_ENTRY = 4  # link source 2 B, frame counter low 16 bits 2 B (§11 port 3)
ACK_SHARE = 20  # root acknowledgements use at most 1/20 (5 %) of the channel (§16)
TIMEOUT_SLOTS = 6  # forwarding delay 1, backoff 4, margin 1 (§9)
DUPLICATES = 128  # duplicate cache entries (§12)
DUPLICATE_PERIODS = 2  # presence periods an entry is kept
HOLD_PERIODS = 2  # presence periods a message without route is held (MP15)
POLL_SLOTS = 32  # held messages query the route again every Imin (§9)

HOLD = -1  # reroute result: no route for now, hold the message

# Error reasons (port 4, §11).
UNREACHABLE = 1  # next hop unreachable (attempts exhausted)
NOT_NEIGHBOUR = 2  # next hop not a neighbour (source route, §10)
NO_ROUTE = 3  # no route (holding expired)
QUEUE_FULL = 4
DUTY_CYCLE = 5
TOO_DEEP = 6  # route beyond 8 hops

ERROR_SIZE = 10  # reason 1, next hop 2, origin 2, destination 2, port 1, sequence 2
NO_HOP = BROADCAST  # next hop field of an error without next hop (reason 3)

# Per-slot state.
_IDLE = 0  # queued, not waiting for an acknowledgement
_PENDING = 1  # sent, waiting for an acknowledgement
_HELD = 2  # held during repair


def _classed(msg):
    return msg.cls != 0


def encode_acks(entries):
    """Return the port 3 payload of entries, an iterable of (link source, counter).

    counter: frame counter (its low 16 bits are sent).
    """
    out = bytearray()
    for source, counter in entries:
        if type(source) is not int or source < 0 or source > 0xFFFF:
            raise ValueError("source out of range")
        out.append(source & 0xFF)
        out.append(source >> 8)
        out.append(counter & 0xFF)
        out.append((counter >> 8) & 0xFF)
    if not out:
        raise ValueError("no entry")
    return bytes(out)


def decode_acks(payload):
    """Return the [(link source, counter low 16 bits)] of a port 3 payload.

    Raises ValueError unless the payload holds whole entries, at least one.
    """
    n = len(payload)
    if not n or n % ACK_ENTRY:
        raise ValueError("bad acknowledgement length")
    return [
        (payload[i] | payload[i + 1] << 8, payload[i + 2] | payload[i + 3] << 8)
        for i in range(0, n, ACK_ENTRY)
    ]


def encode_error(reason, hop, origin, dest, port, seq):
    """Return the port 4 payload: reason, next hop, then the failed message's identity."""
    if type(reason) is not int or reason < 1 or reason > 255:
        raise ValueError("reason out of range")
    for value in (hop, origin, dest, seq):
        if type(value) is not int or value < 0 or value > 0xFFFF:
            raise ValueError("field out of range")
    if type(port) is not int or port < 0 or port > 255:
        raise ValueError("port out of range")
    return bytes(
        (
            reason,
            hop & 0xFF,
            hop >> 8,
            origin & 0xFF,
            origin >> 8,
            dest & 0xFF,
            dest >> 8,
            port,
            seq & 0xFF,
            seq >> 8,
        )
    )


def decode_error(payload):
    """Return (reason, next hop, origin, destination, port, sequence) of a port 4 payload.

    Raises ValueError unless it is exactly `ERROR_SIZE` bytes with a reason
    other than 0.
    """
    p = payload
    if len(p) != ERROR_SIZE or not p[0]:
        raise ValueError("bad error message")
    return (p[0], p[1] | p[2] << 8, p[3] | p[4] << 8, p[5] | p[6] << 8, p[7], p[8] | p[9] << 8)


def identity(msg, source, link_dest):
    """Return the identity (origin, destination, port, sequence) of a class 1/2 message.

    source, link_dest: link source and destination of the frame carrying it
    (the origin of a first-hop message is its link source). The root is the
    destination of upward messages and the origin of downward ones. §11 names
    the flow (origin, destination, port); a link-local message, which §11
    does not cover, is identified by its link source and destination. None
    for broadcasts (class 0 only, §12) and messages without sequence.
    """
    seq = msg.seq
    if seq is None:
        return None
    mode = msg.mode
    if mode == FIRST_HOP:
        return (source, ROOT, msg.port, seq)
    if mode == UPWARD:
        return (msg.origin, ROOT, msg.port, seq)
    if mode == DOWNWARD:
        return (ROOT, msg.dest, msg.port, seq)
    if mode == P2P:
        return (msg.origin, msg.dest, msg.port, seq)
    if mode == LINK_LOCAL:
        return (source, link_dest, msg.port, seq)
    return None


class DuplicateCache:
    """Identities of the class 1/2 messages received lately (§12), bounded.

    A ring of capacity entries: a new identity evicts the oldest one
    (``evictions``); an entry older than the age given to `seen` is ignored.
    """

    def __init__(self, capacity=DUPLICATES):
        if capacity < 1:
            raise ValueError("bad capacity")
        self.capacity = capacity
        self.evictions = 0
        self._keys = [None] * capacity
        self._times = [0] * capacity
        self._index = {}
        self._next = 0

    def __len__(self):
        return len(self._index)

    def seen(self, key, now, max_age):
        """Return True if key was added less than max_age ms before now."""
        i = self._index.get(key)
        if i is None:
            return False
        if now - self._times[i] >= max_age:
            del self._index[key]
            self._keys[i] = None
            return False
        return True

    def add(self, key, now):
        """Record key at now (refreshes it if present)."""
        i = self._index.get(key)
        if i is not None:
            self._times[i] = now
            return
        i = self._next
        old = self._keys[i]
        if old is not None:
            del self._index[old]
            self.evictions += 1
        self._keys[i] = key
        self._times[i] = now
        self._index[key] = i
        self._next = (i + 1) % self.capacity


class Reliable:
    """Class 1 state of one link engine (see the module docstring)."""

    def __init__(self, engine):
        self.engine = engine
        self.queue = engine.queue
        self.counters = engine.counters
        self.duplicates = DuplicateCache()
        slot = engine.slot_ms
        self.relay_timeout_ms = engine.toa_ms(engine.limit) + TIMEOUT_SLOTS * slot
        ack_frame = engine.toa_ms(frame.OVERHEAD + 2 + ACK_BATCH * ACK_ENTRY)
        self.batch_ms = ACK_SHARE * ack_frame
        self.root_timeout_ms = self.batch_ms + ack_frame + TIMEOUT_SLOTS * slot
        self.poll_ms = POLL_SLOTS * slot
        n = self.queue.size
        self._msg = [None] * n  # message tracked in each queue slot
        self._state = bytearray(n)
        self._hop = [0] * n  # next hop of the last transmission
        self._ctr = [0] * n  # its frame counter, low 16 bits
        self._sampled = bytearray(n)  # that frame already has its p_ack sample
        self._due = [None] * n  # acknowledgement timeout
        self._key = [None] * n  # identity, while pending
        self._since = [None] * n  # first time held
        self._osrc = [0] * n  # owed explicit acknowledgement: link source,
        self._octr = [0] * n  # frame counter,
        self._odue = [None] * n  # and time it is due
        self._keys = {}  # identity -> slot, pending messages (passive acknowledgement)
        self._batch = []  # root: (link source, counter) waiting for the batch
        self._batch_due = None
        self._root_fail = None  # end of the frame of the last failed sample towards the root
        self._poll = None
        self._next = None

    # --- Parameters --------------------------------------------------------------

    def timeout_ms(self, hop):
        """Return the acknowledgement timeout (ms) towards hop, from the end of the frame."""
        return self.root_timeout_ms if hop == ROOT else self.relay_timeout_ms

    def hold_ms(self):
        """Return the holding duration: 2 presence periods (§12)."""
        return HOLD_PERIODS * self.engine.neighbours.period_ms

    def held(self):
        """Return the number of messages held during repair (§13 queues)."""
        return self._count(_HELD)

    def pending(self):
        """Return the number of messages waiting for an acknowledgement."""
        return self._count(_PENDING)

    def _count(self, state):
        n = 0
        for slot in range(self.queue.size):
            if self._state[slot] == state and self.queue.holds(slot, self._msg[slot]):
                n += 1
        return n

    # --- Duplicate cache (receiver) ----------------------------------------------

    def duplicate(self, key, now):
        """Return True if identity key was received within 2 presence periods."""
        return self.duplicates.seen(key, now, DUPLICATE_PERIODS * self.engine.neighbours.period_ms)

    def remember(self, key, now):
        """Record a received identity (delivered, queued or held)."""
        self.duplicates.add(key, now)

    def holding(self, key, source=None):
        """Return True if this node holds a copy of identity key (queued, waiting or held).

        Custody (§12): a node other than the destination drops a duplicate
        only while it holds it. source: link source of the duplicate; a copy
        waiting for source's acknowledgement does not count, since source has
        taken it (its acknowledgement may still be on its way) and gives it
        back. Own link-local messages are never matched (a received
        link-local identity names its link source, never this node).
        """
        own = self.engine.address
        for slot, msg in self.queue.select(_classed, True):
            if identity(msg, own, BROADCAST) != key:
                continue
            if self._msg[slot] is msg and self._state[slot] == _PENDING:
                if self._hop[slot] == source:
                    continue
            return True
        return False

    # --- Slots -------------------------------------------------------------------

    def _bind(self, slot, msg):
        if self._msg[slot] is msg:
            return
        self._clear(slot)
        self._msg[slot] = msg

    def _clear(self, slot):
        key = self._key[slot]
        if key is not None and self._keys.get(key) == slot:
            del self._keys[key]
        self._msg[slot] = None
        self._state[slot] = _IDLE
        self._due[slot] = None
        self._key[slot] = None
        self._since[slot] = None
        self._odue[slot] = None

    def _at(self, time):
        if self._next is None or time < self._next:
            self._next = time

    # --- Sender ------------------------------------------------------------------

    def sent(self, sel, counter, now):
        """A frame goes on air (before its commit): its class 1/2 messages wait."""
        queue = self.queue
        c16 = counter & 0xFFFF
        dest = sel.dest
        due = now + sel.toa_ms + self.timeout_ms(dest)
        for j in range(sel.count):
            m = sel.messages[j]
            slot = sel.slots[j]
            if self._msg[slot] is m:
                self._odue[slot] = None  # forwarded now: acknowledged passively
            if not m.cls or not queue.holds(slot, m):
                continue
            key = identity(m, self.engine.address, dest)
            if key is None:
                continue
            self._bind(slot, m)
            old = self._key[slot]
            if old is not None and self._keys.get(old) == slot:
                del self._keys[old]
            if queue.attempts(slot, m):
                self.counters.retries += 1
            self._state[slot] = _PENDING
            self._hop[slot] = dest
            self._ctr[slot] = c16
            self._sampled[slot] = 0
            self._due[slot] = due
            self._key[slot] = key
            self._keys[key] = slot
            self._at(due)

    def overheard(self, header, messages, now):
        """Passive acknowledgement: the next hop forwards a pending message."""
        if not self._keys:
            return
        source = header.source
        for m in messages:
            if not m.cls:
                continue
            slot = self._keys.get(identity(m, source, header.dest))
            if slot is not None and self._hop[slot] == source:
                self._acknowledged(slot, True)

    def on_ack(self, origin, msg, header):
        """Port 3 handler: explicit acknowledgements from the link source origin."""
        try:
            entries = decode_acks(msg.payload)
        except ValueError:
            self.counters.malformed_acks += 1
            return
        own = self.engine.address
        for source, c16 in entries:
            if source != own:
                continue
            for slot in range(self.queue.size):
                if (
                    self._state[slot] == _PENDING
                    and self._ctr[slot] == c16
                    and self._hop[slot] == origin
                ):
                    self._acknowledged(slot, False)

    def _same_frame(self, slot):
        # The other pending slots sent in the frame of slot, not yet sampled.
        hop = self._hop[slot]
        ctr = self._ctr[slot]
        state = self._state
        sampled = self._sampled
        out = []
        for i in range(self.queue.size):
            if i != slot and state[i] == _PENDING and not sampled[i]:
                if self._hop[i] == hop and self._ctr[i] == ctr:
                    out.append(i)
        return out

    def _acknowledged(self, slot, passive):
        msg = self._msg[slot]
        hop = self._hop[slot]
        c = self.counters
        if passive:
            c.acks_passive += 1
        else:
            c.acks_explicit += 1
        if not self._sampled[slot]:
            # One sample per frame (§9): the frame's other messages give none.
            self.engine.neighbours.acked(hop, True, self.engine.now)
            for i in self._same_frame(slot):
                self._sampled[i] = 1
        self.queue.remove(slot, msg)
        self._clear(slot)

    def _timeout(self, slot, now):
        queue = self.queue
        msg = self._msg[slot]
        hop = self._hop[slot]
        if not self._sampled[slot] and not self._same_frame(slot):
            # A failure once the frame's last pending message times out.
            if hop != ROOT or self._root_failure(self._due[slot] - self.root_timeout_ms):
                self.engine.neighbours.acked(hop, False, now)
        self._due[slot] = None
        self._state[slot] = _IDLE
        if queue.attempts(slot, msg) < ATTEMPTS:
            queue.retry(slot, msg)
            self.engine.access_at(now)
            return
        reroute = self.engine.reroute
        alt = reroute(msg, hop) if reroute is not None else None
        if alt == HOLD:
            self.hold(slot, msg, now)
        elif alt is None or alt == hop:
            self.counters.unreachable += 1
            self.fail(slot, msg, UNREACHABLE, hop, now)
        else:
            self.counters.reroutes += 1
            queue.retry(slot, msg, alt)
            self.engine.access_at(now)

    def _root_failure(self, end):
        # True if the failure of the frame ended at end gives a sample: one
        # per batching delay towards the root (one lost batch, one loss).
        last = self._root_fail
        if last is not None and end - last < self.batch_ms:
            return False
        self._root_fail = end
        return True

    # --- Holding -----------------------------------------------------------------

    def hold(self, slot, msg, now):
        """Hold msg (queued in slot) during repair: no next hop for now (MP15).

        now: None outside a step (a submission): the holding starts at the
        next step.
        """
        if self._msg[slot] is msg and self._state[slot] == _HELD:
            return  # already held: its holding goes on
        if not self.queue.hold(slot, msg):
            return
        self._bind(slot, msg)
        key = self._key[slot]
        if key is not None and self._keys.get(key) == slot:
            del self._keys[key]
        self._key[slot] = None
        self._state[slot] = _HELD
        self._due[slot] = None
        self.counters.held += 1
        if now is None:
            self._poll_at(self.engine.now or 0)  # stamped by the next step
            return
        if self._since[slot] is None:
            self._since[slot] = now
        self._poll_at(min(now + self.poll_ms, self._since[slot] + self.hold_ms()))

    def _poll_at(self, time):
        if self._poll is None or time < self._poll:
            self._poll = time
        self._at(self._poll)

    def route_changed(self, now):
        """Query the route of the held messages at the next step (routing calls it)."""
        self._poll = now
        self._at(now)

    def _resume_held(self, now):
        engine = self.engine
        limit = self.hold_ms()
        self._poll = None
        for slot in range(self.queue.size):
            if self._state[slot] != _HELD:
                continue
            msg = self._msg[slot]
            if not self.queue.holds(slot, msg):
                self._clear(slot)
                continue
            if self._since[slot] is None:
                self._since[slot] = now
            if now - self._since[slot] >= limit:
                self.counters.hold_expired += 1
                self.fail(slot, msg, NO_ROUTE, NO_HOP, now)
                continue
            hop = engine.next_hop(msg) if engine.next_hop is not None else None
            if hop is None:
                self._poll_at(min(now + self.poll_ms, self._since[slot] + limit))
                continue
            self._state[slot] = _IDLE
            self.queue.retry(slot, msg, hop, True)
            engine.access_at(now)

    # --- Failure and errors (port 4) -----------------------------------------------

    def fail(self, slot, msg, reason, hop, now):
        """msg (in slot) failed: tell its origin (MP5).

        An own message fails to the application; a forwarded one makes an
        error (port 4) to the root, or from the root to the origin. Reading:
        §12 makes errors go "to the root"; a relay without route therefore
        holds its error like any class 1 message, so the origin learns of a
        failure only if a path to the root returns within that holding.
        """
        engine = self.engine
        self.queue.remove(slot, msg)
        self._clear(slot)
        origin, dest, port, seq = identity(msg, engine.address, hop)
        if port == ERROR:
            self.counters.errors_dropped += 1  # never an error about an error
            return
        if origin == engine.address:
            engine.failed(dest, port, seq, reason)
            return
        payload = encode_error(reason, hop, origin, dest, port, seq)
        self.counters.errors_sent += 1
        self._send_error(payload, origin, now)

    def _send_error(self, payload, origin, now):
        engine = self.engine
        seq = engine.next_seq()
        if engine.is_root:
            msg = Message(DOWNWARD, 1, ERROR, payload, dest=origin, seq=seq)
        else:
            msg = Message(FIRST_HOP, 1, ERROR, payload, seq=seq)
        self.send_own(msg, now)

    def send_own(self, msg, now):
        """Queue an own class 1 message towards next_hop(msg), held without route.

        Returns True if queued (or held), False if the queue refused it.
        """
        engine = self.engine
        hop = engine.next_hop(msg) if engine.next_hop is not None else None
        outcome = engine.send(msg, BROADCAST if hop is None else hop)
        if not accepted(outcome):
            self.counters.errors_dropped += 1
            return False
        if hop is None:
            self.hold(self.queue.slot, msg, now)
        return True

    def on_error(self, origin, msg, header):
        """Port 4 handler: an error about a message (§12)."""
        try:
            reason, hop, failed_origin, dest, port, seq = decode_error(msg.payload)
        except ValueError:
            self.counters.errors_malformed += 1
            return
        engine = self.engine
        if failed_origin == engine.address:
            self.counters.errors_received += 1
            engine.failed(dest, port, seq, reason)
        elif engine.is_root:
            # The root forwards an error about an upward or P2P message to
            # its origin (§12).
            self.counters.errors_forwarded += 1
            self._send_error(bytes(msg.payload), failed_origin, engine.now)
        else:
            self.counters.errors_misrouted += 1

    # --- Receiver --------------------------------------------------------------------

    def acknowledge(self, source, counter, now):
        """Acknowledge the frame (source, counter) explicitly: batched at the root.

        Cancels the acknowledgements owed for that frame (the entry covers
        every class 1/2 message of the frame, §12).
        """
        self.cancel_owed(source, counter)
        c16 = counter & 0xFFFF
        if not self.engine.is_root:
            self._send_acks(((source, c16),))
            return
        entry = (source, c16)
        if entry in self._batch:
            return
        self._batch.append(entry)
        if len(self._batch) >= ACK_BATCH:
            self._flush()
        elif self._batch_due is None:
            self._batch_due = now + self.batch_ms
            self._at(self._batch_due)

    def _flush(self):
        batch = self._batch
        self._batch = []
        self._batch_due = None
        if batch:
            self._send_acks(batch)

    def _send_acks(self, entries):
        # One link source: sent to it; several (root batch): to every neighbour.
        dest = entries[0][0]
        for source, _ in entries:
            if source != dest:
                dest = BROADCAST
                break
        msg = Message(LINK_LOCAL, 0, ACK, encode_acks(entries))
        if accepted(self.engine.send(msg, dest)):
            self.counters.ack_entries += len(entries)
        else:
            self.counters.acks_dropped += len(entries)

    def owe(self, slot, msg, source, counter, now):
        """A forwarded class 1/2 message queued in slot, received in frame (source, counter).

        Acknowledged explicitly unless it goes on air within half a timeout.
        """
        self._bind(slot, msg)
        self._osrc[slot] = source
        self._octr[slot] = counter & 0xFFFF
        due = now + self.timeout_ms(self.engine.address) // 2
        self._odue[slot] = due
        self._at(due)

    def cancel_owed(self, source, counter):
        """Forget the acknowledgements owed for the frame (source, counter)."""
        c16 = counter & 0xFFFF
        for slot in range(self.queue.size):
            if self._odue[slot] is not None and self._osrc[slot] == source:
                if self._octr[slot] == c16:
                    self._odue[slot] = None

    # --- Timers ------------------------------------------------------------------------

    def tick(self, now):
        """Run what is due: timeouts, owed acknowledgements, batch, holding."""
        if self._next is None or now < self._next:
            return
        self._next = None
        if self._batch_due is not None:
            if now >= self._batch_due:
                self._flush()
            else:
                self._at(self._batch_due)
        queue = self.queue
        for slot in range(queue.size):
            msg = self._msg[slot]
            if msg is None:
                continue
            if not queue.holds(slot, msg):
                self._clear(slot)
                continue
            due = self._odue[slot]
            if due is not None:
                if now >= due:
                    self.counters.acks_late += 1
                    self.acknowledge(self._osrc[slot], self._octr[slot], now)
                else:
                    self._at(due)
            due = self._due[slot]
            if due is not None:
                if now >= due:
                    self._timeout(slot, now)
                else:
                    self._at(due)
        if self._poll is not None:
            if now >= self._poll:
                self._resume_held(now)
            else:
                self._at(self._poll)

    def timer(self):
        """Return the time of the next class 1 timer, or None."""
        return self._next
