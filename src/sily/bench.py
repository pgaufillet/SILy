"""Bench console: one link engine on the board, driven by text lines.

Debug tool, started by hand from the REPL (never at boot)::

    from sily import bench
    bench.run(1)                        # address 1, SF7, bench password
    bench.run(2, password="other", allow=(1,), power=-9)

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
- ``zero``: reset the CPU meters;
- ``quit``.

Bench defaults: password ``bench`` with 1000 PBKDF2 iterations (the
production count is set by measurement, ``docs/measurements.md``), TX power
-9 dBm (boards a few centimetres apart), EU profile, static routes (every
destination is a neighbour).
"""

import asyncio
import gc
import json
import struct
import sys
from time import ticks_diff, ticks_ms, ticks_us

from board.platform import HardwareRandom, NvsStorage, RtcRetained
from board.sx1262 import Sx1262
from board.t3s3 import make_modem
from mesh import kdf
from mesh import profile as profiles
from mesh.link import destination, open_link
from mesh.radio import CHANNEL, RX, RX_ERROR, TX_DONE, NeighbourFilter
from mesh.runner import Runner
from sily.radio import MeshRadio, link_source, lora_config

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


class _All:
    """Allowed set of a disabled neighbour filter."""

    def __contains__(self, item):
        return True


def _out(*items):
    print(*items)


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
            next_hop=destination,  # static: every destination is a neighbour
        )
        self.runner = Runner(self.engine, self.filter)
        gc.collect()
        free2 = gc.mem_free()
        self.engine.bind(TEXT_PORT, self._text)
        self.engine.bind(ECHO_PORT, self._echo_request)
        self.engine.bind(REPLY_PORT, self._echo_reply)
        self.engine.bind(FLOOD_PORT, self._flood)
        self.flood_rx = 0
        self.flood_rx_bytes = 0
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
        elif cmd == "zero":
            self.runner.cpu.clear()
            for meter in self.adapter.cpu.values():
                meter[0] = meter[1] = meter[2] = 0
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
