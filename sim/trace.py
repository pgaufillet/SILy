"""Trace recorder: everything a simulated network plays, as JSON Lines.

A `Recorder` observes a `sim.net.Network` (``net.observers``, `sim.net.Observer`)
and its medium (``medium.observers``, `sim.medium.Observer`) and writes one
JSON object per line, for an offline viewer: per-node chronogram (airtime,
receptions and their fate, carrier sense, backoff), lifecycle of a message
from its submission to its delivery or failure (every frame carrying it, at
every hop, with the acknowledgements), journal of every event, and context
graph (links, ranks, preferred parents and parent sets over time).

**No effect on the simulation**: the recorder is called synchronously by the
simulator, at its boundary (medium, `sim.node.Node`, `sim.net.Board`,
`sim.invariants.Checker`); it draws nothing, schedules nothing and only reads
the boards' state (attributes, ``len(queue)``, ``Parents.members``). Frames
are decoded from a copy, decrypted with the recorder's own cipher of the
sender's key (`mesh.security.frame_cipher`, `mesh.frame` codec): no board's
replay table, counters or keys are touched. A run with a recorder computes
what it computes without (tests/test_sim_trace.py).

**Records**. Every record has ``k`` (kind), ``i`` (index: 0 for the header,
then strictly increasing in file order) and ``t`` (simulated time, ms; file
order is time order). Nodes are named by their medium name ``n`` (the
header maps names to mesh addresses: they differ only in tests that say
so); fields of decoded frames and messages hold mesh addresses. Absent
fields are omitted.

- ``hdr`` (first line): ``fmt`` (`FORMAT`), ``label``, ``seed``, ``sf``,
  ``profile`` {``id``, ``name``, ``hz``}, ``air`` {``bw``, ``cr``,
  ``preamble``, ``slot_ms``: time on air of a presence frame}, ``sense_ms``,
  ``capture_db``, ``budget_ms``, ``window_ms``, ``nodes`` [{``n``, ``a``
  address (boards), ``kind`` "board" or "sniffer" (any other radio),
  ``root``, ``pos`` [x, y] if known}], ``links`` [[src, dst, loss, rssi, snr, up]] (the state
  when the header is written: at the first `sim.net.Network.run`; records
  of what happened before it follow). Example::

    {"k":"hdr","i":0,"t":0,"fmt":1,"seed":1,"sf":8,"nodes":[{"n":0,"a":0,
     "kind":"board","root":true}, ...],"links":[[0,257,0.0,-80,10,1], ...]}

- ``tx``: a transmission starts. ``n`` transmitter, ``e`` end, ``len``,
  ``rx`` [[dst, rssi, snr]] its receptions (up links at the start); clear
  header: ``d`` link destination, ``s`` link source, ``fc`` frame counter,
  ``r`` rank, ``v`` DODAG version, ``ep`` key epoch; ``m`` messages, each
  {``m`` mode, ``c`` class, ``p`` port, ``o`` origin (the link source for
  mode 6, as `mesh.reliable.identity`), ``d`` destination, ``q``
  sequence, ``len`` payload length, ``r`` route} plus, by port: 3 ``a``
  [[link source, counter low 16 bits]] (an entry acknowledges the latest
  frame before it whose ``s`` and ``fc`` & 0xFFFF match), 4 ``e`` [reason, next hop,
  origin, destination, port, sequence], 9 ``sol`` 1 (solicitation) or
  ``di`` [flags, presence s, topology s, Imin slots, max increase,
  profile], 1 ``pp`` parent (mode 1 only) and ``pf`` flags; ``bad`` 1 for
  a payload of these ports that does not decode. ``err`` instead of the
  messages when the frame does not decode (`mesh.frame` reason, or
  "authentication": no known key opens it); ``sn`` 1: sent by a sniffer;
  ``fk`` 1: opened by a key other than the network's. Example::

    {"k":"tx","i":812,"t":61234,"n":271,"e":61336,"len":27,"d":257,"s":271,
     "fc":40,"r":44,"v":0,"ep":0,"rx":[[257,-80,10],[258,-80,10]],
     "m":[{"m":6,"c":1,"p":20,"o":271,"q":5871,"len":10}]}

- ``end``: the transmission ``x`` (index of its ``tx``; null if it began
  before the recorder) ends; ``TX_DONE`` reaches the transmitter at this
  time. ``f``: fate of each reception, in
  the order of ``rx``: "ok", "loss", "collision", "half-duplex", "cut"
  (`sim.medium`). Example: ``{"k":"end","i":815,"t":61336,"n":271,
  "x":812,"f":["ok","collision"]}``.
- ``cs``: carrier sense of ``n``, ``b`` 1 busy / 0 free (answer delivered
  ``sense_ms`` later). ``bo``: after a busy answer, backoff of ``n`` until
  ``u``; ``a`` attempts of the procedure so far (0: given up after
  `mesh.link.MAX_ATTEMPTS`, a new procedure after the backoff).
- ``st``: state of ``n`` after a step, when it changed: ``r`` rank, ``v``
  DODAG version, and with routing ``p`` preferred parent, ``ps`` parent set
  (preferred first), ``at`` attached. ``q``: transmit queue length ``q``,
  when it changed. ``cnt``: counters of ``n`` that a step changed, ``c``
  {name: increase} (`COUNTERS`: class 1 retries and acknowledgements,
  errors, refusals, give-ups, rejected frames, DODAG adoptions, poisons,
  solicitations, Trickle resets...). Example: ``{"k":"cnt","i":901,
  "t":61340,"n":257,"c":{"acks_passive":1}}``.
- ``sub``: `sim.net.Board.submit` of ``n``: ``p`` port, ``c`` class, ``d``
  destination, ``len``, ``q`` sequence (>= 0) or refusal (< 0). ``dlv``:
  delivery on a listened port of ``n``: ``o`` origin, ``p``, ``q``, ``len``,
  ``s`` and ``fc`` of the frame that brought it. ``fail``: failure callback
  of ``n``: ``d``, ``p``, ``q``, ``why`` reason (`mesh.link` local reasons
  < 0, `mesh.reliable` network reasons > 0).
- ``boot`` (``n``, ``b`` engines built so far), ``down`` (``n``, ``how``
  "reset" or "power", ``off`` ms), ``node`` (a node added after the
  header, fields of the header's ``nodes``), ``link`` (``s``, ``d``,
  ``up``, ``loss``, ``rssi``, ``snr``: a link created, replaced, cut or
  restored after the header).
- ``viol``: `sim.invariants.Checker` violation: ``chk``, ``at`` its time,
  ``ns`` nodes, ``why``. ``scn``: scenario event of the checker, ``what``
  ("switch-off", "global-repair", "links-changed") and ``arg``.
- ``eof`` (last line, `Recorder.close`): ``stats`` {name: [tx_count, tx_ms,
  busy_ms, rx_ok, rx_loss, rx_collision, rx_half_duplex, rx_cut,
  sense_count, sense_busy]} of the medium.

**Use**: ``Recorder(net, "run.jsonl")`` before or after adding the boards
(attached before, it also sees their first boot), then run the network and
`Recorder.close`. Without editing anything, ``make trace
TEST=MODULE[.CLASS]`` records every network a test class of tests/ builds,
``make trace RUN="SCENARIO TOPOLOGY SEED [NODES]"`` one run of the
large-network suite, into ``OUT`` (default build/trace; `tools/trace.py`).
"""

