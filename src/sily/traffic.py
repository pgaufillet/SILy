"""Test traffic: numbered class 1 messages from the nodes to the gateway.

A field measurement of the mesh before punches exist: each node whose
configuration enables it (``traffic`` section, ``sily.config``) submits a
class 1 message upward every ``period_s`` (each interval drawn within
``jitter_pct`` % of it, the first at a random phase once the node has
attached for the first time, so that nodes do not send in step) of
``size`` bytes on `PORT`, an application port at high priority as punches
will be (MESH_DESIGN §6, §11). The gateway counts what it receives, per
node. Runs on the board, on the unix port and on CPython (`min_period_s`
for ``tools/box.py``).

Payload, little-endian (`HEAD`): the node's boot number (low 16 bits, the
``b`` of its journal), the message number (32 bits, from 0 at each boot,
one per submission, refused or not), the node's clock at submission (ms
since its boot, 32 bits); zeros up to the size.

Node (`Sender`), totals since its boot: submissions (``n``), accepted
(``ok``), refused per reason (``ref``: the negative refusals of
``mesh.link.LinkEngine.submit``), failures after acceptance per reason
(``fail``: the failure callback on `PORT`). A refused message keeps its
number: the gateway counts it lost, the node's figures tell why.

Gateway (`Receiver`), per node, totals since the gateway's boot (`FIELDS`):

- delivery: ``received`` distinct messages against ``expected``, the span
  of the numbers received (lowest to highest) of each node boot, summed
  over its boots. Messages lost before the first or after the last one
  received of a boot are not seen (the node's own counts are). A message
  of a newer boot starts a new span (a reboot is neither losses nor
  duplicates); one of an older boot is counted ``stale`` and ignored;
- ``duplicates`` (a number received again), ``reordered`` (received after
  a higher number), ``late`` (more than `WINDOW` numbers behind the
  highest: duplicate or not cannot be told; counted apart, not received);
- latency above the best case: the boxes' clocks are not synchronised and
  no downward route lets a node time a round trip, so each message gives
  the gateway's reception time minus the node's submission time, which
  holds the unknown offset of the two clocks, and the figure is that value
  minus its smallest over the last `BASELINE` periods of the minute record
  for that node boot (the current one included). It measures the queueing,
  medium access, retries and holding above the fastest delivery seen, not
  the absolute delay; the window bounds the error of the clocks' relative
  drift (tens of ppm: tens of ms over 10 minutes). Per period of the
  minute record: samples, median, 90th percentile and maximum (ms,
  nearest rank) of the messages received in it (first receptions only);
- clock offset (``offset_t``, ``offset_ms``), per period of the minute
  record: of the messages received in it (first receptions), the smallest
  reception time minus submission time minus the time on air of the hops
  the gateway can tell (`Receiver.air`): the message's own frame when the
  frame's link source is its origin (one hop), that frame and a relay's
  otherwise (two hops or more); ``offset_ms`` is that value, the
  gateway's clock minus the node's plus what is left of the fastest
  delivery's delay (zero or more: the steps, sensing, waits and further
  hops), ``offset_t`` the node's clock (ms since its boot, the ``t`` of
  its journal) at that message's submission. An analysis maps the node's
  ``t`` to the gateway's (``tools/survey.py``: the lower envelope of the
  periods' values follows the clocks' drift). A period without sample
  gives none; a node's reboot drops the samples of its old boot in the
  period.

The gateway's port handler runs inside the mesh core's step: it reads the
three fields (the payload is valid during the call only), the payload's
length and whether the link source is the origin, and queues them; a task
does the accounting (`Receiver.drain`), and the minute record closes the
latency period (`Receiver.minute`).
"""

import asyncio
import struct

from mesh.airtime import time_on_air_ms, time_on_air_us
from mesh.frame import OVERHEAD, ROOT
from mesh.platform import randbelow
from sily.journal import Queue

PORT = 110  # high priority (16-127), apart from the bench's 100-103
HEAD = "<HII"  # boot, number, node time (ms)
HEAD_SIZE = 10  # also the size of a punch (MESH_DESIGN §11 sizes)
SIZE_MAX = 200  # mesh.link.MAX_PAYLOAD
# Bytes of a forwarded test message's frame beyond its payload (§11): header
# and tag, control, port, origin, sequence.
FRAME_EXTRA = OVERHEAD + 6
# The same on its first hop: no origin (first-hop compression, §11).
FIRST_EXTRA = FRAME_EXTRA - 2
# Configuration bound (`min_period_s`): the test traffic of LOAD_NODES nodes
# through one relay within SHARE_PERCENT of its duty-cycle budget, the rest
# left to retries, acknowledgements and the mesh's own traffic.
LOAD_NODES = 5
SHARE_PERCENT = 50

