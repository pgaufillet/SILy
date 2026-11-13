"""Sweep tooling of the large-network suite (`tests/large.py`), without playing a run.

The 95 % Wilson interval on known values; the sweep plan's seed ranges,
filters and order; the aggregation of synthetic records into per-run
indicators and per-run counts, those of a field a record lacks (an
older record) not counted; a run's record names the boards still holding
messages at the end and counts the attempts to a stale hop; a record
already written is skipped (a sweep resumes); the load names round trip
through a run's name, and only the ``s15`` load makes the star an
overload case.
"""

import os
import unittest

import large

from mesh.frame import ROOT
from sim import invariants

TMP = "build/test_large.tmp"  # relative to the repository root, where the tests run


def make_record(**fields):
    """Return a record as `large.result` writes it, healthy, with fields replaced."""
    r = {
        "variant": "current",
        "rev": "abc1234",
        "scenario": "forming",
        "topology": "star",
        "seed": 1,
        "nodes": 30,
        "load": "steady",
        "status": "ok",
        "strict": True,
        "violations": {},
        "failing": {},
        "delivery": {"submitted": 100, "delivered": 100, "failed": 0, "lost": 0, "duplicates": 0},
        "failures": {"connected": 0, "detached": 0, "cut_off": 0},
        "floats": {"count": 0, "by_g": 0, "ungrounded_s": 0.0},
        "detaches": {"relay": 0, "relay_false": 0, "leaf": 0},
        "acks": {"passive": 10, "late": 2, "duplicates": 1, "spurious": 1},
        "recovery": None,
        "accepted": None,
        "wall_s": 10.0,
    }
    r.update(fields)
    return r


class WilsonTest(unittest.TestCase):
    """The 95 % Wilson score interval, on values computed independently."""

    def test_none_of_300(self):
        lo, hi = large.wilson(0, 300)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.01264, places=5)

    def test_three_of_300(self):
        lo, hi = large.wilson(3, 300)
        self.assertAlmostEqual(lo, 0.00341, places=5)
        self.assertAlmostEqual(hi, 0.02898, places=5)

    def test_none_of_100(self):
        lo, hi = large.wilson(0, 100)
        self.assertAlmostEqual(hi, 0.03699, places=5)

    def test_all_and_no_trial(self):
        lo, hi = large.wilson(300, 300)
        self.assertAlmostEqual(lo, 1 - 0.01264, places=5)
        self.assertEqual(hi, 1.0)
        self.assertEqual(large.wilson(0, 0), (0.0, 1.0))


class PlanTest(unittest.TestCase):
    """sweep-plan: seed ranges, filters, the largest networks first."""

    def test_default(self):
        plan = large.sweep_plan()
        self.assertEqual(len(plan), 100 * len(large.SCENARIOS) * len(large.TOPOLOGIES))
        self.assertEqual(plan[0], ("forming", "domain", 1, 30, "s15"))
        self.assertEqual(plan[-1][2], 100)

    def test_seed_range_and_filters(self):
        options = large.sweep_options(
            [
                "--seeds",
                "3-5",
                "--scenarios",
                "lossy,forming",
                "--topologies",
                "star",
                "--loads",
                "steady,idle",
            ]
        )
        plan = large.sweep_plan(**options)
        self.assertEqual(len(plan), 3 * 2 * 2)
        self.assertEqual(sorted(set(p[2] for p in plan)), [3, 4, 5])
        self.assertEqual(set(p[0] for p in plan), {"lossy", "forming"})
        self.assertEqual(set(p[1] for p in plan), {"star"})
        self.assertEqual(set(p[4] for p in plan), {"steady", "idle"})
        self.assertEqual(large.sweep_options(["--seeds", "7"])["seeds"], (7, 7))

    def test_largest_first(self):
        plan = large.sweep_plan(**large.sweep_options(["--seeds", "1-2", "--nodes", "30,64"]))
        nodes = [p[3] for p in plan]
        self.assertEqual(nodes, sorted(nodes, reverse=True))
        self.assertEqual(nodes.count(64), nodes.count(30))

    def test_unknown_names_refused(self):
        for args in (
            ["--scenarios", "forming,flood"],
            ["--topologies", "ring"],
            ["--loads", "peak"],
            ["--seed", "1"],
            ["--seeds"],
        ):
            with self.assertRaises(ValueError):
                large.sweep_options(args)


