#!/usr/bin/env python3
"""Two-board bench measurements of the mesh link layer (host side).

CPython 3 with pyserial (the t3s3-micropython venv has it). Drives two
T3S3 boards running the deployed SILy code over their serial ports: board-
side micro-measurements through the raw REPL, then the bench console
(``sily.bench``) on both boards for the radio tests. Prints the results and
writes them as JSON.

    tools/bench.py --a /dev/serial/by-id/...A --b /dev/serial/by-id/...B \\
        [--sf 7] [--out results.json] [--only NAME,...]

Tests (``--only``): platform (RTC memory, NVS write time), kdf (PBKDF2
iterations for 1-2 s), aead (seal/open time), link (sealed frames both ways,
echo round trips, CPU per frame, heap), sense (carrier sense while the other
board transmits, airtime accounted vs measured), errors (authentication
failures with a wrong key, CRC errors from cut frames), filter (neighbour
filter), routing (A root, B a node of its DODAG: B attaches at rank 8 and
its presence, topology and node information reach A; not in the default
set). The boards are left at the REPL, idle.
"""

import argparse
import json
import sys
import time

import serial


class Board:
    """One board on a serial port: raw REPL execution and bench console."""

    def __init__(self, name, port, echo=True):
        self.name = name
        self.port = port
        self.echo = echo
        self.conn = None
        self.lines = []  # (time, line) of the bench console
        self._partial = b""
        self.open()

    def open(self, retries=50):
        for _ in range(retries):
            try:
                self.conn = serial.Serial(self.port, 115200, timeout=0.05)
                return
            except (serial.SerialException, OSError):
                time.sleep(0.2)
        raise RuntimeError("%s: cannot open %s" % (self.name, self.port))

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _read(self, duration):
        end = time.time() + duration
        out = b""
        while time.time() < end:
            out += self.conn.read(4096)
        return out

    def _read_until(self, marker, timeout):
        end = time.time() + timeout
        out = b""
        while marker not in out:
            if time.time() > end:
                raise TimeoutError("%s: no %r in %r" % (self.name, marker, out[-300:]))
            out += self.conn.read(4096)
        return out

    def interrupt(self):
        """Stop any running program and return to the friendly REPL."""
        self.conn.write(b"\r\x03\x03")
        time.sleep(0.3)
        self.conn.write(b"\x02")
        self._read(0.3)
        self._partial = b""

    def exec(self, code, timeout=60):
        """Run code in the raw REPL; return its output, raise on an exception."""
        self.interrupt()
        self.conn.write(b"\x01")
        self._read_until(b"raw REPL; CTRL-B to exit\r\n>", 5)
        data = code.encode()
        for i in range(0, len(data), 256):
            self.conn.write(data[i : i + 256])
            time.sleep(0.01)
        self.conn.write(b"\x04")
        out = self._read_until(b"\x04>", timeout)
        self.conn.write(b"\x02")
        self._read(0.2)
        if not out.startswith(b"OK"):
            raise RuntimeError("%s: raw REPL refused: %r" % (self.name, out[:200]))
        result, error = out[2:].split(b"\x04")[:2]
        if error.strip():
            raise RuntimeError("%s: %s" % (self.name, error.decode(errors="replace")))
        return result.decode(errors="replace")

    def exec_json(self, code, timeout=60):
        """Run code that prints one JSON line last; return it decoded."""
        out = self.exec(code, timeout).strip().splitlines()
        return json.loads(out[-1])

    def soft_reset(self):
        """Soft reset from the raw REPL (Ctrl-D on an empty input)."""
        self.interrupt()
        self.conn.write(b"\x01")
        self._read_until(b">", 5)
        self.conn.write(b"\x04")
        self._read_until(b"raw REPL; CTRL-B to exit\r\n>", 10)
        self.conn.write(b"\x02")
        self._read(0.3)

    def hard_reset(self):
        """machine.reset(): the USB port goes away and comes back."""
        self.interrupt()
        self.conn.write(b"import machine; machine.reset()\r\n")
        time.sleep(0.5)
        self.close()
        time.sleep(2.0)
        self.open()
        time.sleep(1.0)
        self.interrupt()

    # --- Bench console -------------------------------------------------------------

    def pump(self, duration=0.0):
        """Read console lines for duration seconds (at least once)."""
        end = time.time() + duration
        while True:
            data = self.conn.read(4096)
            if data:
                data = self._partial + data
                parts = data.split(b"\n")
                self._partial = parts.pop()
                for raw in parts:
                    line = raw.decode(errors="replace").strip()
                    if line:
                        self.lines.append((time.time(), line))
                        if self.echo and not line.startswith("STATS"):
                            print("  [%s] %s" % (self.name, line))
            if time.time() >= end:
                return

    def wait_for(self, prefix, timeout=10, since=0):
        """Return the first console line from index since starting with prefix."""
        end = time.time() + timeout
        while True:
            for i in range(since, len(self.lines)):
                if self.lines[i][1].startswith(prefix):
                    return self.lines[i][1]
            if time.time() > end:
                raise TimeoutError("%s: no %s line" % (self.name, prefix))
            self.pump(0.05)

    def send(self, line):
        self.conn.write(line.encode() + b"\r\n")

    def command(self, line, prefix, timeout=10):
        """Send a console command and return its answer line (starting with prefix)."""
        since = len(self.lines)
        self.send(line)
        return self.wait_for(prefix, timeout, since)

    def start_bench(self, address, **options):
        """Start sily.bench on the board; return the READY information."""
        self.interrupt()
        args = ", ".join([str(address)] + ["%s=%r" % kv for kv in options.items()])
        since = len(self.lines)
        self.send("from sily import bench; bench.run(%s)" % args)
        line = self.wait_for("READY", 30, since)
        return json.loads(line.split(None, 1)[1])

    def stats(self):
        return json.loads(self.command("stats", "STATS").split(None, 1)[1])

    def stop_bench(self):
        since = len(self.lines)
        self.send("quit")
        try:
            self.wait_for("BYE", 5, since)
        except TimeoutError:
            self.interrupt()

    def lines_since(self, since, prefix):
        return [line for _, line in self.lines[since:] if line.startswith(prefix)]


