"""Verdicts of a campaign (`tools/campaign.py`), on known values and synthetic records.

The tests against values computed by scipy 1.15.3: McNemar
(``binomtest``), Fisher (``fisher_exact``), Welch (``ttest_ind``,
``equal_var=False``), Wilcoxon (``wilcoxon``, ``zero_method="wilcox"``,
above 50 pairs or ``method="asymptotic"``; ties and zeros included),
Benjamini-Hochberg (``false_discovery_control``). ``bounds`` on synthetic
records and seeds lines: each hard breach, the recovery failures on the
root's queue alone and MP10 outside overload listed apart, failing,
missing and crashed seeds shifts, the exit codes. ``compare``: runs
paired by seed, the common cells only, a field one side lacks not
tested, labels of one tree, seeds classes, and ``--exact`` on identical
and on differing records and lines.
"""

import json
import os
import unittest

from test_large import make_record

from tools import campaign

TMP = "build/test_campaign.tmp"  # relative to the repository root, where the tests run
CLASSES = (("test_a", "One"), ("test_a", "Two"))


def remove(path):
    """Remove the file or directory tree path, if it exists."""
    try:
        mode = os.stat(path)[0]
    except OSError:
        return
    if mode & 0x4000:
        for entry in os.listdir(path):
            remove("%s/%s" % (path, entry))
        os.rmdir(path)
    else:
        os.remove(path)


def makedirs(path):
    head = ""
    for part in path.split("/"):
        head = part if not head else "%s/%s" % (head, part)
        try:
            os.mkdir(head)
        except OSError:
            pass


def write(directory, records, name=None):
    """Write records as JSON files into TMP/directory (one per run)."""
    makedirs("%s/%s" % (TMP, directory))
    for r in records:
        base = (
            name(r)
            if name
            else "%s_%s_%d_%d_%s"
            % (
                r["scenario"],
                r["topology"],
                r["seed"],
                r["nodes"],
                r["load"],
            )
        )
        with open("%s/%s/%s_%s.json" % (TMP, directory, base, r["variant"]), "w") as f:
            f.write(json.dumps(r) + "\n")


def write_seeds(directory, first, count, label="current", failing=(), ended=True, ms=100):
    """Write the seeds chunk of shifts first to first + count - 1 of CLASSES into TMP/directory.

    failing: (shift, class) pairs failing test_x.
    """
    makedirs("%s/%s" % (TMP, directory))
    out = []
    for k in range(first, first + count):
        for module, case in CLASSES:
            bad = (k, case) in failing
            failures = [{"test": "test_x", "message": "AssertionError: x"}] if bad else []
            line = {"shift": k, "module": module, "class": case, "ok": not bad, "tests": 1}
            line.update({"failures": failures, "ms": ms, "variant": label, "rev": "abc1234"})
            out.append(json.dumps(line))
    if ended:
        out.append(json.dumps({"end": True, "first": first, "count": count}))
    with open("%s/%s/seeds_%d_%s.jsonl" % (TMP, directory, first, label), "w") as f:
        f.write("\n".join(out) + "\n")


def overload(seed, nodes, failed=0, **fields):
    """Return the record of an s15 star run (an overload case), its recovery failed failed times.

    nodes: the boards holding messages at the end.
    """
    recovery = {"violations": failed, "nodes": nodes, "queued": len(nodes)}
    fields.setdefault("status", "FAIL" if failed else "overload")
    return make_record(load="s15", seed=seed, recovery=recovery, **fields)


class TmpTest(unittest.TestCase):
    """A test writing its inputs under TMP, removed afterwards."""

    def setUp(self):
        remove(TMP)
        makedirs(TMP)

    def tearDown(self):
        remove(TMP)


