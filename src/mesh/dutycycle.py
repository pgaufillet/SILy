"""Duty-cycle accounting (MESH_DESIGN §6, R16).

Transmission time is accounted in ms over a sliding window of bins (60 bins
of 60 s for a one-hour window). Each transmission is charged to the bin of
the current time, so it counts in full until its bin leaves the window. The
window holds the current bin **plus** `bins` complete bins before it: a
charge counts for at least one window (one hour) and at most one window plus
one bin, so that every one-hour interval lies within the bins checked before
a transmission; without the extra bin, the oldest minute of an hour could be
forgotten.

Before a transmission, ``used + time on air <= allowance(priority)``; the
allowances are 100 %, 80 % and 60 % of the budget for priorities 1, 2 and 3
(§6; priority from the port, `mesh.ports`). Mixed frames (§7 aggregation): a
message joins a frame only if the frame, with it, stays within the allowance
of every message it carries (`admit`).

A frame is charged its computed time on air when it starts (`start`), and the
excess of the measured duration (TX_DONE ``duration_ms``) when it ends
(`done`): the maximum of both is accounted.

The window is kept in retained memory (RTC memory, `mesh.platform.Retained`)
at every charge: it survives soft resets and the watchdog. Since the clock may
restart with the board, the restored window resumes as if no time had passed
since the last charge, which can only keep airtime longer than needed. After
a power loss the retained memory is cleared and the window starts empty
(§6: residual risk accepted).

Pure logic: time (monotonic ms) is passed in by the caller.
"""

import struct

from mesh.ports import PRIORITY_LOW, PRIORITY_REPAIR

# Allowance of each priority, in percent of the budget (priority 1 first).
ALLOWANCE_PERCENT = (100, 80, 60)

_MAGIC = 0x4344  # "DC"
_VERSION = 1
# magic, version, slot count, current slot, ms into the current bin, total;
# then the slots (ms each).
_HEADER = "<HBBBII"
_HEADER_SIZE = 13


def _format(slots):
    return _HEADER + "%dI" % slots


def retained_size(bins=60):
    """Return the bytes of retained memory used by a window of bins bins."""
    return _HEADER_SIZE + 4 * (bins + 1)


class DutyCycle:
    """Sliding-window duty-cycle accounting of one board.

    budget_ms: transmission time allowed per window at priority 1
    (`mesh.profile.Profile.budget_ms`, 324 000 ms in the EU profile);
    window_ms: the window (one hour), divided into bins bins (window_ms must
    be a multiple of bins). retained: a `mesh.platform.Retained`, or None for
    no persistence; the window uses retained_size(bins) bytes of its data from
    offset, restored at creation when valid.
    """

    def __init__(self, budget_ms, window_ms=3600000, bins=60, retained=None, offset=0):
        if bins < 1 or bins > 254 or window_ms % bins:
            raise ValueError("bad window")
        self.budget_ms = budget_ms
        self.bin_ms = window_ms // bins
        self._slots = [0] * (bins + 1)
        self._format = _format(bins + 1)
        self._head = 0  # slot of the current bin
        self._total = 0  # sum of the slots
        self._start = None  # start of the current bin; None until the first call
        self._phase = 0  # ms into the current bin, applied at the first call
        self._pending = 0  # computed time on air of the frame in flight
        self._retained = retained
        self._offset = offset
        if retained is not None:
            if offset < 0 or offset + retained_size(bins) > len(retained.data):
                raise ValueError("retained memory too small")
            self._restore()

    def allowance(self, priority):
        """Return the transmission time allowed per window at priority (ms)."""
        if priority < PRIORITY_REPAIR or priority > PRIORITY_LOW:
            raise ValueError("bad priority")
        return self.budget_ms * ALLOWANCE_PERCENT[priority - 1] // 100

    def used(self, now):
        """Return the transmission time accounted in the window at now (ms)."""
        self._advance(now)
        return self._total

    def available(self, now, priority):
        """Return the time on air a priority may still send at now (ms, >= 0)."""
        left = self.allowance(priority) - self.used(now)
        return left if left > 0 else 0

    def allows(self, now, priority, toa_ms):
        """Return True if a frame of toa_ms fits the allowance of priority."""
        return self.used(now) + toa_ms <= self.allowance(priority)

    def admit(self, now, frame_priority, priority, toa_ms):
        """Mixed-frame rule: may a message of priority join a frame?

        frame_priority: the least urgent priority (largest number) of the
        messages already in the frame, `PRIORITY_REPAIR` for an empty frame;
        toa_ms: time on air of the frame with the message. Returns the frame's
        new least urgent priority if the frame, with the message, fits the
        allowances of the message and of the messages already in it; 0 if not.
        """
        p = priority if priority > frame_priority else frame_priority
        return p if self.allows(now, p, toa_ms) else 0

    def next_release(self, now):
        """Return the time (ms) at which the window next drops its oldest bin.

        The earliest time at which `used` may decrease: a frame refused now
        cannot be admitted before it.
        """
        self._advance(now)
        return self._start + self.bin_ms

    def start(self, now, toa_ms):
        """Charge the computed time on air of a frame transmitted at now."""
        self._pending = toa_ms
        self._charge(now, toa_ms)

    def done(self, now, measured_ms):
        """Charge the excess of the measured duration (TX_DONE) at now."""
        excess = measured_ms - self._pending
        self._pending = 0
        if excess > 0:
            self._charge(now, excess)

    def _charge(self, now, ms):
        self._advance(now)
        if ms <= 0:
            return
        self._slots[self._head] += ms
        self._total += ms
        self._save(now)

    def _advance(self, now):
        start = self._start
        if start is None:
            self._start = now - self._phase
            return
        elapsed = (now - start) // self.bin_ms
        if elapsed <= 0:
            return
        slots = self._slots
        n = len(slots)
        if elapsed >= n:
            for i in range(n):
                slots[i] = 0
            self._total = 0
        else:
            head = self._head
            for _ in range(elapsed):
                head = (head + 1) % n
                self._total -= slots[head]
                slots[head] = 0
            self._head = head
        self._start = start + elapsed * self.bin_ms

    def _save(self, now):
        retained = self._retained
        if retained is None:
            return
        struct.pack_into(
            self._format,
            retained.data,
            self._offset,
            _MAGIC,
            _VERSION,
            len(self._slots),
            self._head,
            now - self._start,
            self._total,
            *self._slots,
        )
        retained.save()

    def _restore(self):
        n = len(self._slots)
        values = struct.unpack_from(self._format, self._retained.data, self._offset)
        magic, version, count, head, phase, total = values[:6]
        if magic != _MAGIC or version != _VERSION or count != n or head >= n:
            return
        if phase >= self.bin_ms or sum(values[6:]) != total:
            return
        self._slots = list(values[6:])
        self._head = head
        self._total = total
        self._phase = phase
