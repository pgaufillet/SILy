"""Verdicts of a sweep or campaign: hard rules, and comparison with a baseline.

Usage (MicroPython unix port, MICROPYPATH of the host tests, heap 32 MB)::

    micropython tools/campaign.py bounds DIR... [--label L]
    micropython tools/campaign.py compare BASE NEW [--label L[:M]] [--q Q] [--all]
    micropython tools/campaign.py compare BASE NEW --exact [--label L[:M]]

Inputs: directory trees (or files), walked: each ``*.json`` file is the
record of one large-network run (`tests/large.py` ``record``), each
``*.jsonl`` file a chunk of scenario-suite lines (`tests/seeds.py
--json`); other files (reports, plans, logs) are ignored. Records and
lines carry their label (``variant``: the ``SWEEP_VARIANT`` of the run,
e.g. ``current`` or an ablation variant), and a tree may hold several. A
run is one label, cell (load, scenario, topology, nodes) and seed; a
seeds line one label, shift and test class. A file that cannot be read
or parsed, a record without the fields used, and the same run or line
twice are input problems, listed.

``bounds``: the hard rules, over every run (label L's only with
``--label``). A breach: a silent class 1 loss (``delivery.lost``, check
MP5); a violation of MP11, MP9 or MP13; an overload run whose recovery
failed with a board other than the root still holding messages at the
end (``recovery.nodes``; a record without the field counts as a breach).
Listed apart, not breaches: the recovery failures with only the root's
queue left (the recovery check counts the downward errors the root holds
for origins it cannot reach, which MP5 exempts); the MP10 violations of
the runs that are not overload cases (records without ``recovery``: no
overload window accepts them); each failing (shift, class, test) of the
seeds lines with the command reproducing it, then the shifts missing
between the first and the last of each label and the chunks without end
line (`seeds.verdict`). Each run listed comes with the count of its kind
in its cell (k of n runs) and its ``make large RUN=...`` command. Exit
status: 2 on an input problem, a missing seeds shift or a crashed chunk,
or nothing to judge; else 1 on a breach; else 0.

``compare``: the runs of label L of BASE against those of label M of NEW
(``--label L``: the same label on both sides; ``--label L:M``, e.g.
``--label current:a-late-ack`` with BASE and NEW the same tree; without
``--label``, each side must hold a single label). Only the cells present
on both sides are compared, the others listed. In a cell with at least
`PAIRED_MIN` seeds common to both sides, the runs are paired by seed:
exact McNemar on each yes/no indicator (its discordant pairs), Wilcoxon
signed-rank on each count; below, per run on all the runs of each side:
Fisher exact and Welch's t-test. Yes/no indicators: `large.INDICATORS`,
then each check violated (``violations``); counts: `large.TOTALS`. An
indicator with a value on one side and none on the other (a field older
records lack) is not comparable: listed, not tested; one without value on
either side (recovery outside the overload runs) is left out, as are the
comparisons without a discordant pair, a nonzero difference or an
occurrence. Seeds lines: exact McNemar per test class on the shifts
common to both sides (failed or passed). Multiplicity: Benjamini-Hochberg
over every comparison of the call. Printed: the comparisons at q < Q
(``--q``, default 0.01; ``--all``: every comparison), each with the
rates or means of both sides, the direction of NEW against BASE, p and q.
Exit status: 0 when no comparison is below Q, 1 when one is, 2 on an
input problem, a label absent from its side or nothing in common.

``compare --exact``, for a change meant to keep behaviour, on the same
seeds: counts the common runs whose records are identical except
``wall_s``, ``rev`` and ``variant``, and the common seeds lines identical
except ``ms``, ``rev`` and ``variant``; lists the first `SHOWN` that
differ with their differing fields, and counts the runs and lines on one
side only. Exit status: 0 when every common one is identical, 1 when one
differs, 2 on an input problem or nothing in common.

Method, without scipy (the values of `tests/test_campaign.py` are
scipy 1.15's): McNemar ``min(1, 2 P(X <= min(b, c)))``, X binomial
(b + c, 1/2); Fisher two-sided, the sum over the tables of the same
margins whose probability is at most the observed one's times
(1 + 1e-7) (1 when that is every table), log-factorials from
``math.lgamma``; Welch's t with the
Welch-Satterthwaite degrees of freedom df, p the regularized incomplete
beta I_x(df/2, 1/2) at x = df/(df + t²), by continued fraction (both
sides constant: p 0 when their means differ, else 1); Wilcoxon on the
nonzero differences, average ranks for ties, the tie-corrected variance
and the normal approximation without continuity correction, as scipy's
``wilcoxon(zero_method="wilcox")`` above 50 pairs. At 50 pairs or fewer,
scipy uses the exact distribution when there is no zero and no tie, and
a permutation test at 13 pairs or fewer otherwise: its p-values then
differ from this approximation.

Memory: a record is reduced to the values compared as it is read, and
the heap collected every `GC_EVERY` bytes allocated (the garbage of each
parse, left among the values kept until a collection, would otherwise
break the free heap into pieces too small for the containers' growth).
A sweep of 25 200 records in four labels and 14 500 seeds lines needs the
32 MB heap of the host tests: about 6 MB kept, 9 s for ``bounds`` on one
label, 8 s for ``compare`` of two labels of that tree, 25 s for
``compare --exact`` of 9 600 runs.
"""

