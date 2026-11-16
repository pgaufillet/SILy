"""Field journal (sily.journal): segments, size bound, sync policy, read-back.

Most tests run on `MemFs`, an in-memory file system that keeps, per file,
what was written and what was last flushed (what a power cut leaves), on
a scripted clock; one runs on a real directory under build/.
"""

import os
import unittest

from sily import journal

DIR = "/j"
REAL = "build/test_sily_journal"  # relative to the repository root, where the tests run


class MemFile:
    def __init__(self, fs, path):
        self.fs = fs
        self.path = path

    def write(self, data):
        self.fs.data[self.path].extend(data)
        self.fs.clock.advance(self.fs.write_ms)
        return len(data)

    def read(self):
        return bytes(self.fs.data[self.path])

    def flush(self):
        self.fs.flushes += 1
        self.fs.clock.advance(self.fs.flush_ms)
        self.fs.synced[self.path] = bytes(self.fs.data[self.path])

    def close(self):
        self.fs.synced[self.path] = bytes(self.fs.data[self.path])


class MemFs:
    """In-memory file system: ``data`` (written), ``synced`` (as of the last flush or close).

    write_ms, flush_ms: time each write or flush takes on clock.
    """

    def __init__(self, clock, write_ms=0, flush_ms=0):
        self.clock = clock
        self.write_ms = write_ms
        self.flush_ms = flush_ms
        self.dirs = set()
        self.data = {}
        self.synced = {}
        self.flushes = 0

    def mkdir(self, path):
        if path in self.dirs:
            raise OSError(17)
        self.dirs.add(path)

    def listdir(self, path):
        if path not in self.dirs:
            raise OSError(2)
        prefix = path + "/"
        return [p[len(prefix) :] for p in self.data if p.startswith(prefix)]

    def size(self, path):
        return len(self.data[path])

    def remove(self, path):
        del self.data[path]
        self.synced.pop(path, None)

    def open(self, path, mode):
        if mode.startswith("w"):
            self.data[path] = bytearray()
        elif path not in self.data:
            raise OSError(2)
        return MemFile(self, path)

    def power_cut(self):
        """Keep only what was synced."""
        self.data = {p: bytearray(d) for p, d in self.synced.items()}

    def names(self):
        return sorted(p.rsplit("/", 1)[1] for p in self.data)


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms


def _journal(fs, clock, boot=1, **options):
    j = journal.Journal(DIR, boot, clock, fs=fs, **options)
    j.open()
    return j


def _fields(i):
    return {"i": i, "pad": "x" * 90}  # about 120 bytes per line


