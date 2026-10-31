"""Neighbour table and link estimation (MESH_DESIGN §9 link estimation; MP12, MP13).

One `Neighbour` per link source heard (64 at most by default, MP13), fed by
the link engine's `heard` step with every authenticated frame, whatever its
link destination (the channel is shared, §9). Per neighbour:

- **rank and DODAG version** of its last frame, **time last heard**;
- **inbound delivery ratio** ``d_in``: frames received / counter values
  spent by the neighbour over its last 16 frame counter values (a 16-bit
  window shifted by the counter gap). A gap above 16 (long silence, reboot)
  restarts the window with the frame alone. A frame whose counter is not
  above the last one (possible only when the replay table has evicted the
  sender, §14) is ignored by the table;
- **acknowledgement window** ``p_ack``: acknowledged / sent over the last 16
  samples of the link, one per frame sent through this neighbour (class 1
  explicit or passive acknowledgement or timeout, passive acknowledgement of
  a presence report; `NeighbourTable.acked`). **Failure ageing**: once the
  newest failure (``fail_at``) is `silence_ms` old (2.5 presence periods),
  the failures leave the window and its successes are kept (`expire`), so
  that a link that failed during a cut and is heard again does not stay
  above ETX 4 for ever;
- **ETX** in units of 1/`ETX_ONE`: ``1 / p_ack`` once the link has
  `ACK_MIN` (4) acknowledgement samples (the link is used: both directions
  tested), ``1 / d_in²`` before (symmetry assumed). **Latch**: once the
  entry has held `ACK_MIN` samples, ``p_ack`` is used whenever the window
  is not empty (after an ageing too), so that the estimate does not jump
  between ``d_in`` and ``p_ack`` when a failure ages out; a new entry
  clears it. With samples but no success, ``p_ack`` = 0 gives ETX =
  samples + 1 (as if the next one succeeded) rather than infinity, so that
  one lost acknowledgement does not exclude a parent at once;
- **margin**: link SNR above the demodulation limit of the SF (§5), in
  tenths of dB, averaged over its last 8 frames (fewer before 8). The link
  SNR is the packet SNR, or RSSI minus the noise floor at SNR >= +5 dB when
  a noise floor is known (``noise_floor``), since the SX1262 SNR saturates;
- **eligibility** as a parent link: margin >= 3 dB and ETX <= 4, except
  the links judged by their reachability instead (below): the link to the
  root, and the preferred parent's while it is the node's last candidate
  (``reach``, decided by `mesh.parents`). The margin has a hysteresis
  (MP12): once below 3 dB, it must reach 6 dB again. A new neighbour
  starts on the eligible side;
- **reachability** (RPL neighbour unreachability detection style): a link
  judged by it is not tested against ETX <= 4 but is ineligible while
  **unreachable**: its last `UNREACH_RUN` (4) failed samples are
  consecutive (``run``: failed samples since the last acknowledgement, the
  samples of the acknowledgement window, so that one lost batch of the
  root counts once) and no acknowledgement of the neighbour came for one
  silence limit (``answered``: time of the last one, or of the entry's
  creation). Any acknowledgement of the neighbour ends the run: a success
  sample (explicit, or passive: the node overhears the neighbour forward
  its message), or one that gives no sample (`NeighbourTable.answered`: an
  explicit entry for a frame already sampled, a late one of the root, a
  passive one of a frame already sampled); beacons and other frames do
  not. The node's class 1 transmissions towards the neighbour are the
  probes. Failure ageing ends the run too (its failures left the window:
  the link gets `UNREACH_RUN` more failed samples), ``answered`` stays: a
  node that stopped sending to the neighbour reads it reachable again a
  silence limit after its last failure, as under the ETX rule.
  Reachability is read at selection time (`eligible` with the time and the
  silence limit); it restarts, as at the entry's creation, when routing
  takes a relay as its preferred parent (`restart_reach`). The silence
  limit makes a parent that answers now and then, under collisions of its
  acknowledgements or of the node's frames (hidden terminals), stay
  eligible whatever its ETX; the run makes a parent heard without fresh
  attempts towards it stay eligible. **Cost cap**: the ETX of a link
  judged by reachability used for its link cost (`link_etx`) is at most 4,
  so that a reachable parent above ETX 4 keeps a link cost of 32, within
  the bound L + 56;
- **parent flag**, set by routing: a parent is never evicted.

Bounds (MP13): the entries are allocated at construction and reused. A new
neighbour in a full table evicts the stalest entry that is not a parent; if
every entry is a parent the frame is not recorded (``refused``). An entry
not heard for 4 presence periods is removed by `expire` (also run by
`heard`), which also ages the failures: readers call ``expire(now)`` before
reading. Entries keep their
slot while they live, so that a slot index is a stable offset for paged
reading (§13 management query).

**Silence limit** (`silence_ms`, §9): 2.5 presence periods (strict: a
neighbour heard less than that ago is not silent). Parents use it for
candidates and silent members (`mesh.parents`, `mesh.dodag`), the table for
failure ageing. With a periodic sender jittered by ±10 % it tolerates one
lost frame while the on-air delay of its frames varies by up to T/4, and
detects two lost frames when that variation stays below T/5 (§9).

Integer arithmetic only; the presence period is injected (``period_ms``,
§16 default of the SF until the DODAG information sets it, §9).
"""