CHECK_MS = 1000  # sender's look at the attachment before its first message
DRAIN_MS = 200  # receiver task's look at its queue
QUEUE_ITEMS = 64
WINDOW = 1024  # numbers behind the highest told apart (duplicate, reordered)
BASELINE = 10  # periods of the minute record in the latency's minimum
SAMPLES_MAX = 128  # latency samples per node and period
ORIGINS_MAX = 64

# Columns of a node's row at the gateway (minute record ``tt.o``, `status`).
FIELDS = (
    "address",
    "boot",  # the node's boot of the current span
    "received",
    "expected",
    "duplicates",
    "reordered",
    "late",
    "stale",
    "boots",  # node boots seen
    "samples",  # latency samples of the last period
    "median_ms",  # latency above the best case, last period (None without samples)
    "p90_ms",
    "max_ms",
    "offset_t",  # node's clock at the submission of the period's smallest offset
    "offset_ms",  # that offset: reception - submission - time on air of the hops known
)


def frame_bytes(size):
    """Return the length of the frame of one forwarded test message of size bytes."""
    return FRAME_EXTRA + size


def first_frame_bytes(size):
    """Return the length of the frame of one test message of size bytes on its first hop."""
    return FIRST_EXTRA + size


def min_period_s(profile, sf, size, nodes=LOAD_NODES):
    """Return the shortest period (s) of test messages of size bytes at sf.

    The one that keeps the messages of nodes nodes, all through one relay,
    within `SHARE_PERCENT` of its duty-cycle budget (``mesh.profile``).
    """
    toa = time_on_air_ms(frame_bytes(size), sf, profile.bandwidth_hz, profile.coding_rate)
    share = profile.budget_ms() * SHARE_PERCENT // 100
    ms = (nodes * toa * profile.window_ms() + share - 1) // share
    return (ms + 999) // 1000


def _count(table, key):
    table[key] = table.get(key, 0) + 1


def _pairs(table):
    return [[key, table[key]] for key in sorted(table)]


