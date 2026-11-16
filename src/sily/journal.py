"""Field journal of a box: JSON lines in numbered segments, bounded in size.

Runs on the board, on the unix port and on CPython
(``tools/journal_summary.py``): the common subset only (no ``const``, no
``time.ticks_*``: the clock is injected, and so is the file system).

Layout: a directory (`DIR` on the board) of segments ``NNNNNN.log``,
numbered in order of creation across boots. Each holds JSON lines, one
object per line with at least ``b`` (boot number), ``t`` (ms, the
application's clock) and ``k`` (kind: ``sily.record``). A new segment
starts at each boot (`Journal.open`) and when the next line would take the
current one past ``segment_kb``; before one is opened, the oldest segments
are removed until the existing ones and the new one fit in ``max_kb``. A
line longer than a segment gets one of its own (the bound is then exceeded
by that line).

Durability: lines go into the open file as they come; `Journal.sync`
flushes it (littlefs commits the file), which the application asks for
every ``sync_s`` (`Journal.maybe_sync`), after the boot record, and at stop
and crash. A power cut loses at most what came since the last sync, and
may leave a truncated last line, which `read` skips and counts.

Costs: ``write_max`` and ``sync_max`` hold the longest write (rotation
included) and sync in ms since the last `Journal.stalls` (the minute
record's ``jw``, ``js``). A file-system error (a full partition) counts in
``errors`` and the line is dropped: the journal never stops the
application.

`Queue`: the bounded list where callbacks of the mesh core leave their
items for a task to format and write (``overflow`` counts those refused).
"""

import json
import os

DIR = "/journal"
SUFFIX = ".log"
SEPARATORS = (",", ":")


class Files:
    """The file system through ``os`` (board, unix port, CPython)."""

    def listdir(self, path):
        return os.listdir(path)

    def mkdir(self, path):
        os.mkdir(path)

    def size(self, path):
        return os.stat(path)[6]

    def remove(self, path):
        os.remove(path)

    def open(self, path, mode):
        return open(path, mode)


FILES = Files()


def segment_name(number):
    return "%06d%s" % (number, SUFFIX)


def segments(directory, fs=None):
    """Return [(number, name)] of the segments of directory, in number order."""
    if fs is None:
        fs = FILES
    out = []
    for name in fs.listdir(directory):
        stem = name[: -len(SUFFIX)]
        if name.endswith(SUFFIX) and stem and stem.isdigit():
            out.append((int(stem), name))
    out.sort()
    return out


class Queue:
    """Bounded FIFO of items; `put` never raises, ``overflow`` counts refusals."""

    def __init__(self, capacity=64):
        self.capacity = capacity
        self.items = []
        self.overflow = 0

    def __len__(self):
        return len(self.items)

    def put(self, item):
        """Append item; return False (counted) when the queue is full."""
        if len(self.items) >= self.capacity:
            self.overflow += 1
            return False
        self.items.append(item)
        return True

    def get(self):
        """Remove and return the oldest item, None when empty."""
        if not self.items:
            return None
        return self.items.pop(0)


class Journal:
    """Writer of the journal of boot number boot in directory.

    clock: function() -> ms (monotonic); max_kb, segment_kb, sync_s: as the
    configuration's ``journal`` section; fs: the file system (`Files`).
    Read: ``number`` (current segment, None before `open` or after an
    error), ``size`` (its bytes), ``lines``, ``errors``, ``removed``
    (segments removed for the bound), ``write_max``, ``sync_max``,
    ``synced_at``, ``syncs``.
    """

    def __init__(self, directory, boot, clock, max_kb=1024, segment_kb=64, sync_s=300, fs=None):
        self.directory = directory
        self.boot = boot
        self.clock = clock
        self.max_bytes = max_kb * 1024
        self.segment_bytes = segment_kb * 1024
        self.sync_ms = sync_s * 1000
        self.fs = fs if fs is not None else FILES
        self.number = None
        self.size = 0
        self.lines = 0
        self.errors = 0
        self.removed = 0
        self.write_max = 0
        self.sync_max = 0
        self.synced_at = clock()
        self.syncs = 0
        self._file = None
        self._old = []  # [number, size] of the earlier segments, oldest first

    def _path(self, number):
        return self.directory + "/" + segment_name(number)

    def open(self):
        """Start a new segment (at boot); return False if the file system refused."""
        fs = self.fs
        try:
            try:
                fs.mkdir(self.directory)
            except OSError:
                pass  # exists
            old = []
            for number, name in segments(self.directory, fs):
                old.append([number, fs.size(self.directory + "/" + name)])
            self._old = old
            self._start(old[-1][0] + 1 if old else 1)
        except OSError:
            self.errors += 1
            return False
        self.synced_at = self.clock()
        return True

    def _start(self, number):
        old = self._old
        total = 0
        for item in old:
            total += item[1]
        while old and total + self.segment_bytes > self.max_bytes:
            first, size = old.pop(0)
            self.fs.remove(self._path(first))
            total -= size
            self.removed += 1
        self._file = self.fs.open(self._path(number), "wb")
        self.number = number
        self.size = 0

    def _rotate(self):
        f = self._file
        self._file = None
        f.close()
        self._old.append([self.number, self.size])
        self._start(self.number + 1)

    def write(self, t, kind, fields=None):
        """Write the line {"b": boot, "t": t, "k": kind, **fields}; False when dropped.

        fields (a dict) is completed in place.
        """
        obj = {} if fields is None else fields
        obj["b"] = self.boot
        obj["t"] = t
        obj["k"] = kind
        data = (json.dumps(obj, separators=SEPARATORS) + "\n").encode()
        if self._file is None:
            self.errors += 1
            return False
        t0 = self.clock()
        try:
            if self.size and self.size + len(data) > self.segment_bytes:
                self._rotate()
            self._file.write(data)
        except OSError:
            self.errors += 1
            return False
        finally:
            dt = self.clock() - t0
            if dt > self.write_max:
                self.write_max = dt
        self.size += len(data)
        self.lines += 1
        return True

    def sync(self):
        """Commit the lines written so far to the file system."""
        f = self._file
        t0 = self.clock()
        if f is not None:
            try:
                f.flush()
                self.syncs += 1
            except OSError:
                self.errors += 1
        now = self.clock()
        if now - t0 > self.sync_max:
            self.sync_max = now - t0
        self.synced_at = now

    def maybe_sync(self):
        """Sync if ``sync_s`` passed since the last one; return ms until the next."""
        wait = self.synced_at + self.sync_ms - self.clock()
        if wait > 0:
            return wait
        self.sync()
        return self.sync_ms

    def stalls(self):
        """Return (write_max, sync_max) since the last call, and restart them."""
        out = (self.write_max, self.sync_max)
        self.write_max = 0
        self.sync_max = 0
        return out

    def close(self):
        """Sync and close the segment (idempotent)."""
        f = self._file
        if f is None:
            return
        self.sync()
        self._file = None
        try:
            f.close()
        except OSError:
            self.errors += 1


def read(directory, fs=None):
    """Return (records, bad) of the journal in directory.

    records: the objects of every segment, in segment then line order; bad:
    the lines that do not parse (a truncated last line after a power cut),
    skipped.
    """
    if fs is None:
        fs = FILES
    records = []
    bad = 0
    for _, name in segments(directory, fs):
        f = fs.open(directory + "/" + name, "rb")
        try:
            data = f.read()
        finally:
            f.close()
        for raw in data.split(b"\n"):
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode())
            except ValueError:
                obj = None
            if type(obj) is not dict:
                bad += 1
                continue
            records.append(obj)
    return records, bad
