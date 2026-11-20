"""A field capture (``make sniff``) as a trace for the viewer, with its ciphertext check.

MicroPython unix port, with the MICROPYPATH of the host tests (``make
sniff-trace``; CPython lacks the AES of ``mesh.ccm``, MicroPython's
``cryptolib``)::

    micropython tools/capture.py CAPTURE [--network NETWORK] [--out TRACE] [BOX...]

CAPTURE: the file ``tools/sniff.py`` wrote, each line the host's UTC (ms
since the Unix epoch) at its arrival, a space and a line of the board
(``sily.sniff``: ``SNIFF`` settings, ``F`` frames, ``C`` CRC errors,
``END``; other lines counted, ignored). NETWORK and BOX: the network file
and the box files ``make configure`` takes (``tools/box.py``): the boxes'
names, addresses and roles; the frame key derived from the network file
here, as ``tools/box.py`` derives it (``sily.config.board_config``): the
sniffer never uses it. Without NETWORK, no key: every frame shows as not
opened (the box files' own ``name``, ``address``, ``role`` still name the
boxes). TRACE: default CAPTURE with the extension ``.jsonl``.

**The trace** (format `sim.trace.FORMAT`, the docstring of
``sim/trace.py``), for ``tools/traceview.html``:

- ``hdr``: the profile, SF and time on air of the capture's settings, the
  profile's duty-cycle budget and window, ``nodes``: the boxes of the box
  files and any other transmitter heard (kind "board", named by their
  address), the sniffer (``n`` "sniffer", kind "sniffer") and, when a
  frame too short to name its source was heard, "unknown" (kind
  "sniffer"); no ``links``, no ``seed``, ``sense_ms``, ``capture_db``;
  ``label``: the capture, the sniffer box and the UTC of time 0 (the
  board's RTC when set, else the host's clock). Times: ms since the
  sniffer's start (the ``SNIFF`` line), its ms ticks, moved later by the
  time on air of a frame that began before it.
- per frame heard: ``tx`` of its transmitter (``n``: the link source, the
  header's or, when the header does not decode, bytes 3-4), ``e`` its
  reception (the ``F`` line), ``t`` = ``e`` minus its time on air
  (``mesh.airtime``, the capture's settings), ``len``, ``rx`` [["sniffer",
  RSSI, SNR]], the clear header and the messages decoded as the recorder
  decodes them (``sim.trace.decode_frame``), ``err`` "authentication"
  when the key does not open it; then its ``end`` (``f`` ["ok"]).
- ``st`` of a transmitter after a frame it sent, opened by the key, when
  what the frames tell of it changed: ``r`` rank and ``v`` DODAG version
  of its header; once known, ``p`` its preferred parent and ``at`` (rank
  below 255, `mesh.frame.RANK_INFINITE`): the link destination of its own
  first-hop presence (port 1, mode 6: its parent restores its address
  there, ``mesh.routing``), or ``pp`` of its presence relayed upward
  (mode 1). No parent set (``ps``), no ``q``, ``cnt``, ``cs``, ``bo``,
  ``sub``, ``dlv``, ``fail``, ``boot``.
- ``eof``: ``stats`` as the sniffer saw them: per transmitter its frames
  heard and their time on air, for the sniffer its receptions.

**What a sniffer cannot show**: the frames it missed (out of its range, a
collision at it, its own CRC errors: counted in the summary, not in the
trace, since their transmitter is unknown) and those it heard while
another box did not, or the reverse (each box hears its own neighbours;
its receptions, losses and collisions are not seen); carrier sense,
backoffs, queues, counters, submissions, deliveries and failures (inside
the boxes); the parent sets and a rank or parent change until the box
next sends; reboots (a frame counter that starts again tells one). The
times are the sniffer's: a frame's start is computed from its reception.

**Summary** (standard output): the capture (sniffer, settings, UTC start,
span), the key (network, fingerprint; whether the sniffer's
configuration has the same), then per transmitter the frames heard,
opened by the key, not opened (``err`` "authentication") and
undecodable (the header, or the messages once opened, do not decode;
``err`` the `mesh.frame` reason), the CRC errors and the board's
counters (``END``: its lines printed against those in the capture, the
start guard), then the **ciphertext check**: every decoded message
whose payload has at least `MIN_PLAIN` bytes is searched for in the raw
frame's bytes; each found is reported (frame time, transmitter, port,
length, offset). Shorter payloads are counted, not judged: 3 bytes would
match a random frame by chance once in about 10**5 frames. Exit status 1
when a payload is found or the input is wrong.
"""

