"""Records of the field journal (``sily.journal``), built from a box's mesh node.

Every line holds ``b`` (boot number), ``t`` (ms, the application's clock:
since the board's boot) and ``k``, its kind:

- ``boot``: `boot` (version, reset cause, crash count, role, address,
  name, SF, power, gain, key fingerprint, presence period, next frame
  counter, ``random_restart``, noise floor, free heap);
- ``min``, every ``journal.minute_s``: the pieces of `minute` and the
  application's figures (``bat`` battery mV, ``heap`` free bytes, ``jw``
  and ``js`` the longest journal write and sync in ms since the last
  record, ``jq`` callback items lost to a full queue, ``je`` journal
  errors; all totals since boot);
- ``ev``: ``e`` names the event: ``attach``, ``detach``, ``parent``,
  ``rank``, ``grounded``, ``ungrounded`` (`transitions`, from the state
  polled once a second; each with the parent set's counters ``sw``,
  ``det``, ``flt``, so that a change undone between two polls shows),
  ``fail`` (a failure of an own message: ``dst``, ``port``, ``seq``,
  ``why`` the reason);
- ``stop`` (Ctrl-C), ``fatal`` (``err`` and ``tb``, the traceback);
- on the gateway, the reports received (`report`): ``rp`` presence, ``rt``
  topology, ``ri`` node information, each with ``o`` its origin.

Units: margins and SNR in tenths of dB, RSSI in dBm, ETX in 1/128, ages
in s, d_in in %. Neighbour rows (``nb``): `NEIGHBOUR_FIELDS`.

The presence application extension of a box is one byte (`encode_extension`):
the battery voltage, 3.00 to 4.26 V in 20 mV steps (bits 0-5, clamped),
and the operating mode (`MODES`, bits 6-7).
"""

from mesh.frame import RANK_INFINITE
from mesh.neighbours import EXPIRY_PERIODS
from mesh.ports import PRESENCE, TOPOLOGY

# Operating modes (MESH_DESIGN §17), the extension's bits 6-7.
MODES = ("configuration", "deployment", "competition", "retrieval")
BATTERY_MIN_MV = 3000
BATTERY_STEP_MV = 20
BATTERY_LEVELS = 64  # 3000 to 4260 mV

# Columns of a neighbour row of the minute record and `App.status`.
NEIGHBOUR_FIELDS = (
    "address",
    "rank",
    "margin",  # mean of the last 8 frames, 0.1 dB above the SF's limit
    "last_margin",
    "etx",  # 1/128
    "d_in",  # %
    "age_s",
    "parent",  # 1: in the parent set
    "frames",  # since boot (tap)
    "rssi",  # last, dBm
    "rssi_mean",  # since the last minute record, dBm
    "snr_mean",  # since the last minute record, 0.1 dB
)

# Counter totals of the minute record: (key, attribute).
LINK = (
    ("tf", "tx_frames"),
    ("rf", "rx_frames"),
    ("crc", "rx_crc"),
    ("len", "rx_length"),
    ("rv", "rx_version"),
    ("au", "rx_auth"),
    ("rpl", "rx_replay"),
    ("mal", "rx_malformed"),
    ("ovh", "rx_overheard"),
    ("nh", "rx_no_handler"),
    ("da", "duplicate_address"),
    ("dlv", "delivered"),
    ("fwd", "forwarded"),
    ("nor", "no_route"),
    ("full", "refused_full"),
    ("quo", "refused_quota"),
    ("evi", "evicted"),
    ("ddr", "duty_dropped"),
    ("dw", "duty_waits"),
    ("sen", "senses"),
    ("bsy", "busy"),
    ("bo", "backoffs"),
    ("rw", "reply_waits"),
    ("gu", "give_ups"),
    ("adr", "access_dropped"),
    ("rty", "retries"),
    ("rrt", "reroutes"),
    ("unr", "unreachable"),
    ("hld", "held"),
    ("hex", "hold_expired"),
    ("ap", "acks_passive"),
    ("ae", "acks_explicit"),
    ("ack", "ack_entries"),
    ("rep", "replies"),
    ("dup", "duplicates"),
    ("es", "errors_sent"),
    ("er", "errors_received"),
)
ROUTING = (
    ("loop", "loops"),
    ("rkd", "rank_dropped"),
    ("rdr", "redirected"),
    ("rcv", "reports"),
    ("rmal", "malformed"),
)
DODAG = (
    ("adv", "adverts"),
    ("sol", "solicits"),
    ("adp", "adoptions"),
    ("poi", "poisons"),
    ("rke", "rank_errors"),
)
PARENTS = (
    ("sw", "switches"),
    ("det", "detaches"),
    ("flt", "floats"),
    ("drp", "drops"),
)
REPORTER = (
    ("pr", "presence"),
    ("top", "topology"),
    ("ref", "refused"),
    ("pu", "presence_unacked"),
)
# Adapter statistics (``board.sx1262.Sx1262.stats``): (key, stats key).
RADIO = (
    ("rx", "rx"),
    ("crc", "crc"),
    ("hdr", "hdr_err"),
    ("tx", "tx"),
    ("txto", "tx_timeouts"),
    ("rxr", "rx_restarts"),
    ("err", "errors"),
)


