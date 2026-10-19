"""Scripted medium: the radio of many simulated nodes on one channel.

No propagation model (MP23): links are scripted. A **link** is directed
(source -> destination) with a loss probability, an RSSI and an SNR, so that
asymmetric links are two links with different parameters. A node hears a
transmission only through an up link; links are cut and restored at will
(partitions), typically from scheduled calls.

Model, per transmission of ``airtime_ms(len(frame))`` ms over [start, end):

- **Reception**: each node with an up link from the sender receives over
  the same interval, with the link's RSSI and SNR taken at the start. The
  frame is delivered at the end as ``(RX, frame, rssi, snr)`` unless lost,
  for the first applicable cause: half duplex, collision, link cut before
  the end, link loss.
- **Half duplex**: a node does not receive while it transmits, whether the
  frame started before its own transmission or during it.
- **Collisions**: two receptions overlapping at one node are both lost. With
  ``capture_db`` set, the stronger one survives when its RSSI exceeds the
  other's by at least ``capture_db`` dB, whatever their order (a
  simplification: real capture also depends on timing and SF); comparisons are
  pairwise.
- **Loss**: one draw per reception, made at its start whatever happens next,
  so that a link's loss pattern depends only on the seed and the order of
  transmissions. A lost frame still occupies the receiver (collisions,
  carrier sense): loss stands for a failed demodulation, not for silence.
- **Carrier sense**: ``SENSE`` answers busy when a transmission audible at the
  sensing node (up link at its start) is in progress at the time of the
  request; the answer comes ``sense_ms`` later. Any audible frame is detected,
  as preamble detection does on the board (§8); RSSI thresholds are not
  modelled.
- **Airtime**: ``TX_DONE`` reports the computed time on air; per-node
  statistics give the time spent transmitting and the time the channel was
  heard busy (`Stats`).

Events reach nodes through the scheduler, never re-entrantly. A transmission
or carrier sense requested while the node transmits raises RuntimeError (a
core bug: the contract forbids it).
"""

from mesh.radio import CHANNEL, MAX_FRAME, RX, TX_DONE, Radio

OK = "ok"
LOSS = "loss"
COLLISION = "collision"
HALF_DUPLEX = "half-duplex"
CUT = "cut"


class Link:
    """Directed link parameters: loss probability, RSSI (dBm), SNR (dB)."""

    __slots__ = ("loss", "rssi", "snr", "up")

    def __init__(self, loss, rssi, snr):
        self.loss = loss
        self.rssi = rssi
        self.snr = snr
        self.up = True


class Stats:
    """Per-node counters; times in ms."""

    __slots__ = (
        "tx_count",
        "tx_ms",
        "busy_ms",
        "rx_ok",
        "rx_loss",
        "rx_collision",
        "rx_half_duplex",
        "rx_cut",
        "sense_count",
        "sense_busy",
    )

    def __init__(self):
        self.tx_count = 0
        self.tx_ms = 0  # time spent transmitting
        self.busy_ms = 0  # time with at least one audible transmission
        self.rx_ok = 0
        self.rx_loss = 0
        self.rx_collision = 0
        self.rx_half_duplex = 0
        self.rx_cut = 0
        self.sense_count = 0
        self.sense_busy = 0


class _Transmission:
    __slots__ = ("src", "frame", "start", "end", "receptions")

    def __init__(self, src, frame, start, end):
        self.src = src
        self.frame = frame
        self.start = start
        self.end = end
        self.receptions = []


class _Reception:
    __slots__ = ("port", "end", "rssi", "snr", "lost", "fate")

    def __init__(self, port, end, link, lost):
        self.port = port
        self.end = end
        self.rssi = link.rssi
        self.snr = link.snr
        self.lost = lost  # link loss draw
        self.fate = None  # cause of loss decided before the end, if any


class Port(Radio):
    """A node's radio on the medium (implements `mesh.radio.Radio`)."""

    def __init__(self, medium, address):
        self.medium = medium
        self.address = address
        self.sink = None
        self.stats = Stats()
        self._tx = None  # transmission in progress
        self._rx = []  # receptions not yet ended
        self._busy_since = None

    def transmit(self, frame):
        self.medium._transmit(self, frame)

    def sense(self):
        self.medium._sense(self)

    def _transmitting(self, now):
        return self._tx is not None and self._tx.end > now

    def _hears(self, now):
        for r in self._rx:
            if r.end > now:
                return True
        return False


