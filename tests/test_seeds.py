"""Parallel scenario suites (`tests/seeds.py`), without playing a scenario.

The chunks of a plan cover the shifts asked exactly; a chunk run in JSON
shifts the seed of each network it builds, gives one line per shift and
class (verdict, failures, time) then its end line, and leaves the
networks unshifted afterwards; the verdict lists each failing (shift,
class, test) with its reproduction command, and exits 0 when every shift
passed, 1 when one failed, 2 when a shift is missing, a chunk did not end
or nothing ran.
"""

import json
import os
import unittest

import seeds

from sim.net import Network

TMP = "build/test_seeds.tmp"  # relative to the repository root, where the tests run
KEY = bytes(range(16))
CLASSES = (("test_a", "One"), ("test_a", "Two"), ("test_b", "Three"))


def line(shift, module, case, failures=()):
    """Return the JSON line of a chunk for shift and module.case, failing on failures."""
    return {
        "shift": shift,
        "module": module,
        "class": case,
        "ok": not failures,
        "tests": 2,
        "failures": [{"test": t, "message": "AssertionError: %s" % t} for t in failures],
        "ms": 100,
        "variant": "",
        "rev": "abc1234",
    }


def chunk(first, count, failing=None, skip=(), ended=True):
    """Return the text of a chunk's file: shifts first to first + count - 1 of CLASSES.

    failing: {(shift, class): test}; skip: (shift, class) pairs without a
    line; ended: with the end line.
    """
    failing = failing or {}
    out = []
    for k in range(first, first + count):
        for module, case in CLASSES:
            if (k, case) in skip:
                continue
            test = failing.get((k, case))
            out.append(json.dumps(line(k, module, case, (test,) if test else ())))
    if ended:
        out.append(json.dumps({"end": True, "first": first, "count": count}))
    return "\n".join(out) + "\n"


class PlanTest(unittest.TestCase):
    """plan: chunks of at most CHUNK shifts covering the range exactly, in order."""

    def covered(self, chunks):
        out = []
        for first, n in chunks:
            self.assertTrue(n >= 1)
            out.extend(range(first, first + n))
        return out

    def test_range_covered_exactly(self):
        chunks = seeds.plan(23, 5, 10)
        self.assertEqual(chunks, [(10, 5), (15, 5), (20, 5), (25, 5), (30, 3)])
        self.assertEqual(self.covered(chunks), list(range(10, 33)))

    def test_whole_chunks_and_larger_chunk(self):
        self.assertEqual(self.covered(seeds.plan(500, 5)), list(range(500)))
        self.assertEqual(len(seeds.plan(500, 5)), 100)
        self.assertEqual(seeds.plan(3, 10, 7), [(7, 3)])
        self.assertEqual(seeds.plan(0, 5), [])

    def test_chunk_below_one_refused(self):
        with self.assertRaises(ValueError):
            seeds.plan(10, 0)


class RunTest(unittest.TestCase):
    """run, JSON lines: seeds shifted per shift, one line per class, then the end line."""

    def test_chunk_lines(self):
        built = []

        class Probe(unittest.TestCase):
            def test_seed(self):
                built.append(Network(7, KEY).seed)
                self.assertNotEqual(built[-1], 7 + seeds.STRIDE, "shift 1")

        lines = []
        failed = seeds.run([("probe", Probe)], 3, 0, True, lines.append)
        self.assertEqual(built, [7, 7 + seeds.STRIDE, 7 + 2 * seeds.STRIDE])
        self.assertEqual(Network(7, KEY).seed, 7)  # unshifted after the run
        self.assertEqual(failed, [(1, "test_seed (Probe)")])
        records = [json.loads(t) for t in lines]
        self.assertEqual([r.get("shift") for r in records], [0, 1, 2, None])
        self.assertEqual([r.get("ok") for r in records[:3]], [True, False, True])
        self.assertEqual(records[1]["failures"][0]["test"], "test_seed")
        self.assertIn("shift 1", records[1]["failures"][0]["message"])
        self.assertTrue(all(r["ms"] >= 0 and r["class"] == "Probe" for r in records[:3]))
        self.assertEqual(records[3], {"end": True, "first": 0, "count": 3})


class VerdictTest(unittest.TestCase):
    """aggregate --expect: failing pairs listed with their command, missing ones, exit codes."""

    def setUp(self):
        for d in ("build", TMP):
            try:
                os.mkdir(d)
            except OSError:
                pass

    def tearDown(self):
        for entry in os.listdir(TMP):
            os.remove("%s/%s" % (TMP, entry))
        os.rmdir(TMP)

    def write(self, first, text):
        with open("%s/seeds_%d.jsonl" % (TMP, first), "w") as f:
            f.write(text)

    def status(self, expect="0-5"):
        # The exit status of `aggregate --expect expect TMP`, its lines in self.printed.
        lines, crashed = seeds.read_lines([TMP])
        self.printed = []
        return seeds.verdict(lines, crashed, seeds._expect(expect), self.printed.append)

    def test_all_passed(self):
        self.write(0, chunk(0, 3))
        self.write(3, chunk(3, 3))
        self.assertEqual(self.status(), 0)

    def test_failures_listed(self):
        self.write(0, chunk(0, 3, {(1, "Two"): "test_x", (2, "Three"): "test_y"}))
        self.write(3, chunk(3, 3))
        self.assertEqual(self.status(), 1)
        self.assertEqual(
            self.printed[:4],
            [
                "FAIL shift 1 test_a.Two test_x: AssertionError: test_x",
                "   reproduce: make seeds SEEDS=1 SEEDS_FIRST=1 SEEDS_CLASS=test_a.Two "
                "SEEDS_JOBS=1",
                "FAIL shift 2 test_b.Three test_y: AssertionError: test_y",
                "   reproduce: make seeds SEEDS=1 SEEDS_FIRST=2 SEEDS_CLASS=test_b.Three "
                "SEEDS_JOBS=1",
            ],
        )
        self.assertTrue(self.printed[-1].startswith("seeds: 4 of 6 shifts passed, 2 failing"))

    def test_missing_shifts(self):
        # A chunk never written (shifts 3-5), then one class missing at shift 1.
        self.write(0, chunk(0, 3))
        self.assertEqual(self.status(), 2)
        self.assertEqual(self.printed[0], "MISSING shift 3: all 3 classes")
        self.assertEqual(self.status("0-2"), 0)
        self.write(0, chunk(0, 3, skip=((1, "Two"),)))
        self.assertEqual(self.status("0-2"), 2)
        self.assertEqual(self.printed[0], "MISSING shift 1: test_a.Two")

    def test_chunk_without_end_crashed(self):
        self.write(0, chunk(0, 3))
        self.write(3, chunk(3, 3, ended=False))
        self.assertEqual(self.status(), 2)
        self.assertEqual(
            self.printed[0], "CRASHED %s/seeds_3.jsonl: the chunk did not end (no end line)" % TMP
        )
        self.write(3, chunk(3, 3)[:-20])  # its end line cut short
        self.assertEqual(self.status(), 2)
        self.write(3, chunk(3, 3))
        self.assertEqual(self.status(), 0)

    def test_nothing_ran(self):
        self.write(0, "")
        self.assertEqual(self.status(), 2)