import json

from mesh.airtime import time_on_air_ms
from mesh.dodag import decode_info
from mesh.frame import (
    FIRST_HOP,
    HEADER_SIZE,
    TAG_SIZE,
    UPWARD,
    FormatError,
    aad,
    body,
    decode_header,
    decode_messages,
    tag,
)
from mesh.frame import ROOT as ROOT_ADDRESS
from mesh.link import BACKOFF, PRESENCE_FRAME
from mesh.ports import ACK, DODAG_INFO, ERROR, PRESENCE
from mesh.radio import CHANNEL
from mesh.reliable import decode_acks, decode_error
from mesh.reports import decode_presence
from mesh.security import AUTHENTICATION, NONCE_SIZE, frame_cipher
from sim.net import Board

FORMAT = 1

# Link counters reported by ``cnt`` records (`mesh.link.Counters`): the
# decisions of class 1, medium access and reception a frame does not show.
COUNTERS = (
    "retries",
    "reroutes",
    "unreachable",
    "held",
    "hold_expired",
    "acks_passive",
    "acks_explicit",
    "ack_entries",
    "acks_late",
    "acks_dropped",
    "ack_withheld",
    "duplicates",
    "retaken",
    "errors_sent",
    "errors_received",
    "errors_forwarded",
    "errors_dropped",
    "give_ups",
    "access_dropped",
    "duty_dropped",
    "duty_waits",
    "refused_full",
    "refused_quota",
    "evicted",
    "no_route",
    "rx_auth",
    "rx_replay",
    "rx_malformed",
    "rx_no_handler",
    "duplicate_address",
)
# DODAG (`mesh.dodag.Counters`) and routing (`mesh.routing.Counters`)
# counters, and the Trickle resets.
DODAG_COUNTERS = (
    "adoptions",
    "poisons",
    "solicits",
    "rank_errors",
    "older",
    "newer",
    "incomparable",
    "foreign_profile",
)
ROUTING_COUNTERS = ("loops", "rank_dropped", "redirected")
TRICKLE_RESETS = "trickle_resets"