import gc
import json
import math
import os
import sys


def _parent(path):
    head = path.rsplit("/", 1)
    return head[0] if len(head) == 2 and head[0] else "."


sys.path.insert(0, _parent(_parent(__file__)) + "/tests")
import large  # noqa: E402
import seeds  # noqa: E402

USAGE = (
    "usage: campaign.py bounds DIR... [--label L]\n"
    "       campaign.py compare BASE NEW [--label L[:M]] [--q Q] [--all] [--exact]"
)
PAIRED_MIN = 20  # common seeds from which a cell's runs are paired
Q = 0.01  # default threshold of the BH-adjusted p-values (q)
SHOWN = 10  # differing runs, and lines, listed by --exact
FIELDS_SHOWN = 12  # differing fields listed per run by --exact
RECORD_SKIP = ("wall_s", "rev", "variant")  # record fields --exact ignores
LINE_SKIP = ("ms", "rev", "variant")  # seeds line fields --exact ignores
YESNO = tuple(name for name, _ in large.INDICATORS)
COUNTS = tuple(name for name, _ in large.TOTALS)
LOG2 = math.log(2)
# Bytes allocated between two collections (`main`): the parse of each record
# leaves garbage among the values kept; collecting before the heap is full
# keeps them packed, the rest of the heap free in large pieces.
GC_EVERY = 1 << 23


# --- Tests -------------------------------------------------------------------------------


def _log_choose(n, k):
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def mcnemar(b, c):
    """Return the exact McNemar p-value (two-sided) of b and c discordant pairs."""
    n = b + c
    total = 0.0
    for k in range(min(b, c) + 1):
        total += math.exp(_log_choose(n, k) - n * LOG2)
    return min(1.0, 2 * total)


def fisher(a, b, c, d):
    """Return the two-sided Fisher exact p-value of the table [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2
    if n1 == 0 or n2 == 0 or k == 0 or k == n:
        return 1.0
    whole = _log_choose(n, k)

    def log_p(x):
        return _log_choose(n1, x) + _log_choose(n2, k - x) - whole

    limit = log_p(a) + math.log(1 + 1e-7)
    total = 0.0
    every = True
    for x in range(max(0, k - n2), min(k, n1) + 1):
        lp = log_p(x)
        if lp <= limit:
            total += math.exp(lp)
        else:
            every = False
    return 1.0 if every else min(1.0, total)  # every table: the observed one is the mode


def _mean_var(a):
    # Mean and sample variance (0 for a constant sample) of a.
    m = sum(a) / len(a)
    if len(set(a)) < 2:
        return m, 0.0
    return m, sum((x - m) * (x - m) for x in a) / (len(a) - 1)


def welch(a, b):
    """Return the two-sided p-value of Welch's t-test of samples a and b."""
    ma, va = _mean_var(a)
    mb, vb = _mean_var(b)
    if va == 0 and vb == 0:
        return 0.0 if ma != mb else 1.0
    sa, sb = va / len(a), vb / len(b)
    t = (ma - mb) / math.sqrt(sa + sb)
    den = 0.0
    if va:
        den += sa * sa / (len(a) - 1)
    if vb:
        den += sb * sb / (len(b) - 1)
    df = (sa + sb) * (sa + sb) / den
    return betainc(df / 2, 0.5, df / (df + t * t))


