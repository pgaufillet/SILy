"""Scenario suites on several seeds, for the MicroPython unix port.

Usage: micropython tests/seeds.py [COUNT [MODULE...]]

Runs the unittest.TestCase classes of each scenario module (default
`SCENARIOS`: the routing scenarios of M3-12 and the class 1 ones) COUNT
times (default 24): run k adds k * `STRIDE` to the seed of every
`sim.net.Network` built by the scenarios, so that run 0 is the one of
`make test` and the others draw other collisions, jitters and backoffs from
the same scripted graphs, no two runs sharing a seed. The unittest output
and the time of each run are printed, then a summary of the failing (shift,
test) pairs; exits with status 1 if any run fails or a module cannot be
imported. Not part of `make test` (about COUNT times the scenarios' time).
"""

import sys
import time
import unittest

from sim.net import Network

SCENARIOS = ("test_routing_scenario", "test_reliable_scenario")
STRIDE = 1000  # seed offset between runs (above the seeds a suite uses)

_shift = [0]
_init = Network.__init__


def _shifted(self, seed, *args, **kwargs):
    _init(self, seed + _shift[0], *args, **kwargs)


Network.__init__ = _shifted


def _cases(module):
    out = []
    for attr in dir(module):
        obj = getattr(module, attr)
        if (
            isinstance(obj, type)
            and issubclass(obj, unittest.TestCase)
            and obj is not unittest.TestCase
        ):
            out.append(obj)
    return out


def _problems(result):
    # (test name, class) of the failures and errors of a result.
    out = []
    for kind in (result.failures, result.errors):
        for test, _ in kind:
            name, case = test
            out.append("%s (%s)" % (name, getattr(case, "__name__", case)))
    return out


def run(count, names):
    """Run the scenario modules names count times; return the failing runs."""
    sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")
    cases = []
    for name in names:
        cases.extend(_cases(__import__(name)))
    failed = []
    for k in range(count):
        _shift[0] = k * STRIDE
        suite = unittest.TestSuite("seeds")
        for case in cases:
            suite.addTest(case)
        start = time.ticks_ms()
        result = unittest.TextTestRunner().run(suite)
        print("seed shift %d: %d ms" % (k, time.ticks_diff(time.ticks_ms(), start)))
        for test in _problems(result):
            failed.append((k, test))
            print("SEED SHIFT %d FAILED: %s" % (k, test))
        if not result.testsRun:
            failed.append((k, "no test ran"))
    return failed


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    names = tuple(sys.argv[2:]) or SCENARIOS
    failed = run(count, names)
    runs = set(k for k, _ in failed)
    print("seeds: %d of %d runs passed (%s)" % (count - len(runs), count, ", ".join(names)))
    for k, test in failed:
        print("  shift %d: %s" % (k, test))
    sys.exit(1 if failed else 0)