def _compiled(names, op):
    # function(counters) of the named attributes joined by op (tuple or sum),
    # compiled once: the step hook sums them after every step, and attribute
    # loads in one expression cost far less than getattr by name in a loop
    # (about 10 % of a large run).
    if op == ",":
        return eval("lambda c: (%s,)" % ",".join("c." + n for n in names))
    return eval("lambda c: %s" % "+".join("c." + n for n in names))


_link_counts = _compiled(COUNTERS, ",")
_dodag_counts = _compiled(DODAG_COUNTERS, ",")
_routing_counts = _compiled(ROUTING_COUNTERS, ",")
_link_sum = _compiled(COUNTERS, "+")
_dodag_sum = _compiled(DODAG_COUNTERS, "+")
_routing_sum = _compiled(ROUTING_COUNTERS, "+")


def _total(engine, routing):
    # Sum of the counters of `_counts`: they only grow (from 0 at boot), so
    # the sum changes exactly when one of them does, and computing it
    # allocates nothing (the snapshot is taken only then: allocations per
    # step would make the garbage collector, which marks the whole network,
    # run more often).
    if routing is None:
        return _link_sum(engine.counters)
    dodag = routing.dodag
    return (
        _link_sum(engine.counters)
        + _dodag_sum(dodag.counters)
        + _routing_sum(routing.counters)
        + dodag.trickle.resets
    )


def _counts(engine, routing):
    # Snapshot of the counters of `COUNTERS`, `DODAG_COUNTERS`,
    # `ROUTING_COUNTERS` and the Trickle resets, as a tuple.
    if routing is None:
        return _link_counts(engine.counters)
    dodag = routing.dodag
    return (
        _link_counts(engine.counters)
        + _dodag_counts(dodag.counters)
        + _routing_counts(routing.counters)
        + (dodag.trickle.resets,)
    )


_NAMES = COUNTERS + DODAG_COUNTERS + ROUTING_COUNTERS + (TRICKLE_RESETS,)


