"""Frame counter persistence (MESH_DESIGN §11 frame counter, §14).

The 24-bit frame counter of a board increases for the board's whole life,
across reboots, power losses and key changes: (link source, counter) is the
AEAD nonce and must never repeat.

It is reserved in storage (NVS) by blocks of 256: the record holds the first
counter of the next block, so that the counters below it are the only ones
a board may have used. At creation the board starts at the recorded value and
records the next block (one commit at boot); the following blocks are
recorded in advance, once half of the current block is used, so that no
transmission waits for flash. A reset or power loss therefore skips the rest
of the current block (and the next one if already recorded), never repeats a
counter.

Full flash erase (record lost): the board starts at a random block in
[0, 2^23) and reports it (``random_restart``): a reuse is unlikely, not
excluded; the procedure is a key change. The counter stops at 2^24 - 1:
`FrameCounter.next` then returns None for ever (the board stops
transmitting and reports it, ``exhausted``).

Record: 4 bytes little-endian under `RECORD_KEY` (it may exceed 2^24 near
the end of the range). The class 2 sender sequences will join this record.
"""

from mesh.platform import randbelow

BLOCK = 256
COUNTER_LIMIT = 0xFFFFFF  # 2^24 - 1: never sent
RECORD_KEY = "counters"
RANDOM_RANGE = 1 << 23  # random restart after a full flash erase

_RECORD_SIZE = 4


class FrameCounter:
    """Frame counters of one board, reserved in storage by blocks.

    storage: a `mesh.platform.Storage` (its namespace is the board's mesh
    state); random: a `mesh.platform.Random`, used only when the record is
    missing (or unreadable). Creation reads the record and commits the next
    block: build it once per boot, before the first transmission.
    """

    def __init__(self, storage, random):
        self._storage = storage
        record = storage.get(RECORD_KEY)
        self.random_restart = record is None or len(record) != _RECORD_SIZE
        if self.random_restart:
            start = randbelow(random, RANDOM_RANGE // BLOCK) * BLOCK
        else:
            start = int.from_bytes(record, "little")
        self._next = start
        self._limit = start  # counters below it are recorded as used
        self._reserve()

    @property
    def exhausted(self):
        """True once the counter has reached 2^24 - 1: no more frames."""
        return self._next >= COUNTER_LIMIT

    def peek(self):
        """Return the counter the next frame will use (None if exhausted)."""
        return None if self.exhausted else self._next

    def next(self):
        """Return the counter of the next frame and consume it; None if exhausted."""
        c = self._next
        if c >= COUNTER_LIMIT:
            return None
        if c >= self._limit:  # only if a reservation failed: catch up now
            self._reserve()
        self._next = c + 1
        if c + 1 >= self._limit - BLOCK // 2:
            self._reserve()
        return c

    def _reserve(self):
        """Record the next block, durably, before any of its counters is used."""
        limit = self._limit + BLOCK
        self._storage.set(RECORD_KEY, limit.to_bytes(_RECORD_SIZE, "little"))
        self._storage.commit()
        self._limit = limit