class AggregateTest(unittest.TestCase):
    """aggregate: groups, per-run indicators k of n, per-run counts, failing runs."""

    def test_groups_and_indicators(self):
        records = [make_record(seed=s) for s in range(1, 8)]
        records.append(
            make_record(
                seed=8,
                status="FAIL",
                strict=False,
                violations={"MP5": 2},
                delivery={"failed": 3, "lost": 2, "duplicates": 1},
                failures={"connected": 1, "detached": 2, "cut_off": 0},
                acks={"passive": 20, "late": 9, "duplicates": 4, "spurious": 3},
            )
        )
        records.append(make_record(seed=9, detaches={"relay": 2, "relay_false": 1, "leaf": 5}))
        records.append(make_record(seed=1, load="idle"))
        groups = large.aggregate(records)
        self.assertEqual(
            [g["key"] for g in groups],
            [
                ("current", "idle", "forming", "star", 30),
                ("current", "steady", "forming", "star", 30),
            ],
        )
        g = groups[1]
        self.assertEqual(g["runs"], 9)
        found = dict((name, (k, n)) for name, k, n in g["indicators"])
        self.assertEqual(found["failed"], (1, 9))
        self.assertEqual(found["violation"], (1, 9))
        self.assertEqual(found["silent_loss"], (1, 9))
        self.assertEqual(found["duplicate"], (1, 9))
        self.assertEqual(found["relay_detach"], (1, 9))
        self.assertEqual(found["relay_detach_false"], (1, 9))
        self.assertEqual(found["failed_connected"], (1, 9))
        self.assertEqual(found["MP5"], (1, 9))
        self.assertEqual(found["recovery_failed"], (0, 0))  # no overload run
        totals = dict((name, (t, n)) for name, t, n in g["totals"])
        self.assertEqual(totals["late_acks"], (8 * 2 + 9, 9))
        self.assertEqual(totals["spurious_retries"], (8 + 3, 9))
        self.assertEqual(totals["failed_class1"], (3, 9))
        self.assertEqual(totals["relay_detaches"], (2, 9))
        self.assertEqual(totals["leaf_detaches"], (5, 9))
        self.assertEqual(totals["failed_detached"], (2, 9))
        self.assertEqual(totals["lost"], (2, 9))
        self.assertEqual(totals["delivered"], (8 * 100, 9))  # absent from seed 8: 0
        self.assertEqual(totals["dup_acks"], (8 * 1 + 4, 9))
        self.assertEqual(totals["mp10_accepted"], (0, 9))  # no overload run
        self.assertEqual(g["failing"], ["forming star 8 30 steady"])
        self.assertEqual(g["revs"], ["abc1234"])

    def test_recovery_only_where_checked(self):
        recovery = {"violations": 0}
        records = [
            make_record(load="s15", status="overload", recovery=recovery),
            make_record(load="s15", seed=2, status="FAIL", recovery={"violations": 3}),
        ]
        found = dict((n, (k, m)) for n, k, m in large.aggregate(records)[0]["indicators"])
        self.assertEqual(found["recovery_failed"], (1, 2))
        self.assertEqual(found["failed"], (1, 2))

    def test_fields_absent_not_counted(self):
        # Records written before the root-queue and stale-hop fields: not applicable.
        old = make_record(load="s15", status="FAIL", recovery={"violations": 1})
        acks = {"passive": 1, "late": 0, "duplicates": 0, "spurious": 0}
        root = make_record(
            load="s15",
            seed=2,
            status="FAIL",
            recovery={"violations": 1, "nodes": ["0000"]},
            acks=dict(acks, stale_hop=5, stale_hop_messages=2),
        )
        relay = make_record(
            load="s15",
            seed=3,
            status="FAIL",
            recovery={"violations": 2, "nodes": ["0000", "0003"]},
            acks=dict(acks, stale_hop=0, stale_hop_messages=0),
        )
        g = large.aggregate([old, root, relay])[0]
        found = dict((n, (k, m)) for n, k, m in g["indicators"])
        self.assertEqual(found["root_queue_only"], (1, 2))
        self.assertEqual(found["stale_hop"], (1, 2))
        totals = dict((name, (t, n)) for name, t, n in g["totals"])
        self.assertEqual(totals["stale_hop_attempts"], (5, 2))
        self.assertEqual(totals["stale_hop_messages"], (2, 2))
        self.assertEqual(totals["late_acks"], (2, 3))


