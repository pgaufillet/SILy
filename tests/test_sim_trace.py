"""Trace recorder (`sim.trace`): reception fates, decoded frames, message
lifecycle, state changes, checker events, and no effect on the simulation.

Small networks on `sim.net`: sniffers inject raw frames at chosen times
(collisions, losses, cuts), boards with static next hops carry class 1
messages, boards with routing form a DODAG. The trace is written to memory
and read back as JSON Lines.
"""

import io
import json
import unittest

from mesh.dodag import DodagInfo
from mesh.frame import FIRST_HOP, ROOT, VERSION
from mesh.ports import ACK
from mesh.profile import EU_869
from mesh.reliable import UNREACHABLE
from mesh.routing import Routing
from mesh.security import AUTHENTICATION
from sim.invariants import Check, Checker
from sim.medium import COLLISION, CUT, LOSS, OK
from sim.net import Network
from sim.trace import FORMAT, Recorder

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
APP = 20
A, B = 0x00A1, 0x00B1
GARBAGE = bytes(20)  # protocol version 0: not a frame of this protocol


def _records(out):
    lines = out.getvalue().split("\n")
    return [json.loads(line) for line in lines if line]


def _kind(records, kind, **fields):
    out = []
    for r in records:
        if r["k"] == kind and all(r.get(k) == v for k, v in fields.items()):
            out.append(r)
    return out


def _routing(engine):
    return Routing(engine, EU_869, INFO)


class Fates(unittest.TestCase):
    """Two sniffers transmit together to A: collision; B hears the first
    one only, through a link losing every frame; a third one's frame is cut
    halfway to A and received by B."""

    def setUp(self):
        net = Network(1, _KEY)
        self.out = io.StringIO()
        self.rec = Recorder(net, self.out, label="fates")
        net.add(A)
        net.add(B)
        s1, s2, s3 = net.sniffer("s1"), net.sniffer("s2"), net.sniffer("s3")
        net.medium.link("s1", A, rssi=-71, snr=7)
        net.medium.link("s2", A, rssi=-90, snr=2)
        net.medium.link("s1", B, loss=1.0)
        net.medium.link("s3", A)
        net.medium.link("s3", B)
        s1.inject([GARBAGE], 1000, 0)
        s2.inject([GARBAGE], 1000, 0)
        s3.inject([bytes(200)], 2000, 0)
        net.at(2100, net.medium.cut, "s3", A)
        net.run(3000)
        self.rec.close()
        self.net = net
        self.records = _records(self.out)

    def test_header_first_then_time_order(self):
        h = self.records[0]
        self.assertEqual((h["k"], h["i"], h["fmt"], h["seed"], h["sf"]), ("hdr", 0, FORMAT, 1, 7))
        names = [n["n"] for n in h["nodes"]]
        self.assertEqual(names, [A, B, "s1", "s2", "s3"])
        self.assertEqual(h["nodes"][0]["a"], A)
        self.assertEqual(h["nodes"][2]["kind"], "sniffer")
        self.assertIn(["s1", B, 1.0, -80, 10, 1], h["links"])
        index = [r["i"] for r in self.records]
        self.assertEqual(index, list(range(len(self.records))))
        times = [r["t"] for r in self.records]
        self.assertEqual(times, sorted(times))
        self.assertEqual(self.records[-1]["k"], "eof")

    def test_fates_of_each_reception(self):
        r = self.records
        tx1 = _kind(r, "tx", n="s1")[0]
        tx2 = _kind(r, "tx", n="s2")[0]
        tx3 = _kind(r, "tx", n="s3")[0]
        self.assertEqual(tx1["rx"], [[A, -71, 7], [B, -80, 10]])
        self.assertEqual(tx2["rx"], [[A, -90, 2]])
        self.assertEqual((tx1["sn"], tx1["err"], tx1["len"]), (1, VERSION, len(GARBAGE)))
        self.assertEqual(tx1["e"] - tx1["t"], self.net.medium.airtime_ms(len(GARBAGE)))
        end1 = _kind(r, "end", x=tx1["i"])[0]
        end2 = _kind(r, "end", x=tx2["i"])[0]
        end3 = _kind(r, "end", x=tx3["i"])[0]
        self.assertEqual((end1["n"], end1["t"]), ("s1", tx1["e"]))
        self.assertEqual(end1["f"], [COLLISION, LOSS])
        self.assertEqual(end2["f"], [COLLISION])
        self.assertEqual(end3["f"], [CUT, OK])
        # Each fate is the medium's own count.
        stats = self.net.medium.stats
        self.assertEqual((stats(A).rx_collision, stats(A).rx_cut), (2, 1))
        self.assertEqual((stats(B).rx_loss, stats(B).rx_ok), (1, 1))

    def test_link_cut_recorded(self):
        cut = _kind(self.records, "link", s="s3", d=A)
        self.assertEqual(len(cut), 1)
        self.assertEqual((cut[0]["t"], cut[0]["up"]), (2100, 0))