from array import array

from mesh.airtime import SF_MIN, snr_limit_x10
from mesh.frame import ROOT

CAPACITY = 64  # entries (MP13)
WINDOW = 16  # counter values of the reception window, messages of p_ack
MARGIN_FRAMES = 8  # frames of the margin average
EXPIRY_PERIODS = 4  # presence periods without a frame before removal
SILENCE_HALVES = 5  # silence limit in half presence periods (2.5 periods, §9)
ACK_MIN = 4  # acknowledgement samples before p_ack replaces d_in (then latched)
ETX_ONE = 128  # fixed-point unit of the ETX
ETX_MAX = 4 * ETX_ONE  # eligibility limit (ETX <= 4); cost cap of a link judged by reachability
UNREACH_RUN = 4  # consecutive failed samples of an unreachable link (with a silence limit)
MARGIN_LOW = 30  # tenths of dB: an eligible link becomes ineligible below
MARGIN_HIGH = 60  # tenths of dB: an ineligible link becomes eligible again at
SNR_SATURATION = 5  # dB: above, the packet SNR saturates (§5)
ASYMMETRY_POINTS = 30  # inbound ratios of the two ends differing by more: asymmetric

# Presence period (s) per SF 7-12, SILy profile (§16), until the DODAG
# information carries the network's (§9).
PRESENCE_S = (30, 30, 60, 120, 300, 600)

_MASK = (1 << WINDOW) - 1
_INT16 = 32767


def presence_ms(sf):
    """Return the default presence period of sf in ms (§16)."""
    return PRESENCE_S[sf - SF_MIN] * 1000


def silence_ms(period_ms):
    """Return the silence limit of a presence period: 2.5 periods (ms, §9)."""
    return SILENCE_HALVES * period_ms // 2


def popcount16(x):
    """Return the number of bits set in a 16-bit integer."""
    x = x - ((x >> 1) & 0x5555)
    x = (x & 0x3333) + ((x >> 2) & 0x3333)
    x = (x + (x >> 4)) & 0x0F0F
    return (x + (x >> 8)) & 0x1F


def asymmetric(ratio_ab, ratio_ba):
    """Return True if the inbound ratios (%) of a link's two ends differ by > 30 points.

    ratio_ab: d_in of a as heard by b, ratio_ba: of b as heard by a (each end
    reports its own inbound ratio, MP12).
    """
    return abs(ratio_ab - ratio_ba) > ASYMMETRY_POINTS


