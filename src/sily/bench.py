"""Bench console: one link engine on the board, driven by text lines.

Debug tool, started by hand from the REPL (never at boot)::

    from sily import bench
    bench.run(1)                        # address 1, SF7, bench password
    bench.run(2, password="other", allow=(1,), power=-9)
    bench.run(0, routing=True, presence_s=10)   # root of a DODAG
    bench.run(2, routing=True)                  # a node joining it
    bench.run(0, timed=True, gc_every=10)       # root, reply turnaround timed

It builds the board's node (SX1262 adapter, neighbour filter, link engine on
the ESP32 platform, asyncio runner), prints one ``READY`` line, then reads
commands from the console, one per line, and answers with lines starting with
an upper-case word, so that a host script drives it over the serial port
(``tools/bench.py``). Ctrl-C or ``quit`` returns to the REPL.

Commands (addresses in decimal):

- ``stats``: one ``STATS`` line, a JSON object: link counters, radio
  counters and noise floor, CPU time per path, duty cycle, heap;
- ``send DEST TEXT``: a class 0 message on `TEXT_PORT`; the receiver prints
  ``RX ORIGIN PORT LEN RSSI SNR TEXT``;
- ``echo DEST COUNT [SIZE [GAP_MS]]``: echo requests on `ECHO_PORT`, answered
  on `REPLY_PORT`; prints ``ECHO SEQ RTT_MS`` per reply;
- ``flood DEST COUNT SIZE``: COUNT messages of SIZE bytes on `FLOOD_PORT`
  (counted, not printed, by the receiver);
- ``survey COUNT GAP_MS``: COUNT carrier senses of the adapter; prints
  ``SURVEY`` with the number found busy, by cause, and the instantaneous RSSI
  sampled after each (how many alone are above the threshold);
- ``cut SIZE MS``: a raw frame of SIZE bytes cut after MS ms (outside the
  mesh): neighbours receive a CRC error;
- ``filter off`` or ``filter A,B,...``: neighbour filter (§1);
- ``route``: one ``ROUTE`` line, a JSON object: rank, DODAG version,
  preferred parent and parent set, routing and report counters (with
  ``routing``);
- ``probe DEST COUNT GAP_MIN_MS GAP_MAX_MS``: COUNT frames to the root DEST,
  one every GAP_MIN_MS to GAP_MAX_MS (uniform), each carrying an upward
  message of another node (origin 3, `FLOOD_PORT`), so that the root counts
  this node as relaying, and this node's own presence: the root answers each
  at once (reply window, MESH_DESIGN §8); prints ``DONE probe SENT``;
- ``turn`` (with ``timed``): one ``TURN`` line, a JSON object: the reply
  turnaround samples (below) and the link counters they are checked
  against;
- ``zero``: reset the CPU meters and the turnaround samples;
- ``quit``.

With ``routing`` the node runs the mesh routing (`mesh.routing.Routing`:
DODAG, upward forwarding, presence and topology reports) instead of static
routes; address 0 is the root, which prints ``PRESENCE ORIGIN PARENT FLAGS
EXTENSION``, ``TOPOLOGY ORIGIN [[ADDRESS, MARGIN, MEMBER, RATIO]...]`` and
``NODEINFO ORIGIN MAC NAME`` for each report it receives; ``presence_s``
(root only) sets the presence period of the DODAG information it issues
(§9; topology period 6 times longer).

With ``timed`` a radio wrapper between the neighbour filter and the runner
times the root's immediate reply from the adapter's timestamps (µs). On the
root, one sample per reply: from the RxDone IRQ of the frame it answers to
the start (SetTx) of the reply, with the source's frame counter, the RxDone
service delay and flags (1: a collection forced in the turnaround, every
``gc_every``-th frame received; 2: the heap shrank since the previous reply
went on air, a collection; 4: a frame counter block recorded in storage
before the reply; 8: another frame went on air first). On a node, one
sample per reply to a frame that opened its reply window: from its TxDone to
the start of the reply (the reply's RxDone minus its computed time on air)
and what was left of the window at that RxDone (ms).

Bench defaults: password ``bench`` with 1000 PBKDF2 iterations (the
production count is set by measurement, ``docs/measurements.md``), TX power
-9 dBm (boards a few centimetres apart), EU profile, static routes (every
destination is a neighbour) unless ``routing``.
"""

