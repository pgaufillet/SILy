"""Trace recording of simulated networks (`sim.trace`), without editing tests.

MicroPython unix port, with the MICROPYPATH of the host tests (``make
trace``)::

    micropython tools/trace.py OUT test MODULE[.CLASS]
    micropython tools/trace.py OUT large SCENARIO TOPOLOGY SEED [NODES]

``test`` runs the unittest.TestCase classes of tests/MODULE.py (only CLASS
if given), one class at a time, and records every `sim.net.Network` they
build into its own file, ``OUT/MODULE.CLASS-NN.jsonl`` (NN: order of
construction in the class, from 01). For that, `sim.net.Network.__init__`
is replaced in this process by a wrapper that builds the network, then
attaches a `sim.trace.Recorder` (as tests/seeds.py shifts the seeds); the
recorders of a class are closed after it.

``large`` plays one run of the large-network suite (`tests/large.py`
``Run``, as ``make large RUN=...``) into
``OUT/SCENARIO_TOPOLOGY_SEED_NODES-01.jsonl`` (through the same wrapper), the
topology's positions in the header, then prints the run's report.

The exit status is that of the tests or of the run (1 if a test fails or an
invariant is violated); the traces are written either way.

Each file written is listed with its size and record count, with the wall
clock of the traced run.
"""

import os
import sys
import time
import unittest

from sim.net import Network
from sim.trace import Recorder


def _parent(path):
    head = path.rsplit("/", 1)
    return head[0] if len(head) == 2 and head[0] else "."


_TESTS = _parent(_parent(__file__)) + "/tests"

_init = Network.__init__
_recorders = []  # (path, Recorder) in construction order
_target = [None]  # (directory, file prefix) of the next network built


def _recorded(self, *args, **kwargs):
    _init(self, *args, **kwargs)
    directory, prefix = _target[0]
    number = 1 + sum(
        1 for path, _ in _recorders if path.startswith(directory + "/" + prefix + "-")
    )
    path = "%s/%s-%02d.jsonl" % (directory, prefix, number)
    _recorders.append((path, Recorder(self, path, label=prefix)))


def _close():
    # Close the recorders still open: each releases its network.
    for _, rec in _recorders:
        rec.close()


Network.__init__ = _recorded


def _mkdirs(path):
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        p = "/".join(parts[:i])
        if p and p != ".":
            try:
                os.mkdir(p)
            except OSError:
                pass  # exists


def _size(path):
    return os.stat(path)[6]


def _summary(paths, wall_ms):
    total = 0
    for path, rec in paths:
        size = _size(path)
        total += size
        print("%s: %d records, %d kB" % (path, rec.count, size // 1024))
    print("trace: %d files, %d kB, %.1f s" % (len(paths), total // 1024, wall_ms / 1000))


def _cases(module, name):
    out = []
    for attr in dir(module):
        obj = getattr(module, attr)
        if (
            isinstance(obj, type)
            and issubclass(obj, unittest.TestCase)
            and obj is not unittest.TestCase
            and (name is None or attr == name)
        ):
            out.append(obj)
    return out


def test(out, target):
    """Run the test classes of target (MODULE or MODULE.CLASS), recording; 0 if they pass."""
    sys.path.insert(0, _TESTS)
    module_name, _, class_name = target.partition(".")
    module = __import__(module_name)
    cases = _cases(module, class_name or None)
    if not cases:
        print("no test class %s" % target)
        return 1
    # One class at a time, its recorders closed after it: an open recorder
    # keeps its network in memory, and a heap full of finished networks
    # slows every garbage collection down.
    start = time.ticks_ms()
    passed = True
    for case in cases:
        _target[0] = (out, "%s.%s" % (module_name, case.__name__))
        suite = unittest.TestSuite(target)
        suite.addTest(case)
        result = unittest.TextTestRunner().run(suite)
        passed = passed and result.wasSuccessful() and result.testsRun > 0
        _close()
    wall = time.ticks_diff(time.ticks_ms(), start)
    _summary(_recorders, wall)
    return 0 if passed else 1


def large(out, args):
    """Play one large-suite run, recording; return 0 if no invariant failed."""
    sys.path.insert(0, _TESTS)
    import large as suite

    scenario, kind, seed = args[0], args[1], int(args[2])
    nodes = int(args[3]) if len(args) > 3 else suite.NODES
    r = suite.Run(scenario, kind, seed, nodes)
    _target[0] = (out, r.name.replace(" ", "_"))
    # Run.run, the topology's positions given to the recorder before the
    # first step of the run (when the header is written).
    start = time.ticks_ms()
    r.build()
    path, rec = _recorders[-1]
    rec.positions = r.topology.positions
    r.net.run(r.end_ms)
    r.violations = r.checker.finish()
    r.wall_ms = time.ticks_diff(time.ticks_ms(), start)
    rec.close()
    for line in r.report(None):
        print(line)
    _summary(_recorders, r.wall_ms)
    return 1 if r.violations else 0


def main(argv):
    if len(argv) < 4 or argv[2] not in ("test", "large"):
        print(__doc__)
        return 2
    out = argv[1]
    _mkdirs(out)
    if argv[2] == "test":
        return test(out, argv[3])
    return large(out, argv[3:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