class StatisticsTest(unittest.TestCase):
    """The tests and the BH adjustment, against scipy 1.15.3."""

    def close(self, value, expected, rel=1e-9):
        self.assertTrue(
            abs(value - expected) <= rel * abs(expected), "%r vs %r" % (value, expected)
        )

    def test_mcnemar(self):
        self.close(campaign.mcnemar(19, 0), 3.814697265625e-06)  # 19 against 0: 3.8e-6
        self.close(campaign.mcnemar(0, 19), 3.814697265625e-06)
        self.close(campaign.mcnemar(10, 3), 0.09228515625)
        self.close(campaign.mcnemar(0, 5), 0.0625)
        self.close(campaign.mcnemar(40, 8), 3.3052667092192682e-06)
        self.close(campaign.mcnemar(266, 25), 4.8935951044434664e-52)
        self.assertEqual(campaign.mcnemar(3, 3), 1.0)

    def test_fisher(self):
        self.close(campaign.fisher(3, 97, 20, 80), 0.00022398367242939325)
        self.close(campaign.fisher(42, 58, 74, 26), 7.419970757967605e-06)
        self.close(campaign.fisher(0, 300, 6, 294), 0.03047137597331363)
        self.close(campaign.fisher(25, 75, 50, 50), 0.00042033045869994034)
        self.close(campaign.fisher(2, 7, 8, 2), 0.02301413756522116)
        # tables as probable as the observed one but for rounding: counted (tolerance)
        self.close(campaign.fisher(0, 3, 8, 5), 0.19999999999999998)
        self.close(campaign.fisher(2, 1, 4, 10), 0.5147058823529411)
        # symmetric tables: the table mirroring the observed one counted
        self.close(campaign.fisher(7, 3, 3, 7), 0.1788954079975752)
        self.assertEqual(campaign.fisher(5, 5, 5, 5), 1.0)
        self.assertEqual(campaign.fisher(1, 99, 0, 100), 1.0)

    def test_welch(self):
        a = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3]
        b = [2, 7, 1, 8, 2, 8, 1, 8, 2, 8, 4, 5]
        self.close(campaign.welch(a, b), 0.5180269060345886)
        self.close(campaign.welch([0] * 10, [0, 0, 1, 0, 2, 0, 0, 1, 0, 0]), 0.1038881310621017)
        e = [i % 7 for i in range(300)]
        f = [(i * 3) % 7 + 2 for i in range(300)]
        self.close(campaign.welch(e, f), 4.8669232486600475e-31, 1e-7)
        g = [0.5 * i + (i % 3) * 0.25 for i in range(15)]
        h = [0.45 * i + 1.1 for i in range(18)]
        self.close(campaign.welch(g, h), 0.16100073807059023)

    def test_welch_constant_samples(self):
        # Both sides constant (as the prototype): 0 when the means differ, else 1.
        self.assertEqual(campaign.welch([2, 2, 2], [2, 2]), 1.0)
        self.assertEqual(campaign.welch([2, 2, 2], [3, 3]), 0.0)

    def test_betainc(self):
        self.close(campaign.betainc(5.0, 0.5, 0.3), 0.0006913033857629756)
        self.close(campaign.betainc(150.0, 0.5, 0.97), 0.002524730899404836)
        self.close(campaign.betainc(2.5, 0.5, 0.999), 0.9463423453081866)

    def test_wilcoxon_ties_and_zeros(self):
        # 60 pairs, 5 zero differences, ties: scipy's normal approximation.
        a = [(i * 7) % 11 for i in range(60)]
        b = [(i * 5) % 9 for i in range(60)]
        self.close(campaign.wilcoxon(a, b), 0.0658225923927253)
        self.close(campaign.wilcoxon(b, a), 0.0658225923927253)  # two-sided
        # 20 pairs with zeros and ties (scipy: method="asymptotic", also its choice here)
        a = [(i * 3) % 5 for i in range(20)]
        b = [(i * 2) % 7 for i in range(20)]
        self.close(campaign.wilcoxon(a, b), 0.13159269611041666)

    def test_wilcoxon_floats_and_shift(self):
        a = [round((i * 0.37) % 3.1, 2) for i in range(80)]
        b = [round(x + ((i * 13) % 7 - 2) * 0.1, 2) for i, x in enumerate(a)]
        self.close(campaign.wilcoxon(a, b), 2.4403969245967337e-05)
        a = list(range(100))
        self.close(campaign.wilcoxon(a, [x + 1 + (x % 3) for x in a]), 1.3318191740832362e-18)
        self.assertEqual(campaign.wilcoxon([1, 2, 3], [1, 2, 3]), 1.0)

    def test_bh(self):
        q = campaign.bh([0.01, 0.04, 0.03, 0.005, 0.5])
        for value, expected in zip(q, [0.025, 0.05, 0.05, 0.025, 0.5]):
            self.close(value, expected)
        self.assertEqual(campaign.bh([]), [])


