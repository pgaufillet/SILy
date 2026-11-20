"""Field sniffer: the board's lines (sily.sniff) on fake hardware, and the
host side of ``make sniff`` (tools/sniff.py) without a board.

The board module runs on `test_sily_stack.FakeHw`, whose adapter here
reports scripted frames and CRC errors from its task, then raises
KeyboardInterrupt (mpremote's Ctrl-C). The host tool's subprocess and
signal handling need CPython: only its commands, lines and writer are
tested here.
"""

import asyncio
import io
import json
import os
import unittest
from binascii import unhexlify

from test_sily_stack import Adapter, FakeHw

from mesh import profile as profiles
from sily import config, guard, sniff, utc
from sily.radio import PREAMBLE, SYNC_WORD
from tools import sniff as host

DIR = "build/test_sily_sniff"  # relative to the repository root, where the tests run
PATH = DIR + "/config.json"
UTC0 = 1795000000000  # 2026-11-18, the RTC set by make configure
FRAME_A = bytes(range(0x40, 0x40 + 24))
FRAME_B = b"\x41\xff\xff\x07\x00\x01\x02\x03\x2a" + bytes(10)


def _config(**fields):
    box = {"role": "node", "address": 9, "name": "spare", "tx_power": 14}
    box.update(fields)
    network = {"password": "secret words", "network": "club", "iterations": 10, "sf": 8}
    cfg, errors = config.board_config(network, box)
    assert not errors, errors
    return cfg


def _write(text):
    try:
        os.mkdir("build")
    except OSError:
        pass
    try:
        os.mkdir(DIR)
    except OSError:
        pass
    with open(PATH, "w") as f:
        f.write(text)


class Modem:
    def __init__(self):
        self.sleeps = 0

    def sleep(self):
        self.sleeps += 1


class Scripted(Adapter):
    """Reports script's events from its task, (delay ms, rssi, snr, frame
    or None for a CRC error), then raises KeyboardInterrupt."""

    def __init__(self, lora_cfg, script):
        super().__init__(lora_cfg)
        self.modem = Modem()
        self.script = script

    def stats(self):
        return {
            "rx": 2,
            "crc": 1,
            "hdr_err": 3,
            "preambles": 7,
            "headers": 4,
            "errors": 0,
            "rx_restarts": 0,
            "noise": -112.5,
            "tx": 0,
        }

    async def run(self):
        for delay, rssi, snr, frame in self.script:
            await asyncio.sleep_ms(delay)
            if frame is None:
                self.on_crc_error(rssi, snr)
            else:
                self.on_receive(bytearray(frame), rssi, snr)
        await asyncio.sleep_ms(5)
        raise KeyboardInterrupt


class Hw(FakeHw):
    def __init__(self, script=()):
        super().__init__()
        self.script = script
        self.rtc.datetime(utc.to_datetime(UTC0))

    def adapter(self, lora_cfg):
        a = Scripted(lora_cfg, self.script)
        self.adapters.append(a)
        return a


SCRIPT = ((30, -71, 7.25, FRAME_A), (20, -118, -9.5, None), (25, -96, -3.0, FRAME_B))


