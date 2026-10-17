"""Link engine (MESH_DESIGN §1 link layer; §6, §7, §8, §12, §13, §14; R14).

A `mesh.core.Core`: the same object runs on the board (asyncio runner and
SX1262 adapter) and in the host simulator (`sim.node`). It owns the frame
security (`mesh.security`), the duty-cycle window (`mesh.dutycycle`) and the
transmit queue (`mesh.txqueue`) of one board.

**Submission** (applications, MP5): `submit` checks a message and queues it,
returning at once its sequence number (>= 0; 0 for a message without one) or
a negative refusal reason (`BAD_PORT`...). Mesh components queue their own
messages with `send` (mesh ports, explicit next hop). Applications register
one handler per port with `bind` (ports 16–255; ports 0–15 belong to the
mesh, `bind_mesh`), and a failure callback with `on_failure` (explicit
failures after submission, MP5: local reasons, negative, or the network's,
positive, `mesh.reliable` reasons 1–6).

**Class 1** (§12): `mesh.reliable.Reliable` (``reliable``) keeps class 1/2
messages until acknowledged (passive or explicit, port 3), retries them, holds
them without route and turns failures into errors (port 4) or failure
callbacks; it is plugged into the extension points below, the forwarding
path and the receive path (duplicate cache, explicit acknowledgements). A
class 1 submission without next hop is held, not refused.

**Transmit path** (§6, §7, §8): when the queue holds messages and the radio
is free, the engine drops the priority-2/3 reports (class 0) that the
duty-cycle allowance refuses, packs the next frame (`TxQueue.pack`: priority,
round robin by origin, aggregation per next hop, within the frame limit and
the allowances) and senses the channel. Idle: it builds the header, seals the
frame (`Security.seal`, new frame counter), charges its time on air (TX
start), commits the selection and transmits; ``TX_DONE`` charges the excess
of the measured duration. Busy: random backoff of [1, CW] slots (slot = time
on air of a presence frame at the SF; CW 4, doubling up to 32), then packs and
senses again. After `MAX_ATTEMPTS` busy senses the engine gives up: the
priority-2/3 reports of the frame are dropped; the other messages (class 1/2,
priority-1 datagrams) stay queued and a new procedure starts after one more
backoff of [1, 32] slots. Messages the allowance refuses otherwise stay
queued; the engine wakes up when the window next releases airtime.

**Receive path** (§14 reception order): steps 1–3 by `Security.receive`
(length and version, authentication, replay; failures counted, per sender
for authentication and replay). Step 4: duplicate address (link source equal
to the own address: counted, flag set, frame dropped), `heard` (records
the frame in the neighbour table ``neighbours``, `mesh.neighbours`: link
estimation, §9; every frame counts, overheard ones included), message
decoding (a malformed body drops the whole frame), then the link
destination: own address or `BROADCAST` processes the messages, any other
one goes to `overheard` (passive acknowledgements). Each message is
delivered to its port's handler when this node is its destination, otherwise
passed to `forward` (next hop from the injected ``next_hop`` function, after
a random delay of at most one slot, §7). A class 1/2 message already in the
duplicate cache is neither delivered nor forwarded again; the frame then
gets an explicit acknowledgement, as when it carries a class 1/2 message
delivered here or held (`mesh.reliable`).

**Extension points** (minimal defaults, overridden or replaced by the later
steps): `heard` (neighbour table), `overheard` (passive acknowledgements),
`sent` (class 1 bookkeeping, frame counter of the frame), `tick` and `timer`
(protocol timers): an override calls the base method (class 1 lives there);
`give_up` (busy channel), `expand` (first-hop compression undone on every
message addressed to this node: origin, and port fields such as the
presence report's parent), `repeated` (a class 1/2 message found in the
duplicate cache: routing tells a loop from a retransmission), `forward`
(routing: data-path validation, replace by newer, source routes; returns
`QUEUED`, `HELD`, `DELIVERED` or `NOT_TAKEN`, on which class 1 acknowledges
or not), ``next_hop`` and ``reroute`` (routing: next hop, next hop after
failure, `mesh.reliable`), ``rank`` and ``dodag_version`` (header fields set
by routing).

**Runner contract**: a submission made outside `step` (application task)
takes effect at the next step; the runner steps the core (``step(now,
None)``) after such a call (host: `sim.node.Node.wake`).

Bounded memory: buffers, counters and tables are allocated at construction
(per-address counters: `SENDERS` entries); queued messages are bounded by
the transmit queue.
"""