def betainc(a, b, x):
    """Return the regularized incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1 - x) - log_beta)
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(a, b, x) / a
    return 1 - front * _beta_fraction(b, a, 1 - x) / b


def _beta_fraction(a, b, x):
    # Continued fraction of the incomplete beta function (modified Lentz).
    tiny = 1e-300
    c = 1.0
    d = 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 10000):
        m2 = 2 * m
        for num in (
            m * (b - m) * x / ((a + m2 - 1) * (a + m2)),
            -(a + m) * (a + b + m) * x / ((a + m2) * (a + m2 + 1)),
        ):
            d = 1 + num * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + num / c
            if abs(c) < tiny:
                c = tiny
            step = c * d
            h *= step
        if abs(step - 1) < 1e-15:
            break
    return h


def wilcoxon(a, b):
    """Return the two-sided p-value of Wilcoxon's signed-rank test of the pairs (a, b).

    Zero differences dropped, average ranks for ties, tie-corrected
    variance, normal approximation without continuity correction (scipy's
    method above 50 pairs). 1.0 when every difference is zero.
    """
    d = [y - x for x, y in zip(a, b) if y != x]
    n = len(d)
    if not n:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    r_plus = 0.0
    ties = 0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        rank = (i + j) / 2 + 1
        size = j - i + 1
        ties += size * size * size - size
        for k in range(i, j + 1):
            if d[order[k]] > 0:
                r_plus += rank
        i = j + 1
    se = math.sqrt((n * (n + 1) * (2 * n + 1) - ties / 2) / 24)
    z = (r_plus - n * (n + 1) / 4) / se
    return math.erfc(abs(z) / math.sqrt(2))


def bh(ps):
    """Return the Benjamini-Hochberg adjusted p-values (q) of ps, in their order."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, ps[i] * m / rank)
        q[i] = prev
    return q


# --- Inputs ------------------------------------------------------------------------------


def _files(path, lines, problems):
    # Yield the record files (*.json) of path, a file or a directory walked in
    # name order; append the seeds files (*.jsonl) to lines.
    try:
        mode = os.stat(path)[0]
    except OSError:
        problems.append("%s: not found" % path)
        return
    if mode & 0x4000:
        for name in sorted(e[0] for e in os.ilistdir(path)):
            yield from _files("%s/%s" % (path, name), lines, problems)
    elif path.endswith(".json"):
        yield path
    elif path.endswith(".jsonl"):
        lines.append(path)


def _read(path, problems):
    # The JSON value of the file path, None (a problem) if unreadable.
    try:
        with open(path) as f:
            return json.loads(f.read())
    except (OSError, ValueError) as e:
        problems.append("%s: unreadable (%s)" % (path, e))
        return None


def _cell(r):
    return (r["load"], r["scenario"], r["topology"], r["nodes"])


def _cell_text(cell):
    return "%s %s %s %d" % cell


def _run_name(r):
    return large.Run(r["scenario"], r["topology"], r["seed"], r["nodes"], r["load"]).name


def _seeds_lines(files, problems, project=None):
    # (lines, crashed files) of the seeds files, read one at a time; each
    # line kept as project(key, line) (None: the line), key (label, shift,
    # module, class) with its names shared. A line without the fields used,
    # or a key twice, is a problem (the line dropped).
    names = {}
    seen = set()
    lines, crashed = [], []
    for path in files:
        try:
            found, bad = seeds.read_lines([path])
        except OSError as e:
            problems.append("%s: unreadable (%s)" % (path, e))
            continue
        crashed.extend(bad)
        for d in found:
            try:
                key = (d.get("variant", ""), d["shift"], d["module"], d["class"])
                d["ok"], d["failures"]
            except (KeyError, AttributeError, TypeError) as e:
                problems.append("%s: seeds line without %r: %s" % (path, e, json.dumps(d)))
                continue
            key = tuple(names.setdefault(x, x) for x in key)
            if key in seen:
                problems.append("%s: seeds line twice: %s shift %d %s.%s" % ((path,) + key))
                continue
            seen.add(key)
            lines.append(d if project is None else project(key, d))
    return lines, crashed


def _problems(problems, out):
    for p in problems:
        out("INPUT %s" % p)


# --- bounds ------------------------------------------------------------------------------


def _failed_recovery(r):
    rec = r["recovery"]
    return rec is not None and rec["violations"] > 0


def _queues(r):
    nodes = r["recovery"].get("nodes")
    if nodes is None:
        return "boards holding messages at the end: not recorded"
    return "boards holding messages at the end: %s" % (" ".join(nodes) or "none")