class BoundsTest(TmpTest):
    """bounds: hard breaches, runs listed apart, seeds failures, exit codes."""

    def bounds(self, label=None, paths=None):
        self.printed = []
        return campaign.bounds(paths or [TMP], label, self.printed.append)

    def kind(self, name):
        # The lines of the kind name: its header, then its cells and runs.
        for i, line in enumerate(self.printed):
            if line.startswith(name + ": "):
                out = [line]
                for more in self.printed[i + 1 :]:
                    if not more.startswith("  "):
                        break
                    out.append(more)
                return out
        self.fail("no kind %s in %r" % (name, self.printed))

    def test_healthy(self):
        write("a", [make_record(seed=s) for s in range(1, 4)])
        write("a", [overload(s, []) for s in range(1, 3)])
        write_seeds("s", 0, 3)
        self.assertEqual(self.bounds(), 0)
        for name, _, _, _ in campaign.KINDS:
            self.assertEqual(self.kind(name), ["%s: none" % name])
        self.assertIn("seeds, failing (shift, class): none", self.printed)

    def test_each_breach(self):
        delivery = {"submitted": 10, "delivered": 9, "failed": 0, "lost": 1, "duplicates": 0}
        write(
            "a",
            [
                make_record(seed=1, delivery=delivery, violations={"MP5": 1}, status="FAIL"),
                make_record(seed=2, violations={"MP11": 2}, status="FAIL"),
                make_record(seed=3, violations={"MP9": 1}, status="FAIL"),
                make_record(seed=4, violations={"MP13": 1}, status="FAIL"),
                make_record(seed=5),
                overload(6, ["0000", "0101"], 2),  # a relay's queue too
                overload(7, ["0000"], 1),  # the root's alone: not a breach
                overload(8, [], 0),
            ],
        )
        old = overload(9, [], 1)
        del old["recovery"]["nodes"]  # a record without the field: a breach
        write("a", [old])
        self.assertEqual(self.bounds(), 1)
        self.assertEqual(
            self.kind("silent loss (MP5)"),
            [
                "silent loss (MP5): BREACH, 1 run",
                "  current steady forming star 30: 1 of 5 runs",
                '    make large RUN="forming star 1 30 steady"  (1 silent)',
            ],
        )
        for name, seed in (("MP11", 2), ("MP9", 3), ("MP13", 4)):
            lines = self.kind(name)
            self.assertEqual(lines[0], "%s: BREACH, 1 run" % name)
            self.assertEqual(lines[2], '    make large RUN="forming star %d 30 steady"' % seed)
        lines = self.kind("recovery failed, not on the root's queue alone")
        self.assertEqual(
            lines[0], "recovery failed, not on the root's queue alone: BREACH, 2 runs"
        )
        self.assertEqual(lines[1], "  current s15 forming star 30: 2 of 4 runs")
        self.assertIn("0000 0101", lines[2])
        self.assertIn("not recorded", lines[3])
        self.assertEqual(
            self.kind("recovery failed on the root's queue alone")[2],
            '    make large RUN="forming star 7 30"',
        )
        self.assertTrue(self.printed[-1].startswith("bounds: 6 runs with a hard breach"))

    def test_listed_apart(self):
        write(
            "a",
            [
                overload(1, ["0000"], 3),
                make_record(seed=2, violations={"MP10": 1}, status="FAIL"),
                make_record(seed=3, load="idle", violations={"MP10": 2}, status="FAIL"),
                overload(4, [], 0, violations={"MP10": 5}),  # accepted in the overload
            ],
        )
        write_seeds("s", 0, 3, failing=((1, "Two"),))
        self.assertEqual(self.bounds(), 0)
        lines = self.kind("MP10 outside overload runs")
        self.assertEqual(lines[0], "MP10 outside overload runs: listed, 2 runs")
        self.assertEqual(
            lines[1:],
            [
                "  current idle forming star 30: 1 of 1 runs",
                '    make large RUN="forming star 3 30 idle"  (2 MP10)',
                "  current steady forming star 30: 1 of 1 runs",
                '    make large RUN="forming star 2 30 steady"  (1 MP10)',
            ],
        )
        lines = self.kind("recovery failed on the root's queue alone")
        self.assertTrue(lines[0].endswith(": listed, 1 run"))
        self.assertIn("  current test_a.Two: 1 of 3 shifts; test_x 1", self.printed)
        self.assertIn(
            "   reproduce: make seeds SEEDS=1 SEEDS_FIRST=1 SEEDS_CLASS=test_a.Two SEEDS_JOBS=1",
            self.printed,
        )

    def test_label(self):
        write("a", [make_record(seed=1)])
        bad = make_record(seed=1, variant="other", violations={"MP11": 1}, status="FAIL")
        write("a", [bad])
        self.assertEqual(self.bounds(), 1)
        self.assertEqual(self.bounds("current"), 0)
        self.assertEqual(self.bounds("absent"), 2)  # nothing to judge

    def test_input_problems(self):
        write("a", [make_record(seed=1)])
        write_seeds("s", 0, 3)
        self.assertEqual(self.bounds(), 0)
        self.assertEqual(self.bounds(paths=[TMP, TMP + "/none"]), 2)
        with open(TMP + "/a/cut.json", "w") as f:
            f.write('{"variant": "current", "load"')
        self.assertEqual(self.bounds(), 2)
        self.assertTrue(any(x.startswith("INPUT %s/a/cut.json" % TMP) for x in self.printed))
        os.remove(TMP + "/a/cut.json")
        write("b", [make_record(seed=1)])  # the same run twice
        self.assertEqual(self.bounds(), 2)
        remove(TMP + "/b")
        self.assertEqual(self.bounds(), 0)

    def test_seeds_missing_or_crashed(self):
        write_seeds("s", 0, 3)
        write_seeds("s", 6, 3)  # shifts 3-5 missing
        self.assertEqual(self.bounds(), 2)
        self.assertIn("MISSING current shift 3: all 2 classes", self.printed)
        write_seeds("s", 3, 3, ended=False)
        self.assertEqual(self.bounds(), 2)
        write_seeds("s", 3, 3)
        self.assertEqual(self.bounds(), 0)


