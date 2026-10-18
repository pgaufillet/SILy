"""Presence, topology and node information reports (MESH_DESIGN §11 ports 1, 2, 5; §16; R20, R24).

Codecs (little-endian; ValueError on a malformed payload):

- **Presence** (port 1, upward, class 0): preferred parent 2 B (mode 1
  only: on the first hop, mode 6, the parent is the link destination and the
  first relay restores it, `restore_parent`), flags 1 B, then if flag
  `TAILS`: tail count 1 B and per flow port 1 B, oldest unacknowledged
  sequence 2 B, last sequence 2 B (§12 class 2), then the **application
  extension**: the remaining bytes, opaque to the mesh. `Presence`,
  `encode_presence`, `decode_presence`.
- **Topology** (port 2, upward, class 0): count 1 B, then per neighbour
  address 2 B, margin 1 B (signed dB, saturated to ±127; -128 when no
  margin was measured), quality 1 B (bit 7 parent-set member, bits 6–0
  inbound delivery ratio 0–100 %). `encode_topology`, `decode_topology`:
  lists of (address, margin, member, ratio).
- **Node information** (port 5, upward, class 0): MAC 6 B, protocol version
  1 B, firmware version 2 B, name (remaining bytes, UTF-8, at most 16 B).
  `NodeInfo`, `encode_node_info`, `decode_node_info`.

**`Reporter`**: the reports of one node (not the root), plugged into its
link engine (``tick``, ``timer``: each wrapper calls the previous method)
and driven by its `mesh.dodag.Dodag`:

- **Presence** every presence period (``engine.neighbours.period_ms``, from
  the DODAG information, §9, §17), jittered by ±10 % (§7), and **at once
  after a change of preferred parent** (`parent_changed`, §9): after a
  random delay of at most `CHANGE_SLOTS` slots, so that the nodes that
  attach or switch on one frame (the children of a relay) do not transmit
  together; it also restarts the period. Sent only while attached, in mode 6 to the preferred
  parent (17 B with a 1-byte extension, §11 sizes), class 0 replace by
  newer (§12). Flags: bit 0 first report since boot; bit 1 queue overflow,
  bit 2 duty-cycle use above 80 % of the budget (seen by the link engine at
  each charge, ``duty_high``, or at the report), bit 4 rank errors (§9
  data-path validation), bit 5 address conflict, bit 6 authentication
  failures, each **since the last report**: kept until a presence carrying
  it goes on air, so that a presence refused or dropped (the duty-cycle
  allowance refuses presence above 80 %) passes its flags to the next one;
  bit 7 next key loaded (§14), the current state; bit 3 when the class 2
  tails hook returns tails.
- **Passive acknowledgement of presence** (§9, presence reports matched by
  origin): when a presence goes on air to a preferred parent other than the
  root, in a frame without class 1/2 message (those give the frame's sample,
  `mesh.reliable`), the node waits for the parent's forward of a presence of
  its origin, overheard: one sample of the parent's acknowledgement window
  (`NeighbourTable.acked`), a success when it is heard, a failure after the
  class 1 timeout towards a relay (`Reliable.timeout_ms`: from the end of
  the frame, a frame of the frame limit + 6 slots, which covers the relay's
  forwarding delay of at most one slot, §7). No retry; a newer presence on
  air replaces the wait.
- **Topology** every topology period, jittered by ±10 %, the first one at a
  random time within the first presence period after the first attachment;
  with the DODAG information flag *topology report on change* (deployment,
  §17), also at a presence time when the neighbour set or the parent set
  changed since the last one. Entries (``entries``, 6 by default): the
  parent set first, then the other neighbours by delivery ratio, margin,
  address.
- **Node information** with the first topology report, then with every
  `NODE_INFO_EVERY`-th one (when ``node_info`` is given).

Hooks given by the application: ``extension`` (function() -> bytes, at most
`MAX_EXTENSION`: SILy battery and mode, R23, read at each presence),
``tails`` (function() -> list of (port, oldest, last), the class 2 sender's
idle unacknowledged flows, §12; filled in by the class 2 step, None until
then), ``next_key`` (function() -> bool; by default, a key is loaded for the
other key epoch).

Readings where §11/§16/§17 are silent: the delay of the presence after a
parent change (an initial backoff window, §8), the margin of a link never measured
(-128), the order of the non-parent entries, the period of node
information (with every 8th topology report), "since the last report" read
as since the last presence on air ("first report since boot" included),
the timeout of the presence's passive acknowledgement (the class 1 one: a
forward later than that, behind priority-1 traffic or the relay's own
duty-cycle allowance, counts as a failure), presence and topology withheld
while detached (no route; the topology timer restarts at the next
attachment).
"""