# Kinds of run of `bounds`: name, test(record), note(record) or None, breach.
KINDS = (
    (
        "silent loss (MP5)",
        lambda r: r["delivery"].get("lost", 0) > 0 or r["violations"].get("MP5", 0) > 0,
        lambda r: "%d silent" % r["delivery"].get("lost", 0),
        True,
    ),
    ("MP11", lambda r: r["violations"].get("MP11", 0) > 0, None, True),
    ("MP9", lambda r: r["violations"].get("MP9", 0) > 0, None, True),
    ("MP13", lambda r: r["violations"].get("MP13", 0) > 0, None, True),
    (
        "recovery failed, not on the root's queue alone",
        lambda r: _failed_recovery(r) and large._root_queue_only(r) is not True,
        _queues,
        True,
    ),
    (
        "recovery failed on the root's queue alone",
        lambda r: _failed_recovery(r) and large._root_queue_only(r) is True,
        None,
        False,
    ),
    (
        "MP10 outside overload runs",
        lambda r: r["recovery"] is None and r["violations"].get("MP10", 0) > 0,
        lambda r: "%d MP10" % r["violations"]["MP10"],
        False,
    ),
)


def bounds(paths, label=None, out=print):
    """Print the hard breaches and the listed runs of the records under paths; return the status.

    label: only the records and seeds lines of that label (None: all).
    out: the printer of each output line.
    """
    problems = []
    seeds_files = []
    cells = {}  # (label, cell) -> seeds of its runs
    found = [[] for _ in KINDS]  # per kind: (label, cell, seed, name, note)
    for path in _files_of(paths, seeds_files, problems):
        r = _read(path, problems)
        if r is None:
            continue
        try:
            if label is not None and r["variant"] != label:
                continue
            key = (r["variant"], _cell(r))
            runs = cells.setdefault(key, set())
            if r["seed"] in runs:
                problems.append("%s: run twice (%s, seed %d)" % (path, key[0], r["seed"]))
                continue
            runs.add(r["seed"])
            for i, (_, test, note, _) in enumerate(KINDS):
                if test(r):
                    found[i].append(
                        (key[0], key[1], r["seed"], _run_name(r), note(r) if note else "")
                    )
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            problems.append("%s: not a run record (%r)" % (path, e))
    lines, crashed = _seeds_lines(seeds_files, problems)
    if label is not None:
        lines = [d for d in lines if d.get("variant", "") == label]
    labels = sorted(set(k[0] for k in cells) | set(d.get("variant", "") for d in lines))
    out(
        "bounds: %d runs in %d cells, %d seeds lines; labels %s"
        % (
            sum(len(s) for s in cells.values()),
            len(cells),
            len(lines),
            " ".join(x or '""' for x in labels) or "none",
        )
    )
    breached = set()
    for (name, _, _, breach), runs in zip(KINDS, found):
        if breach:
            breached.update((x[0], x[1], x[2]) for x in runs)
        _print_kind(name, breach, runs, cells, out)
    status = _print_seeds(lines, crashed, out)
    _problems(problems, out)
    listed = sum(len(runs) for (_, _, _, breach), runs in zip(KINDS, found) if not breach)
    out(
        "bounds: %d runs with a hard breach, %d runs listed apart, %d input problems"
        % (len(breached), listed, len(problems))
    )
    if problems or status == 2 or not (cells or lines):
        return 2
    return 1 if breached else 0


def _files_of(paths, lines, problems):
    for path in paths:
        yield from _files(path, lines, problems)


def _print_kind(name, breach, runs, cells, out):
    # One kind of `bounds`: its runs per cell, k of n, each with its command.
    if not runs:
        out("%s: none" % name)
        return
    out(
        "%s: %s%d run%s"
        % (name, "BREACH, " if breach else "listed, ", len(runs), "s" if len(runs) > 1 else "")
    )
    by_cell = {}
    for run in runs:
        by_cell.setdefault((run[0], run[1]), []).append(run)
    for key in sorted(by_cell):
        group = sorted(by_cell[key], key=lambda x: x[2])
        out(
            "  %s %s: %d of %d runs"
            % (key[0] or '""', _cell_text(key[1]), len(group), len(cells[key]))
        )
        for run in group:
            note = "  (%s)" % run[4] if run[4] else ""
            out("    %s%s" % (large._command(run[3]), note))