class Decoding(unittest.TestCase):
    """Frames of a board under another key, a sniffer's replay of a frame of
    A and a tampered copy of it."""

    def test_foreign_key_replay_and_tampered_frame(self):
        net = Network(2, _KEY)
        out = io.StringIO()
        rec = Recorder(net, out)
        root = net.add(ROOT, next_hop=lambda m: A)
        net.add(A, next_hop=lambda m: ROOT)
        net.add(B, key=bytes(16), next_hop=lambda m: ROOT)
        eve = net.sniffer("eve")
        for name in (A, B, "eve"):
            net.link_both(name, ROOT)
        net.medium.link(A, "eve")
        net.at(100, net.boards[A].submit, APP, 0, ROOT, b"hi")
        net.at(1000, net.boards[B].submit, APP, 0, ROOT, b"ho")
        net.run(2000)
        captured = eve.frames[0][1]
        tampered = bytearray(captured)
        tampered[-1] ^= 1
        eve.inject([captured, bytes(tampered)], 3000, 500)
        net.run(5000)
        rec.close()
        r = _records(out)
        foreign = _kind(r, "tx", n=B)[0]
        self.assertEqual(
            (foreign["fk"], foreign["m"][0]["p"], foreign["m"][0]["len"]), (1, APP, 2)
        )
        own = _kind(r, "tx", n=A)[0]
        self.assertFalse("fk" in own or "sn" in own)
        replay, forged = _kind(r, "tx", n="eve")
        self.assertEqual((replay["sn"], replay["s"], replay["fc"]), (1, A, own["fc"]))
        self.assertEqual(replay["m"], own["m"])
        self.assertEqual((forged["sn"], forged["err"]), (1, AUTHENTICATION))
        self.assertFalse("m" in forged)
        # The root rejected all three; its counters say why.
        c = root.engine.counters
        self.assertEqual((c.rx_replay, c.rx_auth), (1, 2))
        rejected = {}
        for x in _kind(r, "cnt", n=ROOT):
            for name, n in x["c"].items():
                rejected[name] = rejected.get(name, 0) + n
        self.assertEqual((rejected["rx_replay"], rejected["rx_auth"]), (1, 2))


class ClassOne(unittest.TestCase):
    """A sends class 1 messages to the root, its next hop: decoded frames,
    acknowledgement entries, submission, delivery and failure records."""

    def build(self, linked):
        net = Network(3, _KEY)
        out = io.StringIO()
        rec = Recorder(net, out)
        root = net.add(ROOT, next_hop=lambda m: A)
        node = net.add(A, next_hop=lambda m: ROOT)
        if linked:
            net.link_both(A, ROOT)
        root.listen(APP)
        seq = node.submit(APP, 1, ROOT, b"punch")
        net.run(60000)
        rec.close()
        return net, _records(out), seq

    def test_message_and_its_acknowledgement(self):
        net, r, seq = self.build(True)
        self.assertTrue(seq >= 0)
        data = [t for t in _kind(r, "tx", n=A) if t["m"][0]["p"] == APP]
        self.assertEqual(len(data), 1)
        m = data[0]["m"][0]
        self.assertEqual((m["m"], m["c"], m["o"], m["q"], m["len"]), (FIRST_HOP, 1, A, seq, 5))
        self.assertEqual((data[0]["d"], data[0]["s"]), (ROOT, A))
        # The root's port 3 entry names that frame (link source, counter low 16 bits).
        acks = [
            e
            for t in _kind(r, "tx", n=ROOT)
            for msg in t["m"]
            if msg["p"] == ACK
            for e in msg["a"]
        ]
        self.assertEqual(acks, [[A, data[0]["fc"] & 0xFFFF]])
        self.assertEqual(net.boards[A].engine.counters.acks_explicit, 1)
        explicit = [c for c in _kind(r, "cnt", n=A) if "acks_explicit" in c["c"]]
        self.assertEqual(len(explicit), 1)

    def test_submission_then_delivery(self):
        net, r, seq = self.build(True)
        sub = _kind(r, "sub", n=A)
        self.assertEqual(len(sub), 1)
        self.assertEqual((sub[0]["p"], sub[0]["c"], sub[0]["d"], sub[0]["q"]), (APP, 1, ROOT, seq))
        dlv = _kind(r, "dlv", n=ROOT)
        self.assertEqual(len(dlv), 1)
        self.assertEqual((dlv[0]["o"], dlv[0]["p"], dlv[0]["q"], dlv[0]["s"]), (A, APP, seq, A))
        data = _kind(r, "tx", n=A)[0]
        self.assertEqual(dlv[0]["fc"], data["fc"])
        self.assertTrue(sub[0]["i"] < data["i"] < dlv[0]["i"])
        self.assertEqual(dlv[0]["t"], net.boards[ROOT].inbox[0][0])

    def test_failure(self):
        net, r, seq = self.build(False)  # no link: attempts exhausted, no alternative
        tries = [t for t in _kind(r, "tx", n=A) if t["m"][0]["p"] == APP]
        self.assertEqual([t["rx"] for t in tries], [[], [], []])
        fail = _kind(r, "fail", n=A)
        self.assertEqual(len(fail), 1)
        self.assertEqual((fail[0]["d"], fail[0]["p"], fail[0]["q"]), (ROOT, APP, seq))
        self.assertEqual(fail[0]["why"], UNREACHABLE)
        self.assertEqual(fail[0]["t"], net.boards[A].failures[0][0])
        self.assertTrue(fail[0]["i"] > tries[-1]["i"])


