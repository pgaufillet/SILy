"""Invariant checks of `sim.invariants`, each shown able to fail (MP5, MP9-11, MP13, recovery).

Small networks of real link engines with their routing (`mesh.routing.Routing`)
on `sim.net`, SF7 (slot 52 ms, Imin 1.7 s), presence period 10 s. Each check
runs once on a healthy network (no violation) and once with a defect
injected into a core (a cycle kept, a node that never selects, a version
ignored, a message dropped after its acknowledgement, a duty cycle not
enforced, a table larger than its bound, a node never routed, a rootless
cycle, a message still queued at the end): the check must fire, naming the
nodes at fault.
"""

import unittest

from mesh.dodag import DodagInfo
from mesh.frame import ROOT
from mesh.link import DELIVERED
from mesh.neighbours import NeighbourTable
from mesh.profile import EU_869
from mesh.routing import Routing
from sim.invariants import (
    Attachment,
    Checker,
    Delivery,
    DutyCycle,
    Loops,
    Recovery,
    Resources,
    VersionWave,
    Violation,
    overload,
    repair_ms,
)
from sim.net import Network

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
APP = 20
A, B, C, D, E = 0x00A1, 0x00B1, 0x00C1, 0x00D1, 0x00E1
FORMED = 40000  # every node attached on these graphs (ms)


def build(names, links, seed=1, capacity=None, **kwargs):
    """Return a network of names (root first) with routing; links: pairs, both ways.

    capacity: {name: neighbour table entries} for the boards given.
    """
    net = Network(seed, _KEY, **kwargs)

    def make(name):
        def routing(engine):
            if capacity is not None and name in capacity:
                engine.neighbours = NeighbourTable(engine.sf, capacity=capacity[name])
            return Routing(engine, EU_869, INFO)

        return routing

    for n in names:
        net.add(n, routing=make(n))
    for a, b in links:
        net.link_both(a, b)
    return net


def line(*names):
    """Links of the line root - names[0] - names[1] ..."""
    out = []
    prev = ROOT
    for n in names:
        out.append((prev, n))
        prev = n
    return out


def violations(checker, name):
    return [v for v in checker.violations if v.check == name]


def keep_cycle(net, a, b):
    """Make a and b keep each other as their only parent from now on."""
    for name, other in ((a, b), (b, a)):
        parents = net.boards[name].routing.parents

        def select(now, parents=parents, other=other):
            parents._set_members([other])
            return 0

        parents._select = select
        select(net.now)


class LoopsTest(unittest.TestCase):
    """MP11: a node kept in a cycle beyond the bound is reported."""

    def run_line(self, defect):
        net = build((ROOT, A, B, C), line(A, B, C))
        checker = Checker(net, [Loops()])
        checker.start()

        if defect:
            net.at(FORMED, keep_cycle, net, B, C)
        net.run(FORMED + 30000)
        return checker.finish(), checker

    def test_healthy_line_has_no_cycle(self):
        found, checker = self.run_line(False)
        self.assertEqual(violations(checker, "MP11"), [])
        self.assertEqual(checker.checks[0].episodes, 0)

    def test_kept_cycle_reported(self):
        found, checker = self.run_line(True)
        v = violations(checker, "MP11")
        self.assertEqual(len(v), 1)
        self.assertEqual(sorted(v[0].nodes), [B, C])
        bound = checker.checks[0].bound_ms()
        self.assertTrue(v[0].time > FORMED + bound)
        self.assertLessEqual(v[0].time, FORMED + bound + 2 * checker.every_ms)