def _print_seeds(lines, crashed, out):
    # The failing seeds (shift, class, test), per class then each with its
    # command, the missing shifts and crashed chunks; return the worst status
    # of `seeds.verdict` over the labels (0 without lines).
    classes = {}  # (label, module, class) -> [shifts, failing shifts, {test: shifts}]
    for d in lines:
        c = classes.setdefault((d.get("variant", ""), d["module"], d["class"]), [set(), set(), {}])
        c[0].add(d["shift"])
        if not d["ok"]:
            c[1].add(d["shift"])
            for t in d["failures"] or [{"test": "no test ran"}]:
                c[2][t["test"]] = c[2].get(t["test"], 0) + 1
    failing = [(key, c) for key, c in sorted(classes.items()) if c[1]]
    pairs = sum(sum(c[2].values()) for _, c in failing)
    tests = sum(len(c[2]) for _, c in failing)
    if not failing:
        out("seeds, failing (shift, class): none")
    else:
        out(
            "seeds, failing (shift, class): listed, %d in %d classes; (shift, test) %d in %d tests"
            % (sum(len(c[1]) for _, c in failing), len(failing), pairs, tests)
        )
    for key, c in failing:
        out(
            "  %s %s.%s: %d of %d shifts; %s"
            % (
                (key[0] or '""',)
                + key[1:]
                + (len(c[1]), len(c[0]), ", ".join("%s %d" % t for t in sorted(c[2].items())))
            )
        )
    status = 0
    by_label = {}
    for d in lines:
        by_label.setdefault(d.get("variant", ""), []).append(d)
    for name in sorted(by_label):
        group = by_label[name]
        shifts = [d["shift"] for d in group]
        # crashed chunks reported once, with the first label
        first = name == sorted(by_label)[0]
        s = seeds.verdict(group, crashed if first else (), (min(shifts), max(shifts)), out)
        status = max(status, s)
    if crashed and not by_label:
        for path in crashed:
            out("CRASHED %s: the chunk did not end (no end line)" % path)
        status = 2
    return status


# --- compare -----------------------------------------------------------------------------


def project(r):
    """Return the values compared of record r: `YESNO`, `COUNTS`, then the checks violated."""
    out = [test(r) for _, test in large.INDICATORS]
    out.extend(count(r) for _, count in large.TOTALS)
    v = r["violations"]
    out.append(tuple(sorted(c for c in v if v[c])))
    return tuple(out)


def load(path, labels, exact, problems):
    """Return (runs, lines, labels seen) of the tree path.

    labels: the labels kept (None: all). runs: {label: {cell: {seed:
    value}}}, value `project` of the record, or (exact) its file path;
    lines: {label: {(module, class): {shift: value}}}, value the line's
    verdict (ok), or (exact) the line without `LINE_SKIP`.
    """
    runs = {}
    seen = {}
    interned = {}
    seeds_files = []
    for name in _files(path, seeds_files, problems):
        r = _read(name, problems)
        if r is None:
            continue
        try:
            label = r["variant"]
            seen[label] = seen.get(label, 0) + 1
            if labels is not None and label not in labels:
                continue
            cell = _cell(r)
            cell = interned.setdefault(cell, cell)
            cell_runs = runs.setdefault(label, {}).setdefault(cell, {})
            if r["seed"] in cell_runs:
                problems.append("%s: run twice (%s, seed %d)" % (name, label, r["seed"]))
                continue
            if exact:
                value = name
            else:
                value = project(r)
                checks = value[-1]
                value = value[:-1] + (interned.setdefault(checks, checks),)
            cell_runs[r["seed"]] = value
        except (KeyError, TypeError, AttributeError) as e:
            problems.append("%s: not a run record (%r)" % (name, e))

    def kept(key, d):
        if exact:
            return key, dict((k, v) for k, v in d.items() if k not in LINE_SKIP)
        return key, d["ok"]

    lines = {}
    found, crashed = _seeds_lines(seeds_files, problems, kept)
    for name in crashed:
        problems.append("%s: seeds chunk without end line" % name)
    for (label, shift, module, case), v in found:
        seen[label] = seen.get(label, 0) + 1
        if labels is None or label in labels:
            lines.setdefault(label, {}).setdefault((module, case), {})[shift] = v
    return runs, lines, seen


def _label_pair(spec):
    # (BASE label, NEW label) of --label L[:M], (None, None) without it.
    if spec is None:
        return None, None
    a, sep, b = spec.partition(":")
    return a, (b if sep else a)


def _pick(side, label, seen, problems):
    # The label of a side: the one asked, or its only one.
    if label is not None:
        if label not in seen:
            problems.append(
                "%s: no record or seeds line of label %s (labels: %s)"
                % (side, label, " ".join(sorted(seen)) or "none")
            )
        return label
    if len(seen) != 1:
        problems.append(
            "%s holds labels %s: name one with --label"
            % (side, " ".join(sorted(x or '""' for x in seen)) or "none")
        )
        return None
    return list(seen)[0]