def _rank(values, percent):
    """Return the nearest-rank percentile of sorted values."""
    return values[(percent * len(values) + 99) // 100 - 1]


class Sender:
    """A node's test traffic.

    submit(port, cls, dest, payload) -> sequence or negative refusal (the
    engine's, then a wake of the runner); rng: ``mesh.platform.Random``;
    boot: the node's boot number; period_ms, size (bytes), jitter_pct: as
    the configuration; port: `PORT`.
    """

    def __init__(self, submit, rng, boot, period_ms, size=HEAD_SIZE, jitter_pct=0, port=PORT):
        if size < HEAD_SIZE:
            raise ValueError("size below the header")
        self.submit = submit
        self.rng = rng
        self.boot = boot & 0xFFFF
        self.period_ms = period_ms
        self.spread = period_ms * jitter_pct // 100
        self.port = port
        self.size = size
        self.buf = bytearray(size)
        self.seq = 0  # number of the next message: messages submitted
        self.ok = 0
        self.refused = {}  # reason -> count
        self.failures = {}  # reason -> count
        self.next = None  # time of the next message; None before the first attachment

    def _interval(self):
        return self.period_ms - self.spread + randbelow(self.rng, 2 * self.spread + 1)

    def tick(self, now, attached):
        """Submit the message due at now (ms); return the ms until the next look.

        attached: whether the node is attached; the messages start at a
        random phase after the first attachment, then go on whatever the
        state (a detached node's messages are held, then fail).
        """
        if self.next is None:
            if not attached:
                return CHECK_MS
            self.next = now + randbelow(self.rng, self.period_ms)
        if now >= self.next:
            self.send(now)
            self.next += self._interval()
            if self.next <= now:  # late by more than an interval: no burst
                self.next = now + self._interval()
        return self.next - now

    def send(self, now):
        """Submit the next numbered message at now; return the submission's result."""
        struct.pack_into(HEAD, self.buf, 0, self.boot, self.seq & 0xFFFFFFFF, now & 0xFFFFFFFF)
        result = self.submit(self.port, 1, ROOT, bytes(self.buf))
        self.seq += 1
        if result >= 0:
            self.ok += 1
        else:
            _count(self.refused, result)
        return result

    def failed(self, dest, port, seq, reason):
        """Failure of an own message (the application's failure listener)."""
        if port == self.port:
            _count(self.failures, reason)

    def minute(self):
        """Return the ``tt`` field of the minute record (totals since boot)."""
        return {
            "n": self.seq,
            "ok": self.ok,
            "ref": _pairs(self.refused),
            "fail": _pairs(self.failures),
        }

    def status(self):
        """Return the node's figures for ``App.status``."""
        return {
            "role": "sender",
            "period_s": self.period_ms // 1000,
            "size": self.size,
            "submitted": self.seq,
            "accepted": self.ok,
            "refused": sum(self.refused.values()),
            "failed": sum(self.failures.values()),
            "refusals": _pairs(self.refused),
            "failures": _pairs(self.failures),
        }


class Origin:
    """The gateway's figures of one node (module documentation, `FIELDS`)."""

    def __init__(self, address):
        self.address = address
        self.boot = None
        self.lo = 0
        self.hi = 0
        self.bits = bytearray(WINDOW // 8)  # numbers received in (hi - WINDOW, hi]
        self.received = 0
        self.closed = 0  # expected of the earlier boots
        self.duplicates = 0
        self.reordered = 0
        self.late = 0
        self.stale = 0
        self.boots = 0
        self.samples = []  # reception minus submission time, this period
        self.minima = []  # smallest of each earlier period with samples
        self.latency = (0, None, None, None)  # samples, median, p90, max
        self.low = None  # smallest offset of this period (module documentation)
        self.low_t = None  # node's clock at the submission of its message
        self.offset = (None, None)  # offset_t, offset_ms of the last period

    def expected(self):
        if self.boot is None:
            return self.closed
        return self.closed + self.hi - self.lo + 1

    def _bit(self, seq):
        return self.bits[(seq % WINDOW) >> 3] & (1 << (seq & 7))

    def _set(self, seq):
        self.bits[(seq % WINDOW) >> 3] |= 1 << (seq & 7)

    def _clear(self, seq):
        self.bits[(seq % WINDOW) >> 3] &= ~(1 << (seq & 7)) & 0xFF

    def _start(self, boot, seq):
        self.closed = self.expected()
        self.boot = boot
        self.lo = seq
        self.hi = seq
        bits = self.bits
        for i in range(len(bits)):
            bits[i] = 0
        self._set(seq)
        self.received += 1
        self.boots += 1
        self.samples = []
        self.minima = []
        self.low = None
        self.low_t = None

    def _number(self, seq):
        """Account number seq of the current boot; return True on a first reception."""
        hi = self.hi
        if seq > hi:
            if seq - hi >= WINDOW:
                bits = self.bits
                for i in range(len(bits)):
                    bits[i] = 0
            else:
                for s in range(hi + 1, seq + 1):
                    self._clear(s)
            self._set(seq)
            self.hi = seq
            self.received += 1
            return True
        if hi - seq >= WINDOW:
            self.late += 1
            return False
        if self._bit(seq):
            self.duplicates += 1
            return False
        self._set(seq)
        self.received += 1
        self.reordered += 1
        if seq < self.lo:
            self.lo = seq
        return True

    def account(self, t, boot, seq, sent, air=0):
        """Account message seq of boot, submitted at sent (node ms), received at
        t; air: time on air (ms) of the hops known."""
        if self.boot is None or boot != self.boot:
            if self.boot is not None and (boot - self.boot) & 0xFFFF >= 0x8000:
                self.stale += 1
                return
            self._start(boot, seq)
        elif not self._number(seq):
            return
        d = t - sent
        if len(self.samples) < SAMPLES_MAX:
            self.samples.append(d)
        d -= air
        if self.low is None or d < self.low:
            self.low = d
            self.low_t = sent

    def close(self):
        """End the latency period: compute its figures, start the next one."""
        self.offset = (self.low_t, self.low)
        self.low = None
        self.low_t = None
        samples = self.samples
        if not samples:
            self.latency = (0, None, None, None)
            return
        low = min(samples)
        base = low
        for m in self.minima:
            if m < base:
                base = m
        self.minima.append(low)
        if len(self.minima) >= BASELINE:
            self.minima.pop(0)
        lat = sorted([d - base for d in samples])
        self.latency = (len(lat), _rank(lat, 50), _rank(lat, 90), lat[-1])
        self.samples = []

    def row(self):
        """Return the node's row, columns `FIELDS`."""
        row = [
            self.address,
            self.boot,
            self.received,
            self.expected(),
            self.duplicates,
            self.reordered,
            self.late,
            self.stale,
            self.boots,
        ]
        row.extend(self.latency)
        row.extend(self.offset)
        return row


class Receiver:
    """The gateway's accounting of the test traffic. clock: function() -> ms;
    sf, profile (``mesh.profile``): the network's, for the time on air of
    the clock offset (`air`); None: no time on air taken off."""

    def __init__(self, clock, capacity=QUEUE_ITEMS, sf=None, profile=None):
        self.clock = clock
        self.queue = Queue(capacity)
        self.origins = {}  # address -> Origin
        self.malformed = 0  # payloads shorter than the header
        self.overflow = 0  # messages of nodes beyond ORIGINS_MAX
        self.sf = sf
        self.profile = profile
        self._air_size = None  # payload size of the last `air` computed
        self._air = (0, 0)  # its (one hop, two hops) times on air, ms

    def handler(self, origin, msg, header):
        """Port handler (``LinkEngine.bind``), inside the mesh's step: queue only."""
        payload = msg.payload
        size = len(payload)
        if size < HEAD_SIZE:
            self.malformed += 1
            return
        boot, seq, sent = struct.unpack_from(HEAD, payload, 0)
        self.queue.put((self.clock(), origin, boot, seq, sent, size, header.source == origin))

    def air(self, size, direct):
        """Return the time on air (ms, rounded down) of the hops known of a test
        message of size bytes: its first-hop frame when direct (the link source
        is the origin), that frame and a relay's (forwarded frame) otherwise.

        The last size's figures are kept (one size per network): computed
        again only when the size changes.
        """
        if self.sf is None:
            return 0
        if size != self._air_size:
            p = self.profile
            one = time_on_air_us(first_frame_bytes(size), self.sf, p.bandwidth_hz, p.coding_rate)
            two = one + time_on_air_us(frame_bytes(size), self.sf, p.bandwidth_hz, p.coding_rate)
            self._air = (one // 1000, two // 1000)
            self._air_size = size
        return self._air[0] if direct else self._air[1]

    def account(self, item):
        """Account one queued item (t, origin, boot, seq, sent, size, direct)."""
        origin = self.origins.get(item[1])
        if origin is None:
            if len(self.origins) >= ORIGINS_MAX:
                self.overflow += 1
                return
            origin = Origin(item[1])
            self.origins[item[1]] = origin
        origin.account(item[0], item[2], item[3], item[4], self.air(item[5], item[6]))

    def drain(self):
        """Account every queued item; return how many."""
        n = 0
        queue = self.queue
        while len(queue):
            self.account(queue.get())
            n += 1
        return n

    def rows(self):
        return [self.origins[a].row() for a in sorted(self.origins)]

    def minute(self):
        """Close the latency period; return the ``tt`` field of the minute
        record, None while nothing came."""
        self.drain()
        for origin in self.origins.values():
            origin.close()
        lost = self.queue.overflow
        if not self.origins and not (self.malformed or self.overflow or lost):
            return None
        return {"o": self.rows(), "mal": self.malformed, "q": lost, "ovf": self.overflow}

    def status(self):
        """Return the per-node figures for ``App.status`` (latency: the last period)."""
        nodes = []
        for row in self.rows():
            node = dict(zip(FIELDS, row))
            node["delivery"] = row[2] / row[3] if row[3] else None
            nodes.append(node)
        return {
            "role": "receiver",
            "nodes": nodes,
            "malformed": self.malformed,
            "items_lost": self.queue.overflow,
            "nodes_beyond": self.overflow,
        }


async def send(sender, clock, attached):
    """Task of a node's test traffic: clock() -> ms, attached() -> bool."""
    while True:
        await asyncio.sleep_ms(sender.tick(clock(), attached()))


async def receive(receiver):
    """Task of the gateway's accounting, one item at a time (other tasks between)."""
    queue = receiver.queue
    while True:
        while len(queue):
            receiver.account(queue.get())
            await asyncio.sleep_ms(0)
        await asyncio.sleep_ms(DRAIN_MS)