import json
import sys

from mesh import profile as profiles
from mesh.airtime import time_on_air_ms
from mesh.frame import BROADCAST, FIRST_HOP, RANK_INFINITE, ROOT, UPWARD
from mesh.link import PRESENCE_FRAME
from mesh.ports import PRESENCE
from mesh.security import AUTHENTICATION
from sily import config, sniff, utc
from sily.guard import MAX_CRASHES as GUARD_MAX
from sily.radio import link_source
from sim.trace import FORMAT, decode_frame
from tools import box as boxes

SNIFFER = "sniffer"  # the sniffer's node in the trace
UNKNOWN = "unknown"  # the transmitter of a frame too short to name its source
MIN_PLAIN = 4  # bytes of a payload searched for in its frame
TICKS_PERIOD = 1 << 30  # the board's, when the SNIFF line lacks it

USAGE = "usage: capture.py CAPTURE [--network NETWORK] [--out TRACE] [BOX...]"


class Capture:
    """A capture file, parsed (`parse`).

    ``settings``: the ``SNIFF`` line's fields; ``host0``: the host's UTC of
    that line; ``frames``: [(t, utc, rssi, snr, data)] of the ``F`` lines,
    t ms since the ``SNIFF`` line (its ticks unwrapped); ``crc``: [(t,
    utc, rssi, snr)] of the ``C`` lines; ``end``: the ``END`` line's
    fields (None: missing); ``errors``: the ``SNIFF ERROR`` lines;
    ``other``: lines of no kind above (mpremote, a traceback);
    ``malformed``: frame and ``END`` lines that do not parse (a line cut
    at the end);
    ``host1``: the host's UTC of the last line.
    """

    def __init__(self):
        self.settings = None
        self.host0 = None
        self.host1 = None
        self.frames = []
        self.crc = []
        self.end = None
        self.errors = []
        self.other = 0
        self.malformed = 0


def parse(lines):
    """Return the `Capture` of lines (strings, the capture file's).

    Raises ValueError without a ``SNIFF`` line, or with two (one run per
    capture).
    """
    cap = Capture()
    last = None  # board ticks of the last line
    elapsed = 0
    period = TICKS_PERIOD
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        head, _, rest = line.partition(" ")
        try:
            host = int(head)
        except ValueError:
            host = None
            rest = line
        if host is not None:
            cap.host1 = host
        if rest.startswith(sniff.ERROR + " "):
            cap.errors.append(rest[len(sniff.ERROR) + 1 :])
            continue
        kind, _, fields = rest.partition(" ")
        if kind == sniff.SETTINGS:
            if cap.settings is not None:
                raise ValueError("two SNIFF lines: one run per capture")
            cap.settings = json.loads(fields)
            cap.host0 = host
            last = cap.settings["t"]
            period = cap.settings.get("tp", TICKS_PERIOD)
            continue
        if kind == sniff.END and cap.settings is not None:
            try:
                cap.end = json.loads(fields)
            except ValueError:
                cap.malformed += 1
            continue
        if kind not in (sniff.FRAME, sniff.CRC) or cap.settings is None:
            cap.other += 1
            continue
        parts = fields.split(" ")
        try:
            t = int(parts[0])
            u = int(parts[1])
            rssi = int(parts[2])
            snr = float(parts[3])
            if kind == sniff.FRAME:
                if len(parts) != 5:
                    raise ValueError("fields")
                data = bytes.fromhex(parts[4])
            elif len(parts) != 4:
                raise ValueError("fields")
        except (ValueError, IndexError):
            cap.malformed += 1
            continue
        elapsed += (t - last) % period
        last = t
        if kind == sniff.FRAME:
            cap.frames.append((elapsed, u, rssi, snr, data))
        else:
            cap.crc.append((elapsed, u, rssi, snr))
    if cap.settings is None:
        raise ValueError("no SNIFF line: not a capture of make sniff")
    return cap