# --- Board-side measurements (raw REPL) ----------------------------------------------

PLATFORM_CODE = """
import json, esp32, machine, time
r = machine.RTC()
size = 0
for n in (512, 1024, 2048, 2049, 3072, 4096, 8192):
    try:
        r.memory(bytes(n))
        size = n
    except ValueError:
        break
n = esp32.NVS('bench')
w = []
c = []
for i in range(20):
    t = time.ticks_us()
    n.set_blob('counters', i.to_bytes(4, 'little'))
    t1 = time.ticks_us()
    n.commit()
    t2 = time.ticks_us()
    w.append(time.ticks_diff(t1, t))
    c.append(time.ticks_diff(t2, t1))
n.erase_key('counters')
n.commit()
pattern = bytes((i * 7) & 0xFF for i in range(2048))
r.memory(pattern)
print(json.dumps({'rtc_max': size, 'cpu_mhz': machine.freq() // 1000000,
    'nvs_set_us': [sum(w) // len(w), max(w)],
    'nvs_commit_us': [sum(c) // len(c), max(c)],
    'nvs_set_commit_us': [sum(w[i] + c[i] for i in range(20)) // 20,
                          max(w[i] + c[i] for i in range(20))]}))
"""

IMPORT_CODE = """
import gc, json
gc.collect()
f0 = gc.mem_free()
from sily import bench
gc.collect()
print(json.dumps({'heap_imports': f0 - gc.mem_free(), 'heap_free': gc.mem_free()}))
"""

RTC_CHECK_CODE = """
import json, machine
m = machine.RTC().memory()
pattern = bytes((i * 7) & 0xFF for i in range(2048))
print(json.dumps({'len': len(m), 'kept': m == pattern, 'cause': machine.reset_cause()}))
"""

KDF_CODE = """
import gc, json, time
from mesh import kdf
def t(n):
    gc.collect()
    t0 = time.ticks_ms()
    kdf.master_key('password', 'network', n)
    return time.ticks_diff(time.ticks_ms(), t0)
res = {'fresh': [t(500), t(2000)]}
import sily.bench
res['app'] = [t(500), t(2000)]
print(json.dumps(res))
"""