class Neighbour:
    """One entry of the table; read its attributes, change it through the table.

    address; heard (ms, last frame); rank, version (of its last frame);
    counter (last frame counter); parent (set by routing); rx_bits,
    rx_span (reception window); ack_bits, ack_count (acknowledgement
    window), fail_at (ms, newest failure still in it, or None), latched
    (`ACK_MIN` samples reached once); run (failed samples since the last
    acknowledgement or ageing, saturated at `UNREACH_RUN`), answered (ms,
    last acknowledgement, or creation of the entry); margin_ok (hysteresis
    state).
    """

    def __init__(self):
        self.margins = array("h", [0] * MARGIN_FRAMES)
        self.address = None
        self.reset(None, 0, 0, 0, 0)

    def reset(self, address, counter, rank, version, now):
        self.address = address
        self.heard = now
        self.rank = rank
        self.version = version
        self.counter = counter
        self.parent = False
        self.rx_bits = 1
        self.rx_span = 1
        self.ack_bits = 0
        self.ack_count = 0
        self.fail_at = None
        self.latched = False
        self.run = 0
        self.answered = now
        self.m_count = 0
        self.m_next = 0
        self.m_sum = 0
        self.margin_ok = True

    # --- Inbound delivery ratio ----------------------------------------------

    def received(self):
        """Return the frames received over the reception window."""
        return popcount16(self.rx_bits)

    def d_in(self):
        """Return d_in in percent (0-100, rounded)."""
        span = self.rx_span
        return (popcount16(self.rx_bits) * 100 + span // 2) // span

    def _counter(self, counter):
        gap = counter - self.counter
        if gap > WINDOW:
            self.rx_bits = 1
            self.rx_span = 1
        else:
            self.rx_bits = ((self.rx_bits << gap) | 1) & _MASK
            span = self.rx_span + gap
            self.rx_span = span if span < WINDOW else WINDOW
        self.counter = counter

    # --- Acknowledgement window ------------------------------------------------

    def acknowledged(self):
        """Return the acknowledged messages over the acknowledgement window."""
        return popcount16(self.ack_bits)

    def p_ack(self):
        """Return p_ack in percent (0-100, rounded), None without samples."""
        n = self.ack_count
        if not n:
            return None
        return (popcount16(self.ack_bits) * 100 + n // 2) // n

    def _ack(self, ok, now):
        self.ack_bits = ((self.ack_bits << 1) | (1 if ok else 0)) & _MASK
        if self.ack_count < WINDOW:
            self.ack_count += 1
        if self.ack_count >= ACK_MIN:
            self.latched = True
        if ok:
            self._answered(now)
        else:
            self.fail_at = now
            if self.run < UNREACH_RUN:
                self.run += 1

    def _answered(self, now):
        # An acknowledgement of the neighbour: ends the run of failed samples.
        self.run = 0
        self.answered = now

    def _age(self):
        # Failures removed, successes kept (as the newest samples); the run,
        # counted over the same failed samples, ends with them.
        n = popcount16(self.ack_bits)
        self.ack_count = n
        self.ack_bits = (1 << n) - 1
        self.fail_at = None
        self.run = 0

    # --- ETX, margin, eligibility ----------------------------------------------

    def etx(self):
        """Return the ETX in units of 1/ETX_ONE (rounded), per §9."""
        n = self.ack_count
        if n >= ACK_MIN or (n and self.latched):
            acked = popcount16(self.ack_bits)
            if not acked:
                return (n + 1) * ETX_ONE
            return (n * ETX_ONE + acked // 2) // acked
        received = popcount16(self.rx_bits)
        span = self.rx_span
        r2 = received * received
        return (span * span * ETX_ONE + r2 // 2) // r2

    def margin(self):
        """Return the margin averaged over the last 8 frames, tenths of dB; None if none."""
        n = self.m_count
        if not n:
            return None
        return (2 * self.m_sum + n) // (2 * n)

    def last_margin(self):
        """Return the margin of the last frame with a measurement, tenths of dB."""
        if not self.m_count:
            return None
        return self.margins[(self.m_next - 1) % MARGIN_FRAMES]

    def _margin(self, sample):
        if sample > _INT16:
            sample = _INT16
        elif sample < -_INT16:
            sample = -_INT16
        i = self.m_next
        if self.m_count < MARGIN_FRAMES:
            self.m_count += 1
        else:
            self.m_sum -= self.margins[i]
        self.margins[i] = sample
        self.m_sum += sample
        self.m_next = (i + 1) % MARGIN_FRAMES
        average = self.margin()
        if self.margin_ok:
            if average < MARGIN_LOW:
                self.margin_ok = False
        elif average >= MARGIN_HIGH:
            self.margin_ok = True

    def link_etx(self, reach=False):
        """Return the ETX of the link cost (units of 1/ETX_ONE): capped at 4 on a link
        judged by reachability (the root's, or reach: see `eligible`)."""
        etx = self.etx()
        if (reach or self.address == ROOT) and etx > ETX_MAX:
            return ETX_MAX
        return etx

    def reachable(self, now, silence):
        """Return False once the last `UNREACH_RUN` failed samples are consecutive
        and no acknowledgement came for silence ms (the silence limit, strict)."""
        return self.run < UNREACH_RUN or now - self.answered < silence

    def eligible(self, now=None, silence=None, reach=False):
        """Return True if the link is eligible as a parent link (§9, MP12).

        Margin, and ETX <= 4, or for a link judged by reachability (the
        root's, or with reach: the preferred parent's while it is the
        node's last candidate, `mesh.parents`) its reachability at now (ms)
        with the silence limit silence (ms); without now, such a link is
        judged by its run alone (the silence taken as passed).
        """
        if not (self.margin_ok and self.m_count > 0):
            return False
        if not reach and self.address != ROOT:
            return self.etx() <= ETX_MAX
        if now is None:
            return self.run < UNREACH_RUN
        return self.reachable(now, silence)

    def age(self, now):
        """Return the time since the last frame (ms)."""
        return now - self.heard


class NeighbourTable:
    """Neighbour table of one board.

    sf: spreading factor (demodulation limit of the margin); period_ms:
    presence period (expiry after `EXPIRY_PERIODS`); capacity: entries.
    ``noise_floor``: measured noise floor (dBm) or None. Counters:
    ``evictions``, ``expired``, ``refused`` (new neighbour, every entry a
    parent), ``stale`` (frames with a counter not above the last one),
    ``aged`` (failure ageings).
    """

    def __init__(self, sf, period_ms=None, capacity=CAPACITY):
        if capacity < 1:
            raise ValueError("bad capacity")
        self.capacity = capacity
        self.limit_x10 = snr_limit_x10(sf)
        self.noise_floor = None
        self.period_ms = presence_ms(sf) if period_ms is None else period_ms
        self.evictions = 0
        self.expired = 0
        self.refused = 0
        self.stale = 0
        self.aged = 0
        self._slots = [None] * capacity
        self._free = [Neighbour() for _ in range(capacity)]
        self._index = {}  # address -> slot
        self._next_expiry = 0

    def __len__(self):
        return len(self._index)

    def set_period(self, period_ms):
        """Set the presence period (DODAG information, §9; operating mode, §17)."""
        self.period_ms = period_ms
        self._next_expiry = 0

    # --- Lookup and iteration --------------------------------------------------

    def get(self, address):
        """Return the entry of address, or None."""
        i = self._index.get(address)
        return None if i is None else self._slots[i]

    def slot(self, i):
        """Return the entry in slot i (0 to capacity - 1), or None (paged reading)."""
        return self._slots[i]

    def entries(self):
        """Yield the entries in slot order (do not change the table meanwhile)."""
        for n in self._slots:
            if n is not None:
                yield n

    def parents(self):
        """Return the list of the entries flagged as parents."""
        return [n for n in self._slots if n is not None and n.parent]

    # --- Updates -----------------------------------------------------------------

    def set_parent(self, address, flag=True):
        """Flag or unflag address as a parent (routing); False if unknown."""
        n = self.get(address)
        if n is None:
            return False
        n.parent = bool(flag)
        return True

    def acked(self, address, ok, now):
        """Record one sample of address at now: a frame acknowledged or not (p_ack).

        Returns False if address is not in the table (nothing recorded). A
        failure ages out `silence_ms` after now unless a newer one follows.
        """
        n = self.get(address)
        if n is None:
            return False
        n._ack(ok, now)
        if not ok:
            end = now + silence_ms(self.period_ms)
            if end < self._next_expiry:
                self._next_expiry = end
        return True

    def answered(self, address, now):
        """Record an acknowledgement of address at now that gives no sample.

        It ends the link's run of failed samples (reachability).
        Returns False if address is not in the table.
        """
        n = self.get(address)
        if n is None:
            return False
        n._answered(now)
        return True

    def restart_reach(self, address, now):
        """Restart the reachability of address at now, as at the entry's creation.

        Routing calls it when it takes address as its preferred parent
        (`mesh.parents`): the link then gets `UNREACH_RUN` failed samples
        and a silence limit of probes before it is unreachable. Returns
        False if address is not in the table.
        """
        n = self.get(address)
        if n is None:
            return False
        n.run = 0
        n.answered = now
        return True

    def heard(self, address, counter, rank, version, rssi, snr, now):
        """Record an authenticated frame of address; return its entry or None.

        counter, rank, version: from the frame header; rssi (dBm), snr (dB,
        None if not measured): link measurements; now: ms.
        """
        self.expire(now)
        n = self.get(address)
        if n is None:
            n = self._insert(address, now)
            if n is None:
                self.refused += 1
                return None
            n.reset(address, counter, rank, version, now)
        elif counter <= n.counter:
            self.stale += 1
            return None
        else:
            n._counter(counter)
            n.heard = now
            n.rank = rank
            n.version = version
        if snr is not None:
            if snr >= SNR_SATURATION and self.noise_floor is not None and rssi is not None:
                snr = rssi - self.noise_floor
            n._margin(round(snr * 10) - self.limit_x10)
        return n

    def remove(self, address):
        """Remove the entry of address; False if unknown."""
        i = self._index.pop(address, None)
        if i is None:
            return False
        self._release(i)
        return True

    def expire(self, now):
        """Remove the entries not heard for 4 presence periods; return their number.

        Also ages the failures `silence_ms` old (``aged`` counts the entries).
        """
        if now < self._next_expiry:
            return 0
        limit = EXPIRY_PERIODS * self.period_ms
        silence = silence_ms(self.period_ms)
        removed = 0
        nxt = now + limit
        for i in range(self.capacity):
            n = self._slots[i]
            if n is None:
                continue
            end = n.heard + limit
            if now >= end:
                del self._index[n.address]
                self._release(i)
                removed += 1
                continue
            if end < nxt:
                nxt = end
            if n.fail_at is not None:
                end = n.fail_at + silence
                if now >= end:
                    n._age()
                    self.aged += 1
                elif end < nxt:
                    nxt = end
        self._next_expiry = nxt
        self.expired += removed
        return removed

    def _release(self, i):
        n = self._slots[i]
        self._slots[i] = None
        n.address = None
        n.parent = False
        self._free.append(n)

    def _insert(self, address, now):
        if not self._free:
            self._evict()
            if not self._free:
                return None
        n = self._free.pop()
        slots = self._slots
        for i in range(self.capacity):
            if slots[i] is None:
                slots[i] = n
                self._index[address] = i
                return n
        raise RuntimeError("slots and free list disagree")

    def _evict(self):
        # The stalest entry that is not a parent (MP13).
        victim = -1
        oldest = None
        for i in range(self.capacity):
            n = self._slots[i]
            if n is not None and not n.parent and (oldest is None or n.heard < oldest):
                victim = i
                oldest = n.heard
        if victim >= 0:
            del self._index[self._slots[victim].address]
            self._release(victim)
            self.evictions += 1
