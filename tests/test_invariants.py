"""Invariant checks of `sim.invariants`, each shown able to fail (MP5, MP9, MP10, MP11, MP13).

Small networks of real link engines with their routing (`mesh.routing.Routing`)
on `sim.net`, SF7 (slot 52 ms, Imin 1.7 s), presence period 10 s. Each check
runs once on a healthy network (no violation) and once with a defect
injected into a core (a cycle kept, a node that never selects, a version
ignored, a message dropped after its acknowledgement, a duty cycle not
enforced, a table larger than its bound): the check must fire, naming the
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
    Resources,
    VersionWave,
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


class LoopsTest(unittest.TestCase):
    """MP11: a node kept in a cycle beyond the bound is reported."""

    def run_line(self, defect):
        net = build((ROOT, A, B, C), line(A, B, C))
        checker = Checker(net, [Loops()])
        checker.start()

        def keep_cycle():
            # Each of B and C keeps the other as its only parent.
            for name, other in ((B, C), (C, B)):
                parents = net.boards[name].routing.parents

                def select(now, parents=parents, other=other):
                    parents._set_members([other])
                    return 0

                parents._select = select
                select(net.now)

        if defect:
            net.at(FORMED, keep_cycle)
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


if __name__ == "__main__":
    unittest.main()