class Boxes:
    """The boxes of a network and its key (`load_boxes`).

    ``names``: address -> (name, role); ``key``: the frame key (16 B, None
    without the network file); ``fingerprint``, ``network``, ``sf``,
    ``profile``: the network's (None without it).
    """

    def __init__(self):
        self.names = {}
        self.key = None
        self.fingerprint = None
        self.network = None
        self.sf = None
        self.profile = None


def load_boxes(network, box_files, network_path="network"):
    """Return (`Boxes`, errors) of a decoded network file (None: none) and
    box files [(path, decoded)], merged as ``make configure`` merges them."""
    out = Boxes()
    errors = []
    cache = {}

    def derive(password, name, iterations):
        k = (password, name, iterations)
        if k not in cache:
            from mesh import kdf

            cache[k] = kdf.derive(password, name, iterations)
        return cache[k]

    if network is not None:
        # The key alone, through a stand-in box: the derivation of make configure.
        cfg, problems = boxes.merge(
            network, {"role": config.NODE, "address": 1}, network_path, derive
        )
        if cfg is None:
            return None, ["%s: %s" % (network_path, e) for e in problems]
        out.key = bytes.fromhex(cfg["key"])
        out.fingerprint = cfg["fingerprint"]
        out.network = cfg["network"]
        out.sf = cfg["sf"]
        out.profile = cfg["profile"]
    for path, b in box_files:
        if network is not None:
            cfg, problems = boxes.merge(network, b, network_path, derive)
            if cfg is None:
                errors.extend(boxes._located(problems, network_path, path))
                continue
        elif type(b) is dict and type(b.get("address")) is int:
            cfg = {"address": b["address"], "role": b.get("role")}
            cfg["name"] = b.get("name") or "box%d" % b["address"]
        else:
            errors.append("%s: address: an integer expected" % path)
            continue
        if cfg["address"] in out.names:
            errors.append("address %d: in two box files" % cfg["address"])
        out.names[cfg["address"]] = (cfg["name"], cfg["role"])
    return out, errors


class Result:
    """What `convert` made: ``records`` (the trace, in file order) and the
    summary's figures: ``counts`` {transmitter: [heard, opened, not
    opened, undecodable]}, ``checked`` (payloads searched), ``short``
    (payloads too short to judge), ``found`` [(t, transmitter, port,
    length, offset)], ``shift`` (ms the times moved), ``utc0`` (UTC of
    time 0), ``span`` (ms from time 0 to the last record)."""

    def __init__(self):
        self.records = []
        self.counts = {}
        self.checked = 0
        self.short = 0
        self.found = []
        self.shift = 0
        self.utc0 = None
        self.span = 0


OPENED = 1
NOT_OPENED = 2
UNDECODABLE = 3


def outcome(rec):
    """Return `OPENED`, `NOT_OPENED` or `UNDECODABLE` of a decoded ``tx`` record."""
    if "m" in rec:
        return OPENED
    if rec.get("err") == AUTHENTICATION:
        return NOT_OPENED
    return UNDECODABLE


def plaintext_in(data, messages, min_len=MIN_PLAIN):
    """Return ([(message index, port, length, offset)] of the payloads of
    messages found in the raw frame data, payloads checked, payloads too
    short to judge)."""
    raw = bytes(data)
    found = []
    checked = 0
    short = 0
    for i, m in enumerate(messages):
        n = len(m.payload)
        if not n:
            continue
        if n < min_len:
            short += 1
            continue
        checked += 1
        at = raw.find(bytes(m.payload))
        if at >= 0:
            found.append((i, m.port, n, at))
    return found, checked, short