from mesh.dodag import TOPOLOGY_ON_CHANGE
from mesh.frame import FIRST_HOP, PROTOCOL_VERSION, ROOT, UPWARD, Message
from mesh.link import DUTY_HIGH_PERCENT
from mesh.parents import PARENT_CHANGED
from mesh.platform import jittered, randbelow
from mesh.ports import NODE_INFO, PRESENCE, TOPOLOGY
from mesh.txqueue import accepted

# Presence flags (§11 port 1).
FIRST_REPORT = 0x01
QUEUE_OVERFLOW = 0x02
DUTY_HIGH = 0x04
TAILS = 0x08
RANK_ERRORS = 0x10
ADDRESS_CONFLICT = 0x20
AUTH_FAILURES = 0x40
NEXT_KEY = 0x80

TAIL_SIZE = 5  # port 1, oldest 2, last 2
MAX_TAILS = 8  # class 2 flows per node (§12 queues and resources)
MAX_EXTENSION = 16  # bytes of application extension accepted
TOPOLOGY_ENTRIES = 6  # entries per topology report by default (§11)
MAX_ENTRIES = 50  # entries a topology report may carry (fits any frame limit >= 214 B)
ENTRY_SIZE = 4
MEMBER = 0x80  # quality bit 7: parent-set member
MARGIN_UNKNOWN = -128
NODE_INFO_FIXED = 9  # MAC 6, protocol 1, firmware 2
NAME_MAX = 16
MAC_SIZE = 6
NODE_INFO_EVERY = 8  # topology reports per node information report
CHANGE_SLOTS = 4  # presence after a parent change: random delay in [0, 4 slots] (CW_MIN, §8)


def _u16(value, name):
    if type(value) is not int or value < 0 or value > 0xFFFF:
        raise ValueError(name + " out of range")


# --- Presence (port 1) ------------------------------------------------------------


class Presence:
    """A presence report: parent (None on the first hop), flags, tails, extension.

    flags: the flag byte (bit 3 `TAILS` is set by `encode_presence` from
    tails); tails: tuple of (port, oldest, last); extension: bytes.
    """

    def __init__(self, flags=0, extension=b"", parent=None, tails=()):
        self.parent = parent
        self.flags = flags
        self.tails = tuple(tails)
        self.extension = bytes(extension)

    def __eq__(self, other):
        return (
            isinstance(other, Presence)
            and self.parent == other.parent
            and self.flags == other.flags
            and self.tails == other.tails
            and self.extension == other.extension
        )

    def __repr__(self):
        return "Presence(parent=%r, flags=0x%02x, tails=%r, extension=%r)" % (
            self.parent,
            self.flags,
            self.tails,
            self.extension,
        )


def encode_presence(p):
    """Return the payload of `Presence` p (parent field only if p.parent is not None)."""
    if type(p.flags) is not int or p.flags < 0 or p.flags > 0xFF:
        raise ValueError("flags out of range")
    if len(p.tails) > MAX_TAILS:
        raise ValueError("too many tails")
    out = bytearray()
    if p.parent is not None:
        _u16(p.parent, "parent")
        out.append(p.parent & 0xFF)
        out.append(p.parent >> 8)
    flags = p.flags & ~TAILS
    if p.tails:
        flags |= TAILS
    out.append(flags)
    if p.tails:
        out.append(len(p.tails))
        for port, oldest, last in p.tails:
            if type(port) is not int or port < 0 or port > 255:
                raise ValueError("tail port out of range")
            _u16(oldest, "oldest")
            _u16(last, "last")
            out.append(port)
            out.append(oldest & 0xFF)
            out.append(oldest >> 8)
            out.append(last & 0xFF)
            out.append(last >> 8)
    out.extend(p.extension)
    return bytes(out)