from mesh import frame
from mesh.airtime import time_on_air_ms
from mesh.core import Core
from mesh.counter import FrameCounter
from mesh.dutycycle import DutyCycle
from mesh.dutycycle import retained_size as dutycycle_size
from mesh.frame import (
    BROADCAST,
    DOWNWARD,
    FIRST_HOP,
    LINK_LOCAL,
    MAX_FRAME,
    NODE_BROADCAST,
    P2P,
    ROOT,
    ROOT_BROADCAST,
    UPWARD,
    FormatError,
    Header,
    Message,
)
from mesh.neighbours import NeighbourTable
from mesh.platform import randbelow, retained_layout
from mesh.ports import ACK, APP_HIGH, ERROR, PRIORITY_LOW, PRIORITY_PERIODIC
from mesh.ports import priority as port_priority
from mesh.radio import CHANNEL, RX, RX_ERROR, SENSE_ACTION, TX, TX_DONE
from mesh.reliable import Reliable, identity
from mesh.replay import ReplayTable
from mesh.replay import retained_size as replay_size
from mesh.security import AUTHENTICATION, REPLAY, KeyRing, Security
from mesh.txqueue import EVICTED, OVER_QUOTA, REFUSED, SIZE, TxQueue, accepted

# Medium access (§8).
CW_MIN = 4
CW_MAX = 32
MAX_ATTEMPTS = 6  # carrier senses of one access procedure
PRESENCE_FRAME = 17  # bytes of a presence frame (§11 sizes): the backoff slot

MAX_PAYLOAD = 200  # announced maximum application payload (MP6)
SENDERS = 32  # entries of the per-address counters

# Submission refusals (negative) and local failure reasons (MP5). Failures
# reported by the network (port 4, §11) will use its positive reasons.
BAD_PORT = -1  # mesh port (0-15) or out of range
BAD_CLASS = -2  # class other than 0, 1, 2
BAD_DEST = -3  # own address or out of range
TOO_LONG = -4  # payload above MAX_PAYLOAD or above the frame limit
BROADCAST_CLASS = -5  # class 1 or 2 requested for a broadcast
NO_ROUTE = -6  # class 0 without next hop (class 1/2 messages are held, §12)
QUEUE_FULL = -7  # transmit queue full
ORIGIN_QUOTA = -8  # own traffic over its quota (§7)
EXHAUSTED = -9  # frame counter exhausted: the board no longer transmits
UNSUPPORTED = -10  # class 2: its sender (window, sequences) is not there yet

# Per-address counter fields (`SenderCounters`).
AUTH = 0  # authentication failures (wrong key, foreign network, forgery)
REPLAYS = 1  # replays rejected
QUOTA = 2  # forwarded messages of this origin refused by the queue
KNOWN = 3  # 1 if the address had authenticated before an authentication failure

# Outcomes of `forward` (and of the dispatch of a received message).
NOT_TAKEN = 0  # dropped or refused: not acknowledged
QUEUED = 1  # queued towards its next hop
HELD = 2  # queued without next hop: held during repair (§12)
DELIVERED = 3  # this node is its destination

# Transmit states.
IDLE = 0
SENSING = 1
BACKOFF = 2
TRANSMITTING = 3