import asyncio
import gc
import json
import os
import struct
import sys
from array import array
from time import ticks_add, ticks_diff, ticks_ms, ticks_us

import machine

import sily
from board.platform import HardwareRandom, NvsStorage, RtcRetained
from board.sx1262 import Sx1262
from board.t3s3 import make_modem
from mesh import kdf
from mesh import profile as profiles
from mesh.airtime import time_on_air_us
from mesh.dodag import DodagInfo, default_info
from mesh.frame import COUNTER_OFFSET, DEST_OFFSET, FIRST_HOP, HEADER_SIZE, ROOT, UPWARD, Message
from mesh.link import destination, open_link
from mesh.ports import NODE_INFO, PRESENCE
from mesh.radio import CHANNEL, RX, RX_ERROR, TX_DONE, NeighbourFilter, Radio
from mesh.reports import NodeInfo, Presence, encode_presence
from mesh.routing import Routing
from mesh.runner import Runner
from mesh.txqueue import accepted
from sily.radio import PREAMBLE, MeshRadio, link_source, lora_config

TEXT_PORT = 100
ECHO_PORT = 101
REPLY_PORT = 102
FLOOD_PORT = 103

BENCH_ITERATIONS = 1000

_EVENT_NAMES = {
    None: "timer",
    RX: "rx",
    RX_ERROR: "rx_error",
    TX_DONE: "tx_done",
    CHANNEL: "channel",
}
_ECHO = "<HI"  # sequence, send time (ticks_ms)

_PROBE_ORIGIN = 3  # origin of the probe's upward message: another node, relayed
_SAMPLES = 512  # turnaround samples kept

# Flags of a root turnaround sample (``turn``).
_FORCED_GC = 1  # gc.collect() in the turnaround (gc_every)
_HEAP_SHRANK = 2  # the heap shrank since the previous reply went on air
_RESERVED = 4  # a frame counter block recorded in storage before the reply
_OTHER_TX = 8  # another frame went on air first


class _All:
    """Allowed set of a disabled neighbour filter."""

    def __contains__(self, item):
        return True


def _out(*items):
    print(*items)


def firmware_version():
    """Return the application version as the 16-bit node information field (major, minor)."""
    major, minor = sily.__version__.split(".")[:2]
    return int(major) << 8 | int(minor)


def _u16(data, offset):
    return data[offset] | data[offset + 1] << 8


def _counter(data):
    o = COUNTER_OFFSET
    return data[o] | data[o + 1] << 8 | data[o + 2] << 16