KDF_CHECK_CODE = """
import gc, json, time
from mesh import kdf
import sily.bench
gc.collect()
t = time.ticks_ms()
kdf.derive('password', 'network', %d)
print(json.dumps({'ms': time.ticks_diff(time.ticks_ms(), t)}))
"""

AEAD_CODE = """
import json, time
from mesh import frame
from mesh.frame import Header, Message, P2P
from mesh.replay import ReplayTable
from mesh.security import KeyRing, Security
from mesh.counter import FrameCounter
from board.platform import HardwareRandom, NvsStorage
key = bytes(range(16))
tx = Security(KeyRing(key), FrameCounter(NvsStorage('bench'), HardwareRandom()), ReplayTable())
rx = Security(KeyRing(key), FrameCounter(NvsStorage('bench'), HardwareRandom()), ReplayTable())
res = {}
for size in (17, 64, 128, 255):
    payload = bytes(max(0, size - frame.OVERHEAD - 7))
    h = Header(2, 1, 0, 0)
    buf = bytearray(255)
    msgs = [Message(P2P, 0, 100, payload, 1, 2)]
    n = frame.encode(buf, h, msgs, 255)
    frames = []
    seal = []
    for i in range(31):
        b = bytearray(buf)
        t = time.ticks_us()
        tx.seal(b, n)
        seal.append(time.ticks_diff(time.ticks_us(), t))
        frames.append(b)
    opened = []
    dec = []
    h2 = Header(0, 0, 0, 0)
    for b in frames:
        t = time.ticks_us()
        r = rx.receive(b, n, h2)
        t1 = time.ticks_us()
        frame.decode_messages(b, frame.HEADER_SIZE, n - frame.TAG_SIZE)
        dec.append(time.ticks_diff(time.ticks_us(), t1))
        opened.append(time.ticks_diff(t1, t))
        assert r is None, r
    for v in (seal, opened, dec):
        v.sort()
    res[n] = {'seal_us': seal[15], 'open_us': opened[15], 'decode_us': dec[15],
              'seal_max_us': seal[-1], 'open_max_us': opened[-1]}
NvsStorage('bench').erase('counters')
NvsStorage('bench').commit()
print(json.dumps(res))
"""


