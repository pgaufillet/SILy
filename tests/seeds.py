"""Scenario suites on several seeds, for the MicroPython unix port.

Usage::

    micropython tests/seeds.py [COUNT [MODULE...]] [--first K] [--class NAME]... [--json]
    micropython tests/seeds.py aggregate PATH...

Runs the unittest.TestCase classes of each scenario module (default
`SCENARIOS`: the DODAG, routing and class 1 scenarios) COUNT
times (default 24): run k adds k * `STRIDE` to the seed of every
`sim.net.Network` built by the scenarios, so that run 0 is the one of
`make test` and the others draw other collisions, jitters and backoffs from
the same scripted graphs, no two runs sharing a seed. The unittest output
and the time of each run are printed, then a summary of the failing (shift,
test) pairs; exits with status 1 if any run fails or a module cannot be
imported. Not part of `make test` (about COUNT times the scenarios' time).

Options, so that ranges of shifts run as parallel jobs: ``--first K``, the
first shift (default 0: shifts K to K + COUNT - 1); ``--class NAME``, only
that test class (repeatable); ``--json``, instead of the unittest output,
one JSON line per shift and class: ``shift``, ``module``, ``class``,
``ok``, ``tests``, ``failures`` (test name and the last line of its
traceback), ``variant`` and ``rev`` (the ``SWEEP_VARIANT`` and
``SWEEP_REV`` of the environment). ``aggregate`` reads such lines (files,
or the ``*.jsonl`` files of directories) and prints per variant, module and
class the failing shifts k of n with their 95 % Wilson interval, and the
failing shifts.
"""

import json
import os
import sys
import time
import unittest

from sim.net import Network

SCENARIOS = ("test_dodag_scenario", "test_routing_scenario", "test_reliable_scenario")
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


def _messages(result):
    # [{test, message}] of the failures and errors of a result: the last
    # line of each traceback.
    out = []
    for kind in (result.failures, result.errors):
        for test, trace in kind:
            lines = [line for line in str(trace).split("\n") if line.strip()]
            out.append({"test": test[0], "message": lines[-1] if lines else ""})
    return out


def _quiet(*args, **kwargs):
    pass


def options(argv):
    """Return (count, first, names, classes, as_json) of the arguments argv[1:]."""
    count, first, classes, as_json = 24, 0, [], False
    positional = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("--first", "--class"):
            if i + 1 >= len(argv):
                raise ValueError("%s without value" % a)
            if a == "--first":
                first = int(argv[i + 1])
            else:
                classes.append(argv[i + 1])
            i += 2
            continue
        if a == "--json":
            as_json = True
        elif a.startswith("--"):
            raise ValueError("unknown option %s" % a)
        else:
            positional.append(a)
        i += 1
    if positional:
        count = int(positional[0])
    return count, first, tuple(positional[1:]) or SCENARIOS, tuple(classes), as_json


def run(count, names, first=0, classes=(), as_json=False):
    """Run the scenario modules names count times from shift first; return the failing runs.

    classes: only these test classes (names), if given. as_json: one JSON
    line per shift and class instead of the unittest output.
    """
    sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")
    cases = []
    for name in names:
        for case in _cases(__import__(name)):
            if not classes or case.__name__ in classes:
                cases.append((name, case))
    missing = set(classes) - set(case.__name__ for _, case in cases)
    if missing:
        raise ValueError("no test class %s in %s" % (" ".join(sorted(missing)), " ".join(names)))
    if as_json:
        unittest.print = _quiet  # the runner's lines, not the JSON ones
    variant = os.getenv("SWEEP_VARIANT") or ""
    rev = os.getenv("SWEEP_REV") or ""
    failed = []
    for k in range(first, first + count):
        _shift[0] = k * STRIDE
        if as_json:
            for module, case in cases:
                result = unittest.TestResult()
                suite = unittest.TestSuite("seeds")
                suite.addTest(case)
                suite.run(result)
                ok = result.wasSuccessful() and result.testsRun > 0
                line = {
                    "shift": k,
                    "module": module,
                    "class": case.__name__,
                    "ok": ok,
                    "tests": result.testsRun,
                    "failures": _messages(result),
                    "variant": variant,
                    "rev": rev,
                }
                print(json.dumps(line))
                for test in _problems(result):
                    failed.append((k, test))
                if not result.testsRun:
                    failed.append((k, "no test ran (%s)" % case.__name__))
            continue
        suite = unittest.TestSuite("seeds")
        for _, case in cases:
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


def read_lines(paths):
    """Return the JSON lines (dicts) of the files paths, or of the *.jsonl files of directories."""
    out = []
    for path in paths:
        files = [path]
        if os.stat(path)[0] & 0x4000:  # a directory
            files = ["%s/%s" % (path, e) for e in sorted(os.listdir(path)) if e.endswith(".jsonl")]
        for name in files:
            with open(name) as f:
                for line in f:
                    if line.startswith("{"):
                        out.append(json.loads(line))
    return out


def aggregate(lines):
    """Print the failing shifts per (variant, module, class) of lines; return the groups.

    Each group: (key, failing shifts, shifts run).
    """
    sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")
    from large import wilson

    groups = {}
    for d in lines:
        key = (d.get("variant", ""), d["module"], d["class"])
        shifts = groups.setdefault(key, {})
        shifts[d["shift"]] = shifts.get(d["shift"], True) and d["ok"]
    out = []
    for key in sorted(groups):
        shifts = groups[key]
        bad = sorted(k for k in shifts if not shifts[k])
        n = len(shifts)
        lo, hi = wilson(len(bad), n)
        print(
            "%s %s.%s: %d/%d shifts failed, %.2f %% [%.2f, %.2f] %%"
            % ((key[0] or "-",) + key[1:] + (len(bad), n, 100 * len(bad) / n, 100 * lo, 100 * hi))
        )
        if bad:
            print("   failing shifts: %s" % " ".join(str(k) for k in bad))
        out.append((key, bad, n))
    return out


def main(argv):
    if len(argv) > 1 and argv[1] == "aggregate":
        lines = read_lines(argv[2:])
        aggregate(lines)
        return 0 if lines else 1
    try:
        count, first, names, classes, as_json = options(argv)
        failed = run(count, names, first, classes, as_json)
    except ValueError as e:
        print("seeds: %s" % e)
        return 2
    runs = set(k for k, _ in failed)
    print("seeds: %d of %d runs passed (%s)" % (count - len(runs), count, ", ".join(names)))
    for k, test in failed:
        print("  shift %d: %s" % (k, test))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