class Segments(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fs = MemFs(self.clock)

    def test_round_trip(self):
        j = _journal(self.fs, self.clock, boot=4)
        self.clock.advance(15)
        self.assertTrue(j.write(self.clock(), "boot", {"sf": 7}))
        j.write(20, "ev", {"e": "attach", "par": None})
        j.close()
        records, bad = journal.read(DIR, self.fs)
        self.assertEqual(bad, 0)
        self.assertEqual(
            records,
            [
                {"b": 4, "t": 15, "k": "boot", "sf": 7},
                {"b": 4, "t": 20, "k": "ev", "e": "attach", "par": None},
            ],
        )
        self.assertEqual(self.fs.names(), ["000001.log"])
        self.assertEqual(j.lines, 2)

    def test_rotation_at_the_segment_size(self):
        j = _journal(self.fs, self.clock, max_kb=64, segment_kb=4)
        for i in range(100):  # about 12 KB
            j.write(i, "x", _fields(i))
        j.close()
        names = self.fs.names()
        self.assertEqual(names[:2], ["000001.log", "000002.log"])
        self.assertTrue(len(names) >= 3, names)
        for name in names:
            self.assertTrue(self.fs.size(DIR + "/" + name) <= 4096, name)
        records, bad = journal.read(DIR, self.fs)
        self.assertEqual([r["i"] for r in records], list(range(100)))

    def test_total_bound_keeps_the_newest(self):
        j = _journal(self.fs, self.clock, max_kb=16, segment_kb=4)
        for i in range(1000):  # about 120 KB
            j.write(i, "x", _fields(i))
        j.close()
        total = sum(len(d) for d in self.fs.data.values())
        self.assertTrue(total <= 16 * 1024, total)
        self.assertTrue(j.removed > 0)
        records, _ = journal.read(DIR, self.fs)
        self.assertEqual(records[-1]["i"], 999)
        indices = [r["i"] for r in records]
        self.assertEqual(indices, list(range(indices[0], 1000)))  # no gap: oldest removed

    def test_numbering_continues_after_the_oldest_were_removed(self):
        j = _journal(self.fs, self.clock, max_kb=16, segment_kb=4)
        for i in range(400):
            j.write(i, "x", _fields(i))
        j.close()
        last = journal.segments(DIR, self.fs)[-1][0]
        self.assertTrue(journal.segments(DIR, self.fs)[0][0] > 1)  # oldest removed
        k = _journal(self.fs, self.clock, boot=2, max_kb=16, segment_kb=4)
        self.assertEqual(k.number, last + 1)
        k.write(0, "boot")
        k.close()
        records, _ = journal.read(DIR, self.fs)
        self.assertEqual(records[-1], {"b": 2, "t": 0, "k": "boot"})

    def test_new_segment_at_each_boot(self):
        for boot in (1, 2, 3):
            j = _journal(self.fs, self.clock, boot=boot)
            j.write(boot, "boot")
            j.close()
        self.assertEqual(self.fs.names(), ["000001.log", "000002.log", "000003.log"])
        records, _ = journal.read(DIR, self.fs)
        self.assertEqual([r["b"] for r in records], [1, 2, 3])

    def test_segment_9_read_before_10(self):
        self.fs.mkdir(DIR)
        for number in (10, 9, 11):  # listed out of order; names not zero-padded
            f = self.fs.open("%s/%d.log" % (DIR, number), "wb")
            f.write(b'{"b":%d,"t":0,"k":"boot"}\n' % number)
        self.fs.open(DIR + "/notes.txt", "wb").write(b"not a segment\n")
        records, bad = journal.read(DIR, self.fs)
        self.assertEqual([r["b"] for r in records], [9, 10, 11])
        self.assertEqual(bad, 0)


class Durability(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fs = MemFs(self.clock)

    def test_one_flush_per_sync_period_not_per_record(self):
        j = _journal(self.fs, self.clock, sync_s=10)
        for i in range(60):  # one record a second for a minute
            self.clock.advance(1000)
            j.write(self.clock(), "x", {"i": i})
            j.maybe_sync()
        self.assertEqual(self.fs.flushes, 6)
        self.assertEqual(j.syncs, 6)

    def test_maybe_sync_tells_the_wait(self):
        j = _journal(self.fs, self.clock, sync_s=10)
        self.clock.advance(4000)
        self.assertEqual(j.maybe_sync(), 6000)
        self.assertEqual(self.fs.flushes, 0)
        self.clock.advance(6000)
        self.assertEqual(j.maybe_sync(), 10000)
        self.assertEqual(self.fs.flushes, 1)

    def test_power_cut_keeps_the_synced_lines(self):
        j = _journal(self.fs, self.clock, sync_s=10)
        for i in range(25):
            self.clock.advance(1000)
            j.write(self.clock(), "x", {"i": i})
            j.maybe_sync()
        self.fs.power_cut()
        records, bad = journal.read(DIR, self.fs)
        self.assertEqual([r["i"] for r in records], list(range(20)))  # synced at 10 s, 20 s
        self.assertEqual(bad, 0)

    def test_truncated_last_line_skipped_and_counted(self):
        j = _journal(self.fs, self.clock)
        j.write(1, "a")
        j.write(2, "b", {"pad": "y" * 40})
        j.close()
        path = DIR + "/000001.log"
        self.fs.data[path] = self.fs.data[path][:-20]  # a power cut in the last line
        records, bad = journal.read(DIR, self.fs)
        self.assertEqual(records, [{"b": 1, "t": 1, "k": "a"}])
        self.assertEqual(bad, 1)

    def test_write_and_sync_stalls_measured_then_restarted(self):
        self.fs.write_ms = 3
        self.fs.flush_ms = 40
        j = _journal(self.fs, self.clock)
        j.write(0, "a")
        j.sync()
        self.assertEqual(j.stalls(), (3, 40))
        self.assertEqual(j.stalls(), (0, 0))

    def test_file_system_errors_counted_never_raised(self):
        fs = self.fs

        def refuse(path):
            raise OSError(5)

        fs.listdir = refuse  # the directory cannot be read: no segment opens
        j = journal.Journal(DIR, 1, self.clock, fs=fs)
        self.assertFalse(j.open())
        self.assertFalse(j.write(0, "boot"))  # dropped, counted
        j.sync()
        j.close()
        self.assertEqual(j.errors, 2)

    def test_full_partition_drops_the_line(self):
        j = _journal(self.fs, self.clock)

        def full(data):
            raise OSError(28)

        j._file.write = full
        self.assertFalse(j.write(0, "a"))
        self.assertEqual((j.errors, j.lines), (1, 0))


class Queue(unittest.TestCase):
    def test_overflow_counted_never_raised(self):
        q = journal.Queue(3)
        for i in range(5):
            q.put(i)
        self.assertEqual((len(q), q.overflow), (3, 2))
        self.assertEqual([q.get(), q.get(), q.get(), q.get()], [0, 1, 2, None])


def _clear(path):
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        os.remove(path + "/" + name)


class RealDirectory(unittest.TestCase):
    def test_segments_on_the_host_file_system(self):
        try:
            os.mkdir("build")
        except OSError:
            pass
        _clear(REAL)
        clock = Clock()
        for boot in (1, 2):
            j = journal.Journal(REAL, boot, clock, max_kb=16, segment_kb=4)
            self.assertTrue(j.open())
            for i in range(50):
                j.write(i, "x", _fields(i))
            j.close()
        self.assertEqual(j.errors, 0)
        names = sorted(os.listdir(REAL))
        self.assertTrue(len(names) >= 4, names)
        total = sum(os.stat(REAL + "/" + n)[6] for n in names)
        self.assertTrue(total <= 16 * 1024, total)
        records, bad = journal.read(REAL)
        self.assertEqual(bad, 0)
        self.assertEqual(records[-1], {"b": 2, "t": 49, "k": "x", "i": 49, "pad": "x" * 90})