class Counters:
    """Link counters since boot (§13 local observability).

    Per port (lists of 256): messages sent (``tx_messages``) and received in
    frames addressed to this node (``rx_messages``). The other attributes
    are totals, named after their cause.
    """

    def __init__(self):
        self.tx_frames = 0
        self.tx_messages = [0] * 256
        self.rx_frames = 0  # accepted by reception steps 1-3
        self.rx_messages = [0] * 256
        self.rx_crc = 0  # RX_ERROR from the radio
        self.rx_length = 0
        self.rx_version = 0
        self.rx_auth = 0
        self.rx_replay = 0
        self.rx_malformed = 0
        self.rx_overheard = 0  # frames for another link destination
        self.rx_no_handler = 0  # messages for an unbound port
        self.duplicate_address = 0
        self.delivered = 0
        self.forwarded = 0
        self.no_route = 0
        self.refused_full = 0  # queue refusals: queue full
        self.refused_quota = 0  # queue refusals: origin over its quota
        self.evicted = 0  # forwarded class 0 messages evicted from the queue
        self.duty_dropped = 0  # reports dropped: duty-cycle allowance
        self.duty_waits = 0  # waits for the duty-cycle window to release airtime
        self.senses = 0
        self.busy = 0
        self.backoffs = 0
        self.give_ups = 0
        self.access_dropped = 0  # reports dropped after MAX_ATTEMPTS busy senses
        self.unexpected = 0  # radio events not matching the transmit state
        # Class 1 (§12, `mesh.reliable`).
        self.retries = 0  # transmissions after the first to one next hop
        self.reroutes = 0  # attempts exhausted: switched to another next hop
        self.unreachable = 0  # attempts exhausted, no alternative: failed (reason 1)
        self.held = 0  # messages put on hold (no next hop)
        self.hold_expired = 0  # held messages failed after the holding (reason 3)
        self.acks_passive = 0  # messages acknowledged by overhearing the next hop
        self.acks_explicit = 0  # messages acknowledged by a port 3 entry
        self.ack_entries = 0  # port 3 entries sent
        self.acks_late = 0  # entries sent for a forward not on air within half a timeout
        self.acks_dropped = 0  # port 3 entries refused by the queue
        self.ack_withheld = 0  # frames not acknowledged: a class 1/2 message refused
        self.malformed_acks = 0
        self.duplicates = 0  # class 1/2 messages received again: acknowledged, dropped
        self.errors_sent = 0  # port 4 errors about forwarded messages
        self.errors_received = 0  # errors about own messages (failure callback)
        self.errors_forwarded = 0  # root: errors forwarded to the origin
        self.errors_dropped = 0  # errors failed (never an error about an error) or refused
        self.errors_misrouted = 0
        self.errors_malformed = 0


class SenderCounters:
    """Counters per link source or origin (§13, MP22), bounded.

    Fields `AUTH`, `REPLAYS`, `QUOTA`, `KNOWN`. At most capacity addresses;
    a new one evicts the least recently updated (``evictions``).
    """

    def __init__(self, capacity=SENDERS):
        self.capacity = capacity
        self.evictions = 0
        self._rows = {}
        self._order = []  # least recently updated first

    def __len__(self):
        return len(self._order)

    def add(self, address, field):
        """Count one event of field for address; return its row (a list)."""
        row = self._rows.get(address)
        if row is None:
            if len(self._order) >= self.capacity:
                del self._rows[self._order.pop(0)]
                self.evictions += 1
            row = [0, 0, 0, 0]
            self._rows[address] = row
        else:
            self._order.remove(address)
        self._order.append(address)
        row[field] += 1
        return row

    def get(self, address, field):
        """Return the count of field for address (0 if unknown)."""
        row = self._rows.get(address)
        return row[field] if row is not None else 0

    def addresses(self):
        """Return the addresses held, least recently updated first."""
        return list(self._order)


def _is_report(msg, priority, local):
    # Given up on a busy channel or refused by the allowance (§6, §8).
    return msg.cls == 0 and priority >= PRIORITY_PERIODIC


def destination(msg):
    """Return the final destination of a message (ROOT upward, BROADCAST...)."""
    mode = msg.mode
    if mode == FIRST_HOP or mode == UPWARD:
        return ROOT
    if mode == DOWNWARD or mode == P2P:
        return msg.dest
    return BROADCAST