class Board(unittest.TestCase):
    """A run on a configured board: settings line, one line per frame or
    CRC error, radio asleep at Ctrl-C, nothing transmitted; then `stop`."""

    def setUp(self):
        self.cfg = _config()
        _write(json.dumps(self.cfg))
        self.hw = Hw(SCRIPT)
        self.lines = []
        self.s = sniff.run(self.hw, PATH, self.lines.append)
        self.adapter = self.hw.adapters[0]

    def tearDown(self):
        sniff.current = None

    def test_settings_line_first(self):
        head, _, text = self.lines[0].partition(" ")
        self.assertEqual(head, sniff.SETTINGS)
        s = json.loads(text)
        eu = profiles.get(profiles.EU_869)
        self.assertEqual(
            (s["fmt"], s["name"], s["address"], s["role"], s["fingerprint"]),
            (sniff.FORMAT, "spare", 9, "node", self.cfg["fingerprint"]),
        )
        self.assertEqual(
            (
                s["profile"],
                s["hz"],
                s["sf"],
                s["bw"],
                s["cr"],
                s["preamble"],
                s["sync"],
                s["power"],
            ),
            (
                eu.ident,
                eu.frequency_hz,
                8,
                eu.bandwidth_hz,
                eu.coding_rate,
                PREAMBLE,
                SYNC_WORD,
                14,
            ),
        )
        self.assertTrue(0 <= s["utc"] - UTC0 < 1000)
        self.assertEqual(s["tp"], sniff.ticks_period())
        self.assertFalse(self.cfg["key"] in self.lines[0])  # the key is never printed
        # The radio as the application sets it: profile, SF, power.
        cfg = self.adapter.lora_cfg
        self.assertEqual((cfg["sf"], cfg["output_power"], cfg["syncword"]), (8, 14, SYNC_WORD))
        self.assertEqual(self.adapter.started, 1)

    def test_one_line_per_frame_and_crc_error(self):
        s = json.loads(self.lines[0].partition(" ")[2])
        kinds = [line.split(" ")[0] for line in self.lines[1:]]
        self.assertEqual(kinds, [sniff.FRAME, sniff.CRC, sniff.FRAME])
        a = self.lines[1].split(" ")
        self.assertEqual(a[3:], ["-71", "7.25", FRAME_A.hex()])
        self.assertEqual(unhexlify(a[5]), FRAME_A)
        c = self.lines[2].split(" ")
        self.assertEqual((len(c), c[3], c[4]), (5, "-118", "-9.5"))
        b = self.lines[3].split(" ")
        self.assertEqual((b[3], b[4], b[5]), ("-96", "-3.0", FRAME_B.hex()))
        # Ticks and RTC at each report: in order, about the scripted delays.
        ticks = [s["t"]] + [int(line.split(" ")[1]) for line in self.lines[1:]]
        utcs = [s["utc"]] + [int(line.split(" ")[2]) for line in self.lines[1:]]
        for i in range(1, 4):
            dt = (ticks[i] - ticks[i - 1]) % s["tp"]
            self.assertTrue(SCRIPT[i - 1][0] <= dt < SCRIPT[i - 1][0] + 30, (i, dt))
            self.assertTrue(abs((utcs[i] - utcs[i - 1]) - dt) <= 2)
        self.assertEqual((self.s.frames, self.s.crc), (2, 1))

    def test_ctrl_c_leaves_the_radio_asleep_never_transmitting(self):
        self.assertEqual(self.adapter.modem.sleeps, 1)
        self.assertEqual(self.adapter.sent, [])
        self.assertIsNone(self.adapter.on_transmitted)
        self.assertEqual(self.hw.resets, 0)

    def test_stop_prints_the_end_line_guard_untouched(self):
        out = []
        sniff.stop(out.append)
        self.assertEqual(self.adapter.modem.sleeps, 2)
        head, _, text = out[0].partition(" ")
        e = json.loads(text)
        self.assertEqual(head, sniff.END)
        self.assertEqual((e["frames"], e["crc"], e["hdr_err"], e["preambles"]), (2, 1, 3, 7))
        self.assertEqual((e["crashes"], e["skip"], e["noise"]), (0, 0, -112.5))
        # The next boot starts the application.
        g = guard.Guard(self.hw.storage(guard.NAMESPACE))
        self.assertEqual(g.decide(False), guard.START)

    def test_stop_tells_a_skip_flag_left(self):
        g = guard.Guard(self.hw.storage(guard.NAMESPACE))
        g.skip_next()
        g.crashed()
        out = []
        sniff.stop(out.append)
        e = json.loads(out[0].partition(" ")[2])
        self.assertEqual((e["crashes"], e["skip"]), (1, 1))


class BadConfig(unittest.TestCase):
    def tearDown(self):
        sniff.current = None

    def test_errors_listed_radio_untouched(self):
        cfg = _config()
        cfg["sf"] = 13
        del cfg["role"]
        _write(json.dumps(cfg))
        hw = Hw()
        lines = []
        self.assertIsNone(sniff.run(hw, PATH, lines.append))
        self.assertEqual(hw.adapters, [])
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith(sniff.ERROR + " ") for line in lines))
        self.assertTrue(any("role" in line for line in lines))

    def test_missing_file(self):
        lines = []
        self.assertIsNone(sniff.run(Hw(), DIR + "/none.json", lines.append))
        self.assertEqual(
            lines, ["%s file: %s/none.json missing (make configure)" % (sniff.ERROR, DIR)]
        )

    def test_stop_without_run(self):
        out = []
        sniff.stop(out.append)
        self.assertEqual(out, [sniff.ERROR + " no run in this interpreter"])


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        self.t += 7
        return self.t


class Stream:
    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class Host(unittest.TestCase):
    """tools/sniff.py: the mpremote commands, the stamped lines, the echo."""

    def test_commands(self):
        run = host.run_command("mp", "/dev/ttyACM1")
        self.assertEqual(run[:4], ["mp", "connect", "/dev/ttyACM1", "exec"])  # soft reset: no app
        self.assertTrue("sniff.run()" in run[4])
        stop = host.stop_command("mp", "/dev/ttyACM1")
        self.assertEqual(stop[:5], ["mp", "connect", "/dev/ttyACM1", "resume", "exec"])
        self.assertTrue("sniff.stop()" in stop[5])

    def test_every_line_stamped_and_flushed(self):
        out = io.StringIO()
        shown = []
        w = host.Writer(out, Clock(1795000000000), shown.append)
        frame = sniff.frame_line(5000, UTC0, -80, 6.5, FRAME_B)
        w.pump(Stream([b"SNIFF {}\r\n", (frame + "\r\n").encode(), b"\r\n", b"END {}\r\n"]))
        lines = out.getvalue().split("\n")
        self.assertEqual(
            lines,
            ["1795000000007 SNIFF {}", "1795000000014 " + frame, "1795000000021 END {}", ""],
        )
        self.assertEqual((w.frames, w.settings, w.end), (1, True, True))
        self.assertEqual(shown[1], "frame 1: %d B from 0x0007, -80 dBm, SNR 6.5 dB" % len(FRAME_B))

    def test_echo_of_other_lines(self):
        self.assertEqual(host.echo("C 1 2 -120 -11.0", 0), "CRC error: 1 2 -120 -11.0")
        self.assertEqual(
            host.echo("Traceback (most recent call last):", 3),
            "Traceback (most recent call last):",
        )
        self.assertEqual(host.echo("F 1 2", 4), "frame 4")


if __name__ == "__main__":
    unittest.main()