def counted(seed, relay, **fields):
    """Return a healthy record whose run counted relay relay detaches."""
    fields["detaches"] = {"relay": relay, "relay_false": 0, "leaf": 0}
    return make_record(seed=seed, **fields)


class CompareTest(TmpTest):
    """compare: pairing by seed, common cells, not comparable fields, labels, seeds, --exact."""

    def compare(self, label=None, show_all=False, exact=False, base="base", new="new"):
        self.printed = []
        b, n = "%s/%s" % (TMP, base), "%s/%s" % (TMP, new)
        return campaign.compare(b, n, label, 0.01, show_all, exact, self.printed.append)

    def rows(self, name):
        return [line for line in self.printed if line.startswith("   %-24s" % name)]

    def test_paired_by_seed(self):
        # NEW has one more relay detach on every seed: significant only when paired.
        seeds = range(1, 26)
        write("base", [counted(s, (s % 5) * 3) for s in seeds])
        # file names out of seed order: the pairing is by seed
        write(
            "new",
            [counted(s, (s % 5) * 3 + 1) for s in seeds],
            lambda r: "x%d" % (r["seed"] * 7 % 25),
        )
        self.assertEqual(self.compare(), 1)
        self.assertIn("== steady forming star 30: runs 25/25, paired on 25 seeds", self.printed)
        row = self.rows("relay_detaches")
        self.assertEqual(len(row), 1)
        self.assertIn("(new>base 25, new<base 0)", row[0])
        self.assertIn(" Wilcoxon p=5.7e-07 ", row[0])
        self.assertIn("higher", row[0])

    def test_unpaired_below_twenty_seeds(self):
        seeds = range(1, 11)
        write("base", [counted(s, (s % 5) * 3) for s in seeds])
        write("new", [counted(s, (s % 5) * 3 + 1) for s in seeds])
        self.assertEqual(self.compare(), 0)  # Welch on per-run values: no difference
        self.assertEqual(self.rows("relay_detaches"), [])
        self.assertEqual(self.compare(show_all=True), 0)
        self.assertIn(
            "== steady forming star 30: runs 10/10, 10 common seeds: unpaired", self.printed
        )
        p = campaign.welch([(s % 5) * 3 for s in seeds], [(s % 5) * 3 + 1 for s in seeds])
        self.assertIn("Welch p=%.2g" % p, self.rows("relay_detaches")[0])
        self.assertIn("Fisher", self.rows("relay_detach")[0])

    def test_common_cells_only(self):
        write("base", [counted(s, 0) for s in range(1, 21)])
        write("base", [counted(s, 0, scenario="lossy") for s in range(1, 21)])
        write("new", [counted(s, 9) for s in range(1, 21)])
        write("new", [counted(s, 9, topology="star2b") for s in range(1, 21)])
        self.assertEqual(self.compare(), 1)
        self.assertIn("cells only in BASE: 1", self.printed)
        self.assertIn("   steady lossy star 30 (20)", self.printed)
        self.assertIn("   steady forming star2b 30 (20)", self.printed)
        heads = [line for line in self.printed if line.startswith("== ")]
        self.assertEqual(heads, ["== steady forming star 30: runs 20/20, paired on 20 seeds"])

    def test_field_absent_not_comparable(self):
        acks = {"passive": 10, "late": 2, "duplicates": 1, "spurious": 1}
        write("base", [make_record(seed=s) for s in range(1, 21)])
        stale = dict(acks, stale_hop=3, stale_hop_messages=1)
        write("new", [make_record(seed=s, acks=stale) for s in range(1, 21)])
        self.assertEqual(self.compare(show_all=True), 0)
        self.assertIn(
            "not comparable, not tested: stale_hop, BASE lacks it in 1 cells", self.printed
        )
        self.assertIn(
            "not comparable, not tested: stale_hop_attempts, BASE lacks it in 1 cells",
            self.printed,
        )
        self.assertEqual(self.rows("stale_hop"), [])
        self.assertEqual(self.rows("stale_hop_attempts"), [])
        self.assertTrue(self.printed[-1].startswith("comparisons: 0 "))

    def test_labels_of_one_tree(self):
        write("tree", [counted(s, 0) for s in range(1, 21)])
        write("tree", [counted(s, 2, variant="other") for s in range(1, 21)])
        self.assertEqual(self.compare(base="tree", new="tree"), 2)  # two labels: name one
        self.assertEqual(self.compare("current:other", base="tree", new="tree"), 1)
        self.assertIn("(new>base 20, new<base 0)", self.rows("relay_detaches")[0])
        self.assertEqual(self.compare("current", base="tree", new="tree"), 0)  # itself
        self.assertEqual(self.compare("current:absent", base="tree", new="tree"), 2)

    def test_seeds_classes(self):
        failing = [(k, "Two") for k in range(12)]
        write_seeds("base", 0, 30)
        write_seeds("new", 0, 30, failing=failing)
        self.assertEqual(self.compare(), 1)
        row = self.rows("failed")
        self.assertEqual(len(row), 1)
        self.assertIn("base       0/30  new      12/30", row[0])
        self.assertIn("McNemar p=0.00049", row[0])
        self.assertIn("== seeds test_a.Two: shifts 30/30, paired on 30 shifts", self.printed)

    def test_exact_identical(self):
        records = [overload(s, ["0000"], 1) for s in range(1, 4)]
        write("base", records)
        for r in records:
            r.update({"wall_s": 99.0, "rev": "def5678", "variant": "new"})
        write("new", records + [make_record(seed=9, variant="new")])  # one run more
        write_seeds("base", 0, 3)
        write_seeds("new", 0, 3, label="new", ms=7)
        self.assertEqual(self.compare("current:new", exact=True), 0)
        self.assertIn(
            "exact: 3 common runs, 3 identical, 0 differ; 0 runs only in BASE, 1 only in NEW",
            self.printed,
        )
        self.assertIn(
            "exact: 6 common seeds lines, 6 identical, 0 differ; 0 only in BASE, 0 only in NEW",
            self.printed,
        )

    def test_exact_differs(self):
        records = [make_record(seed=s) for s in range(1, 4)]
        write("base", records)
        records[1]["delivery"] = dict(records[1]["delivery"], delivered=99, lost=1)
        write("new", records)
        write_seeds("base", 0, 3)
        write_seeds("new", 0, 3, failing=((2, "One"),))
        self.assertEqual(self.compare(exact=True), 1)
        i = self.printed.index(
            "exact: 3 common runs, 2 identical, 1 differ; 0 runs only in BASE, 0 only in NEW"
        )
        self.assertEqual(
            self.printed[i + 1 : i + 4],
            [
                'DIFF steady forming star 30 seed 2: make large RUN="forming star 2 30 steady"',
                "   delivery.delivered 100 -> 99",
                "   delivery.lost 0 -> 1",
            ],
        )
        self.assertIn(
            "DIFF seeds shift 2 test_a.One: make seeds SEEDS=1 SEEDS_FIRST=2 "
            "SEEDS_CLASS=test_a.One SEEDS_JOBS=1",
            self.printed,
        )
        self.assertIn("   ok true -> false", self.printed)
        remove(TMP + "/new")
        write("new", [make_record(seed=7)])
        self.assertEqual(self.compare(exact=True), 2)  # nothing in common