def _insert(events, t, rec):
    # After every event of time t or earlier: a tx before its st, an end
    # before a tx of the same time. The frames come in the order of their
    # receptions, which do not overlap: a few steps back at most (no sort:
    # MicroPython's is neither stable nor fast on sorted input).
    i = len(events)
    while i and events[i - 1][0] > t:
        i -= 1
    events.insert(i, (t, rec))


def _parents(rec, messages):
    # The preferred parents the frame tells: [(address, parent)].
    out = []
    for i, m in enumerate(messages):
        if m.port != PRESENCE:
            continue
        if m.mode == FIRST_HOP and rec["d"] != BROADCAST:
            out.append((rec["s"], rec["d"]))
        elif m.mode == UPWARD:
            pp = rec["m"][i].get("pp")
            if pp is not None:
                out.append((m.origin, pp))
    return out


def convert(cap, known, label=None):
    """Return the `Result` of a `Capture` with the `Boxes` known (module
    documentation); label: the header's (default: the sniffer and its
    start)."""
    s = cap.settings
    res = Result()
    sf, bw, cr, pre = s["sf"], s["bw"], s["cr"], s.get("preamble", 8)

    def toa(n):
        return time_on_air_ms(n, sf, bw, cr, pre)

    starts = [t - toa(len(d)) for t, _, _, _, d in cap.frames]
    res.shift = max(0, -min(starts)) if starts else 0
    shift = res.shift
    board_utc = utc.valid(s.get("utc"))
    if board_utc is not None:
        res.utc0 = board_utc - shift
    elif cap.host0 is not None:
        res.utc0 = cap.host0 - shift
    keys = [known.key] if known.key is not None else []
    ciphers = {}
    events = []  # (time, record), by time then as made (`_insert`)
    ends = []  # (end record, its tx record)
    state = {}  # address -> [rank, version, parent]
    shown = {}  # address -> the (r, v, p) of its last st
    heard = {}  # transmitter -> [frames, ms on air]
    for k, (t, _, rssi, snr, data) in enumerate(cap.frames):
        e = t + shift
        start = starts[k] + shift
        rec = {"k": "tx", "t": start, "n": None, "e": e, "len": len(data)}
        rec["rx"] = [[SNIFFER, rssi, snr]]
        messages = decode_frame(rec, data, keys, ciphers, known.key)
        n = rec.get("s")
        if n is None:
            n = link_source(data)
        if n is None:
            n = UNKNOWN
        rec["n"] = n
        _insert(events, start, rec)
        end = {"k": "end", "t": e, "n": n, "f": ["ok"]}
        _insert(events, e, end)
        ends.append((end, rec))
        h = heard.setdefault(n, [0, 0])
        h[0] += 1
        h[1] += e - start
        c = res.counts.setdefault(n, [0, 0, 0, 0])
        c[0] += 1
        c[outcome(rec)] += 1
        if messages is None:
            continue
        found, checked, short = plaintext_in(data, messages)
        res.checked += checked
        res.short += short
        for _, port, length, at in found:
            res.found.append((start, n, port, length, at))
        # State of the transmitter, and of the origins whose parents it tells.
        st = state.setdefault(n, [None, None, None])
        st[0] = rec["r"]
        st[1] = rec["v"]
        touched = [n]
        for a, parent in _parents(rec, messages):
            state.setdefault(a, [None, None, None])[2] = parent
            if a not in touched:
                touched.append(a)
        for a in touched:
            r, v, p = state[a]
            if r is None or shown.get(a) == (r, v, p):
                continue
            shown[a] = (r, v, p)
            x = {"k": "st", "t": start, "n": a, "r": r, "v": v}
            if p is not None:
                x["p"] = p
                x["at"] = r != RANK_INFINITE
            _insert(events, start, x)
    nodes = _nodes(known, heard)
    regional = profiles.get(s["profile"])
    hdr = {
        "k": "hdr",
        "i": 0,
        "t": 0,
        "fmt": FORMAT,
        "label": label if label is not None else _label(s, res.utc0),
        "sf": sf,
        "profile": {"id": s["profile"], "name": s.get("pname"), "hz": s.get("hz")},
        "air": {"bw": bw, "cr": cr, "preamble": pre, "slot_ms": toa(PRESENCE_FRAME)},
        "nodes": nodes,
        "links": [],
    }
    if regional is not None:
        hdr["budget_ms"] = regional.budget_ms()
        hdr["window_ms"] = regional.window_ms()
    records = [hdr]
    for _, rec in events:
        rec["i"] = len(records)
        records.append(rec)
    for end, tx in ends:
        end["x"] = tx["i"]
    last = 0
    for _, rec in events:
        last = max(last, rec["t"])
    for t, _, _, _ in cap.crc:
        last = max(last, t + shift)
    stats = {}
    for n, (count, ms) in heard.items():
        stats[str(n)] = [count, ms, 0, 0, 0, 0, 0, 0, 0, 0]
    stats[SNIFFER] = [0, 0, 0, len(cap.frames), 0, 0, 0, 0, 0, 0]
    records.append({"k": "eof", "i": len(records), "t": last, "stats": stats})
    res.span = last
    res.records = records
    return res