def compare(base, new, label=None, q_max=Q, show_all=False, exact=False, out=print):
    """Compare the trees base and new (see the module); print the result, return the status.

    label: ``L`` or ``L:M`` (`_label_pair`), None: each side's only label.
    """
    problems = []
    lb, ln = _label_pair(label)
    wanted = None if lb is None else (lb, ln)
    a_runs, a_lines, a_seen = load(base, wanted, exact, problems)
    if new == base:
        b_runs, b_lines, b_seen = a_runs, a_lines, a_seen
    else:
        b_runs, b_lines, b_seen = load(new, wanted, exact, problems)
    known = len(problems)
    lb = _pick("BASE " + base, lb, a_seen, problems)
    ln = _pick("NEW " + new, ln, b_seen, problems)
    if len(problems) > known:
        _problems(problems, out)
        return 2
    a_runs, b_runs = a_runs.get(lb, {}), b_runs.get(ln, {})
    a_lines, b_lines = a_lines.get(lb, {}), b_lines.get(ln, {})
    out(
        "compare: BASE %s label %s: %d runs in %d cells, %d seeds lines; "
        "NEW %s label %s: %d runs in %d cells, %d seeds lines"
        % (
            base,
            lb,
            sum(len(x) for x in a_runs.values()),
            len(a_runs),
            sum(len(x) for x in a_lines.values()),
            new,
            ln,
            sum(len(x) for x in b_runs.values()),
            len(b_runs),
            sum(len(x) for x in b_lines.values()),
        )
    )
    if exact:
        status = _exact(a_runs, b_runs, a_lines, b_lines, out)
    else:
        status = _statistical(a_runs, b_runs, a_lines, b_lines, q_max, show_all, out)
    _problems(problems, out)
    return 2 if problems else status


def _one_side(title, text, a, b, out):
    # List the keys of a absent from b (cells, or seeds classes), with their runs or shifts.
    only = sorted(k for k in a if k not in b)
    out("%s: %s" % (title, len(only) if only else "none"))
    for k in only:
        out("   %s (%d)" % (text(k), len(a[k])))


def _class_text(key):
    return "%s.%s" % key


def _statistical(a_runs, b_runs, a_lines, b_lines, q_max, show_all, out):
    _one_side("cells only in BASE", _cell_text, a_runs, b_runs, out)
    _one_side("cells only in NEW", _cell_text, b_runs, a_runs, out)
    _one_side("seeds classes only in BASE", _class_text, a_lines, b_lines, out)
    _one_side("seeds classes only in NEW", _class_text, b_lines, a_lines, out)
    rows = []
    heads = {}
    absent = {}  # indicator -> {which side lacks it: cells}
    modes = [0, 0]  # cells paired, unpaired
    common = sorted(c for c in a_runs if c in b_runs)
    for cell in common:
        where = _cell_text(cell)
        heads[where] = _cell_rows(where, a_runs[cell], b_runs[cell], rows, absent, modes)
    classes = sorted(c for c in a_lines if c in b_lines)
    for key in classes:
        where = "seeds " + _class_text(key)
        heads[where] = _seeds_rows(where, a_lines[key], b_lines[key], rows)
    for name in sorted(absent):
        sides = absent[name]
        out(
            "not comparable, not tested: %s, %s"
            % (name, "; ".join("%s in %d cells" % (s, sides[s]) for s in sorted(sides)))
        )
    qs = bh([row[-1] for row in rows])
    below = sum(1 for q in qs if q < q_max)
    out(
        "comparisons: %d (cells paired %d, unpaired %d; seeds classes %d); q < %g: %d"
        % (len(rows), modes[0], modes[1], len(classes), q_max, below)
    )
    last = None
    for row, q in zip(rows, qs):
        if not show_all and q >= q_max:
            continue
        if row[0] != last:
            out("== %s: %s" % (row[0], heads[row[0]]))
            last = row[0]
        out("   %-24s base %10s  new %10s  %-6s %-28s %s p=%.2g q=%.2g" % (row[1:] + (q,)))
    if not common and not classes:
        out("compare: nothing in common")
        return 2
    return 1 if below else 0


def _direction(a, b):
    return "higher" if b > a else "lower" if b < a else "same"