def toa_ms(length, sf, bw=125000, cr=5, preamble=8):
    """LoRa time on air (ms), as mesh.airtime (explicit header, CRC on)."""
    ldr = (1 << sf) * 125 >= 2 * bw
    bits = max(0, 8 * length - 4 * sf + 8 + 16 + 20)
    per = 4 * (sf - 2 if ldr else sf)
    sym_x4 = 17 + 4 * (preamble + 8 + ((bits + per - 1) // per) * cr)
    return sym_x4 * (1 << sf) * 1000 / 4 / bw


# --- Tests ---------------------------------------------------------------------------


def test_platform(a, res):
    print("== platform: RTC memory, NVS write time")
    p = a.exec_json(PLATFORM_CODE)
    a.soft_reset()
    p["rtc_after_soft_reset"] = a.exec_json(RTC_CHECK_CODE)
    a.hard_reset()
    p["rtc_after_machine_reset"] = a.exec_json(RTC_CHECK_CODE)
    a.exec("import machine; machine.RTC().memory(b'')")
    a.soft_reset()
    p.update(a.exec_json(IMPORT_CODE))
    res["platform"] = p
    print(json.dumps(p, indent=1))


def test_kdf(a, res):
    """PBKDF2 time per iteration, on a fresh heap and with the application loaded.

    Small objects land in internal RAM on a fresh heap and in PSRAM once the
    application's modules fill it: the second state is the one that counts.
    """
    print("== kdf: PBKDF2-HMAC-SHA256 iterations")
    a.soft_reset()
    t = a.exec_json(KDF_CODE, 120)
    k = {}
    for state in ("fresh", "app"):
        ms500, ms2000 = t[state]
        per = (ms2000 - ms500) / 1500
        k[state] = {
            "ms_500": ms500,
            "ms_2000": ms2000,
            "ms_per_iteration": round(per, 4),
            "iterations_1s": int(1000 / per),
            "iterations_2s": int(2000 / per),
        }
    chosen = int(1500 / k["app"]["ms_per_iteration"]) // 1000 * 1000
    a.soft_reset()
    k["chosen"] = chosen
    k["chosen_derive_ms_app"] = a.exec_json(KDF_CHECK_CODE % chosen, 120)["ms"]
    a.soft_reset()
    k["default_derive_ms_app"] = a.exec_json(KDF_CHECK_CODE % 10000, 120)["ms"]
    res["kdf"] = k
    print(json.dumps(k, indent=1))


def test_aead(a, res):
    print("== aead: seal / open per frame")
    r = a.exec_json(AEAD_CODE, 120)
    res["aead"] = r
    print(json.dumps(r, indent=1))


def _mean_ms(meter):
    return round(meter[1] / meter[0] / 1000, 2) if meter and meter[0] else 0


def cpu_summary(stats):
    """CPU per frame (ms, means) from a STATS line.

    tx_frame: the step that seals and starts the transmission (CHANNEL idle,
    adapter transmit included), the TX done step and the adapter's return
    to reception; rx_frame: the adapter's IRQ servicing (preamble, header,
    RxDone with the buffer read) and the RX step (open, decode, handler).
    """
    r = stats["radio"]["cpu"]
    s = stats["cpu_step"]
    out = {
        "adapter_tx": _mean_ms(r["tx"]),
        "adapter_tx_done": _mean_ms(r["tx_done"]),
        "adapter_rx": _mean_ms(r["rx"]),
        "adapter_irq": _mean_ms(r["irq"]),
        "adapter_sense": _mean_ms(r["sense"]),
        "step_channel": _mean_ms(s.get("channel")),
        "step_tx_done": _mean_ms(s.get("tx_done")),
        "step_rx": _mean_ms(s.get("rx")),
        "step_timer": _mean_ms(s.get("timer")),
    }
    out["tx_frame"] = round(out["step_channel"] + out["step_tx_done"] + out["adapter_tx_done"], 2)
    out["rx_frame"] = round(out["adapter_rx"] + 2 * out["adapter_irq"] + out["step_rx"], 2)
    out["driver_tx"] = round(out["adapter_tx"] + out["adapter_tx_done"], 2)
    out["driver_rx"] = round(out["adapter_rx"] + 2 * out["adapter_irq"], 2)
    return out


def _start(a, b, sf, a_opts=None, b_opts=None):
    ra = a.start_bench(1, sf=sf, **(a_opts or {}))
    rb = b.start_bench(2, sf=sf, **(b_opts or {}))
    a.pump(0.5)
    b.pump(0.5)
    return ra, rb


def _stop(a, b):
    a.stop_bench()
    b.stop_bench()


def test_link(a, b, res, sf):
    print("== link: sealed frames both ways, echo, CPU per frame, heap")
    ra, rb = _start(a, b, sf)
    out = {"ready_a": ra, "ready_b": rb}
    sa, sb = len(a.lines), len(b.lines)
    a.command("send 2 hello from 1", "SEND")
    b.wait_for("RX 1 100", 10, sb)
    b.command("send 1 hello from 2", "SEND")
    a.wait_for("RX 2 100", 10, sa)
    out["text_a_to_b"] = b.lines_since(sb, "RX")
    out["text_b_to_a"] = a.lines_since(sa, "RX")
    a.command("zero", "ZERO")
    b.command("zero", "ZERO")
    count = 20
    sa = len(a.lines)
    a.send("echo 2 %d 16 700" % count)
    a.wait_for("DONE echo", count + 10, sa)
    a.pump(2.0)
    b.pump(0.2)
    echoes = a.lines_since(sa, "ECHO")
    rtts = sorted(int(line.split()[2]) for line in echoes)
    out["echo"] = {
        "sent": count,
        "replies": len(echoes),
        "rtt_ms_min": rtts[0] if rtts else None,
        "rtt_ms_median": rtts[len(rtts) // 2] if rtts else None,
        "rtt_ms_max": rtts[-1] if rtts else None,
        "toa_ms_frame": round(toa_ms(16 + 13 + 7, sf), 1),
    }
    out["stats_a"] = a.stats()
    out["stats_b"] = b.stats()
    out["cpu_a"] = cpu_summary(out["stats_a"])
    out["cpu_b"] = cpu_summary(out["stats_b"])
    _stop(a, b)
    res["link"] = out
    for key in ("echo", "cpu_a", "cpu_b"):
        print(key, json.dumps(out[key], indent=1))
    print("heap", {k: ra[k] for k in ("heap_engine", "heap_node", "heap_free")})


def test_sense(a, b, res, sf):
    print("== sense: carrier sense while the other board transmits; airtime")
    _start(a, b, sf)
    out = {}
    # Idle channel first.
    sa = len(a.lines)
    a.send("survey 50 20")
    out["idle"] = json.loads(a.wait_for("SURVEY", 20, sa).split(None, 1)[1])
    a_start = a.stats()
    b0 = b.stats()
    count, size = 20, 200
    b.command("flood 1 %d %d" % (count, size), "FLOOD")
    time.sleep(0.3)
    sa = len(a.lines)
    a.send("survey 100 30")
    out["busy"] = json.loads(a.wait_for("SURVEY", 30, sa).split(None, 1)[1])
    # A transmits while B floods: its link engine senses busy and backs off.
    a0 = a.stats()
    b.command("flood 1 %d %d" % (count, size), "FLOOD")
    time.sleep(0.5)
    a.command("flood 2 5 50", "FLOOD")
    deadline = time.time() + 40
    while time.time() < deadline:
        time.sleep(1.0)
        sb = b.stats()
        if sb["queue"] == 0 and sb["link"]["tx_frames"] - b0["link"]["tx_frames"] >= 2 * count:
            break
    a.pump(2.0)
    a1 = a.stats()
    b1 = b.stats()
    la0, la1 = a0["link"], a1["link"]
    out["a_access_while_b_floods"] = {
        k: la1[k] - la0[k] for k in ("senses", "busy", "backoffs", "give_ups", "tx_frames")
    }
    rb0, rb1 = b0["radio"], b1["radio"]
    out["airtime_b"] = {
        "frames": b1["link"]["tx_frames"] - b0["link"]["tx_frames"],
        "accounted_ms": b1["duty_used_ms"] - b0["duty_used_ms"],
        "measured_ms": round((rb1["tx_us_total"] - rb0["tx_us_total"]) / 1000, 1),
        "computed_ms": round((rb1["tx_toa_us_total"] - rb0["tx_toa_us_total"]) / 1000, 1),
        "last_frame_len": rb1["last_tx_len"],
        "last_frame_measured_us": rb1["last_tx_us"],
        "last_frame_toa_ms": round(toa_ms(rb1["last_tx_len"], sf), 2),
    }
    lb0, lb1 = b0["link"], b1["link"]
    out["b_access_during_floods"] = {
        k: lb1[k] - lb0[k] for k in ("senses", "busy", "backoffs", "give_ups", "tx_frames")
    }
    out["b_radio_busy"] = {
        k: b1["radio"][k] - b0["radio"][k] for k in ("busy_rx", "busy_rssi", "busy_tx")
    }
    out["flood_rx_a"] = {
        "sent_by_b": 2 * count,
        "received": a1["flood_rx"] - a_start["flood_rx"],
        "a_tx_frames": a1["link"]["tx_frames"] - a_start["link"]["tx_frames"],
    }
    out["radio_a"] = a1["radio"]
    _stop(a, b)
    res["sense"] = out
    print(json.dumps({k: v for k, v in out.items() if k != "radio_a"}, indent=1))


def test_errors(a, b, res, sf):
    print("== errors: wrong key -> authentication failures; cut frames -> CRC errors")
    _start(a, b, sf, b_opts={"password": "wrong"})
    a0 = a.stats()
    for i in range(5):
        b.command("send 1 wrong key %d" % i, "SEND")
        time.sleep(0.4)
    time.sleep(1.0)
    a1 = a.stats()
    for _ in range(5):
        sb = len(b.lines)
        b.send("cut 100 40")
        b.wait_for("DONE cut", 5, sb)
        time.sleep(0.6)
    time.sleep(0.5)
    a2 = a.stats()
    out = {
        "auth": {
            "sent": 5,
            "rx_auth": a1["link"]["rx_auth"] - a0["link"]["rx_auth"],
            "rx_frames": a1["link"]["rx_frames"] - a0["link"]["rx_frames"],
            "rx_crc": a1["link"]["rx_crc"] - a0["link"]["rx_crc"],
            "senders": a1["senders"],
        },
        "crc": {
            "cut": 5,
            "rx_crc": a2["link"]["rx_crc"] - a1["link"]["rx_crc"],
            "rx_auth": a2["link"]["rx_auth"] - a1["link"]["rx_auth"],
            "radio_crc": a2["radio"]["crc"] - a1["radio"]["crc"],
            "radio_hdr_err": a2["radio"]["hdr_err"] - a1["radio"]["hdr_err"],
        },
    }
    _stop(a, b)
    res["errors"] = out
    print(json.dumps(out, indent=1))


def test_filter(a, b, res, sf):
    print("== filter: neighbour filter on the receiver")
    _start(a, b, sf, a_opts={"allow": (3,)})
    a0 = a.stats()
    for i in range(3):
        b.command("send 1 filtered %d" % i, "SEND")
        time.sleep(0.4)
    time.sleep(0.5)
    a1 = a.stats()
    a.command("filter 2", "FILTER")
    sa = len(a.lines)
    b.command("send 1 allowed", "SEND")
    a.wait_for("RX 2", 5, sa)
    a2 = a.stats()
    out = {
        "filtered": a1["filtered"] - a0["filtered"],
        "rx_frames_while_filtered": a1["link"]["rx_frames"] - a0["link"]["rx_frames"],
        "rx_frames_after": a2["link"]["rx_frames"] - a1["link"]["rx_frames"],
    }
    _stop(a, b)
    res["filter"] = out
    print(json.dumps(out, indent=1))


def test_routing(a, b, res, sf, presence_s=10):
    print("== routing: A root (address 0), B a node (address 2) of its DODAG")
    ra = a.start_bench(0, sf=sf, routing=True, presence_s=presence_s)
    sa = len(a.lines)
    t0 = time.time()
    rb = b.start_bench(2, sf=sf, routing=True)
    out = {"ready_a": ra, "ready_b": rb, "presence_s": presence_s}
    attached = None
    for _ in range(4 * presence_s):
        route = json.loads(b.command("route", "ROUTE").split(None, 1)[1])
        if route["attached"]:
            attached = route
            break
        a.pump(1.0)
    out["attach_s"] = round(time.time() - t0, 1) if attached else None
    out["route_b"] = attached
    line = a.wait_for("PRESENCE 2 ", 3 * presence_s, sa)
    out["presence_s_after_start"] = round(time.time() - t0, 1)
    out["presence"] = line
    a.pump(presence_s + 2)  # a periodic presence, topology and node information
    out["presences"] = a.lines_since(sa, "PRESENCE")
    out["topology"] = a.lines_since(sa, "TOPOLOGY")
    out["node_info"] = a.lines_since(sa, "NODEINFO")
    out["route_a"] = json.loads(a.command("route", "ROUTE").split(None, 1)[1])
    out["route_b_end"] = json.loads(b.command("route", "ROUTE").split(None, 1)[1])
    _stop(a, b)
    res["routing"] = out
    print(json.dumps({k: out[k] for k in ("attach_s", "presence", "presences")}, indent=1))


TESTS = ("platform", "kdf", "aead", "link", "sense", "errors", "filter")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--a", required=True, help="serial port of board A (address 1)")
    parser.add_argument("--b", required=True, help="serial port of board B (address 2)")
    parser.add_argument("--sf", type=int, default=7)
    parser.add_argument("--out", default="bench-results.json")
    parser.add_argument("--only", default=",".join(TESTS), help="also: routing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    only = args.only.split(",")
    a = Board("A", args.a, not args.quiet)
    b = Board("B", args.b, not args.quiet)
    res = {"date": time.strftime("%Y-%m-%d %H:%M"), "sf": args.sf}
    try:
        if "platform" in only:
            test_platform(a, res)
        if "kdf" in only:
            test_kdf(a, res)
        if "aead" in only:
            test_aead(a, res)
        if "link" in only:
            test_link(a, b, res, args.sf)
        if "sense" in only:
            test_sense(a, b, res, args.sf)
        if "errors" in only:
            test_errors(a, b, res, args.sf)
        if "filter" in only:
            test_filter(a, b, res, args.sf)
        if "routing" in only:
            test_routing(a, b, res, args.sf)
    finally:
        for board in (a, b):
            board.interrupt()
            board.close()
        with open(args.out, "w") as f:
            json.dump(res, f, indent=1)
        print("results written to", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