def decode_presence(payload, parent=True):
    """Return the `Presence` of a port 1 payload; parent: the mode 1 form (parent field).

    Raises ValueError when the payload is too short for its fields or the
    tail count is 0 or above `MAX_TAILS`.
    """
    p = payload
    n = len(p)
    i = 0
    par = None
    if parent:
        if n < 3:
            raise ValueError("presence too short")
        par = p[0] | p[1] << 8
        i = 2
    elif n < 1:
        raise ValueError("presence too short")
    flags = p[i]
    i += 1
    tails = []
    if flags & TAILS:
        if i >= n:
            raise ValueError("tail count missing")
        count = p[i]
        i += 1
        if not count or count > MAX_TAILS or i + count * TAIL_SIZE > n:
            raise ValueError("bad tails")
        for _ in range(count):
            tails.append((p[i], p[i + 1] | p[i + 2] << 8, p[i + 3] | p[i + 4] << 8))
            i += TAIL_SIZE
    return Presence(flags, bytes(p[i:]), par, tails)


def restore_parent(payload, parent):
    """Return a first-hop presence payload with its parent field restored (§11)."""
    out = bytearray(2 + len(payload))
    out[0] = parent & 0xFF
    out[1] = parent >> 8
    out[2:] = payload
    return bytes(out)


# --- Topology (port 2) ------------------------------------------------------------


def margin_db(tenths):
    """Return a margin in tenths of dB as the signed byte of a report (rounded, saturated)."""
    if tenths is None:
        return MARGIN_UNKNOWN
    db = (tenths + 5) // 10
    return 127 if db > 127 else (-127 if db < -127 else db)


def encode_topology(entries):
    """Return the port 2 payload of entries: (address, margin dB, member, ratio %)."""
    if len(entries) > MAX_ENTRIES:
        raise ValueError("too many entries")
    out = bytearray()
    out.append(len(entries))
    for address, margin, member, ratio in entries:
        _u16(address, "address")
        if type(margin) is not int or margin < -128 or margin > 127:
            raise ValueError("margin out of range")
        if type(ratio) is not int or ratio < 0 or ratio > 100:
            raise ValueError("ratio out of range")
        out.append(address & 0xFF)
        out.append(address >> 8)
        out.append(margin & 0xFF)
        out.append(ratio | (MEMBER if member else 0))
    return bytes(out)


def decode_topology(payload):
    """Return [(address, margin dB, member, ratio %)] of a port 2 payload.

    Raises ValueError unless the length matches the count and every ratio
    is at most 100.
    """
    p = payload
    if not len(p) or len(p) != 1 + ENTRY_SIZE * p[0]:
        raise ValueError("bad topology length")
    out = []
    for i in range(1, len(p), ENTRY_SIZE):
        margin = p[i + 2]
        if margin > 127:
            margin -= 256
        ratio = p[i + 3] & 0x7F
        if ratio > 100:
            raise ValueError("bad ratio")
        out.append((p[i] | p[i + 1] << 8, margin, bool(p[i + 3] & MEMBER), ratio))
    return out


# --- Node information (port 5) ---------------------------------------------------------


class NodeInfo:
    """Node information: mac (6 B), firmware (16-bit version), name (str), protocol."""

    def __init__(self, mac, firmware=0, name="", protocol=PROTOCOL_VERSION):
        self.mac = bytes(mac)
        self.firmware = firmware
        self.name = name
        self.protocol = protocol

    def __eq__(self, other):
        return (
            isinstance(other, NodeInfo)
            and self.mac == other.mac
            and self.firmware == other.firmware
            and self.name == other.name
            and self.protocol == other.protocol
        )

    def __repr__(self):
        return "NodeInfo(%r, %d, %r, %d)" % (self.mac, self.firmware, self.name, self.protocol)


def encode_node_info(info):
    """Return the port 5 payload of `NodeInfo` info."""
    if len(info.mac) != MAC_SIZE:
        raise ValueError("bad MAC")
    _u16(info.firmware, "firmware")
    if type(info.protocol) is not int or info.protocol < 0 or info.protocol > 255:
        raise ValueError("protocol out of range")
    name = info.name.encode()
    if len(name) > NAME_MAX:
        raise ValueError("name too long")
    out = bytearray(info.mac)
    out.append(info.protocol)
    out.append(info.firmware & 0xFF)
    out.append(info.firmware >> 8)
    out.extend(name)
    return bytes(out)


def decode_node_info(payload):
    """Return the `NodeInfo` of a port 5 payload; ValueError for a bad length or name."""
    p = bytes(payload)
    n = len(p)
    if n < NODE_INFO_FIXED or n > NODE_INFO_FIXED + NAME_MAX:
        raise ValueError("bad node information length")
    try:
        name = p[NODE_INFO_FIXED:].decode()
    except UnicodeError:
        name = None
    if name is None:
        raise ValueError("bad name")
    return NodeInfo(p[:MAC_SIZE], p[7] | p[8] << 8, name, p[6])