class _Timed(Radio):
    """Radio wrapper timing the root's immediate reply (``timed``, ``turn``).

    Sits between the neighbour filter and the runner of bench; reads the
    adapter's timestamps (``_irq_us``: last DIO1 IRQ, the RxDone one when a
    frame is reported; ``_tx_us``: SetTx). See the module documentation for
    the samples.
    """

    def __init__(self, bench, radio, sf, profile, gc_every=None):
        self.bench = bench
        self.radio = radio
        self.root = bench.engine.is_root
        self.gc_every = gc_every
        self._sf = sf
        self._bw = profile.bandwidth_hz
        self._cr = profile.coding_rate
        self.counter = array("i", [0] * _SAMPLES)  # the source's (root) or own frame counter
        self.value = array("i", [0] * _SAMPLES)  # root: turnaround µs; node: estimate µs
        self.extra = array("i", [0] * _SAMPLES)  # root: RxDone service delay µs; node: margin ms
        self.flags = array("i", [0] * _SAMPLES)
        radio.sink = self._on_event
        self.reset()

    def reset(self):
        self.n = 0
        self.overflow = 0
        self.frames = 0  # root: frames to it; node: frames that opened a reply window
        self.unanswered = 0  # replaced by the next before a reply
        self._pending = False
        self._sent = -1
        self._end = None
        self._tx_done = None
        self._alloc = gc.mem_alloc()

    def report(self):
        """Return the samples and their counters as a dict (the ``TURN`` line)."""
        n = self.n
        out = {
            "role": "root" if self.root else "node",
            "n": n,
            "overflow": self.overflow,
            "frames": self.frames,
            "unanswered": self.unanswered,
            "pending": self._pending,
            "counter": list(self.counter[:n]),
        }
        if self.root:
            out["gc_every"] = self.gc_every
            out["turn_us"] = list(self.value[:n])
            out["service_us"] = list(self.extra[:n])
            out["flags"] = list(self.flags[:n])
        else:
            out["estimate_us"] = list(self.value[:n])
            out["margin_ms"] = list(self.extra[:n])
        return out

    def transmit(self, frame):
        self.radio.transmit(frame)
        if self.root:
            self._root_tx(frame)
        else:
            self._node_tx(frame)

    def sense(self):
        self.radio.sense()

    def _on_event(self, event):
        kind = event[0]
        if kind == RX:
            if self.root:
                self._root_rx(event[1])
            else:
                self._node_rx(event[1])
        elif kind == TX_DONE:
            if self.root:
                self._root_done()
            else:
                self._node_done()
        self.sink(event)

    def _record(self, counter, value, extra, flags):
        i = self.n
        if i >= _SAMPLES:
            self.overflow += 1
            return -1
        self.counter[i] = counter
        self.value[i] = value
        self.extra[i] = extra
        self.flags[i] = flags
        self.n = i + 1
        return i

    # Root: RxDone of a frame to it, then the start of the reply to its source.

    def _root_rx(self, data):
        service = ticks_us()
        if len(data) < HEADER_SIZE or _u16(data, DEST_OFFSET) != ROOT:
            return
        e = self.bench.engine
        if self._pending:
            self.unanswered += 1
        self._pending = True
        self._irq = self.bench.adapter._irq_us
        self._service = ticks_diff(service, self._irq)
        self._source = link_source(data)
        self._source_counter = _counter(data)
        self._replies = e.counters.replies
        self._limit = e.security.counter._limit
        self._flags = 0
        self.frames += 1
        k = self.gc_every
        if k and self.frames % k == 0:
            gc.collect()
            self._flags = _FORCED_GC

    def _root_tx(self, frame):
        if not self._pending:
            return
        e = self.bench.engine
        if _u16(frame, DEST_OFFSET) != self._source or e.counters.replies == self._replies:
            self._flags |= _OTHER_TX
            return
        start = self.bench.adapter._tx_us
        self._pending = False
        flags = self._flags
        if e.security.counter._limit != self._limit:
            flags |= _RESERVED
        self._sent = self._record(
            self._source_counter, ticks_diff(start, self._irq), self._service, flags
        )

    def _root_done(self):
        # Outside the turnaround: a collection since the previous reply?
        i = self._sent
        if i < 0:
            return
        self._sent = -1
        alloc = gc.mem_alloc()
        if alloc < self._alloc:
            self.flags[i] |= _HEAP_SHRANK
        self._alloc = alloc

    # Node: a frame opening its reply window, its TxDone, the reply's RxDone.

    def _node_tx(self, frame):
        end = self.bench.engine._reply_end
        if _u16(frame, DEST_OFFSET) != ROOT or end is None or end == self._end:
            return
        if self._pending:
            self.unanswered += 1
        self._pending = True
        self._end = end
        self._own_counter = _counter(frame)
        self._tx_done = None
        self.frames += 1

    def _node_done(self):
        if self._pending and self._tx_done is None:
            a = self.bench.adapter
            self._tx_done = ticks_add(a._tx_us, a.last_tx_us)

    def _node_rx(self, data):
        if not self._pending or self._tx_done is None or len(data) < HEADER_SIZE:
            return
        b = self.bench
        if link_source(data) != ROOT or _u16(data, DEST_OFFSET) != b.engine.address:
            return
        margin = self._end - b.runner.clock.now()
        irq = b.adapter._irq_us
        self._pending = False
        toa = time_on_air_us(len(data), self._sf, self._bw, self._cr, PREAMBLE)
        self._record(self._own_counter, ticks_diff(irq, self._tx_done) - toa, margin, 0)