class LinkEngine(Core):
    """Link layer of one board.

    address: own 16-bit address (`ROOT` for the root); security: its
    `Security`; dutycycle: its `DutyCycle`; queue: its `TxQueue`; random: a
    `mesh.platform.Random` (backoff, first sequence number); sf, profile:
    spreading factor and regional profile (time on air, frame limit);
    next_hop: function(msg) -> next hop address or None (routing; static in
    tests; None holds a class 1/2 message); reroute: function(msg, hop) ->
    next hop after hop failed `mesh.reliable.ATTEMPTS` times, `HOLD` or None
    (no alternative; None as a function: no alternative ever); slot_ms:
    backoff slot, the time on air of a presence frame by default.
    ``neighbours``: its `mesh.neighbours.NeighbourTable` (presence period of
    the SF by default; routing sets it). ``reliable``: its class 1 state.
    Use `open_link` to build one from the platform.
    """

    def __init__(
        self,
        address,
        security,
        dutycycle,
        queue,
        random,
        sf,
        profile,
        next_hop=None,
        slot_ms=None,
        reroute=None,
    ):
        super().__init__()
        if type(address) is not int or address < 0 or address >= BROADCAST:
            raise ValueError("bad address")
        self.address = address
        self.is_root = address == ROOT
        self.security = security
        self.dutycycle = dutycycle
        self.queue = queue
        self.random = random
        self.sf = sf
        self._bw = profile.bandwidth_hz
        self._cr = profile.coding_rate
        self.limit = frame.frame_limit(sf, profile)
        self._toa = self.toa_ms  # bound once: pack calls it per message
        self.slot_ms = slot_ms if slot_ms else self.toa_ms(PRESENCE_FRAME)
        self.next_hop = next_hop
        self.reroute = reroute
        self.rank = frame.RANK_ROOT if self.is_root else frame.RANK_INFINITE
        self.dodag_version = 0
        self.neighbours = NeighbourTable(sf)
        self.counters = Counters()
        self.senders = SenderCounters()
        self.address_conflict = False  # presence flag bit 5; the reporter clears it
        self._failure = None
        self._handlers = [None] * 256
        self._seq = randbelow(random, 0x10000)
        self._txbuf = bytearray(MAX_FRAME)
        self._rxbuf = bytearray(MAX_FRAME)
        self._txh = Header(0, address, 0, 0)
        self._rxh = Header(0, 0, 0, 0)
        self._state = IDLE
        self._wake = None
        self._dirty = False
        self._attempts = 0
        self._cw = CW_MIN
        self._sel = None
        self._reliable()

    def _reliable(self):
        self.reliable = Reliable(self)
        self._handlers[ACK] = self.reliable.on_ack
        self._handlers[ERROR] = self.reliable.on_error

    def set_queue(self, queue):
        """Replace the transmit queue (tests: another size); class 1 state restarts."""
        self.queue = queue
        self._reliable()

    def toa_ms(self, length):
        """Return the time on air (ms) of a frame of length bytes."""
        return time_on_air_ms(length, self.sf, self._bw, self._cr)

    # --- Application and mesh API --------------------------------------------

    def bind(self, port, handler):
        """Register handler(origin, msg, header) for application port 16–255.

        None unbinds. msg is the decoded `mesh.frame.Message` (payload: a
        memoryview valid during the call only), header the frame's
        `mesh.frame.Header`; origin is the address of the message's origin.
        Ports 0–15 belong to the mesh: ValueError.
        """
        if type(port) is not int or port < APP_HIGH or port > 255:
            raise ValueError("not an application port")
        self._handlers[port] = handler

    def bind_mesh(self, port, handler):
        """Register the handler of a mesh port (1–10); reserved ports raise."""
        if type(port) is not int or port >= APP_HIGH:
            raise ValueError("not a mesh port")
        port_priority(port)  # ValueError for reserved ports
        self._handlers[port] = handler

    def on_failure(self, callback):
        """Register callback(dest, port, seq, reason) for failures after submission."""
        self._failure = callback

    def fail(self, msg, reason):
        """Report the failure of an own message to the application (MP5)."""
        self.failed(destination(msg), msg.port, msg.seq, reason)

    def failed(self, dest, port, seq, reason):
        """Report the failure of the own message (dest, port, seq) to the application."""
        if self._failure is not None:
            self._failure(dest, port, seq, reason)

    def next_seq(self):
        """Return the next sequence number of the node-wide counter and advance it."""
        seq = self._seq
        self._seq = (seq + 1) & 0xFFFF
        return seq

    def submit(self, port, cls, dest, payload, replace=False):
        """Queue an application message; return its sequence or a refusal.

        port: 16–255 (priority from the port, §6); cls: service class; dest:
        a node, `ROOT` (upward) or `BROADCAST` (every node, class 0 only);
        payload: bytes-like, copied; replace: class 0 replace by newer for this
        port (§12). Returns the message's sequence number (>= 0; 0 when the
        message carries none) once queued, otherwise a negative reason
        (`BAD_PORT`, `BAD_CLASS`, `UNSUPPORTED`, `BAD_DEST`, `BROADCAST_CLASS`,
        `TOO_LONG`, `EXHAUSTED`, `NO_ROUTE`, `QUEUE_FULL`, `ORIGIN_QUOTA`). A
        class 1 message without next hop is queued and held (§12): it is
        sent when a route appears, or fails later (`on_failure`, reason 3).
        """
        if type(port) is not int or port < APP_HIGH or port > 255:
            return BAD_PORT
        if cls == 2:
            return UNSUPPORTED
        if cls != 0 and cls != 1:
            return BAD_CLASS
        if type(dest) is not int or dest < 0 or dest > BROADCAST or dest == self.address:
            return BAD_DEST
        if dest == BROADCAST and cls:
            return BROADCAST_CLASS
        if len(payload) > MAX_PAYLOAD:
            return TOO_LONG
        if self.security.counter.exhausted:
            return EXHAUSTED
        payload = bytes(payload)
        seq = self._seq
        if dest == BROADCAST:
            if self.is_root:
                msg = Message(ROOT_BROADCAST, 0, port, payload, seq=seq)
            else:
                msg = Message(NODE_BROADCAST, 0, port, payload, origin=self.address, seq=seq)
        else:
            if cls == 0:
                seq = None
            if dest == ROOT:
                msg = Message(FIRST_HOP, cls, port, payload, seq=seq)
            elif self.is_root:
                msg = Message(DOWNWARD, cls, port, payload, dest=dest, seq=seq)
            else:
                msg = Message(P2P, cls, port, payload, self.address, dest, seq)
        if frame.frame_size([msg]) > self.limit:
            return TOO_LONG
        if dest == BROADCAST:
            hop = BROADCAST
        else:
            hop = self.next_hop(msg) if self.next_hop is not None else None
            if hop is None and not cls:
                return NO_ROUTE
        outcome = self.send(msg, BROADCAST if hop is None else hop, replace=replace)
        if outcome == REFUSED:
            return ORIGIN_QUOTA if self.queue.reason == OVER_QUOTA else QUEUE_FULL
        if hop is None:
            self.reliable.hold(self.queue.slot, msg, None)  # from the next step
        if seq is None:
            return 0
        self._seq = (seq + 1) & 0xFFFF
        return seq

    def send(self, msg, hop, trickle_reset=False, replace=False):
        """Queue an own message for next hop hop; return the `TxQueue.offer` outcome.

        For mesh components (any port, explicit next hop); no check beyond
        the queue's. trickle_reset, replace: as `TxQueue.offer`.
        """
        outcome = self.queue.offer(msg, hop, True, trickle_reset, replace)
        if accepted(outcome):
            self._dirty = True
        else:
            self._count_refusal(None)
        return outcome

    # --- Extension points ------------------------------------------------------

    def heard(self, header, rssi, snr, now):
        """Reception step 4: a frame of a neighbour passed steps 1–3 (§9 link estimation).

        Records it in the neighbour table; returns its entry, or None when the
        table did not record it (stale counter, table full of parents).
        """
        return self.neighbours.heard(
            header.source, header.counter, header.rank, header.dodag_version, rssi, snr, now
        )

    def overheard(self, header, messages, now):
        """A frame for another link destination (passive acknowledgements)."""
        self.reliable.overheard(header, messages, now)

    def sent(self, sel, counter, now):
        """A frame goes on air: sel (`txqueue.Selection`, before its commit), counter."""
        self.reliable.sent(sel, counter, now)

    def give_up(self, sel, now):
        """MAX_ATTEMPTS busy senses for sel: drop its priority-2/3 reports (§8)."""
        self.counters.access_dropped += self.queue.drop(_is_report, sel)

    def expand(self, msg, header):
        """A message of a frame addressed to this node: undo first-hop compression (§11).

        A mode 6 message becomes mode 1, its origin the link source; routing
        restores the fields its ports compress (the presence report's parent).
        """
        frame.expand_first_hop(msg, header.source)

    def repeated(self, msg, header, now):
        """A class 1/2 message already in the duplicate cache (acknowledged, dropped)."""
        self.counters.duplicates += 1

    def forward(self, msg, header, now, replace=False):
        """A message for another node: queue a copy towards next_hop(msg).

        Returns `QUEUED`, `HELD` (class 1/2 without next hop: held, §12) or
        `NOT_TAKEN` (class 0 without next hop, or refused by the queue). A
        queued class 1/2 message is acknowledged explicitly unless it goes
        on air within half a timeout (`Reliable.owe`). replace: class 0
        replace by newer (§12: a queued message of the same origin and port
        gives its place to the copy; routing sets it for the mesh reports).
        """
        hop = self.next_hop(msg) if self.next_hop is not None else None
        if hop is None and not msg.cls:
            self.counters.no_route += 1
            return NOT_TAKEN
        copy = Message(
            msg.mode,
            msg.cls,
            msg.port,
            bytes(msg.payload),
            msg.origin,
            msg.dest,
            msg.seq,
            msg.route,
        )
        outcome = self.queue.offer(copy, BROADCAST if hop is None else hop, replace=replace)
        if not accepted(outcome):
            self._count_refusal(ROOT if msg.origin is None else msg.origin)
            return NOT_TAKEN
        self.counters.forwarded += 1
        if outcome == EVICTED:
            self.counters.evicted += 1
        if hop is None:
            self.reliable.hold(self.queue.slot, copy, now)
            return HELD
        if copy.cls:
            self.reliable.owe(self.queue.slot, copy, header.source, header.counter, now)
        # §7: a relay waits a random delay in [0, ToA(presence frame)]
        # before forwarding, so that relays (and the sender's next frame)
        # do not sense the end of one frame together.
        self.access_at(now + randbelow(self.random, self.slot_ms + 1))
        return QUEUED

    def access_at(self, time):
        """Start the transmit path at time at the latest (queued messages wait)."""
        if self._state == IDLE and (self._wake is None or time < self._wake):
            self._wake = time

    def tick(self, now):
        """Called at every step, before the transmit path (protocol timers)."""
        self.reliable.tick(now)

    def timer(self):
        """Return the time of the next protocol timer, or None."""
        return self.reliable.timer()

    # --- Core ----------------------------------------------------------------

    def handle(self, now, event):
        if event is not None:
            kind = event[0]
            if kind == RX:
                self._receive(now, event[1], event[2], event[3])
            elif kind == CHANNEL:
                self._channel(now, event[1])
            elif kind == TX_DONE:
                self._tx_done(now, event[1])
            elif kind == RX_ERROR:
                self.counters.rx_crc += 1
            else:
                raise ValueError("unknown event")
        self.tick(now)
        self._service(now)
        wake = self._wake
        t = self.timer()
        if t is not None and (wake is None or t < wake):
            wake = t
        self.set_deadline(wake)

    def _count_refusal(self, origin):
        c = self.counters
        if self.queue.reason == OVER_QUOTA:
            c.refused_quota += 1
        else:
            c.refused_full += 1
        if origin is not None:
            self.senders.add(origin, QUOTA)

    # --- Transmit path ----------------------------------------------------------

    def _service(self, now):
        state = self._state
        wake = self._wake
        if state == IDLE:
            if self._dirty or (wake is not None and now >= wake):
                self._access(now)
        elif state == BACKOFF and now >= wake:
            self._access(now)

    def _access(self, now):
        """Pack the next frame and sense the channel, or wait."""
        self._dirty = False
        self._wake = None
        self._state = IDLE
        if self.security.counter.exhausted:
            self._exhausted()
            return
        self._drop_refused(now)
        sel = self.queue.pack(now, self.dutycycle, self.limit, self._toa)
        if sel is None:
            self._attempts = 0
            self._cw = CW_MIN
            if self.queue.ready():
                self.counters.duty_waits += 1
                self._wake = self.dutycycle.next_release(now)
            return
        self._sel = sel
        self._attempts += 1
        self.counters.senses += 1
        self._state = SENSING
        self.emit(SENSE_ACTION)

    def _drop_refused(self, now):
        # A refused priority-2/3 report is dropped (§6); nothing to do while
        # the least allowance still takes a maximum frame.
        if self.dutycycle.allows(now, PRIORITY_LOW, self._toa(MAX_FRAME)):
            return
        self.counters.duty_dropped += self.queue.drop(self._refused)

    def _refused(self, msg, priority, local):
        if not _is_report(msg, priority, local):
            return False
        size = frame.OVERHEAD + 2 + frame.element_size(msg)
        return not self.dutycycle.allows(self.now, priority, self._toa(size))

    def _channel(self, now, busy):
        if self._state != SENSING:
            self.counters.unexpected += 1
            return
        if not busy:
            self._transmit(now)
            return
        c = self.counters
        c.busy += 1
        if self._attempts >= MAX_ATTEMPTS:
            c.give_ups += 1
            self.give_up(self._sel, now)
            self._attempts = 0
            window = CW_MAX  # retried later: a new procedure after one more backoff
            self._cw = CW_MIN
        else:
            c.backoffs += 1
            window = self._cw
            self._cw = window * 2 if window < CW_MAX else CW_MAX
        self._state = BACKOFF
        self._wake = now + self.slot_ms * (1 + randbelow(self.random, window))

    def _transmit(self, now):
        sel = self._sel
        h = self._txh
        h.dest = sel.dest
        h.counter = 0
        h.rank = self.rank
        h.dodag_version = self.dodag_version
        h.epoch = 0
        buf = self._txbuf
        n = frame.encode(buf, h, sel.messages, self.limit)
        counter = self.security.seal(buf, n)
        if counter is None:
            self._exhausted()
            self._state = IDLE
            return
        self.dutycycle.start(now, sel.toa_ms)
        c = self.counters
        c.tx_frames += 1
        for m in sel.messages:
            c.tx_messages[m.port] += 1
        self.sent(sel, counter, now)
        self.queue.commit(sel)
        self._attempts = 0
        self._cw = CW_MIN
        self._state = TRANSMITTING
        self.emit((TX, bytes(memoryview(buf)[:n])))

    def _tx_done(self, now, duration_ms):
        if self._state != TRANSMITTING:
            self.counters.unexpected += 1
            return
        self.dutycycle.done(now, duration_ms)
        self._state = IDLE
        self._dirty = True

    def _exhausted(self):
        # The board stops transmitting (§14): own class 1/2 messages fail.
        self.queue.drop(self._abandon)

    def _abandon(self, msg, priority, local):
        if local and msg.cls:
            self.fail(msg, EXHAUSTED)
        return True

    # --- Receive path -----------------------------------------------------------

    def _receive(self, now, data, rssi, snr):
        c = self.counters
        n = len(data)
        if n < frame.OVERHEAD or n > MAX_FRAME:
            c.rx_length += 1
            return
        buf = self._rxbuf
        buf[:n] = data
        h = self._rxh
        reason = self.security.receive(buf, n, h)  # steps 1-3
        if reason is not None:
            if reason == AUTHENTICATION:
                c.rx_auth += 1
                row = self.senders.add(h.source, AUTH)
                if self.security.replay.highest(h.source) is not None:
                    row[KNOWN] = 1
            elif reason == REPLAY:
                c.rx_replay += 1
                self.senders.add(h.source, REPLAYS)
            elif reason == frame.VERSION:
                c.rx_version += 1
            else:
                c.rx_length += 1
            return
        c.rx_frames += 1
        # Step 4. A frame from the own address is not a neighbour's: counted
        # and dropped before it reaches the neighbour table (§13).
        if h.source == self.address:
            c.duplicate_address += 1
            self.address_conflict = True
            return
        self.heard(h, rssi, snr, now)
        # Key switch on a frame under the next key (§14 key change) goes here.
        try:
            messages = frame.decode_messages(buf, frame.HEADER_SIZE, n - frame.TAG_SIZE)
        except FormatError:
            c.rx_malformed += 1
            return
        if h.dest != self.address and h.dest != BROADCAST:
            c.rx_overheard += 1
            self.overheard(h, messages, now)
            return
        rel = self.reliable
        ack = False
        refused = False
        for m in messages:
            c.rx_messages[m.port] += 1
            self.expand(m, h)
            key = identity(m, h.source, h.dest) if m.cls else None
            if key is None:
                self._accept(m, h, now)
                continue
            # Class 1/2 (§12): duplicate cache, then one explicit
            # acknowledgement for the frame unless forwarding acknowledges.
            if rel.duplicate(key, now):
                self.repeated(m, h, now)
                ack = True
                continue
            outcome = self._accept(m, h, now)
            if outcome == NOT_TAKEN:
                refused = True
                continue
            rel.remember(key, now)
            if outcome != QUEUED:
                ack = True
        if refused:
            # One entry would acknowledge the refused message too: none, and
            # no late one either (the sender retries the whole frame).
            rel.cancel_owed(h.source, h.counter)
            if ack:
                c.ack_withheld += 1
        elif ack:
            rel.acknowledge(h.source, h.counter, now)

    def _accept(self, m, h, now):
        """Deliver or forward a received message; return `DELIVERED`, `QUEUED`..."""
        mode = m.mode
        if mode == LINK_LOCAL:
            origin = h.source
        elif mode == UPWARD:
            if not self.is_root:
                return self.forward(m, h, now)
            origin = m.origin
        elif mode == DOWNWARD or mode == P2P:
            if m.dest != self.address:
                return self.forward(m, h, now)
            origin = ROOT if mode == DOWNWARD else m.origin
        elif mode == NODE_BROADCAST:
            origin = m.origin  # rebroadcast and duplicate filter: §9 broadcast
        else:
            origin = ROOT
        handler = self._handlers[m.port]
        if handler is None:
            self.counters.rx_no_handler += 1
            return DELIVERED
        self.counters.delivered += 1
        handler(origin, m, h)
        return DELIVERED


