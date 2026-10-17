"""Replay table (MESH_DESIGN §14 replay protection).

Per sender (link source), the highest frame counter authenticated so far; a
frame is fresh only if its counter is strictly greater. Separate from the
neighbour table: entries do not expire on silence. 128 entries by default,
the least recently heard sender evicted when full (its frames can then be
replayed once; evictions are counted).

Kept in retained memory (RTC memory, `mesh.platform.Retained`), worked on in
place and saved at every update: it survives soft resets and the watchdog,
not a power loss (the first frame of each sender then sets its baseline).

Layout from the offset: magic 2 B, format version 1 B, entry count 1 B, then
the entries of 5 B (source 2 B, counter 3 B, little-endian), least recently
heard first. An update moves its entry to the end (C memmove); lookups scan
from the end, where the active neighbours are.
"""

CAPACITY = 128

_MAGIC0 = 0x52  # "RP"
_MAGIC1 = 0x50
_VERSION = 1
_HEADER = 4
_ENTRY = 5


def retained_size(capacity=CAPACITY):
    """Return the bytes of retained memory used by a table of capacity entries."""
    return _HEADER + _ENTRY * capacity


class ReplayTable:
    """Highest authenticated frame counter per sender, LRU-bounded.

    retained: a `mesh.platform.Retained`, or None for a table in RAM only;
    the table uses retained_size(capacity) bytes of its data from offset,
    restored at creation when valid (otherwise emptied). ``evictions``
    counts the senders evicted since creation.
    """

    def __init__(self, retained=None, offset=0, capacity=CAPACITY):
        if capacity < 1 or capacity > 255:
            raise ValueError("bad capacity")
        size = retained_size(capacity)
        if retained is None:
            data = bytearray(size)
        else:
            data = retained.data
            if offset < 0 or offset + size > len(data):
                raise ValueError("retained memory too small")
        self._retained = retained
        self._data = data
        self._mv = memoryview(data)
        self._base = offset
        self._first = offset + _HEADER  # first entry
        self.capacity = capacity
        self.evictions = 0
        o = offset
        if (
            data[o] != _MAGIC0
            or data[o + 1] != _MAGIC1
            or data[o + 2] != _VERSION
            or data[o + 3] > capacity
        ):
            data[o] = _MAGIC0
            data[o + 1] = _MAGIC1
            data[o + 2] = _VERSION
            data[o + 3] = 0

    def __len__(self):
        return self._data[self._base + 3]

    def _save(self):
        if self._retained is not None:
            self._retained.save()

    def _find(self, source):
        """Return the position of source's entry in data, or -1."""
        d = self._data
        lo = source & 0xFF
        hi = source >> 8
        first = self._first
        p = first + _ENTRY * (d[self._base + 3] - 1)
        while p >= first:
            if d[p] == lo and d[p + 1] == hi:
                return p
            p -= _ENTRY
        return -1

    def highest(self, source):
        """Return the highest counter authenticated from source, or None."""
        p = self._find(source)
        if p < 0:
            return None
        d = self._data
        return d[p + 2] | d[p + 3] << 8 | d[p + 4] << 16

    def check(self, source, counter):
        """Return True if a frame of source with counter is not a replay.

        Unknown senders are accepted (the first frame sets the baseline).
        Does not change the table: call `update` once the frame is accepted.
        """
        p = self._find(source)
        if p < 0:
            return True
        d = self._data
        return counter > (d[p + 2] | d[p + 3] << 8 | d[p + 4] << 16)

    def update(self, source, counter):
        """Record counter as the highest of source, now most recently heard.

        A new sender evicts the least recently heard one when the table is
        full. Saves the retained memory.
        """
        d = self._data
        mv = self._mv
        count_at = self._base + 3
        count = d[count_at]
        first = self._first
        end = first + _ENTRY * count  # one past the last entry
        p = self._find(source)
        if p < 0:
            if count == self.capacity:
                mv[first : end - _ENTRY] = mv[first + _ENTRY : end]
                self.evictions += 1
            else:
                d[count_at] = count + 1
                end += _ENTRY
        elif p != end - _ENTRY:
            mv[p : end - _ENTRY] = mv[p + _ENTRY : end]
        p = end - _ENTRY
        d[p] = source & 0xFF
        d[p + 1] = source >> 8
        d[p + 2] = counter & 0xFF
        d[p + 3] = (counter >> 8) & 0xFF
        d[p + 4] = counter >> 16
        self._save()