class States(unittest.TestCase):
    """Root — A — B with routing: attachment recorded, then the detach of B
    once A loses power."""

    def test_rank_and_parent_changes(self):
        net = Network(5, _KEY)
        out = io.StringIO()
        rec = Recorder(net, out)
        for name in (ROOT, A, B):
            net.add(name, routing=_routing)
        net.link_both(ROOT, A)
        net.link_both(A, B)
        net.run(30000)
        net.boards[A].power_loss(off_ms=10**9)
        net.run(90000)
        rec.close()
        r = _records(out)
        sb = _kind(r, "st", n=B)
        self.assertEqual((sb[0]["r"], sb[0]["p"], sb[0]["at"]), (255, None, False))
        attached = [s for s in sb if s["at"]]
        self.assertTrue(attached)
        self.assertEqual((attached[0]["p"], attached[0]["ps"][0]), (A, A))
        self.assertTrue(attached[0]["r"] > _kind(r, "st", n=A, at=True)[0]["r"])
        down = _kind(r, "down", n=A)
        self.assertEqual((len(down), down[0]["how"], down[0]["t"]), (1, "power", 30000))
        # B's last state is the board's: detached once A fell silent.
        last = sb[-1]
        b = net.boards[B]
        self.assertTrue(last["t"] > 30000)
        self.assertEqual((last["r"], last["p"], last["at"]), (b.engine.rank, None, False))
        self.assertEqual(b.routing.dodag.counters.poisons, 1)
        poison = [c for c in _kind(r, "cnt", n=B) if "poisons" in c["c"]]
        self.assertEqual(len(poison), 1)


class _Once(Check):
    name = "TEST"

    def sample(self, now):
        if now == 2000:
            self.violate(now, (A,), "sampled at 2 s")


class CheckerEvents(unittest.TestCase):
    def test_violation_and_switch_off(self):
        net = Network(6, _KEY)
        out = io.StringIO()
        rec = Recorder(net, out)
        for name in (ROOT, A):
            net.add(name, routing=_routing)
        net.link_both(ROOT, A)
        checker = Checker(net, [_Once()])
        checker.start()
        net.at(3000, checker.switch_off, A)
        net.run(4000)
        checker.finish()
        rec.close()
        r = _records(out)
        v = _kind(r, "viol")
        self.assertEqual(len(v), 1)
        self.assertEqual((v[0]["t"], v[0]["chk"], v[0]["ns"]), (2000, "TEST", [A]))
        scn = _kind(r, "scn", what="switch-off")
        self.assertEqual((len(scn), scn[0]["arg"], scn[0]["t"]), (1, A, 3000))
        self.assertTrue(scn[0]["i"] < _kind(r, "down", n=A)[0]["i"])


def _scenario(recorded):
    """Routing on root — A — B, class 1 traffic, a reset of A; return what it computed."""
    net = Network(11, _KEY)
    out = io.StringIO()
    rec = Recorder(net, out) if recorded else None
    names = (ROOT, A, B)
    for name in names:
        net.add(name, routing=_routing)
    net.link_both(ROOT, A, loss=0.2)
    net.link_both(A, B)
    root = net.boards[ROOT]
    root.listen(APP)
    for i in range(20):
        net.at(15000 + 1000 * i, net.boards[B].submit, APP, 1, ROOT, b"%d" % i)
    net.at(25000, net.boards[A].reset, 2000)
    net.run(80000)
    if rec is not None:
        rec.close()
    stats = []
    for name in names:
        s = net.medium.stats(name)
        b = net.boards[name]
        stats.append(
            (
                s.tx_count,
                s.tx_ms,
                s.busy_ms,
                s.rx_ok,
                s.rx_loss,
                s.rx_collision,
                s.sense_count,
                s.sense_busy,
                b.engine.rank,
                b.routing.parents.preferred,
                sorted(b.engine.counters.__dict__.items()),
                b.failures,
            )
        )
    return (net.scheduler.executed, net.rng.next32(), root.inbox, stats), out


class NoEffect(unittest.TestCase):
    def test_same_run_with_and_without_recorder(self):
        plain, _ = _scenario(False)
        recorded, out = _scenario(True)
        self.assertEqual(recorded, plain)
        # The run did what the comparison needs: deliveries, losses, a reboot.
        self.assertTrue(len(plain[2]) > 10)
        self.assertTrue(plain[3][1][4] > 0)
        r = _records(out)
        self.assertEqual(len(_kind(r, "boot", n=A)), 2)
        self.assertEqual(len(_kind(r, "dlv")), len(plain[2]))


if __name__ == "__main__":
    unittest.main()