def _label(s, utc0):
    when = "UTC unknown" if utc0 is None else utc.text(utc0) + " UTC"
    return "capture by sniffer %s from %s" % (s.get("name"), when)


def _nodes(known, heard):
    addresses = sorted(set(list(known.names) + [n for n in heard if type(n) is int]))
    nodes = []
    for a in addresses:
        nodes.append({"n": a, "a": a, "kind": "board", "root": a == ROOT})
    nodes.append({"n": SNIFFER, "kind": "sniffer"})
    if UNKNOWN in heard:
        nodes.append({"n": UNKNOWN, "kind": "sniffer"})
    return nodes


def write_trace(records, out):
    """Write records to out (a file) as JSON Lines, kind, index and time first."""
    for rec in records:
        rec = dict(rec)
        head = '{"k":"%s","i":%d,"t":%s' % (rec.pop("k"), rec.pop("i"), json.dumps(rec.pop("t")))
        rest = json.dumps(rec, separators=(",", ":"))
        out.write(head + ("," + rest[1:] if len(rest) > 2 else "}") + "\n")


def _who(n, known):
    if n == UNKNOWN:
        return "unknown (frame too short)"
    if n in known.names:
        name, role = known.names[n]
        return "%s (%d, %s)" % (name, n, role)
    return "0x%04X (%d, not in the box files)" % (n, n)