class Medium:
    """One channel shared by the ports attached to it.

    scheduler: `sim.scheduler.Scheduler`; airtime_ms(frame_len): time on air
    in ms (> 0); rng: generator for loss draws (`sim.prng.XorShift32`);
    capture_db: capture threshold (None: no capture); sense_ms: carrier sense
    latency; trace: keep `trace`, a list of (end time, source, destination,
    outcome) per reception, outcome being OK, LOSS, COLLISION, HALF_DUPLEX or
    CUT.
    """

    def __init__(self, scheduler, airtime_ms, rng, capture_db=None, sense_ms=0, trace=False):
        self.scheduler = scheduler
        self.airtime_ms = airtime_ms
        self.rng = rng
        self.capture_db = capture_db
        self.sense_ms = sense_ms
        self.trace = [] if trace else None
        self.ports = {}
        self._order = []  # ports in attach order: deterministic iteration
        self._links = {}  # (src, dst) -> Link
        self._out = {}  # src -> [(dst port, Link)] in attach order

    # Topology.

    def attach(self, address):
        """Add a node; return its `Port`."""
        if address in self.ports:
            raise ValueError("address already attached")
        port = Port(self, address)
        self.ports[address] = port
        self._order.append(port)
        return port

    def link(self, src, dst, loss=0.0, rssi=-80, snr=10):
        """Create or replace the directed link src -> dst (up)."""
        if not 0.0 <= loss <= 1.0:
            raise ValueError("loss out of range")
        for address in (src, dst):
            if address not in self.ports:
                raise ValueError("unknown address")
        if src == dst:
            raise ValueError("self link")
        links = self._links
        links[(src, dst)] = Link(loss, rssi, snr)
        self._out[src] = [
            (p, links[(src, p.address)]) for p in self._order if (src, p.address) in links
        ]

    def link_both(self, a, b, loss=0.0, rssi=-80, snr=10):
        """Create the links a -> b and b -> a with the same parameters."""
        self.link(a, b, loss, rssi, snr)
        self.link(b, a, loss, rssi, snr)

    def get_link(self, src, dst):
        """Return the `Link` src -> dst, or None."""
        return self._links.get((src, dst))

    def cut(self, src, dst):
        """Cut the directed link src -> dst (kept for restore)."""
        self._links[(src, dst)].up = False

    def restore(self, src, dst):
        """Restore the directed link src -> dst."""
        self._links[(src, dst)].up = True

    def partition(self, group_a, group_b):
        """Cut every existing link between the two groups, both ways."""
        self._set_between(group_a, group_b, False)

    def heal(self, group_a, group_b):
        """Restore every existing link between the two groups, both ways."""
        self._set_between(group_a, group_b, True)

    def _set_between(self, group_a, group_b, up):
        for a in group_a:
            for b in group_b:
                for key in ((a, b), (b, a)):
                    link = self._links.get(key)
                    if link is not None:
                        link.up = up

    def stats(self, address):
        """Return the `Stats` of a node."""
        return self.ports[address].stats

    # Radio operations.

    def _post(self, port, event, delay=0):
        self.scheduler.after(delay, self._deliver, port, event)

    def _deliver(self, port, event):
        if port.sink is not None:
            port.sink(event)

    def _transmit(self, port, frame):
        now = self.scheduler.now
        if port._transmitting(now):
            raise RuntimeError("transmit while transmitting")
        if not 0 < len(frame) <= MAX_FRAME:
            raise ValueError("frame length")
        duration = self.airtime_ms(len(frame))
        if duration <= 0:
            raise ValueError("airtime must be positive")
        tx = _Transmission(port.address, bytes(frame), now, now + duration)
        port._tx = tx
        port.stats.tx_count += 1
        port.stats.tx_ms += duration
        for r in port._rx:  # half duplex: receptions in progress are lost
            if r.end > now and r.fate is None:
                r.fate = HALF_DUPLEX
        for dst, link in self._out.get(port.address, ()):
            if not link.up:
                continue
            r = _Reception(dst, tx.end, link, self.rng.chance(link.loss))
            if dst._transmitting(now):
                r.fate = HALF_DUPLEX
            for other in dst._rx:
                if other.end > now:
                    self._collide(r, other)
            dst._rx.append(r)
            if dst._busy_since is None:
                dst._busy_since = now
            tx.receptions.append(r)
        self.scheduler.at(tx.end, self._end, port, tx)

    def _collide(self, new, old):
        capture = self.capture_db
        if capture is not None and old.rssi - new.rssi >= capture:
            losers = (new,)
        elif capture is not None and new.rssi - old.rssi >= capture:
            losers = (old,)
        else:
            losers = (new, old)
        for r in losers:
            if r.fate is None:
                r.fate = COLLISION

    def _end(self, port, tx):
        now = self.scheduler.now
        if port._tx is tx:
            port._tx = None
        self._post(port, (TX_DONE, tx.end - tx.start))
        for r in tx.receptions:
            dst = r.port
            dst._rx.remove(r)
            if dst._busy_since is not None and not dst._hears(now):
                dst.stats.busy_ms += now - dst._busy_since
                dst._busy_since = None
            fate = r.fate
            if fate is None:
                link = self._links.get((tx.src, dst.address))
                if not link.up:
                    fate = CUT
                elif r.lost:
                    fate = LOSS
                else:
                    fate = OK
            stats = dst.stats
            if fate == OK:
                stats.rx_ok += 1
                self._post(dst, (RX, tx.frame, r.rssi, r.snr))
            elif fate == LOSS:
                stats.rx_loss += 1
            elif fate == COLLISION:
                stats.rx_collision += 1
            elif fate == HALF_DUPLEX:
                stats.rx_half_duplex += 1
            else:
                stats.rx_cut += 1
            if self.trace is not None:
                self.trace.append((now, tx.src, dst.address, fate))

    def _sense(self, port):
        now = self.scheduler.now
        if port._transmitting(now):
            raise RuntimeError("sense while transmitting")
        busy = port._hears(now)
        port.stats.sense_count += 1
        if busy:
            port.stats.sense_busy += 1
        self._post(port, (CHANNEL, busy), self.sense_ms)