class Stop(Exception):
    pass


class Stub:
    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


class ResultTest(unittest.TestCase):
    """result: the boards still holding messages at the end, the attempts to a stale hop."""

    def test_recovery_nodes_and_stale_hop(self):
        r = large.Run("lossy", "star", 4, 30, "s15")
        r.recovery = invariants.Recovery(r.after_ms)
        r.recovery.nodes = [0x0102, ROOT]  # in the order of the boards
        r.recovery.copies = 3
        floats = invariants.Floats()
        floats.count, floats.by_g, floats.ungrounded_ms = 0, 0, 0
        detaches = invariants.Detaches()
        detaches.relay, detaches.relay_false, detaches.leaf = 0, 0, 0
        acks = invariants.Acknowledgements()
        acks.passive, acks.late, acks.duplicates, acks.spurious = 1, 0, 0, 0
        acks.stale_hop = 4
        acks.stale_messages = set([(1, ROOT, 20, 5), (2, ROOT, 20, 7)])
        r.checker = Stub(checks=[floats, detaches, acks, r.recovery])
        r.delivery = Stub(counts={"submitted": 2}, failures={"connected": 0})
        r.violations = r.failing = r.accepted = []
        r.refused = r.failed = 0
        r.wall_ms = 1000
        record = large.result(r)
        self.assertEqual(record["recovery"]["nodes"], ["0000", "0102"])
        self.assertEqual(record["recovery"]["queued"], 3)
        self.assertEqual(
            (record["acks"]["stale_hop"], record["acks"]["stale_hop_messages"]), (4, 2)
        )
        r.recovery.nodes = [ROOT]
        self.assertEqual(large._root_queue_only(large.result(r)), True)
        r.recovery.nodes = []
        self.assertEqual(large._root_queue_only(large.result(r)), False)


class RecordTest(unittest.TestCase):
    """record: a run whose record exists is not played again."""

    ARGS = ["forming", "star", "3", "30", "idle"]

    def setUp(self):
        for d in ("build", TMP):
            try:
                os.mkdir(d)
            except OSError:
                pass
        self.path = "%s/forming_star_3_30_idle.json" % TMP

    def tearDown(self):
        for entry in os.listdir(TMP):
            os.remove("%s/%s" % (TMP, entry))
        os.rmdir(TMP)

    def test_existing_record_skipped(self):
        played = []
        with open(self.path, "w") as f:
            f.write("{}\n")
        self.assertEqual(large.record(TMP, self.ARGS, played.append), 0)
        self.assertEqual(played, [])
        with open(self.path) as f:
            self.assertEqual(f.read(), "{}\n")

    def test_missing_record_played(self):
        played = []

        def play(args):
            played.append(args)
            raise Stop  # before anything is written

        with self.assertRaises(Stop):
            large.record(TMP, self.ARGS, play)
        self.assertEqual(played, [self.ARGS])
        self.assertEqual(os.listdir(TMP), [])


class LoadsTest(unittest.TestCase):
    """Loads: names round trip through run names; only s15 overloads the star."""

    def test_names_round_trip(self):
        for load in large.LOADS:
            r = large.Run("lossy", "star2b", 4, 64, load)
            args = r.name.split()
            self.assertEqual(large._args(args), ("lossy", "star2b", 4, 64, load))
            self.assertEqual(large.record_name(*large._args(args)), "lossy_star2b_4_64_%s" % load)
        self.assertEqual(large.Run("forming", "star", 1).name, "forming star 1 30")

    def test_load_parameters(self):
        s15 = large.Run("forming", "star", 1, 30, "s15")
        steady = large.Run("forming", "star", 1, 30, "steady")
        idle = large.Run("forming", "star", 1, 30, "idle")
        self.assertEqual((s15.rate, s15.burst, s15.overload), (1000, 100, True))
        self.assertEqual((steady.rate, steady.burst, steady.overload), (1000, 0, False))
        self.assertEqual((idle.rate, idle.burst, idle.overload), (0, 0, False))
        self.assertFalse(large.Run("forming", "domain", 1, 30, "s15").overload)
        with self.assertRaises(ValueError):
            large.Run("forming", "star", 1, 30, "peak")