# --- Reporter --------------------------------------------------------------------------


class Counters:
    """Report counters since boot (§13 local observability)."""

    def __init__(self):
        self.presence = 0  # presence reports queued
        self.topology = 0  # topology reports queued
        self.node_info = 0  # node information reports queued
        self.refused = 0  # reports the queue refused
        self.bad_extension = 0  # extensions above MAX_EXTENSION (sent without)
        self.presence_acked = 0  # presence forwards overheard (passive acknowledgement)
        self.presence_unacked = 0  # presence forwards not overheard in time


class Reporter:
    """Reports of one node (see the module docstring).

    engine: its `mesh.link.LinkEngine`; dodag: its `mesh.dodag.Dodag`;
    node_info: its `NodeInfo` or None; extension, tails, next_key: hooks;
    entries: topology entries per report.
    """

    def __init__(
        self, engine, dodag, node_info=None, extension=None, tails=None, next_key=None, entries=6
    ):
        if engine.is_root:
            raise ValueError("the root sends no report")
        self.engine = engine
        self.dodag = dodag
        self.node_info = encode_node_info(node_info) if node_info is not None else None
        self.extension = extension
        self.tails = tails
        self.next_key = next_key
        self.entries = entries
        self.counters = Counters()
        self.presence_at = None
        self.topology_at = None
        self._presence = None  # (slot, message) of the last presence queued
        self._sticky = FIRST_REPORT  # flags not yet carried by a presence on air
        self._carried = 0  # ... of them, those the queued presence carries
        self._probe_hop = None  # parent whose forward of the presence on air is awaited
        self._probe_due = None  # ... until (then a failed sample)
        self._rank_errors = 0
        self._auth = 0
        self._info_left = 0  # topology reports before the next node information
        self._signature = None  # neighbour and parent sets of the last topology report
        self._base_tick = engine.tick
        self._base_timer = engine.timer
        self._base_sent = engine.sent
        self._base_overheard = engine.overheard
        engine.tick = self._tick
        engine.timer = self._timer
        engine.sent = self._sent
        engine.overheard = self._overheard

    # --- Entry points ---------------------------------------------------------------

    def parent_changed(self, now):
        """The preferred parent changed: presence at once, period restarted (§9)."""
        if not self.dodag.attached:
            return
        e = self.engine
        self.presence_at = now + randbelow(e.random, CHANGE_SLOTS * e.slot_ms + 1)
        if self.topology_at is None:
            self.topology_at = now + randbelow(e.random, e.neighbours.period_ms)

    def changed(self, events, now):
        """`mesh.dodag.Dodag.changed` events: a new preferred parent sends a presence."""
        if events & PARENT_CHANGED:
            self.parent_changed(now)

    def flags(self, now):
        """Return the flags of a presence generated at now (and record what it reports)."""
        e = self.engine
        sticky = self._sticky
        if e.queue.overflow:
            sticky |= QUEUE_OVERFLOW
            e.queue.overflow = False
        errors = self.dodag.counters.rank_errors
        if errors != self._rank_errors:
            sticky |= RANK_ERRORS
            self._rank_errors = errors
        if e.address_conflict:
            sticky |= ADDRESS_CONFLICT
            e.address_conflict = False
        auth = e.counters.rx_auth
        if auth != self._auth:
            sticky |= AUTH_FAILURES
            self._auth = auth
        dc = e.dutycycle
        if e.duty_high or dc.used(now) * 100 > DUTY_HIGH_PERCENT * dc.budget_ms:
            sticky |= DUTY_HIGH
            e.duty_high = False
        self._sticky = sticky
        flags = sticky
        if self._next_key():
            flags |= NEXT_KEY
        return flags

    def _next_key(self):
        if self.next_key is not None:
            return self.next_key()
        keys = self.engine.security.keys
        return keys.cipher(1 - keys.epoch) is not None

    def presence(self, now):
        """Return the `Presence` (first-hop form) a report generated at now carries."""
        ext = b""
        if self.extension is not None:
            ext = self.extension()
            if len(ext) > MAX_EXTENSION:
                self.counters.bad_extension += 1
                ext = b""
        tails = ()
        if self.tails is not None:
            tails = self.tails() or ()
            if len(tails) > MAX_TAILS:
                tails = tails[:MAX_TAILS]
        return Presence(self.flags(now), ext, None, tails)

    def topology(self, now):
        """Return the entries of a topology report at now (parent set first)."""
        table = self.engine.neighbours
        table.expire(now)
        members = self.dodag.parents.members()
        out = []
        for a in members:
            n = table.get(a)
            if n is not None:
                out.append((a, margin_db(n.margin()), True, n.d_in()))
        others = []
        for n in table.entries():
            if n.address not in members:
                m = n.margin()
                others.append((-n.d_in(), -(m if m is not None else -10000), n.address))
        others.sort()
        for _, _, a in others:
            if len(out) >= self.entries:
                break
            n = table.get(a)
            out.append((a, margin_db(n.margin()), False, n.d_in()))
        return out[: self.entries]

    # --- Engine hooks -----------------------------------------------------------------

    def _tick(self, now):
        self._base_tick(now)
        if self._probe_due is not None and now >= self._probe_due:
            self._probe(False, now)
        attached = self.dodag.attached
        if self.presence_at is not None and now >= self.presence_at:
            if attached:
                self._send_presence(now)
                if self.dodag.info.flags & TOPOLOGY_ON_CHANGE:
                    if self._signature != self._sets():
                        self._send_topology(now)
                self.presence_at = now + jittered(
                    self.engine.neighbours.period_ms, self.engine.random
                )
            else:
                self.presence_at = None  # restarted by the next attachment
        if self.topology_at is not None and now >= self.topology_at:
            if attached:
                self._send_topology(now)
                self.topology_at = now + jittered(self.dodag.topology_ms, self.engine.random)
            else:
                self.topology_at = None

    def _timer(self):
        t = self._base_timer()
        for u in (self.presence_at, self.topology_at, self._probe_due):
            if u is not None and (t is None or u < t):
                t = u
        return t

    def _sent(self, sel, counter, now):
        self._base_sent(sel, counter, now)
        entry = self._presence
        if entry is None:
            return
        cls = 0
        on_air = False
        for m in sel.messages:
            cls |= m.cls
            if m is entry[1]:
                on_air = True
        if not on_air:
            return
        # Its flags are reported: those observed since stay for the next one.
        self._sticky &= ~self._carried
        self._carried = 0
        self._presence = None
        # Passive acknowledgement of the presence (§9): one sample when the
        # parent's forward is overheard or the timeout passes; none towards
        # the root (it forwards nothing) nor for a frame whose class 1/2
        # messages already give its sample (`mesh.reliable`).
        self._probe_hop = None
        self._probe_due = None
        hop = sel.dest
        if hop != ROOT and not cls:
            self._probe_hop = hop
            self._probe_due = now + sel.toa_ms + self.engine.reliable.timeout_ms(hop)

    def _overheard(self, header, messages, now):
        self._base_overheard(header, messages, now)
        if self._probe_due is None or header.source != self._probe_hop:
            return
        own = self.engine.address
        for m in messages:
            if m.port == PRESENCE and m.mode == UPWARD and m.origin == own:
                self._probe(True, now)
                return

    def _probe(self, ok, now):
        self.engine.neighbours.acked(self._probe_hop, ok, now)
        if ok:
            self.counters.presence_acked += 1
        else:
            self.counters.presence_unacked += 1
        self._probe_hop = None
        self._probe_due = None

    # --- Sending ----------------------------------------------------------------------

    def _send(self, port, payload):
        hop = self.dodag.parents.preferred
        if hop is None:
            return False
        msg = Message(FIRST_HOP, 0, port, payload)
        e = self.engine
        if not accepted(e.send(msg, hop, replace=True)):
            self.counters.refused += 1
            return False
        if port == PRESENCE:
            self._presence = (e.queue.slot, msg)
        return True

    def _send_presence(self, now):
        if self._send(PRESENCE, encode_presence(self.presence(now))):
            self._carried = self._sticky
            self.counters.presence += 1

    def _sets(self):
        table = self.engine.neighbours
        return (tuple(sorted(n.address for n in table.entries())), self.dodag.parents.members())

    def _send_topology(self, now):
        entries = self.topology(now)
        self._signature = self._sets()
        if self._send(TOPOLOGY, encode_topology(entries)):
            self.counters.topology += 1
        if self.node_info is not None:
            if not self._info_left and self._send(NODE_INFO, self.node_info):
                self.counters.node_info += 1
                self._info_left = NODE_INFO_EVERY
            if self._info_left:
                self._info_left -= 1