class Recorder:
    """Writes the trace of net (a `sim.net.Network`) to out.

    out: a path, or an object with ``write`` (left open by `close`); label:
    text copied into the header (the test or run traced); positions: {name:
    (x, y)} of the nodes for the header (`sim.topologies.Topology.positions`),
    settable until the header is written. ``count`` is the number of records
    written.
    """

    def __init__(self, net, out, label=None, positions=None):
        self.net = net
        self.label = label
        self.positions = positions
        if isinstance(out, str):
            self._file = open(out, "w")
            self._owned = True
        else:
            self._file = out
            self._owned = False
        self.count = 0
        self._index = 0
        self._pending = []  # records before the header
        self._header = False
        self._closed = False
        self._radios = {}  # medium name -> Board or Sniffer
        self._names = []  # medium names in attach order
        self._state = {}  # name -> last st key
        self._queue = {}  # name -> last queue length
        self._counts = {}  # name -> (sum, snapshot) of the counters at the last step
        self._ciphers = {}  # key -> Ccm (the recorder's own)
        self._tx = {}  # (transmitter, start) -> index of its tx record
        for name in net.medium.addresses():  # a sniffer added before: no object
            self._names.append(name)
            board = net.boards.get(name)
            if board is not None:
                self._radios[name] = board
        net.observers.append(self)
        net.medium.observers.append(self)

    # --- Output -------------------------------------------------------------------------

    def _emit(self, rec):
        self._index += 1
        rec["i"] = self._index
        if "t" not in rec:
            rec["t"] = self.net.now
        if self._header:
            self._write(rec)
        else:
            self._pending.append(rec)
        return self._index

    def _write(self, rec):
        # Kind, index and time first, for a reader of the raw file.
        head = '{"k":"%s","i":%d,"t":%s' % (rec.pop("k"), rec.pop("i"), json.dumps(rec.pop("t")))
        rest = json.dumps(rec, separators=(",", ":"))
        if len(rest) > 2:
            line = head + "," + rest[1:] + "\n"
        else:
            line = head + "}\n"
        self._file.write(line)
        self.count += 1

    def _node(self, name):
        radio = self._radios.get(name)
        rec = {"n": name}
        if isinstance(radio, Board):
            rec["kind"] = "board"
            rec["a"] = radio.address
            rec["root"] = radio.address == ROOT_ADDRESS
        else:
            rec["kind"] = "sniffer"
        if self.positions is not None and name in self.positions:
            rec["pos"] = list(self.positions[name])
        return rec

    def header(self):
        """Write the header now (otherwise at the first run, or at `close`)."""
        if self._header:
            return
        net = self.net
        medium = net.medium
        profile = net.profile
        bw = profile.bandwidth_hz
        cr = profile.coding_rate
        links = []
        for src, dst, link in medium.links():
            links.append([src, dst, round(link.loss, 4), link.rssi, link.snr, int(link.up)])
        rec = {
            "k": "hdr",
            "i": 0,
            "t": net.now,
            "fmt": FORMAT,
            "label": self.label,
            "seed": net.seed,
            "sf": net.sf,
            "profile": {"id": profile.ident, "name": profile.name, "hz": profile.frequency_hz},
            "air": {
                "bw": bw,
                "cr": cr,
                "preamble": 8,
                "slot_ms": time_on_air_ms(PRESENCE_FRAME, net.sf, bw, cr),
            },
            "sense_ms": medium.sense_ms,
            "capture_db": medium.capture_db,
            "budget_ms": net.budget_ms if net.budget_ms is not None else profile.budget_ms(),
            "window_ms": net.window_ms if net.window_ms is not None else profile.window_ms(),
            "nodes": [self._node(n) for n in self._names],
            "links": links,
        }
        self._header = True
        self._write(rec)
        for r in self._pending:
            self._write(r)
        self._pending = None

    def close(self):
        """Write the last record (medium statistics), close the output, release the network."""
        if self._closed:
            return
        self.header()
        stats = {}
        medium = self.net.medium
        for name in self._names:
            s = medium.stats(name)
            stats[str(name)] = [
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
            ]
        self._emit({"k": "eof", "stats": stats})
        self._closed = True
        if self in self.net.observers:
            self.net.observers.remove(self)
        if self in medium.observers:
            medium.observers.remove(self)
        if self._owned:
            self._file.close()
        self.net = None
        self._radios = {}

    # --- Frames ---------------------------------------------------------------------------

    def _cipher(self, key):
        key = bytes(key)
        c = self._ciphers.get(key)
        if c is None:
            c = frame_cipher(key)
            self._ciphers[key] = c
        return c

    def _keys(self, radio):
        # The sender's key first, then every other key of the network (a
        # sniffer replays the frames of any board).
        if isinstance(radio, Board):
            yield radio.key
        tried = [radio.key] if isinstance(radio, Board) else []
        for k in [self.net.key] + [b.key for b in self.net.boards.values()]:
            if k not in tried:
                tried.append(k)
                yield k

    def _decode(self, rec, radio, data):
        # Header and messages of a copy of data into rec.
        n = len(data)
        try:
            h = decode_header(data, n)
        except FormatError as e:
            rec["err"] = e.reason
            return
        rec["d"] = h.dest
        rec["s"] = h.source
        rec["fc"] = h.counter
        rec["r"] = h.rank
        rec["v"] = h.dodag_version
        rec["ep"] = h.epoch
        nonce = bytes(data[3:8]) + bytes(NONCE_SIZE - 5)
        buf = None
        for key in self._keys(radio):
            buf = bytearray(data)
            if self._cipher(key).decrypt(nonce, aad(buf), body(buf, n), tag(buf, n)):
                if key != self.net.key:
                    rec["fk"] = 1
                break
            buf = None
        if buf is None:
            rec["err"] = AUTHENTICATION
            return
        try:
            messages = decode_messages(buf, HEADER_SIZE, n - TAG_SIZE)
        except FormatError as e:
            rec["err"] = e.reason
            return
        rec["m"] = [self._message(m, h) for m in messages]

    def _message(self, m, h):
        out = {"m": m.mode, "c": m.cls, "p": m.port, "len": len(m.payload)}
        if m.mode == FIRST_HOP:
            out["o"] = h.source
        elif m.origin is not None:
            out["o"] = m.origin
        if m.dest is not None:
            out["d"] = m.dest
        if m.seq is not None:
            out["q"] = m.seq
        if m.route is not None:
            out["r"] = list(m.route)
        port = m.port
        try:
            if port == ACK:
                out["a"] = [list(e) for e in decode_acks(m.payload)]
            elif port == ERROR:
                out["e"] = list(decode_error(m.payload))
            elif port == DODAG_INFO:
                info = decode_info(m.payload)
                if info is None:
                    out["sol"] = 1
                else:
                    out["di"] = [
                        info.flags,
                        info.presence_s,
                        info.topology_s,
                        info.imin_slots,
                        info.max_increase,
                        info.profile,
                    ]
            elif port == PRESENCE and m.mode in (UPWARD, FIRST_HOP):
                p = decode_presence(m.payload, m.mode == UPWARD)
                if p.parent is not None:
                    out["pp"] = p.parent
                out["pf"] = p.flags
        except ValueError:
            out["bad"] = 1
        return out

    # --- Medium observer (`sim.medium.Observer`) ----------------------------------------

    def transmitting(self, source, frame, start, end, receivers):
        radio = self._radios.get(source)
        rec = {
            "k": "tx",
            "t": start,
            "n": source,
            "e": end,
            "len": len(frame),
            "rx": [list(r) for r in receivers],
        }
        if not isinstance(radio, Board):
            rec["sn"] = 1
        self._decode(rec, radio, frame)
        self._tx[(source, start)] = self._emit(rec)

    def ended(self, source, start, end, fates):
        x = self._tx.pop((source, start), None)
        self._emit({"k": "end", "t": end, "n": source, "x": x, "f": fates})

    def sensed(self, address, now, busy):
        self._emit({"k": "cs", "t": now, "n": address, "b": 1 if busy else 0})

    def linked(self, src, dst, link):
        if not self._header:
            return  # the header lists the links as they are then
        self._emit(
            {
                "k": "link",
                "s": src,
                "d": dst,
                "up": int(link.up),
                "loss": round(link.loss, 4),
                "rssi": link.rssi,
                "snr": link.snr,
            }
        )

    # --- Network observer (`sim.net.Observer`) ------------------------------------------

    def added(self, radio):
        self._radios[radio.name] = radio
        self._names.append(radio.name)
        if self._header:
            rec = self._node(radio.name)
            rec["k"] = "node"
            self._emit(rec)

    def booted(self, board):
        name = board.name
        self._state.pop(name, None)
        self._queue.pop(name, None)
        self._counts.pop(name, None)
        self._emit({"k": "boot", "n": name, "b": board.boots})

    def down(self, board, how, off_ms):
        self._emit({"k": "down", "n": board.name, "how": how, "off": off_ms})

    def running(self, until):
        self.header()

    def stepped(self, node, event):
        name = node.radio.address
        board = self._radios.get(name)
        if board is None or board.node is not node:
            return
        engine = node.core
        routing = board.routing
        if routing is not None:
            parents = routing.parents
            key = (engine.rank, engine.dodag_version, parents.preferred, parents.members())
        else:
            key = (engine.rank, engine.dodag_version)
        if key != self._state.get(name):
            self._state[name] = key
            rec = {"k": "st", "n": name, "r": key[0], "v": key[1]}
            if routing is not None:
                rec["p"] = key[2]
                rec["ps"] = list(key[3])
                rec["at"] = routing.attached
            self._emit(rec)
        q = len(engine.queue)
        if q != self._queue.get(name):
            self._queue[name] = q
            self._emit({"k": "q", "n": name, "q": q})
        total = _total(engine, routing)
        last = self._counts.get(name)
        if last is None or total != last[0]:
            counts = _counts(engine, routing)
            self._counts[name] = (total, counts)
            if last is not None and len(last[1]) == len(counts):
                before = last[1]
                c = {}
                for i in range(len(counts)):
                    if counts[i] != before[i]:
                        c[_NAMES[i]] = counts[i] - before[i]
                self._emit({"k": "cnt", "n": name, "c": c})
        if event is not None and event[0] == CHANNEL and event[1] and engine._state == BACKOFF:
            # Private fields of the link engine, read only: end of the
            # backoff and attempts of the access procedure (§8).
            self._emit({"k": "bo", "n": name, "u": engine._wake, "a": engine._attempts})

    def submitted(self, board, port, cls, dest, payload, result):
        self._emit(
            {
                "k": "sub",
                "n": board.name,
                "p": port,
                "c": cls,
                "d": dest,
                "len": len(payload),
                "q": result,
            }
        )

    def delivered(self, board, origin, msg, header):
        self._emit(
            {
                "k": "dlv",
                "n": board.name,
                "o": origin,
                "p": msg.port,
                "q": msg.seq,
                "len": len(msg.payload),
                "s": header.source,
                "fc": header.counter,
            }
        )

    def failed(self, board, dest, port, seq, reason):
        self._emit({"k": "fail", "n": board.name, "d": dest, "p": port, "q": seq, "why": reason})

    def violated(self, violation):
        self._emit(
            {
                "k": "viol",
                "chk": violation.check,
                "at": violation.time,
                "ns": list(violation.nodes),
                "why": violation.detail,
            }
        )

    def scenario(self, kind, detail):
        self._emit({"k": "scn", "what": kind, "arg": detail})
