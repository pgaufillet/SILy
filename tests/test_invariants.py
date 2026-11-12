"""Invariant checks of `sim.invariants`, each shown able to fail (MP5, MP9-11, MP13, recovery).

Small networks of real link engines with their routing (`mesh.routing.Routing`)
on `sim.net`, SF7 (slot 52 ms, Imin 1.7 s), presence period 10 s. Each check
runs once on a healthy network (no violation) and once with a defect
injected into a core (a cycle kept, a node that never selects, a version
ignored, a message dropped after its acknowledgement, a duty cycle not
enforced, a table larger than its bound, a node never routed, a rootless
cycle, a message still queued at the end): the check must fire, naming the
nodes at fault. The false floats (`Floats`, counted, not a violation): a
loss of grounding injected without a scenario event is counted, one after
an event is not. The metrics (never a violation): a detach is a relay's
when a child prefers the node, a leaf's otherwise (`Detaches`); a forward
held past its sender's relay timeout is a late passive acknowledgement,
held past the sender's retry a duplicate before the copy went on air
(`Acknowledgements`); a failure reported to its sender is split by the
origin's state (`Delivery.failures`).
"""

import unittest

from mesh.dodag import DodagInfo
from mesh.frame import ROOT
from mesh.link import DELIVERED
from mesh.neighbours import NeighbourTable
from mesh.profile import EU_869
from mesh.routing import Routing
from sim.invariants import (
    Acknowledgements,
    Attachment,
    Checker,
    Delivery,
    Detaches,
    DutyCycle,
    Floats,
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


def release_cycle(net, parents_of):
    """Give each node of parents_of its own parent selection back, at that parent."""
    for name, parent in parents_of:
        parents = net.boards[name].routing.parents
        del parents._select
        parents._set_members([parent])


class LoopsTest(unittest.TestCase):
    """MP11: a node in a cycle is reported, however short the cycle."""

    def run_line(self, defect, release=None):
        net = build((ROOT, A, B, C), line(A, B, C))
        checker = Checker(net, [Loops()])
        checker.start()

        if defect:
            net.at(FORMED, keep_cycle, net, B, C)
        if release is not None:
            net.at(FORMED + release, release_cycle, net, ((B, A), (C, B)))
        net.run(FORMED + 30000)
        return checker.finish(), checker

    def test_healthy_line_has_no_cycle(self):
        found, checker = self.run_line(False)
        self.assertEqual(violations(checker, "MP11"), [])
        self.assertEqual(checker.checks[0].episodes, 0)

    def test_kept_cycle_reported_once(self):
        found, checker = self.run_line(True)
        v = violations(checker, "MP11")
        self.assertEqual(len(v), 1)
        self.assertEqual(sorted(v[0].nodes), [B, C])
        self.assertTrue(FORMED <= v[0].time <= FORMED + checker.every_ms, v[0].time)

    def test_short_cycle_reported(self):
        # A cycle of 2 s, far below a presence period, is still a violation:
        # with the root sequence no loop forms at all (§2 MP11).
        found, checker = self.run_line(True, release=2000)
        v = violations(checker, "MP11")
        self.assertEqual(len(v), 1)
        self.assertEqual(sorted(v[0].nodes), [B, C])
        self.assertTrue(FORMED <= v[0].time <= FORMED + checker.every_ms, v[0].time)
        loops = checker.checks[0]
        self.assertEqual(loops.episodes, 1)
        self.assertTrue(1000 <= loops.longest <= 3000, loops.longest)


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

    def test_duplicates_delivered_counted(self):
        # A copy delivered again is counted and reported, not a violation.
        found, check = self.run_line(False)
        root = check.checker.root
        self.assertEqual(check.counts["duplicates"], 0)
        root.inbox.append(root.inbox[-1])
        root.inbox.append(root.inbox[-1])
        check.finish(check.checker.net.now)
        self.assertEqual(violations(check.checker, "MP5"), [])
        self.assertEqual((check.counts["delivered"], check.counts["duplicates"]), (5, 2))
        self.assertTrue(check.stats().endswith("; 2 duplicates delivered"), check.stats())

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

    def test_failures_split_by_origin_state(self):
        # Failures reported to B through its engine (as the reliability
        # layer reports them): B attached and connected, then detached
        # since the submission, then cut off.
        net = build((ROOT, A, B), line(A, B))
        check = Delivery(APP, 20000)
        checker = Checker(net, [check])
        checker.start()
        b = net.boards[B]
        net.run(FORMED)
        seqs = [check.submit(b) for _ in range(3)]
        b.engine.failed(ROOT, APP, seqs[0], 1)
        self.assertEqual(check.failures, {"connected": 1, "detached": 0, "cut_off": 0})
        b.routing.parents.detach(net.now)
        b.engine.failed(ROOT, APP, seqs[1], 1)
        net.medium.cut(A, B)
        net.medium.cut(B, A)
        checker.links_changed()
        b.engine.failed(ROOT, APP, seqs[2], 1)
        self.assertEqual(check.failures, {"connected": 1, "detached": 2, "cut_off": 0})
        seq = check.submit(b)  # submitted detached
        b.engine.failed(ROOT, APP, seq, 1)
        self.assertEqual(check.failures["detached"], 3)
        self.assertEqual(len(b.failures), 4)  # the board's own record, unchanged


def detach(net, name):
    """Make name detach now (poisoning, as on the loss of its last candidate)."""
    board = net.boards[name]
    board.routing.parents.detach(net.now)
    board.node.wake()


class DetachesTest(unittest.TestCase):
    """Detaches: a relay's (a node some child prefers) counted apart from a leaf's."""

    def run_line(self, name, event=False):
        net = build((ROOT, A, B), line(A, B))
        check = Detaches()
        checker = Checker(net, [check])
        checker.start()
        if event:
            net.at(FORMED - 1000, checker.links_changed)
        net.at(FORMED, detach, net, name)
        net.run(FORMED + 30000)
        checker.finish()
        self.assertEqual(checker.violations, [])
        return check

    def test_relay_detach(self):
        # A, B's parent, detaches: a relay, connected, without event.
        check = self.run_line(A)
        self.assertEqual(check.records[0], (FORMED, A, 1, True, False))
        self.assertEqual((check.relay, check.relay_false), (1, 1))
        self.assertTrue(check.stats().startswith("relay 1 (1 connected without event)"))

    def test_leaf_detach(self):
        check = self.run_line(B)
        self.assertEqual(check.records[0], (FORMED, B, 0, True, False))
        self.assertEqual((check.relay, check.leaf), (0, 1))

    def test_relay_detach_after_an_event(self):
        check = self.run_line(A, event=True)
        self.assertEqual(check.records[0], (FORMED, A, 1, True, True))
        self.assertEqual((check.relay, check.relay_false), (1, 0))


def slow_forward(net, name, delay):
    """Make name hold each class 1 message it forwards for delay ms, unacknowledged."""
    board = net.boards[name]
    engine = board.engine
    forward = engine.forward

    def release(slot, copy):
        engine.queue.retry(slot, copy)
        engine.access_at(net.now)
        board.node.wake()

    def slow(msg, header, now, *rest):
        outcome = forward(msg, header, now, *rest)
        if msg.port == APP:
            slot = engine.queue.slot
            copy = engine.queue.select(lambda m: m.port == APP, True)[-1][1]
            engine.queue.hold(slot, copy)
            engine.reliable._odue[slot] = None  # no explicit acknowledgement either
            net.at(now + delay, release, slot, copy)
        return outcome

    engine.forward = slow


class AcknowledgementsTest(unittest.TestCase):
    """Passive acknowledgements after the sender's relay timeout, duplicates of slow forwards."""

    def run_line(self, extra=None):
        # B submits one message; A holds its forward the relay timeout of
        # B plus extra ms (None: forwards at once).
        net = build((ROOT, A, B), line(A, B))
        net.boards[ROOT].listen(APP)
        check = Acknowledgements()
        checker = Checker(net, [check])
        checker.start()
        if extra is not None:
            timeout = net.boards[B].engine.reliable.timeout_ms(A)
            slow_forward(net, A, timeout + extra)
        net.at(FORMED, net.boards[B].submit, APP, 1, ROOT, b"punch")
        net.run(FORMED + 20000)
        checker.finish()
        self.assertEqual(checker.violations, [])
        self.assertEqual(net.boards[ROOT].received(APP), [b"punch"])
        return check

    def test_forward_on_time(self):
        check = self.run_line()
        self.assertEqual((check.passive, check.late), (1, 0))
        self.assertEqual((check.duplicates, check.spurious), (0, 0))

    def test_forward_after_the_timeout_late(self):
        # Forwarded within B's retry delay: B overhears it while waiting to retry.
        check = self.run_line(100)
        self.assertEqual((check.passive, check.late), (1, 1))
        self.assertEqual(check.spurious, 0)

    def test_forward_after_the_retry_spurious(self):
        # B's retry window (32 slots of 52 ms at SF7) over: A receives the
        # retry while its copy waits, unsent.
        check = self.run_line(3000)
        self.assertEqual((check.duplicates, check.spurious), (1, 1))
        self.assertEqual(check.late, 0)


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


def strand(net, name):
    """Make name detach now and never select a parent again (no candidate)."""
    board = net.boards[name]
    p = board.routing.parents
    p.candidate_cost = lambda *args, **kwargs: None
    p._events |= p.detach(net.now)
    board.node.wake()


class RecoveryTest(unittest.TestCase):
    """Overload: a board neither routed nor detached at the deadline, and not routed since
    the overload ended, is reported, so is a message still queued at the end."""

    def run_line(self, names, defect=None, kill=None, after=FORMED, stranded=None):
        # The overload ends at after; the run ends 15 s after the deadline.
        # stranded: the last board detaches for good at that time (ms).
        net = build((ROOT,) + names, line(*names))
        net.boards[ROOT].listen(APP)
        check = Recovery(after)
        checker = Checker(net, [check])
        if defect == "never":
            net.boards[names[-1]].routing.parents.candidate_cost = lambda n, now: None
        checker.start()
        if kill is not None:
            net.at(after, checker.switch_off, kill)
        if stranded is not None:
            net.at(stranded, strand, net, names[-1])
        if defect == "cycle":
            net.at(after + 1, keep_cycle, net, names[-2], names[-1])
        deadline = after + check.detect_ms() + repair_ms(checker, len(names))
        end = deadline + 15000
        last = net.boards[names[-1]]
        if defect == "queued":
            # The last board's links cut just before it submits: its message stays.
            for a, b in ((names[-2], names[-1]), (names[-1], names[-2])):
                net.at(end - 6000, net.medium.cut, a, b)
            net.at(end - 6000, checker.links_changed)
        if kill is None and stranded is None and defect in (None, "queued"):
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

    def test_outage_begun_in_the_overload_reported(self):
        # B, routed, loses its route 20 s before the overload ends, for good.
        found, check, net, deadline = self.run_line((A, B), after=FORMED + 20000, stranded=FORMED)
        self.assertEqual([v.nodes for v in found], [(B,)])
        self.assertIn("not routed since: connected, no route", found[0].detail)
        self.assertTrue(found[0].time >= deadline)
        self.assertEqual((check.routed, check.unrouted, check.repairing), (1, 1, 0))

    def test_outage_begun_after_the_overload_not_reported(self):
        # B, routed after the overload ended, loses its route 10 s later, for
        # good: an ordinary repair (MP10's), counted, not a failure.
        found, check, net, deadline = self.run_line((A, B), stranded=FORMED + 10000)
        self.assertEqual(found, [])
        self.assertTrue(deadline <= check.checked < deadline + 2000)
        self.assertIsNone(net.boards[B].routing.parents.preferred)
        self.assertEqual((check.routed, check.unrouted, check.repairing), (1, 1, 1))

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


def unground(net, name):
    """Make name lose its grounding now, as if G had run out."""
    board = net.boards[name]
    p = board.routing.parents
    p._events |= p._unground(net.now)
    board.node.wake()


class FloatsTest(unittest.TestCase):
    """False floats: counted without a scenario event within G, with the time ungrounded."""

    def run_line(self, defect=False, event=False):
        net = build((ROOT, A, B), line(A, B))
        check = Floats()
        checker = Checker(net, [check])
        checker.start()
        if event:
            net.at(FORMED - 1000, checker.links_changed)
        if defect:
            net.at(FORMED, unground, net, B)
        net.run(FORMED + 30000)
        checker.finish()
        self.assertEqual(checker.violations, [])
        return check, net

    def test_healthy_line_does_not_float(self):
        check, net = self.run_line()
        self.assertEqual((check.count, check.nodes, check.ungrounded_ms), (0, [], 0))
        self.assertTrue(check.solicits >= 2, check.solicits)  # at boot

    def test_loss_of_grounding_counted(self):
        # B grounded again by its parent's next newer sequence (its
        # presence, once per period of 10 s).
        check, net = self.run_line(defect=True)
        self.assertTrue(net.boards[B].routing.parents.grounded)
        self.assertEqual((check.count, check.by_g, check.nodes), (1, 0, [B]))
        self.assertTrue(1000 <= check.ungrounded_ms <= 21000, check.ungrounded_ms)
        self.assertEqual(check.longest, check.ungrounded_ms)

    def test_loss_after_a_scenario_event_not_counted(self):
        check, net = self.run_line(defect=True, event=True)
        self.assertEqual(net.boards[B].routing.parents.floats, 1)
        self.assertEqual((check.count, check.ungrounded_ms), (0, 0))


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