def _cell_rows(where, base, new, rows, absent, modes):
    # Append the comparisons of a cell's runs to rows; return its header.
    common = sorted(s for s in base if s in new)
    paired = len(common) >= PAIRED_MIN
    if paired:
        a_runs = [base[s] for s in common]
        b_runs = [new[s] for s in common]
        head = "runs %d/%d, paired on %d seeds" % (len(base), len(new), len(common))
    else:
        a_runs = list(base.values())
        b_runs = list(new.values())
        head = "runs %d/%d, %d common seeds: unpaired" % (len(base), len(new), len(common))
    modes[0 if paired else 1] += 1
    checks = set()
    for v in a_runs + b_runs:
        checks.update(v[-1])
    names = [(name, i) for i, name in enumerate(YESNO)] + [(c, None) for c in sorted(checks)]
    for name, i in names:
        if i is None:
            a = [name in v[-1] for v in a_runs]
            b = [name in v[-1] for v in b_runs]
        else:
            a = [v[i] for v in a_runs]
            b = [v[i] for v in b_runs]
        if _comparable(name, a, b, absent):
            _yesno_row(where, name, a, b, paired, rows)
    for j, name in enumerate(COUNTS):
        i = len(YESNO) + j
        a = [v[i] for v in a_runs]
        b = [v[i] for v in b_runs]
        if _comparable(name, a, b, absent):
            _count_row(where, name, a, b, paired, rows)
    return head


def _comparable(name, a, b, absent):
    # True when neither side lacks a value; a side without any while the
    # other has some: counted in absent.
    a_none = sum(1 for x in a if x is None)
    b_none = sum(1 for x in b if x is None)
    if not a_none and not b_none:
        return True
    if a_none == len(a) and b_none == len(b):
        return False  # not applicable on either side
    if a_none == len(a):
        side = "BASE lacks it"
    elif b_none == len(b):
        side = "NEW lacks it"
    else:
        side = "some runs lack it"
    sides = absent.setdefault(name, {})
    sides[side] = sides.get(side, 0) + 1
    return False


def _yesno_row(where, name, a, b, paired, rows):
    ka, kb = sum(1 for x in a if x), sum(1 for x in b if x)
    rate = ("%d/%d" % (ka, len(a)), "%d/%d" % (kb, len(b)))
    direction = _direction(ka / len(a), kb / len(b))
    if paired:
        up = sum(1 for x, y in zip(a, b) if y and not x)
        down = sum(1 for x, y in zip(a, b) if x and not y)
        if not up + down:
            return
        detail = "(new only %d, base only %d)" % (up, down)
        rows.append((where, name) + rate + (direction, detail, "McNemar", mcnemar(up, down)))
        return
    if not ka and not kb:
        return
    p = fisher(ka, len(a) - ka, kb, len(b) - kb)
    rows.append((where, name) + rate + (direction, "", "Fisher", p))


def _count_row(where, name, a, b, paired, rows):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    means = ("%.3f" % ma, "%.3f" % mb)
    if paired:
        up = sum(1 for x, y in zip(a, b) if y > x)
        down = sum(1 for x, y in zip(a, b) if y < x)
        if not up + down:
            return
        detail = "(new>base %d, new<base %d)" % (up, down)
        p = wilcoxon(a, b)
        rows.append((where, name) + means + (_direction(ma, mb), detail, "Wilcoxon", p))
        return
    if not sum(a) and not sum(b):
        return
    rows.append((where, name) + means + (_direction(ma, mb), "", "Welch", welch(a, b)))


def _seeds_rows(where, base, new, rows):
    # Append the McNemar comparison of a test class on its common shifts.
    common = sorted(k for k in base if k in new)
    a = [not base[k] for k in common]  # failed
    b = [not new[k] for k in common]
    up = sum(1 for x, y in zip(a, b) if y and not x)
    down = sum(1 for x, y in zip(a, b) if x and not y)
    if up + down:
        rate = ("%d/%d" % (sum(a), len(a)), "%d/%d" % (sum(b), len(b)))
        detail = "(new only %d, base only %d)" % (up, down)
        row = (where, "failed") + rate + (_direction(sum(a), sum(b)), detail, "McNemar")
        rows.append(row + (mcnemar(up, down),))
    return "shifts %d/%d, paired on %d shifts" % (len(base), len(new), len(common))


# --- compare --exact ---------------------------------------------------------------------


def _stripped(d, skip):
    return dict((k, v) for k, v in d.items() if k not in skip)


def _text(v):
    return "absent" if v is _ABSENT else json.dumps(v)


_ABSENT = object()