class Bench:
    """One bench node. See the module documentation for the parameters."""

    def __init__(
        self,
        address,
        sf=7,
        password="bench",
        network="",
        iterations=BENCH_ITERATIONS,
        key=None,
        allow=None,
        power=-9,
        routing=False,
        presence_s=None,
        timed=False,
        gc_every=None,
    ):
        gc.collect()
        free0 = gc.mem_free()
        t0 = ticks_ms()
        if key is None:
            keys = kdf.derive(password, network, iterations)
            key = keys.frame_key
            fingerprint = keys.fingerprint.hex()
        else:
            fingerprint = None
        kdf_ms = ticks_diff(ticks_ms(), t0)
        profile = profiles.get(profiles.EU_869)
        problems = profiles.validate(profile, sf, power)
        if problems:
            raise ValueError("bad radio settings: %s" % problems)
        self.adapter = Sx1262(make_modem(lora_config(profile, sf, power)))
        self.adapter.start()
        self.filter = NeighbourFilter(MeshRadio(self.adapter), _All(), link_source)
        if allow is not None:
            self.filter.allowed = set(allow)
        gc.collect()
        free1 = gc.mem_free()
        self.engine = open_link(
            address,
            key,
            NvsStorage(),
            RtcRetained(),
            HardwareRandom(),
            profile,
            sf,
            next_hop=None if routing else destination,  # static: every one a neighbour
        )
        self.routing = None
        if routing:
            info = default_info(sf, profiles.EU_869)
            if presence_s is not None:
                info = DodagInfo(
                    info.flags,
                    presence_s,
                    6 * presence_s,
                    info.imin_slots,
                    info.max_increase,
                    info.profile,
                )
            node = NodeInfo(machine.unique_id(), firmware_version(), "bench%d" % address)
            self.routing = Routing(self.engine, profiles.EU_869, info, node_info=node)
            self.routing.on_report = self._report
        radio = self.filter
        self.timed = None
        if timed:
            radio = self.timed = _Timed(self, self.filter, sf, profile, gc_every)
        self.runner = Runner(self.engine, radio)
        gc.collect()
        free2 = gc.mem_free()
        self.engine.bind(TEXT_PORT, self._text)
        self.engine.bind(ECHO_PORT, self._echo_request)
        self.engine.bind(REPLY_PORT, self._echo_reply)
        self.engine.bind(FLOOD_PORT, self._flood)
        self.flood_rx = 0
        self.flood_rx_bytes = 0
        self.probes = 0
        self.ready = {
            "address": address,
            "sf": sf,
            "power": power,
            "fingerprint": fingerprint,
            "iterations": iterations if fingerprint else None,
            "kdf_ms": kdf_ms,
            "noise": self.adapter.noise.floor,
            "heap_engine": free1 - free2,
            "heap_node": free0 - free2,
            "heap_free": free2,
            "counter": self.engine.security.counter.peek(),
            "random_restart": self.engine.security.counter.random_restart,
            "duty_used_ms": self.engine.dutycycle.used(self.runner.clock.now()),
        }
        self._done = False

    # --- Handlers ------------------------------------------------------------------

    def _measures(self):
        a = self.adapter
        return a.last_rssi, a.last_snr

    def _text(self, origin, msg, header):
        rssi, snr = self._measures()
        text = bytes(msg.payload).decode("utf-8", "replace")
        _out("RX", origin, msg.port, len(msg.payload), rssi, snr, text)

    def _echo_request(self, origin, msg, header):
        self._submit(REPLY_PORT, origin, bytes(msg.payload))

    def _echo_reply(self, origin, msg, header):
        seq, sent = struct.unpack_from(_ECHO, msg.payload)
        rssi, snr = self._measures()
        _out("ECHO", seq, ticks_diff(ticks_ms(), sent), origin, rssi, snr)

    def _flood(self, origin, msg, header):
        self.flood_rx += 1
        self.flood_rx_bytes += len(msg.payload)

    def _report(self, port, origin, report, header):
        if port == PRESENCE:
            _out("PRESENCE", origin, report.parent, report.flags, report.extension.hex())
        elif port == NODE_INFO:
            _out("NODEINFO", origin, report.mac.hex(), report.name)
        else:
            _out("TOPOLOGY", origin, json.dumps(report))

    def route(self):
        """Return the routing state as a dict (the ``ROUTE`` line)."""
        r = self.routing
        if r is None:
            return {"routing": False}
        d = r.dodag
        p = r.parents
        out = {
            "address": self.engine.address,
            "rank": self.engine.rank,
            "version": self.engine.dodag_version,
            "attached": r.attached,
            "preferred": p.preferred,
            "members": list(p.members()),
            "dodag": d.counters.__dict__,
            "routing": r.counters.__dict__,
        }
        if r.reporter is not None:
            out["reports"] = r.reporter.counters.__dict__
        return out

    def _submit(self, port, dest, payload):
        result = self.engine.submit(port, 0, dest, payload)
        self.runner.wake()
        return result

    # --- Commands ------------------------------------------------------------------

    def stats(self):
        """Return the state of the node as a dict (the ``STATS`` line)."""
        e = self.engine
        c = e.counters
        link = {}
        for name, value in c.__dict__.items():
            if isinstance(value, list):  # per port
                value = {str(p): n for p, n in enumerate(value) if n}
            link[name] = value
        now = self.runner.clock.now()
        cpu = {_EVENT_NAMES.get(k, str(k)): v for k, v in self.runner.cpu.items()}
        gc.collect()
        return {
            "address": e.address,
            "link": link,
            "senders": {str(a): e.senders._rows[a] for a in e.senders.addresses()},
            "filtered": self.filter.filtered,
            "radio": self.adapter.stats(),
            "cpu_step": cpu,
            "steps": self.runner.steps,
            "duty_used_ms": e.dutycycle.used(now),
            "counter": e.security.counter.peek(),
            "replay_entries": len(e.security.replay),
            "queue": len(e.queue),
            "flood_rx": self.flood_rx,
            "flood_rx_bytes": self.flood_rx_bytes,
            "heap_free": gc.mem_free(),
        }

    async def _echo(self, dest, count, size, gap_ms):
        pad = bytes(max(0, size - struct.calcsize(_ECHO)))
        for seq in range(count):
            payload = struct.pack(_ECHO, seq, ticks_ms()) + pad
            result = self._submit(ECHO_PORT, dest, payload)
            if result < 0:
                _out("ERROR echo", seq, result)
            await asyncio.sleep_ms(gap_ms)
        _out("DONE echo")

    def _send_probe(self, dest):
        r = self.routing
        if r is not None and r.reporter is not None:
            presence = r.reporter.presence(self.runner.clock.now())
        else:
            presence = Presence(0, b"\0\0", None, (), 0)
        e = self.engine
        relayed = Message(UPWARD, 0, FLOOD_PORT, b"..", origin=_PROBE_ORIGIN)
        own = Message(FIRST_HOP, 0, PRESENCE, encode_presence(presence))
        ok = accepted(e.send(relayed, dest)) and accepted(e.send(own, dest))
        self.runner.wake()
        return ok

    async def _probe(self, dest, count, gap_min, gap_max):
        sent = 0
        for _ in range(count):
            if self._send_probe(dest):
                sent += 1
                self.probes += 1
            gap = gap_min + int.from_bytes(os.urandom(2), "little") % (gap_max - gap_min + 1)
            await asyncio.sleep_ms(gap)
        _out("DONE probe", sent)

    def turn(self):
        """Return the turnaround samples and their counters as a dict (the ``TURN`` line)."""
        out = self.timed.report()
        c = self.engine.counters
        out["replies"] = c.replies
        out["tx_frames"] = c.tx_frames
        out["rx_frames"] = c.rx_frames
        out["flood_rx"] = self.flood_rx
        out["probes"] = self.probes
        return out

    async def _survey(self, count, gap_ms):
        a = self.adapter
        rx0, rssi0, tx0 = a.busy_rx, a.busy_rssi, a.busy_tx
        busy = 0
        high = 0  # instantaneous RSSI alone above the threshold
        levels = []
        t0 = ticks_us()
        for _ in range(count):
            busy += a.busy()
            rssi = a.rssi_inst()
            high += a.noise.busy(rssi)
            levels.append(rssi)
            await asyncio.sleep_ms(gap_ms)
        levels.sort()
        _out(
            "SURVEY",
            json.dumps(
                {
                    "count": count,
                    "busy": busy,
                    "rx": a.busy_rx - rx0,
                    "rssi": a.busy_rssi - rssi0,
                    "tx": a.busy_tx - tx0,
                    "rssi_high": high,
                    "rssi_min": levels[0],
                    "rssi_median": levels[count // 2],
                    "rssi_max": levels[-1],
                    "us": ticks_diff(ticks_us(), t0),
                    "noise": a.noise.floor,
                }
            ),
        )

    async def _cut(self, size, cut_ms):
        await self.adapter.transmit_raw(bytes(size), cut_ms)
        _out("DONE cut")

    def command(self, line):
        """Execute one command line."""
        words = line.split()
        if not words:
            return
        cmd = words[0]
        args = words[1:]
        if cmd == "stats":
            _out("STATS", json.dumps(self.stats()))
        elif cmd == "send":
            text = line.split(None, 2)[2] if len(words) > 2 else ""
            _out("SEND", self._submit(TEXT_PORT, int(args[0]), text.encode()))
        elif cmd == "echo":
            size = int(args[2]) if len(args) > 2 else 16
            gap = int(args[3]) if len(args) > 3 else 1000
            asyncio.create_task(self._echo(int(args[0]), int(args[1]), size, gap))
        elif cmd == "flood":
            dest, count, size = int(args[0]), int(args[1]), int(args[2])
            ok = 0
            for i in range(count):
                if self._submit(FLOOD_PORT, dest, bytes([i & 0xFF]) * size) >= 0:
                    ok += 1
            _out("FLOOD", ok)
        elif cmd == "survey":
            asyncio.create_task(self._survey(int(args[0]), int(args[1])))
        elif cmd == "cut":
            asyncio.create_task(self._cut(int(args[0]), int(args[1])))
        elif cmd == "filter":
            if args and args[0] == "off":
                self.filter.allowed = _All()
            else:
                self.filter.allowed = set(int(a) for a in "".join(args).split(",") if a)
            _out("FILTER", args)
        elif cmd == "route":
            _out("ROUTE", json.dumps(self.route()))
        elif cmd == "probe":
            dest, count, gap_min, gap_max = (int(a) for a in args[:4])
            asyncio.create_task(self._probe(dest, count, gap_min, gap_max))
        elif cmd == "turn":
            _out("TURN", json.dumps(self.turn()))
        elif cmd == "zero":
            self.runner.cpu.clear()
            for meter in self.adapter.cpu.values():
                meter[0] = meter[1] = meter[2] = 0
            if self.timed is not None:
                self.timed.reset()
            self.probes = 0
            _out("ZERO")
        elif cmd == "quit":
            self._done = True
            self.runner.stop()
        else:
            _out("ERROR unknown command", cmd)

    async def _console(self):
        reader = asyncio.StreamReader(sys.stdin)
        while not self._done:
            line = await reader.readline()
            try:
                self.command(line.decode().strip())
            except Exception as e:  # a bad line must not stop the bench
                _out("ERROR", repr(e))

    async def main(self):
        """Run the node and the console until ``quit``."""
        asyncio.create_task(self.adapter.run())
        asyncio.create_task(self._console())
        _out("READY", json.dumps(self.ready))
        await self.runner.run()

    def close(self):
        """Stop the radio (standby): the node no longer receives."""
        self.adapter.modem.standby()


def run(address, **options):
    """Start a bench node with address and the `Bench` options; block until quit."""
    asyncio.new_event_loop()  # drop the tasks of a previous run
    bench = Bench(address, **options)
    try:
        asyncio.run(bench.main())
    finally:
        bench.close()
        asyncio.new_event_loop()
    _out("BYE")