def summary(cap, known, res, path=None):
    """Return the summary's lines (module documentation)."""
    s = cap.settings
    out = []
    span = (
        ""
        if cap.host0 is None or cap.host1 is None
        else ", %.1f s" % ((cap.host1 - cap.host0) / 1000)
    )
    out.append(
        "capture%s: sniffer %s (address %s), profile %s %s, %s Hz, SF%d, BW %d Hz, CR 4/%d"
        % (
            " " + path if path else "",
            s.get("name"),
            s.get("address"),
            s["profile"],
            s.get("pname"),
            s.get("hz"),
            s["sf"],
            s["bw"],
            s["cr"],
        )
    )
    board = utc.valid(s.get("utc"))
    out.append(
        "start: board RTC %s, host %s%s"
        % (
            "not set" if board is None else utc.text(board) + " UTC",
            "unknown" if cap.host0 is None else utc.text(cap.host0) + " UTC",
            span,
        )
    )
    if known.key is None:
        out.append("key: none (no network file): no frame can be opened")
    else:
        same = s.get("fingerprint")
        out.append(
            "key: network %r, fingerprint %s, derived on the host; the sniffer's configuration: %s"
            % (
                known.network,
                known.fingerprint,
                "the same network" if same == known.fingerprint else "another network (%s)" % same,
            )
        )
        if known.sf != s["sf"] or known.profile != s["profile"]:
            out.append(
                "warning: the network file says profile %s SF%s, the sniffer listened on"
                " profile %s SF%s" % (known.profile, known.sf, s["profile"], s["sf"])
            )
    out.append(
        "%-34s %6s %7s %11s %12s" % ("transmitter", "heard", "opened", "not opened", "undecodable")
    )
    total = [0, 0, 0, 0]
    order = sorted([n for n in res.counts if type(n) is int]) + [
        n for n in res.counts if type(n) is not int
    ]
    for n in order:
        c = res.counts[n]
        for i in range(4):
            total[i] += c[i]
        out.append("%-34s %6d %7d %11d %12d" % (_who(n, known), c[0], c[1], c[2], c[3]))
    out.append("%-34s %6d %7d %11d %12d" % ("total", total[0], total[1], total[2], total[3]))
    end = cap.end
    line = "CRC errors: %d (not in the trace: their transmitter is unknown)" % len(cap.crc)
    if end is None:
        line += "; END line missing (the board stopped otherwise: its counters unknown)"
    else:
        line += "; board: %s" % ", ".join(
            "%s %s" % (k, end[k])
            for k in ("hdr_err", "preambles", "headers", "errors", "rx_restarts", "noise")
            if k in end
        )
    out.append(line)
    if end is not None:
        out.append(
            "lines printed by the board: %s frames, %s CRC errors; in the capture: %d, %d"
            " (fewer: printed after the host stopped reading, or lost on the way)"
            % (end.get("frames"), end.get("crc"), len(cap.frames), len(cap.crc))
        )
        startable = end.get("skip") == 0 and end.get("crashes", 0) < GUARD_MAX
        out.append(
            "board left at the REPL, radio asleep; start guard: crashes %s, skip flag %s: %s"
            % (
                end.get("crashes"),
                end.get("skip"),
                "its next reset starts the application"
                if startable
                else "CHECK: its next reset may not start the application",
            )
        )
    if cap.errors:
        out.append("sniffer errors: " + "; ".join(cap.errors))
    if cap.malformed or cap.other:
        out.append(
            "lines skipped: %d malformed frame lines, %d other lines" % (cap.malformed, cap.other)
        )
    if known.key is None:
        out.append("ciphertext check: no key, no plaintext to look for")
    else:
        out.append(
            "ciphertext check: %d payloads of %d bytes or more searched in their raw"
            " frames, %d shorter not judged: %s"
            % (
                res.checked,
                MIN_PLAIN,
                res.short,
                "no plaintext found" if not res.found else "%d FOUND" % len(res.found),
            )
        )
        for t, n, port, length, at in res.found:
            out.append(
                "  PLAINTEXT: frame at %.3f s from %s: port %d payload of %d B at byte %d"
                " of the raw frame" % (t / 1000, _who(n, known), port, length, at)
            )
    return out


def _load_json(path):
    with open(path) as f:
        return json.loads(f.read())


def main(argv):
    args = argv[1:]
    network_path = None
    out_path = None
    paths = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--network", "--out") and i + 1 < len(args):
            if a == "--network":
                network_path = args[i + 1]
            else:
                out_path = args[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            print(USAGE)
            return 2
        paths.append(a)
        i += 1
    if not paths:
        print(USAGE)
        return 2
    capture_path = paths[0]
    try:
        with open(capture_path) as f:
            cap = parse(f)
        network = _load_json(network_path) if network_path else None
        box_files = [(p, _load_json(p)) for p in paths[1:]]
    except (OSError, ValueError) as e:
        print("error: %s" % e)
        return 1
    known, errors = load_boxes(network, box_files, network_path or "network")
    if errors:
        for e in errors:
            print(e)
        return 1
    res = convert(cap, known)
    if out_path is None:
        stem = (
            capture_path.rsplit(".", 1)[0]
            if "." in capture_path.rsplit("/", 1)[-1]
            else capture_path
        )
        out_path = stem + ".jsonl"
    with open(out_path, "w") as f:
        write_trace(res.records, f)
    for line in summary(cap, known, res, capture_path):
        print(line)
    print("trace: %s, %d records (tools/traceview.html)" % (out_path, len(res.records)))
    return 1 if res.found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