class AttachmentTest(unittest.TestCase):
    """MP10: a node without route beyond the forming or repair bound is reported."""

    def test_forming_within_bound(self):
        net = build((ROOT, A, B, C), line(A, B, C))
        checker = Checker(net, [Attachment()])
        checker.start()
        net.run(FORMED + 60000)
        checker.finish()
        self.assertEqual(violations(checker, "MP10"), [])
        self.assertTrue(checker.checks[0].slowest["start"] > 0)

    def test_node_never_attached_reported(self):
        net = build((ROOT, A, B, C), line(A, B, C))
        net.boards[C].routing.parents.candidate_cost = lambda n, now: None  # no candidate
        check = Attachment()
        checker = Checker(net, [check])
        checker.start()
        net.run(150000)
        checker.finish()
        v = violations(checker, "MP10")
        self.assertEqual([x.nodes for x in v], [(C,)])
        self.assertTrue(v[0].time > check.bound_ms(3, Attachment.START))
        self.assertIn("after start", v[0].detail)

    def run_diamond(self, defect):
        # C under A or B; its preferred parent is switched off once formed.
        net = build((ROOT, A, B, C), ((ROOT, A), (ROOT, B), (A, C), (B, C)))
        check = Attachment()
        checker = Checker(net, [check])
        checker.start()
        killed = []

        def kill():
            c = net.boards[C]
            if defect:
                c.routing.parents.candidate_cost = lambda n, now: None  # no candidate
            killed.append(c.routing.parents.preferred)
            checker.switch_off(killed[0])

        net.at(FORMED, kill)
        net.run(FORMED + check.bound_ms(2, Attachment.REPAIR) + 10000)
        checker.finish()
        return violations(checker, "MP10"), check, killed[0]

    def test_repair_within_bound(self):
        found, check, killed = self.run_diamond(False)
        self.assertIn(killed, (A, B))
        self.assertEqual(found, [])
        self.assertTrue(check.slowest["repair"] > 0)

    def test_node_not_repaired_reported(self):
        found, check, killed = self.run_diamond(True)
        self.assertEqual([x.nodes for x in found], [(C,)])
        self.assertIn("after repair", found[0].detail)
        self.assertTrue(found[0].time > FORMED + check.bound_ms(2, Attachment.REPAIR))


class VersionWaveTest(unittest.TestCase):
    """MP10 (global repair): a node that does not adopt the new version is reported."""

    def run_line(self, defect):
        net = build((ROOT, A, B), line(A, B))
        check = VersionWave()
        checker = Checker(net, [check])
        checker.start()
        if defect:
            net.at(FORMED - 1, setattr, net.boards[B].routing.dodag, "_adopt", lambda *a: None)
        net.at(FORMED, checker.global_repair)
        net.run(FORMED + 3 * check.hop_ms() + 5000)
        checker.finish()
        return violations(checker, "MP10"), check

    def test_wave_within_bound(self):
        found, check = self.run_line(False)
        self.assertEqual(found, [])
        self.assertEqual(sorted(check.adopted), [A, B])

    def test_version_ignored_reported(self):
        found, check = self.run_line(True)
        self.assertEqual([x.nodes for x in found], [(B,)])
        self.assertTrue(found[0].time > FORMED + 2 * check.hop_ms())
        self.assertEqual(list(check.adopted), [A])


class DeliveryTest(unittest.TestCase):
    """MP5: a class 1 message neither delivered nor failed is reported."""

    def run_line(self, defect, kill=False):
        net = build((ROOT, A, B), line(A, B))
        net.boards[ROOT].listen(APP)
        check = Delivery(APP, 20000)
        checker = Checker(net, [check])
        checker.start()
        if defect:
            # A acknowledges B's messages, then drops them.
            engine = net.boards[A].engine
            forward = engine.forward

            def drop(msg, header, now, *rest):
                if msg.port == APP:
                    return DELIVERED
                return forward(msg, header, now, *rest)

            engine.forward = drop
        for i in range(5):
            net.at(FORMED + 1000 * i, check.submit, net.boards[B])
        if kill:
            # A's frames no longer reach the root: A holds every message
            # (retries) when it is switched off.
            net.at(FORMED - 1, net.medium.cut, A, ROOT)
            net.at(FORMED + 4500, checker.switch_off, A)
        # After A's death B keeps A, its last candidate, until A is silent
        # (2.5 periods), then holds what it still has (2 periods).
        net.run(FORMED + (60000 if kill else 40000))
        checker.finish()
        return violations(checker, "MP5"), check

    def test_all_delivered(self):
        found, check = self.run_line(False)
        self.assertEqual(found, [])
        self.assertEqual(check.counts["delivered"], 5)

    def test_dropped_after_acknowledgement_reported(self):
        found, check = self.run_line(True)
        self.assertEqual(len(found), 5)
        self.assertEqual(set(v.nodes for v in found), {(B,)})
        self.assertEqual(check.counts["lost"], 5)

    def test_switched_off_relay_exempt(self):
        # Messages A held when it died are not counted.
        found, check = self.run_line(False, kill=True)
        k = check.counts
        self.assertEqual(found, [])
        self.assertEqual(k["submitted"], 5)
        self.assertTrue(k["exempt"] >= 4)  # the last one may still be at B: failed there
        self.assertEqual(k["exempt"] + k["failed"], 5)