def counters(obj, fields, out=None):
    """Return {key: obj.attribute} for fields [(key, attribute)] (into out)."""
    if out is None:
        out = {}
    for key, name in fields:
        out[key] = getattr(obj, name)
    return out


# --- Presence extension ---------------------------------------------------------------


def encode_extension(battery_mv, mode=0):
    """Return the 1-byte presence extension: battery level (clamped) and mode (0-3)."""
    if battery_mv is None:
        level = 0
    else:
        level = (battery_mv - BATTERY_MIN_MV + BATTERY_STEP_MV // 2) // BATTERY_STEP_MV
        if level < 0:
            level = 0
        elif level >= BATTERY_LEVELS:
            level = BATTERY_LEVELS - 1
    return bytes(((mode & 3) << 6 | level,))


def decode_extension(data):
    """Return (battery mV, mode) of an extension, None if it is not 1 byte."""
    if len(data) != 1:
        return None
    b = data[0]
    return BATTERY_MIN_MV + (b & 0x3F) * BATTERY_STEP_MV, b >> 6


# --- State and events ------------------------------------------------------------------

# State tuple: attached, preferred parent, rank, grounded, then the parent
# set's counters switches, detaches, floats.
DETACHED = (0, None, RANK_INFINITE, 0, 0, 0, 0)


def state(engine, routing):
    """Return the node's state tuple (see `DETACHED`), read without changing it."""
    if routing is None:  # static routes
        return (1, None, engine.rank, 1, 0, 0, 0)
    p = routing.parents
    return (
        1 if routing.attached else 0,
        p.preferred,
        engine.rank,
        1 if p.grounded else 0,
        p.switches,
        p.detaches,
        p.floats,
    )


def transitions(prev, cur):
    """Return the events [(name, fields)] from state prev to state cur."""
    events = []
    was, par0, rank0, gnd0 = prev[0], prev[1], prev[2], prev[3]
    att, par, rank, gnd = cur[0], cur[1], cur[2], cur[3]
    if att and not was:
        events.append(("attach", {"par": par, "rank": rank}))
    elif was and not att:
        events.append(("detach", {"was": par0}))
    elif att and par != par0:
        events.append(("parent", {"par": par, "was": par0, "rank": rank}))
    elif att and rank != rank0:
        events.append(("rank", {"rank": rank, "was": rank0}))
    if gnd != gnd0:
        events.append(("grounded" if gnd else "ungrounded", {}))
    for _, fields in events:
        fields["sw"] = cur[4]
        fields["det"] = cur[5]
        fields["flt"] = cur[6]
    return events


def failure(dest, port, seq, reason):
    """Return the fields of a ``fail`` event."""
    return {"e": "fail", "dst": dest, "port": port, "seq": seq, "why": reason}


# --- Gateway reports ---------------------------------------------------------------------


def report(port, origin, rep):
    """Return (kind, fields) of a report the root received (``Routing.on_report``)."""
    if port == PRESENCE:
        out = {"o": origin, "par": rep.parent, "fl": rep.flags}
        ext = decode_extension(rep.extension)
        if ext is not None:
            out["bat"] = ext[0]
            out["mode"] = ext[1]
        elif rep.extension:
            out["x"] = rep.extension.hex()
        if rep.tails:
            out["tl"] = [list(tail) for tail in rep.tails]
        return "rp", out
    if port == TOPOLOGY:
        entries = [[a, m, 1 if member else 0, ratio] for a, m, member, ratio in rep]
        return "rt", {"o": origin, "e": entries}
    # NODE_INFO, the last kind of report
    fields = {"o": origin, "mac": rep.mac.hex(), "fw": rep.firmware, "name": rep.name}
    fields["pv"] = rep.protocol
    return "ri", fields


# --- Boot and minute records ------------------------------------------------------------


def _noise(adapter):
    stats = adapter.stats() if adapter is not None else None
    return stats, (None if stats is None else stats.get("noise"))


def boot(cfg, stack, reset, crashes, version):
    """Return the fields of the ``boot`` record of configuration cfg and its stack."""
    engine = stack.engine
    counter = engine.security.counter
    routing = stack.routing
    _, noise = _noise(stack.adapter)
    return {
        "version": version,
        "reset": reset,
        "crashes": crashes,
        "role": cfg["role"],
        "address": cfg["address"],
        "name": cfg["name"],
        "sf": cfg["sf"],
        "power": cfg["tx_power"],
        "gain": cfg["antenna_gain"],
        "fingerprint": cfg["fingerprint"],
        "presence_s": None if routing is None else routing.dodag.info.presence_s,
        "counter": counter.peek(),
        "random_restart": 1 if counter.random_restart else 0,
        "noise": noise,
        "heap": stack.heap_node,
    }


def neighbours(table, now, tap=None):
    """Return (rows, foreign) of the neighbour table at now (ms).

    rows: one list per entry heard within `EXPIRY_PERIODS` presence periods
    (older ones wait for the table's own expiry, which runs inside the
    mesh's step), columns `NEIGHBOUR_FIELDS`, the tap's figures (or None);
    foreign: [sources, frames] of the tap's sources absent from the table
    (other networks, frames failing authentication, entries removed).
    """
    limit = EXPIRY_PERIODS * table.period_ms
    rows = []
    known = {}
    for n in table.entries():
        known[n.address] = True
        age = now - n.heard
        if age > limit:
            continue
        row = [
            n.address,
            n.rank,
            n.margin(),
            n.last_margin(),
            n.etx(),
            n.d_in(),
            age // 1000,
            1 if n.parent else 0,
        ]
        figures = None if tap is None else tap.figures(n.address)
        row.extend(figures if figures is not None else (0, None, None, None))
        rows.append(row)
    sources = 0
    frames = 0
    if tap is not None:
        for address, row in tap.sources.items():
            if address not in known:
                sources += 1
                frames += row[0]
    return rows, [sources, frames]


def minute(stack, now, tap=None):
    """Yield the pieces (dicts) of the minute record of stack at now.

    stack: has ``engine``, ``routing`` (None: static routes), ``filter``
    and ``adapter`` (None: absent). The caller may yield to other tasks
    between pieces (each one is short); `Tap.restart` follows the last.

    Keys: ``att``, ``par``, ``rank``, ``gnd`` (`state`), ``ver`` (DODAG
    version), ``mem`` (parent set), ``q`` (transmit queue length), ``dc``
    (ms on air in the duty-cycle window), ``fil`` (frames of the neighbour
    filter); counter totals ``link`` (`LINK`), ``rt`` (`ROUTING`, `DODAG`,
    `PARENTS`, `REPORTER`, ``nev`` and ``nex`` the neighbour table's
    evictions and expiries), ``radio`` (`RADIO`) with ``noise`` (the
    adapter's noise floor, dBm); ``nb`` (`neighbours`), ``foreign``
    ([sources, frames] heard outside the table), ``tov`` (frames of sources
    beyond the tap's bound).
    """
    engine = stack.engine
    routing = stack.routing
    s = state(engine, routing)
    head = {
        "att": s[0],
        "par": s[1],
        "rank": s[2],
        "gnd": s[3],
        "ver": engine.dodag_version,
        "mem": [] if routing is None else list(routing.parents.members()),
        "q": len(engine.queue),
        "dc": engine.dutycycle.used(now),
    }
    if stack.filter is not None:
        head["fil"] = stack.filter.filtered
    yield head
    yield {"link": counters(engine.counters, LINK)}
    if routing is not None:
        rt = counters(routing.counters, ROUTING)
        counters(routing.dodag.counters, DODAG, rt)
        counters(routing.parents, PARENTS, rt)
        if routing.reporter is not None:
            counters(routing.reporter.counters, REPORTER, rt)
        table = engine.neighbours
        rt["nev"] = table.evictions
        rt["nex"] = table.expired
        yield {"rt": rt}
    stats, noise = _noise(stack.adapter)
    if stats is not None:
        radio = {}
        for key, name in RADIO:
            radio[key] = stats.get(name)
        yield {"radio": radio, "noise": noise}
    rows, foreign = neighbours(engine.neighbours, now, tap)
    piece = {"nb": rows, "foreign": foreign}
    if tap is not None:
        piece["tov"] = tap.overflow
        tap.restart()
    yield piece
