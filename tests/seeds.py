"""Scenario suites on several seeds, for the MicroPython unix port.

Usage::

    micropython tests/seeds.py [COUNT [MODULE...]] [--first K] [--class NAME]... [--json]
    micropython tests/seeds.py plan COUNT CHUNK [--first K]
    micropython tests/seeds.py aggregate [--expect FIRST-LAST] PATH...

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
that test class, NAME the class or MODULE.CLASS (repeatable); ``--json``,
instead of the unittest output, one JSON line per shift and class:
``shift``, ``module``, ``class``, ``ok``, ``tests``, ``failures`` (test
name and the last line of its traceback), ``ms`` (the class's run time),
``variant`` and ``rev`` (the ``SWEEP_VARIANT`` and ``SWEEP_REV`` of the
environment), then, once every shift has run, an end line (``end``,
``first``, ``count``): a file of lines without it is a chunk that crashed
or was killed.

``plan`` prints the chunks of shifts K to K + COUNT - 1, one per line, FIRST
N (N shifts from FIRST, at most CHUNK), for parallel jobs (`make seeds`).
``aggregate`` reads JSON lines (files, or the ``*.jsonl`` files of
directories) and prints per variant, module and class the failing shifts k
of n with their 95 % Wilson interval, then each failing (shift, class,
test, last message) with the command reproducing it alone, the (shift,
class) pairs without a line (every shift of ``--expect``, or of the
lines, for every class of the variant) and the files without an end line;
exits with status 0 when every shift passed, 1 when some failed, 2 when
some are missing or a chunk crashed.
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


def collect(names, classes=()):
    """Return the (module name, test class) pairs of the modules names.

    classes: only these classes, each named CLASS or MODULE.CLASS, if given.
    """
    sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")
    cases = []
    found = set()
    for name in names:
        for case in _cases(__import__(name)):
            full = "%s.%s" % (name, case.__name__)
            if not classes or case.__name__ in classes or full in classes:
                cases.append((name, case))
                found.update((case.__name__, full))
    missing = set(classes) - found
    if missing:
        raise ValueError("no test class %s in %s" % (" ".join(sorted(missing)), " ".join(names)))
    return cases


def run(cases, count, first=0, as_json=False, out=print):
    """Run the test classes cases count times from shift first; return the failing runs.

    cases: (module name, class) pairs (`collect`). as_json: one JSON line
    per shift and class, then the end line, given to out, instead of the
    unittest output.
    """
    Network.__init__ = _shifted
    previous = getattr(unittest, "print", print)
    try:
        if as_json:
            unittest.print = _quiet  # the runner's lines, not the JSON ones
        return _run(cases, count, first, as_json, out)
    finally:
        Network.__init__ = _init
        unittest.print = previous


def _run(cases, count, first, as_json, out):
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
                start = time.ticks_ms()
                suite.run(result)
                ok = result.wasSuccessful() and result.testsRun > 0
                line = {
                    "shift": k,
                    "module": module,
                    "class": case.__name__,
                    "ok": ok,
                    "tests": result.testsRun,
                    "failures": _messages(result),
                    "ms": time.ticks_diff(time.ticks_ms(), start),
                    "variant": variant,
                    "rev": rev,
                }
                out(json.dumps(line))
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
    if as_json:
        out(json.dumps({"end": True, "first": first, "count": count}))
    return failed


def plan(count, chunk, first=0):
    """Return the chunks (first shift, shifts) of shifts first to first + count - 1.

    Each chunk has at most chunk shifts.
    """
    if chunk < 1:
        raise ValueError("chunk %d below 1" % chunk)
    return [(k, min(chunk, first + count - k)) for k in range(first, first + count, chunk)]


def read_lines(paths):
    """Return (lines, crashed) of the files paths, or of the *.jsonl files of directories.

    lines: the shift lines (dicts); crashed: the files without an end line,
    or with a line cut short.
    """
    out = []
    crashed = []
    for path in paths:
        files = [path]
        if os.stat(path)[0] & 0x4000:  # a directory
            files = ["%s/%s" % (path, e) for e in sorted(os.listdir(path)) if e.endswith(".jsonl")]
        for name in files:
            ended = False
            broken = False
            with open(name) as f:
                for line in f:
                    if not line.startswith("{"):
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:  # cut short by the end of the job
                        broken = True
                        continue
                    if d.get("end"):
                        ended = True
                    else:
                        out.append(d)
            if broken or not ended:
                crashed.append(name)
    return out, crashed


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


def command(shift, module, case):
    """Return the command running shift of the test class module.case alone (unittest output)."""
    return "make seeds SEEDS=1 SEEDS_FIRST=%d SEEDS_CLASS=%s.%s SEEDS_JOBS=1" % (
        shift,
        module,
        case,
    )


def verdict(lines, crashed=(), expect=None, out=print):
    """Print the failing and missing (shift, class) pairs of lines; return the exit status.

    expect: (first, last) shifts every class of a variant must have (default:
    those of its lines). Status 0 every shift passed, 1 some failed, 2 some
    missing, a chunk crashed (crashed: its files) or no line at all. out:
    the printer of each output line.
    """
    variants = {}
    failing = []
    for d in lines:
        v = variants.setdefault(d.get("variant", ""), (set(), set(), set()))
        v[0].add((d["module"], d["class"]))
        v[1].add(d["shift"])
        v[2].add((d["shift"], d["module"], d["class"]))
        if not d["ok"]:
            failing.append(d)
    failing.sort(key=lambda d: (d.get("variant", ""), d["module"], d["class"], d["shift"]))
    for d in failing:
        label = "%s.%s" % (d["module"], d["class"])
        if d.get("variant"):
            label = "%s %s" % (d["variant"], label)
        tests = d["failures"] or [{"test": "no test ran", "message": ""}]
        for t in tests:
            out("FAIL shift %d %s %s: %s" % (d["shift"], label, t["test"], t["message"]))
        out("   reproduce: %s" % command(d["shift"], d["module"], d["class"]))
    missing = 0
    for variant in sorted(variants):
        classes, shifts, seen = variants[variant]
        if expect is not None:
            shifts = range(expect[0], expect[1] + 1)
        for k in sorted(shifts):
            absent = sorted("%s.%s" % c for c in classes if (k,) + c not in seen)
            if absent:
                missing += 1
                what = "all %d classes" % len(classes) if len(absent) == len(classes) else ""
                out(
                    "MISSING %sshift %d: %s"
                    % (variant + " " if variant else "", k, what or " ".join(absent))
                )
    for name in crashed:
        out("CRASHED %s: the chunk did not end (no end line)" % name)
    runs = set((d.get("variant", ""), d["shift"]) for d in lines)
    bad = set((d.get("variant", ""), d["shift"]) for d in failing)
    out(
        "seeds: %d of %d shifts passed, %d failing (shift, class), %d shifts missing, "
        "%d chunks crashed"
        % (len(runs) - len(bad), len(runs), len(failing), missing, len(crashed))
    )
    if missing or crashed or not lines:
        return 2
    return 1 if failing else 0


def _expect(text):
    first, _, last = text.partition("-")
    return int(first), int(last or first)


def main(argv):
    try:
        if len(argv) > 1 and argv[1] == "plan":
            args = argv[2:]
            first = 0
            if len(args) == 4 and args[2] == "--first":
                first = int(args[3])
            elif len(args) != 2:
                raise ValueError("plan COUNT CHUNK [--first K]")
            for p in plan(int(args[0]), int(args[1]), first):
                print("%d %d" % p)
            return 0
        if len(argv) > 1 and argv[1] == "aggregate":
            args = argv[2:]
            expect = None
            if args and args[0] == "--expect":
                if len(args) < 2:
                    raise ValueError("--expect without value")
                expect = _expect(args[1])
                args = args[2:]
            lines, crashed = read_lines(args)
            aggregate(lines)
            return verdict(lines, crashed, expect)
        count, first, names, classes, as_json = options(argv)
        failed = run(collect(names, classes), count, first, as_json)
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