def open_link(
    address,
    key,
    storage,
    retained,
    random,
    profile,
    sf,
    next_hop=None,
    reroute=None,
    epoch=0,
    queue_size=SIZE,
    budget_ms=None,
    window_ms=None,
    bins=60,
):
    """Build the `LinkEngine` of a board from its platform (§6, §14).

    key: current frame key (16 B) of key epoch epoch; storage, retained,
    random: the `mesh.platform` objects (frame counter in storage; duty-cycle
    window, then replay table, in retained memory, restored when valid).
    budget_ms, window_ms: duty-cycle budget and window, from the profile by
    default (tests scale them down); next_hop, reroute: as `LinkEngine`.
    Build it once per boot.
    """
    if budget_ms is None:
        budget_ms = profile.budget_ms()
    if window_ms is None:
        window_ms = profile.window_ms()
    dc_at, replay_at = retained_layout(retained, dutycycle_size(bins), replay_size())
    security = Security(
        KeyRing(key, epoch),
        FrameCounter(storage, random),
        ReplayTable(retained, replay_at),
    )
    dutycycle = DutyCycle(budget_ms, window_ms, bins, retained, dc_at)
    return LinkEngine(
        address,
        security,
        dutycycle,
        TxQueue(queue_size),
        random,
        sf,
        profile,
        next_hop,
        reroute=reroute,
    )