class DutyCycleTest(unittest.TestCase):
    """MP9: a board transmitting beyond its budget in a window is reported."""

    BUDGET = 3000
    WINDOW = 60000

    def run_pair(self, defect):
        net = build((ROOT, A), line(A), budget_ms=self.BUDGET, window_ms=self.WINDOW)
        checker = Checker(net, [DutyCycle()])
        checker.start()
        engine = net.boards[A].engine
        if defect:
            engine.dutycycle.allows = lambda now, priority, toa: True
            engine.dutycycle.admit = lambda now, fp, p, toa: p if p > fp else fp
        payload = bytes(150)

        def flood():
            net.boards[A].submit(APP, 0, ROOT, payload)
            if net.now < FORMED + 2 * self.WINDOW:
                net.at(net.now + 300, flood)

        net.at(FORMED, flood)
        net.run(FORMED + 2 * self.WINDOW)
        checker.finish()
        return violations(checker, "MP9"), checker.checks[0], net

    def test_budget_kept(self):
        found, check, net = self.run_pair(False)
        self.assertEqual(found, [])
        self.assertLessEqual(check.busiest[0], self.BUDGET)
        self.assertTrue(check.busiest[0] > self.BUDGET // 2)  # the flood used it

    def test_budget_exceeded_reported(self):
        found, check, net = self.run_pair(True)
        self.assertEqual([v.nodes for v in found], [(A,)])
        self.assertTrue(check.busiest[0] > self.BUDGET)


class ResourcesTest(unittest.TestCase):
    """MP13: a table above its bound is reported."""

    def run_mesh(self, defect):
        names = (ROOT, A, B, C, D, E)
        links = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                links.append((names[i], names[j]))
        capacity = dict((n, 4) for n in names)
        if defect:
            capacity[D] = 6  # table sized above the bound
        net = build(names, links, capacity=capacity)
        check = Resources(neighbours=4)
        checker = Checker(net, [check])
        checker.start()
        net.run(FORMED)
        checker.finish()
        return violations(checker, "MP13"), check

    def test_tables_within_bounds(self):
        found, check = self.run_mesh(False)
        self.assertEqual(found, [])
        self.assertEqual(check.highest["neighbours"], 4)

    def test_table_above_bound_reported(self):
        found, check = self.run_mesh(True)
        self.assertEqual([v.nodes for v in found], [(D,)])
        self.assertIn("neighbours 5 > bound 4", found[0].detail)


class RecoveryTest(unittest.TestCase):
    """Overload: a board neither routed nor detached at the deadline is reported, so is a
    message still queued at the end."""

    def run_line(self, names, defect=None, kill=None):
        # The overload ends at FORMED; the run ends 15 s after the deadline.
        net = build((ROOT,) + names, line(*names))
        net.boards[ROOT].listen(APP)
        check = Recovery(FORMED)
        checker = Checker(net, [check])
        if defect == "never":
            net.boards[names[-1]].routing.parents.candidate_cost = lambda n, now: None
        checker.start()
        if kill is not None:
            net.at(FORMED, checker.switch_off, kill)
        if defect == "cycle":
            net.at(FORMED + 1, keep_cycle, net, names[-2], names[-1])
        deadline = FORMED + check.detect_ms() + repair_ms(checker, len(names))
        end = deadline + 15000
        last = net.boards[names[-1]]
        if defect == "queued":
            # The last board's links cut just before it submits: its message stays.
            for a, b in ((names[-2], names[-1]), (names[-1], names[-2])):
                net.at(end - 6000, net.medium.cut, a, b)
            net.at(end - 6000, checker.links_changed)
        if kill is None and defect in (None, "queued"):
            net.at(end - 5000, last.submit, APP, 1, ROOT, b"punch")
        net.run(end)
        checker.finish()
        return violations(checker, "recovery"), check, net, deadline

    def test_recovered_line(self):
        found, check, net, deadline = self.run_line((A, B))
        self.assertEqual(found, [])
        self.assertEqual(check.deadline, deadline)
        self.assertTrue(deadline <= check.checked < deadline + 2000)
        self.assertEqual((check.routed, check.detached, check.copies), (2, 0, 0))
        self.assertEqual([r[3] for r in net.boards[ROOT].inbox if r[2] == APP], [b"punch"])

    def test_deadline_from_the_design_periods(self):
        # 3.6 presence periods, then the repair bound of MP10 at the depth seen.
        found, check, net, deadline = self.run_line((A, B))
        self.assertEqual(check.detect_ms(), 36000)  # T = 10 s
        self.assertEqual(check.depth, 2)

    def test_never_routed_reported(self):
        found, check, net, deadline = self.run_line((A, B), "never")
        self.assertEqual([v.nodes for v in found], [(B,)])
        self.assertIn("connected, no route", found[0].detail)
        self.assertTrue(found[0].time >= deadline)
        self.assertEqual(check.routed, 1)

    def test_cut_off_detached(self):
        # A dies: B and C, cut off, detach.
        found, check, net, deadline = self.run_line((A, B, C), kill=A)
        self.assertEqual(found, [])
        self.assertEqual((check.routed, check.detached), (0, 2))

    def test_rootless_cycle_reported(self):
        found, check, net, deadline = self.run_line((A, B, C), "cycle", kill=A)
        self.assertEqual(sorted(v.nodes for v in found), [(B,), (C,)])
        self.assertTrue(all("cut off, rootless (preferred" in v.detail for v in found))
        self.assertEqual((check.routed, check.detached), (0, 0))

    def test_queued_at_the_end_reported(self):
        found, check, net, deadline = self.run_line((A, B), "queued")
        self.assertEqual([v.nodes for v in found], [(B,)])
        self.assertIn("still queued at the end (ports %d)" % APP, found[0].detail)
        self.assertEqual(check.copies, 1)


class OverloadTest(unittest.TestCase):
    """The split of an overload run: MP10 within the window accepted, the rest failing."""

    def test_split(self):
        vs = [
            Violation(1000, "MP10", (A,), "before the window"),
            Violation(5000, "MP10", (A,), "in the window"),
            Violation(6000, "MP11", (B,), "loop in the window"),
            Violation(7000, "MP5", (B,), "silent"),
            Violation(9000, "MP10", (C,), "at the deadline"),
            Violation(9001, "MP10", (C,), "after the deadline"),
            Violation(9500, "recovery", (C,), "not recovered"),
        ]
        outcomes = [
            (1000, "refused"),
            (4000, "refused"),
            (4000, "failed"),
            (5000, "at_root"),
            (6000, "partition"),
            (6000, "delivered"),
            (6000, "exempt"),
            (9500, "failed"),
        ]
        failing, accepted, refused, failed = overload(vs, 2000, 9000, outcomes)
        self.assertEqual([v.detail for v in accepted], ["in the window", "at the deadline"])
        self.assertEqual(
            [v.detail for v in failing],
            [
                "before the window",
                "loop in the window",
                "silent",
                "after the deadline",
                "not recovered",
            ],
        )
        self.assertEqual((refused, failed), (1, 3))


if __name__ == "__main__":
    unittest.main()
