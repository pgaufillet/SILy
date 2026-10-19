"""Simulator benchmark: wall-clock time of a grid network, with a digest.

MicroPython unix port, with the MICROPYPATH of the host tests (``make
simbench``)::

    micropython tools/simbench.py [NODES [MINUTES [SEED]]]

Builds NODES boards (default 30) on a grid of width ceil(sqrt(NODES)), the
root at a corner, each board hearing its 4 grid neighbours only. Every board
runs its link engine and routing (`mesh.routing.Routing`, presence period
10 s, topology period 60 s) on `sim.net`; one board in 7 also sends a class 1
message to the root every 60 s from the second minute. Runs MINUTES
simulated minutes (default 10) from SEED (default 1), then prints the
wall-clock time, the scheduler's event count and a digest: SHA-256 over every
reception on the medium (time, source, destination, outcome), the medium
statistics, the link counters, rank and preferred parent of every board, and
what the root received. Two runs with the same arguments have the same
digest; a change of the simulator or the mesh that keeps the digest keeps
the behaviour.
"""

import hashlib
import sys
import time

from mesh.dodag import DodagInfo
from mesh.frame import ROOT
from mesh.profile import EU_869
from mesh.routing import Routing
from sim.net import Network

_KEY = bytes(range(16))
INFO = DodagInfo(0, 10, 60, 32, 56, EU_869)
APP = 20
TRAFFIC_EVERY = 7  # one board in 7 sends class 1 messages
TRAFFIC_MS = 60000


class _Digest:
    """Folds the medium trace into a hash instead of keeping it."""

    def __init__(self):
        self.hash = hashlib.sha256()
        self.count = 0

    def append(self, item):
        self.count += 1
        self.hash.update(repr(item).encode())

    def add(self, *items):
        self.hash.update(repr(items).encode())


def address(i):
    return ROOT if i == 0 else 0x0100 + i


def build(nodes, seed):
    """Return (network, boards, reports, digest) of the grid."""
    net = Network(seed, _KEY)
    digest = _Digest()
    net.medium.trace = digest
    reports = []

    def on_report(port, origin, report, header):
        reports.append((net.now, port, origin))

    def make(engine):
        r = Routing(engine, EU_869, INFO)
        if engine.is_root:
            r.on_report = on_report
        return r

    width = 1
    while width * width < nodes:
        width += 1
    boards = [net.add(address(i), routing=make) for i in range(nodes)]
    for i in range(nodes):
        x = i % width
        if x + 1 < width and i + 1 < nodes:
            net.link_both(address(i), address(i + 1))
        if i + width < nodes:
            net.link_both(address(i), address(i + width))
    boards[0].listen(APP)
    for i in range(TRAFFIC_EVERY, nodes, TRAFFIC_EVERY):
        _traffic(net, boards[i])
    return net, boards, reports, digest


def _traffic(net, board):
    sent = [0]

    def submit():
        board.submit(APP, 1, ROOT, b"%d:%d" % (board.address, sent[0]))
        sent[0] += 1
        net.at(net.now + TRAFFIC_MS, submit)

    net.at(TRAFFIC_MS + board.address % 1000, submit)


def summary(net, boards, reports, digest):
    """Fold the final state into digest; return its hex digest."""
    medium = net.medium
    for b in boards:
        s = medium.stats(b.name)
        digest.add(
            b.address,
            s.tx_count,
            s.tx_ms,
            s.busy_ms,
            s.rx_ok,
            s.rx_loss,
            s.rx_collision,
            s.rx_half_duplex,
            s.rx_cut,
            s.sense_count,
            s.sense_busy,
        )
        e = b.engine
        digest.add(e.rank, b.routing.parents.preferred if not e.is_root else None)
        digest.add(sorted(e.counters.__dict__.items()))
    digest.add(boards[0].inbox)
    digest.add(reports)
    return "".join("%02x" % c for c in digest.hash.digest())


def main(argv):
    nodes = int(argv[1]) if len(argv) > 1 else 30
    minutes = int(argv[2]) if len(argv) > 2 else 10
    seed = int(argv[3]) if len(argv) > 3 else 1
    t0 = time.ticks_ms()
    net, boards, reports, digest = build(nodes, seed)
    net.run(minutes * 60000)
    wall = time.ticks_diff(time.ticks_ms(), t0)
    attached = sum(1 for b in boards[1:] if b.routing.attached)
    print("nodes %d, %d simulated min, seed %d" % (nodes, minutes, seed))
    print("wall clock %.1f s" % (wall / 1000))
    print("events %d, receptions %d" % (net.scheduler.executed, digest.count))
    print(
        "attached %d/%d, root inbox %d, reports %d"
        % (attached, nodes - 1, len(boards[0].inbox), len(reports))
    )
    print("digest %s" % summary(net, boards, reports, digest))


main(sys.argv)