def differences(a, b, prefix=""):
    """Return the differing fields of the dicts a and b: "path base -> new", sorted."""
    out = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k, _ABSENT), b.get(k, _ABSENT)
        if isinstance(x, dict) and isinstance(y, dict):
            out.extend(differences(x, y, prefix + k + "."))
        elif x != y:
            out.append("%s%s %s -> %s" % (prefix, k, _text(x), _text(y)))
    return out


def _exact(a_runs, b_runs, a_lines, b_lines, out):
    problems = []
    keys = []  # (cell, seed) of the common runs
    only = [0, 0]  # runs only in BASE, only in NEW
    for cells, other, i in ((a_runs, b_runs, 0), (b_runs, a_runs, 1)):
        for cell, runs in cells.items():
            for seed in runs:
                if seed in other.get(cell, ()):
                    if i == 0:
                        keys.append((cell, seed))
                else:
                    only[i] += 1
    keys.sort()
    differ = []
    for cell, seed in keys:
        ra = _read(a_runs[cell][seed], problems)
        rb = _read(b_runs[cell][seed], problems)
        if ra is None or rb is None:
            continue
        ra, rb = _stripped(ra, RECORD_SKIP), _stripped(rb, RECORD_SKIP)
        if ra != rb:
            shown = len(differ) < SHOWN  # the fields of the first ones only
            differ.append((cell, seed, differences(ra, rb), _run_name(ra)) if shown else None)
    out(
        "exact: %d common runs, %d identical, %d differ; %d runs only in BASE, %d only in NEW"
        % (len(keys), len(keys) - len(differ), len(differ), only[0], only[1])
    )
    for cell, seed, fields, name in differ[:SHOWN]:
        out("DIFF %s seed %d: %s" % (_cell_text(cell), seed, large._command(name)))
        for f in fields[:FIELDS_SHOWN]:
            out("   %s" % f)
        if len(fields) > FIELDS_SHOWN:
            out("   ... %d more fields" % (len(fields) - FIELDS_SHOWN))
    common, line_differ, line_only = 0, [], [0, 0]
    for classes, other, i in ((a_lines, b_lines, 0), (b_lines, a_lines, 1)):
        for key, shifts in classes.items():
            for shift in shifts:
                if shift not in other.get(key, ()):
                    line_only[i] += 1
                elif i == 0:
                    common += 1
                    if shifts[shift] != b_lines[key][shift]:
                        line_differ.append((shift, key))
    line_differ.sort()
    out(
        "exact: %d common seeds lines, %d identical, %d differ; %d only in BASE, %d only in NEW"
        % (common, common - len(line_differ), len(line_differ), line_only[0], line_only[1])
    )
    for shift, key in line_differ[:SHOWN]:
        out("DIFF seeds shift %d %s: %s" % (shift, _class_text(key), seeds.command(shift, *key)))
        for f in differences(a_lines[key][shift], b_lines[key][shift])[:FIELDS_SHOWN]:
            out("   %s" % f)
    _problems(problems, out)
    if problems or not (keys or common):
        if not (keys or common):
            out("exact: nothing in common")
        return 2
    return 1 if differ or line_differ else 0


# --- Command line ------------------------------------------------------------------------


def _options(args, valued, flags):
    # (options, positional arguments) of args; each valued option takes a value.
    options, positional = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in valued:
            if i + 1 >= len(args):
                raise ValueError("%s without value" % a)
            options[a] = args[i + 1]
            i += 2
            continue
        if a in flags:
            options[a] = True
        elif a.startswith("--"):
            raise ValueError("unknown option %s" % a)
        else:
            positional.append(a)
        i += 1
    return options, positional


def main(argv):
    gc.threshold(GC_EVERY)
    try:
        if len(argv) > 1 and argv[1] == "bounds":
            options, paths = _options(argv[2:], ("--label",), ())
            if not paths:
                raise ValueError("bounds without directory")
            return bounds(paths, options.get("--label"))
        if len(argv) > 1 and argv[1] == "compare":
            options, paths = _options(argv[2:], ("--label", "--q"), ("--all", "--exact"))
            if len(paths) != 2:
                raise ValueError("compare takes BASE and NEW")
            return compare(
                paths[0],
                paths[1],
                options.get("--label"),
                float(options.get("--q", Q)),
                options.get("--all", False),
                options.get("--exact", False),
            )
        raise ValueError("bounds or compare")
    except ValueError as e:
        print("campaign: %s" % e)
        print(USAGE)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
