"""The application (sily.app) on fake hardware: boot decision, run, stop, crash.

The configuration is a real file under build/; the hardware is the
`FakeHw` of ``test_sily_stack`` (scripted radio adapter, simulator platform
fakes, LED and board controls recorded). Runs last a few hundred ms of
real time on the unix port's asyncio. The web server listens for real, on
a free high port, and is fetched over loopback (as in ``test_sily_web``).
"""

import asyncio
import io
import json
import os
import socket
import struct
import unittest
from time import ticks_diff, ticks_us

from test_sily_stack import FakeHw, peer_frame, probe_frame
from test_sily_web import _get, _request, _split

from mesh.frame import DEST_OFFSET, ROOT
from mesh.ports import NODE_INFO, PRESENCE, TOPOLOGY
from mesh.reports import FIRST_REPORT, NodeInfo, Presence
from sily import app, guard, journal, record, traffic
from sily.config import check

DIR = "build/test_sily_app"  # relative to the repository root, where the tests run
PATH = DIR + "/config.json"
JOURNAL = DIR + "/journal"
KEY = "00112233445566778899aabbccddeeff"
FINGERPRINT = "a1b2c3d4"
AP = {"prefix": "SILy", "password": "ap-words-7731"}
FIRST_PORT = 18740  # the web server's (test_sily_web counts from 18640)
_ports = [FIRST_PORT]


def _mkdirs(path):
    head = ""
    for part in path.split("/"):
        head = head + "/" + part if head else part
        try:
            os.mkdir(head)
        except OSError:
            pass  # exists


def _write(**fields):
    cfg = {
        "version": 1,
        "role": "gateway",
        "address": 0,
        "key": KEY,
        "fingerprint": FINGERPRINT,
        "sf": 7,
    }
    cfg.update(fields)
    _mkdirs(DIR)
    with open(PATH, "w") as f:
        f.write(json.dumps(cfg))
    return cfg


def _remove():
    try:
        os.remove(PATH)
    except OSError:
        pass
    try:
        names = os.listdir(JOURNAL)
    except OSError:
        return
    for name in names:
        os.remove(JOURNAL + "/" + name)


def _bind(port):
    """Return a socket bound to port on every address (OSError: taken)."""
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(socket.getaddrinfo("0.0.0.0", port)[0][-1])
    except OSError:
        s.close()
        raise
    return s


def _free_port():
    """Return the next port nothing listens on."""
    for _ in range(50):
        port = _ports[0]
        _ports[0] += 1
        try:
            _bind(port).close()
            return port
        except OSError:
            continue
    raise OSError("no free port")


def _guard(hw):
    return guard.Guard(hw.storage(guard.NAMESPACE))


def _app(hw, **fields):
    cfg, errors = check(_write(**fields))
    assert not errors, errors
    return app.App(cfg, hw, _guard(hw), monitor_ms=20, journal_dir=JOURNAL)


def _after(ms, action):
    """A task factory: action(app) after ms."""

    async def task(a):
        await asyncio.sleep_ms(ms)
        action(a)

    return task


def _raise(error):
    def action(a):
        raise error

    return action


class Console(unittest.TestCase):
    """Console lines captured; the configuration file and journal removed after each test."""

    def setUp(self):
        _remove()
        self.console = app.console = io.StringIO()

    def tearDown(self):
        app.console = None
        app.current = None
        _remove()

    def lines(self):
        return self.console.getvalue().split("\n")


class Boot(Console):
    def test_missing_config_gives_safe_mode(self):
        _remove()
        hw = FakeHw()
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), guard.SAFE)
        self.assertEqual(hw.blinks, [app.SAFE_BLINK_MS])
        self.assertEqual(hw.adapters, [])
        self.assertTrue("SILY SAFE file: %s missing (make configure)" % PATH in self.lines())

    def test_one_line_per_problem(self):
        _write(sf=13, tx_pwr=1)
        hw = FakeHw()
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), guard.SAFE)
        lines = self.lines()
        self.assertTrue("SILY SAFE sf: out of range 7 to 12" in lines, lines)
        self.assertTrue("SILY SAFE tx_pwr: unknown key" in lines, lines)
        self.assertEqual(hw.adapters, [])

    def test_crash_count_gives_safe_mode(self):
        _write()
        hw = FakeHw()
        for _ in range(guard.MAX_CRASHES):
            _guard(hw).crashed()
        decision, cfg, reasons = app.prepare(hw, _guard(hw), PATH)
        self.assertEqual((decision, cfg), (guard.SAFE, None))
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("crashes: 3 without 10 min"), reasons)

    def test_power_on_retries(self):
        _write()
        hw = FakeHw(power_on=True)
        for _ in range(guard.MAX_CRASHES):
            _guard(hw).crashed()
        decision, cfg, reasons = app.prepare(hw, _guard(hw), PATH)
        self.assertEqual((decision, reasons), (guard.START, []))
        self.assertEqual(cfg["address"], 0)

    def test_skip_flag_skips_one_boot(self):
        _write()
        hw = FakeHw()
        _guard(hw).skip_next()
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), guard.SKIP)
        self.assertEqual((hw.adapters, hw.blinks), ([], []))
        self.assertEqual(app.prepare(hw, _guard(hw), PATH)[0], guard.START)


class Run(Console):
    def test_gateway_runs_then_ctrl_c(self):
        _write()
        hw = FakeHw(fail=(250, KeyboardInterrupt()))
        self.assertEqual(app.boot(hw, PATH, monitor_ms=20, journal_dir=JOURNAL), app.STOPPED)
        a = app.current
        self.assertTrue(a.stack.runner.steps > 0)
        self.assertEqual(hw.led_pin.values[0], 0)
        self.assertTrue(1 in hw.led_pin.values)  # the root is attached
        self.assertEqual(hw.led_pin.values[-1], 0)
        self.assertEqual(hw.adapters[0].modem.standbys, 1)
        self.assertEqual(hw.kbd, [-1, 3])
        self.assertEqual((hw.resets, hw.sleeps), (0, []))
        self.assertEqual(_guard(hw).crashes, 0)
        self.assertTrue("SILY STOP" in self.lines())
        self.assertIsNone(asyncio.get_event_loop().get_exception_handler())

    def test_detached_node_keeps_the_led_off(self):
        hw = FakeHw()
        a = _app(hw, role="node", address=3)
        a.add_task(_after(150, lambda a: a.stop()))
        self.assertEqual(app.start(a), app.STOPPED)
        self.assertFalse(a.attached)
        self.assertFalse(1 in hw.led_pin.values)
        self.assertEqual(hw.adapters[0].modem.standbys, 1)

    def test_led_disabled(self):
        hw = FakeHw()
        a = _app(hw, led=False)
        a.add_task(_after(100, lambda a: a.stop()))
        app.start(a)
        self.assertTrue(a.attached)
        self.assertEqual(hw.led_pin.values, [])

    def test_stable_run_clears_the_crash_count(self):
        for stable_ms, expected in ((50, 0), (10000, 2)):
            hw = FakeHw()
            g = _guard(hw)
            g.crashed()
            g.crashed()
            a = _app(hw)
            a.stable_ms = stable_ms
            a.add_task(_after(200, lambda a: a.stop()))
            self.assertEqual(app.start(a), app.STOPPED)
            self.assertEqual(g.crashes, expected, stable_ms)

    def test_close_is_idempotent(self):
        hw = FakeHw()
        a = _app(hw)
        a.add_task(_after(50, lambda a: a.stop()))
        app.start(a)
        a.close()
        app.close()
        self.assertEqual(hw.adapters[0].modem.standbys, 1)
        self.assertEqual(hw.kbd, [-1, 3])


class Crash(Console):
    def test_task_exception_resets_after_the_pause(self):
        hw = FakeHw()
        a = _app(hw)
        a.add_task(_after(100, _raise(ValueError("task"))))
        self.assertEqual(app.start(a), app.RESET)
        self.assertEqual(hw.sleeps, [app.CRASH_PAUSE_MS])
        self.assertEqual(hw.resets, 1)
        self.assertEqual(_guard(hw).crashes, 1)
        self.assertEqual(hw.adapters[0].modem.standbys, 1)
        self.assertEqual(hw.led_pin.values[-1], 0)
        self.assertTrue("SILY FATAL ValueError('task',)" in self.lines(), self.lines())
        self.assertTrue("ValueError: task" in self.lines())  # the traceback
        self.assertIsNone(asyncio.get_event_loop().get_exception_handler())

    def test_radio_task_exception(self):
        _write()
        hw = FakeHw(fail=(100, OSError(5)))
        self.assertEqual(app.boot(hw, PATH, monitor_ms=20, journal_dir=JOURNAL), app.RESET)
        self.assertEqual((hw.resets, _guard(hw).crashes), (1, 1))

    def test_silent_radio_at_start(self):
        _write()
        hw = FakeHw(adapter_error=RuntimeError("no SX1262"))
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), app.RESET)
        self.assertEqual((hw.resets, _guard(hw).crashes), (1, 1))

    def test_safe_mode_after_three_crashes(self):
        _write()
        hw = FakeHw(adapter_error=RuntimeError("no SX1262"))
        for _ in range(guard.MAX_CRASHES):
            self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), app.RESET)
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), guard.SAFE)
        self.assertEqual(hw.resets, guard.MAX_CRASHES)
        self.assertEqual(hw.blinks, [app.SAFE_BLINK_MS])
        hw.on = True  # switched off and on: a new try
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), app.RESET)

    def test_ctrl_c_during_the_pause_stays_at_the_repl(self):
        _write()
        hw = FakeHw(adapter_error=RuntimeError("no SX1262"))
        hw.interrupt_sleep = True
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), app.STOPPED)
        self.assertEqual((hw.resets, _guard(hw).crashes), (0, 1))


def _kinds(records):
    return [r["k"] if r["k"] != "ev" else "ev:" + r["e"] for r in records]


class Journal(Console):
    def _run(self, hw, ms, **fields):
        fields.setdefault("journal", {"minute_s": 1})
        a = _app(hw, **fields)
        a.add_task(_after(ms, lambda a: a.stop()))
        result = app.start(a)
        records, bad = journal.read(JOURNAL)
        self.assertEqual(bad, 0)
        return a, result, records

    def test_boot_minute_stop(self):
        hw = FakeHw()
        hw.battery = 3870
        a, result, records = self._run(hw, 1500)
        self.assertEqual(result, app.STOPPED)
        kinds = _kinds(records)
        self.assertEqual(kinds[0], "boot")
        self.assertEqual(kinds[-1], "stop")
        self.assertEqual(kinds.count("min"), 1, kinds)
        self.assertTrue("ev:attach" in kinds, kinds)  # the gateway, attached at once
        boot = records[0]
        self.assertEqual((boot["role"], boot["address"], boot["sf"]), ("gateway", 0, 7))
        self.assertEqual((boot["reset"], boot["crashes"], boot["b"]), ("hard", 0, 1))
        self.assertEqual(boot["random_restart"], 1)  # a fresh frame counter store
        self.assertTrue(0 <= boot["counter"] < a.stack.engine.security.counter.peek())
        minute = records[kinds.index("min")]
        self.assertEqual((minute["att"], minute["rank"], minute["bat"]), (1, 0, 3870))
        self.assertTrue(minute["t"] - boot["t"] >= 1000)
        for key in ("jw", "js", "jq", "je", "link", "rt", "radio", "nb", "heap"):
            self.assertTrue(key in minute, key)
        self.assertEqual(a.journal.syncs, 2)  # after the boot record and at the stop

    def test_boot_numbers_and_segments_continue(self):
        hw = FakeHw()
        for _ in range(2):
            self._run(hw, 50)
        _, _, records = self._run(hw, 50)
        boots = [r["b"] for r in records if r["k"] == "boot"]
        self.assertEqual(boots, [1, 2, 3])
        self.assertEqual(
            [n for n, _ in journal.segments(JOURNAL)],
            [1, 2, 3],
        )

    def test_crash_writes_fatal_with_the_traceback(self):
        hw = FakeHw()
        a = _app(hw)
        a.add_task(_after(100, _raise(ValueError("task"))))
        self.assertEqual(app.start(a), app.RESET)
        records, _ = journal.read(JOURNAL)
        last = records[-1]
        self.assertEqual(last["k"], "fatal")
        self.assertEqual(last["err"], "ValueError('task',)")
        self.assertTrue("ValueError: task" in last["tb"], last["tb"])
        self.assertFalse("stop" in _kinds(records))

    def test_silent_radio_journals_fatal(self):
        _write()
        hw = FakeHw(adapter_error=RuntimeError("no SX1262"))
        self.assertEqual(app.boot(hw, PATH, journal_dir=JOURNAL), app.RESET)
        records, _ = journal.read(JOURNAL)
        self.assertEqual(_kinds(records), ["fatal"])
        self.assertTrue("no SX1262" in records[0]["tb"])

    def test_report_handler_only_queues(self):
        hw = FakeHw()
        seen = []
        ext = record.encode_extension(3920, 1)

        def deliver(a):
            lines = a.journal.lines
            a.stack.routing.on_report(PRESENCE, 9, Presence(0, ext, parent=0), None)
            a.stack.engine.failed(0, 20, 7, 1)
            seen.append((a.journal.lines - lines, len(a.queue)))

        a = _app(hw)
        a.add_task(_after(50, deliver))
        a.add_task(_after(600, lambda a: a.stop()))
        app.start(a)
        self.assertEqual(seen, [(0, 2)])  # nothing written inside the step
        records, _ = journal.read(JOURNAL)
        rp = [r for r in records if r["k"] == "rp"]
        self.assertEqual(len(rp), 1)
        self.assertEqual((rp[0]["o"], rp[0]["bat"], rp[0]["mode"]), (9, 3920, 1))
        fail = [r for r in records if r["k"] == "ev" and r["e"] == "fail"]
        self.assertEqual(len(fail), 1)
        self.assertEqual((fail[0]["dst"], fail[0]["port"], fail[0]["seq"]), (0, 20, 7))
        self.assertTrue(rp[0]["t"] < records[-1]["t"])

    def test_queued_items_written_at_the_stop(self):
        hw = FakeHw()

        def deliver(a):
            a.stack.routing.on_report(PRESENCE, 9, Presence(0, b"", parent=0), None)
            a.stop()

        a = _app(hw)
        a.add_task(_after(50, deliver))
        app.start(a)
        kinds = _kinds(journal.read(JOURNAL)[0])
        self.assertEqual(kinds[-2:], ["rp", "stop"])
        self.assertEqual([r["address"] for r in a.mesh()["nodes"]], [9])

    def test_presence_extension_carries_the_battery(self):
        hw = FakeHw()
        hw.battery = 4000
        a = _app(hw, role="node", address=3, journal={"minute_s": 1})
        a.add_task(_after(1300, lambda a: a.stop()))
        hook = []

        def battery_drops(a):
            hook.append(record.decode_extension(a.stack.routing.reporter.extension()))
            hw.battery = 3500

        a.add_task(_after(50, battery_drops))
        app.start(a)
        self.assertEqual(hook, [(4000, 0)])
        # read again by the minute record
        self.assertEqual(record.decode_extension(a.extension), (3500, 0))

    def test_status(self):
        hw = FakeHw()
        hw.battery = 3950
        out = []
        a = _app(hw, name="base")
        a.add_task(_after(100, lambda a: out.append(a.status())))
        a.add_task(_after(150, lambda a: a.stop()))
        app.start(a)
        s = out[0]
        self.assertEqual((s["role"], s["address"], s["name"]), ("gateway", 0, "base"))
        self.assertEqual((s["attached"], s["parent"], s["rank"]), (True, None, 0))
        self.assertEqual((s["battery"], s["mode"], s["boot"]), (3950, "configuration", 1))
        self.assertEqual(s["neighbours"], [])
        self.assertEqual(s["counters"]["journal_lines"], a.journal.lines - 1)  # before stop
        self.assertTrue("tx_frames" in s["counters"])


class Mesh(Console):
    """The gateway's view of the mesh (``sily.nodes``), fed by the journal writer."""

    def test_reports_reach_the_view(self):
        hw = FakeHw()
        ext = record.encode_extension(3920, 1)
        seen = []

        def deliver(a):
            on_report = a.stack.routing.on_report
            on_report(PRESENCE, 9, Presence(FIRST_REPORT, ext, parent=0), None)
            on_report(PRESENCE, 12, Presence(0, b"", parent=9), None)
            on_report(TOPOLOGY, 9, [(0, 11, True, 97), (12, -2, False, 80)], None)
            on_report(NODE_INFO, 9, NodeInfo(b"\x00" * 6, 0x0003, "relay"), None)
            seen.append(len(a.nodes))  # inside the step: queued only

        def look(a):
            view = a.mesh()
            seen.append((view["period_s"], view["beyond"], list(view["nodes"])))

        a = _app(hw, presence_s=20)
        a.add_task(_after(50, deliver))
        a.add_task(_after(400, look))
        a.add_task(_after(450, lambda a: a.stop()))
        self.assertEqual(app.start(a), app.STOPPED)
        self.assertEqual(seen[0], 0)
        period, beyond, rows = seen[1]
        self.assertEqual((period, beyond), (20, 0))
        self.assertEqual([r["address"] for r in rows], [9, 12])
        relay, leaf = rows
        self.assertEqual(
            (relay["name"], relay["firmware"], relay["state"]), ("relay", "0.3", "ok")
        )
        self.assertEqual((relay["parent"], relay["hops"], relay["restarts"]), (0, 1, 1))
        self.assertEqual(
            (relay["battery"], relay["mode"], relay["age_s"]), (3920, "deployment", 0)
        )
        self.assertEqual(relay["links"], [[0, 11, 1, 97], [12, -2, 0, 80]])
        self.assertEqual((leaf["parent"], leaf["hops"], leaf["battery"]), (9, 2, None))

    def test_node_has_no_view(self):
        hw = FakeHw()
        seen = []
        a = _app(hw, role="node", address=3)
        a.add_task(_after(50, lambda a: seen.append(a.mesh())))
        a.add_task(_after(100, lambda a: a.stop()))
        self.assertEqual(app.start(a), app.STOPPED)
        self.assertEqual(seen, [None])


class Traffic(Console):
    """Test traffic in the application (``sily.traffic``): wiring, journal, status."""

    def _run(self, hw, ms, tasks=(), **fields):
        fields.setdefault("journal", {"minute_s": 1})
        a = _app(hw, **fields)
        for factory in tasks:
            a.add_task(factory)
        out = []
        a.add_task(_after(ms - 10, lambda a: out.append(a.status())))
        a.add_task(_after(ms, lambda a: a.stop()))
        self.assertEqual(app.start(a), app.STOPPED)
        records, _ = journal.read(JOURNAL)
        minutes = [r for r in records if r["k"] == "min"]
        self.assertEqual(len(minutes), 1)
        return a, minutes[0], out[0]

    def test_gateway_counts_a_node_message(self):
        hw = FakeHw()
        payload = bytearray(traffic.HEAD_SIZE + 6)
        struct.pack_into(traffic.HEAD, payload, 0, 4, 17, 1000)
        key = bytes.fromhex(KEY)
        frame = peer_frame(1, ROOT, bytes(payload), key, traffic.PORT, 1)

        def deliver(a):
            a.stack.adapter.on_receive(frame, -70, 6.5)

        a, minute, status = self._run(hw, 1500, (_after(50, deliver),))
        self.assertEqual(minute["tt"]["o"], [[1, 4, 1, 1, 0, 0, 0, 0, 1, 1, 0, 0, 0]])
        self.assertEqual((minute["tt"]["mal"], minute["tt"]["q"]), (0, 0))
        node = status["traffic"]["nodes"][0]
        self.assertEqual((node["address"], node["boot"], node["delivery"]), (1, 4, 1.0))

    def test_gateway_without_traffic_adds_nothing(self):
        a, minute, status = self._run(FakeHw(), 1500)
        self.assertFalse("tt" in minute)
        self.assertEqual(status["traffic"]["nodes"], [])

    def test_node_counts_its_messages_and_failures(self):
        hw = FakeHw()
        seen = []

        def send(a):
            seen.append(a.traffic.send(a.clock.now()))  # detached: held
            a.stack.engine.failed(ROOT, traffic.PORT, seen[0], 3)
            a.stack.engine.failed(ROOT, 20, 9, 3)  # another port's

        a, minute, status = self._run(
            hw,
            1500,
            (_after(50, send),),
            role="node",
            address=3,
            traffic={"enabled": True, "period_s": 3600},
        )
        self.assertTrue(seen[0] >= 0, seen)
        self.assertEqual(minute["tt"], {"n": 1, "ok": 1, "ref": [], "fail": [[3, 1]]})
        t = status["traffic"]
        self.assertEqual((t["submitted"], t["accepted"], t["failed"]), (1, 1, 1))
        self.assertEqual(a.failure_listeners, [a.traffic.failed])

    def test_node_without_traffic(self):
        a, minute, status = self._run(FakeHw(), 1500, role="node", address=3)
        self.assertIsNone(a.traffic)
        self.assertFalse("tt" in minute)
        self.assertFalse("traffic" in status)


class Turnaround(Console):
    """The gateway's reply turnaround in the application (``sily.turn``)."""

    def _run(self, hw, tasks=(), **fields):
        fields.setdefault("journal", {"minute_s": 1})
        a = _app(hw, **fields)
        for factory in tasks:
            a.add_task(factory)
        out = []
        a.add_task(_after(1490, lambda a: out.append(a.status())))
        a.add_task(_after(1500, lambda a: a.stop()))
        self.assertEqual(app.start(a), app.STOPPED)
        records, _ = journal.read(JOURNAL)
        minutes = [r for r in records if r["k"] == "min"]
        self.assertEqual(len(minutes), 1)
        return a, minutes[0], out[0]

    def test_gateway_times_its_immediate_reply(self):
        frame = probe_frame(1, bytes.fromhex(KEY))
        irq = []
        starts = []  # (link destination, SetTx) of each frame sent after the probe

        def deliver(a):
            adapter = a.stack.adapter
            transmit = adapter.transmit

            def stamped(f):
                transmit(f)
                starts.append((f[DEST_OFFSET], adapter._tx_us))

            adapter.transmit = stamped
            irq.append(ticks_us())
            adapter._irq_us = irq[0]
            adapter.on_receive(frame, -70, 6.5)

        a, minute, status = self._run(FakeHw(), (_after(50, deliver),))
        self.assertIs(a.stack.runner.radio, a.turn)  # between the tap and the runner
        self.assertIs(a.turn.radio, a.tap)
        self.assertEqual(minute["link"]["rep"], 1)  # answered at once
        dest, start = starts[0]  # the reply, first on air
        self.assertEqual(dest, 1)
        us = ticks_diff(start, irq[0])
        self.assertEqual(a.turn.samples[0], us)
        tenths = (us + 50) // 100
        self.assertEqual(minute["tr"], [1, tenths, tenths, tenths])
        self.assertTrue(us < 200000, us)
        figures = status["turnaround"]  # the last minute record's
        self.assertEqual((figures["n"], figures["max_ms"]), (1, tenths / 10))

    def test_gateway_without_reply(self):
        a, minute, status = self._run(FakeHw())
        self.assertIs(a.stack.runner.radio, a.turn)
        self.assertFalse("tr" in minute)
        self.assertEqual(
            status["turnaround"], {"n": 0, "median_ms": None, "p90_ms": None, "max_ms": None}
        )

    def test_node_not_timed(self):
        frame = probe_frame(1, bytes.fromhex(KEY))
        a, minute, status = self._run(
            FakeHw(),
            (_after(50, lambda a: a.stack.adapter.on_receive(frame, -70, 6.5)),),
            role="node",
            address=3,
        )
        self.assertIsNone(a.turn)
        self.assertIs(a.stack.runner.radio, a.tap)
        self.assertFalse("tr" in minute)
        self.assertFalse("turnaround" in status)


class Display(Console):
    """The OLED display in the application (``sily.display``) on `FakeOled`."""

    def _run(self, hw, ms, tasks=(), **fields):
        fields.setdefault("journal", {"minute_s": 1})
        a = _app(hw, **fields)
        for factory in tasks:
            a.add_task(factory)
        a.add_task(_after(ms, lambda a: a.stop()))
        result = app.start(a)
        records, _ = journal.read(JOURNAL)
        events = [r for r in records if r["k"] == "ev" and r["e"] == "display"]
        minutes = [r for r in records if r["k"] == "min"]
        return a, result, events, minutes

    def test_pages_shown_and_timed(self):
        hw = FakeHw()
        a, result, events, minutes = self._run(hw, 1500, name="base")
        self.assertEqual((result, events), (app.STOPPED, []))
        self.assertEqual(a.display.screen.shown, [None] * 8)  # off at the stop
        oled = hw.oled
        self.assertFalse(oled.powered)
        self.assertEqual(oled.ram, oled.buffer)
        self.assertTrue(len(oled.writes) >= 8)
        for first, last, data in oled.writes:
            self.assertEqual((first == last, len(data)), (True, 128))
        m = minutes[0]
        self.assertTrue(type(m["ow"]) is int and type(m["or"]) is int, m)
        self.assertTrue(m["ow"] <= m["or"], m)

    def test_first_page_from_status(self):
        hw = FakeHw()
        seen = []
        a, _, _, _ = self._run(
            hw, 300, (_after(250, lambda a: seen.append(list(a.display.screen.shown))),)
        )
        self.assertEqual(seen[0][:4], ["GW0 box0     1/4", "attached", "parent -", "rank 0"])

    def test_window_mode(self):
        a, _, _, _ = self._run(FakeHw(), 50, display={"mode": "window", "window_min": 3})
        self.assertEqual((a.display.mode, a.display.window_ms), ("window", 180000))

    def test_mode_off_never_touches_the_display(self):
        hw = FakeHw()
        a, result, events, minutes = self._run(hw, 1500, display={"mode": "off"})
        self.assertEqual((result, events, hw.displays, a.display), (app.STOPPED, [], 0, None))
        self.assertFalse("ow" in minutes[0])
        self.assertEqual(hw.oled.cmds, [])

    def test_missing_display(self):
        hw = FakeHw()
        hw.oled = None
        a, result, events, minutes = self._run(hw, 1500)
        self.assertEqual((result, a.display, hw.displays), (app.STOPPED, None, 1))
        self.assertEqual([e["err"] for e in events], ["no device"])
        self.assertFalse("ow" in minutes[0])
        self.assertEqual(_guard(hw).crashes, 0)

    def test_display_error_at_start(self):
        hw = FakeHw()
        hw.display_error = OSError(19)
        a, result, events, _ = self._run(hw, 200)
        self.assertEqual((result, a.display), (app.STOPPED, None))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["err"].startswith("OSError(19"), events)

    def test_failure_while_running(self):
        hw = FakeHw()

        def fail(a):
            hw.oled.fail = OSError(5)
            a.display.screen.shown[1] = None  # rewritten at the next refresh

        a, result, events, _ = self._run(hw, 1500, (_after(100, fail),), led=False)
        self.assertEqual(result, app.STOPPED)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["err"].startswith("OSError(5"), events)
        self.assertFalse(a.display.lit)
        self.assertTrue(a.stack.runner.steps > 0)
        self.assertEqual(_guard(hw).crashes, 0)


def _fetch(ms, path, out, stop=True):
    """A task factory: GET path from the app's web server after ms, the
    response (`_split`) appended to out; then the app stopped."""

    async def task(a):
        await asyncio.sleep_ms(ms)
        data, reset = await _get(a.web_port, _request(path))
        out.append(_split(data))
        if stop:
            a.stop()

    return task


def _secrets(cfg):
    """Return the forms of the configuration's secrets that must stay off the page."""
    forms = [AP["password"]]
    for text in (cfg["key"], cfg["fingerprint"]):
        raw = repr(bytes.fromhex(text))
        forms += [text, text.upper(), raw, json.dumps(raw)[1:-1]]
    return forms


class Wifi(Console):
    """The access point and the web server in the application (``sily.web``)."""

    def _app(self, hw, tasks=(), **fields):
        fields.setdefault("journal", {"minute_s": 1})
        a = _app(hw, **fields)
        a.web_port = _free_port()
        for factory in tasks:
            a.add_task(factory)
        return a

    def _records(self):
        records, bad = journal.read(JOURNAL)
        self.assertEqual(bad, 0)
        return records

    def _events(self):
        return [r["err"] for r in self._records() if r["k"] == "ev" and r["e"] == "wifi"]

    def test_gateway_access_point_and_server(self):
        hw = FakeHw()
        seen = []

        def look(a):
            seen.append((a.web.running, a.ap.on, a.status()))

        a = self._app(
            hw, (_after(100, look), _after(150, lambda a: a.stop())), name="base", wifi={"ap": AP}
        )
        self.assertEqual(app.start(a), app.STOPPED)
        self.assertEqual(len(hw.aps), 1)
        ap = hw.aps[0]
        self.assertEqual((ap.ssid, ap.password), ("SILy-base", AP["password"]))
        running, on, status = seen[0]
        self.assertEqual((running, on, a.web.port), (True, True, a.web_port))
        self.assertEqual(status["web"]["requests"], 0)
        self.assertEqual((a.web.running, ap.on, ap.offs), (False, False, 1))  # off at stop
        records = self._records()
        self.assertEqual(records[0]["ap"], "SILy-base")
        self.assertEqual(self._events(), [])

    def test_access_point_failure(self):
        hw = FakeHw()
        hw.ap_error = OSError(5)
        a = self._app(hw, (_after(1500, lambda a: a.stop()),), wifi={"ap": AP})
        self.assertEqual(app.start(a), app.STOPPED)
        errors = self._events()
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("OSError(5"), errors)
        self.assertEqual((a.web, a.ap, hw.aps), (None, None, []))
        self.assertTrue(a.stack.runner.steps > 0)  # the application runs on
        minutes = [r for r in self._records() if r["k"] == "min"]
        self.assertEqual(len(minutes), 1)
        self.assertFalse("web" in minutes[0])
        self.assertEqual(_guard(hw).crashes, 0)

    def test_access_point_without_address(self):
        hw = FakeHw()
        hw.ap_address = "0.0.0.0"
        a = self._app(hw, (_after(100, lambda a: a.stop()),), wifi={"ap": AP})
        self.assertEqual(app.start(a), app.STOPPED)
        self.assertEqual(self._events(), ["no address"])
        self.assertEqual((a.web, a.ap, hw.aps[0].on), (None, None, False))

    def test_port_taken(self):
        hw = FakeHw()
        a = self._app(hw, (_after(100, lambda a: a.stop()),), wifi={"ap": AP})
        taken = _bind(a.web_port)
        taken.listen(1)
        try:
            self.assertEqual(app.start(a), app.STOPPED)
        finally:
            taken.close()
        errors = self._events()
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("OSError("), errors)
        self.assertEqual((a.web, a.ap, hw.aps[0].on), (None, None, False))
        self.assertTrue(a.stack.runner.steps > 0)

    def test_no_access_point(self):
        hw = FakeHw()
        out = []
        a = self._app(
            hw, (_after(1400, lambda a: out.append(a.status())), _after(1500, lambda a: a.stop()))
        )
        self.assertEqual(app.start(a), app.STOPPED)
        self.assertEqual((hw.aps, a.ap, a.web), ([], None, None))
        records = self._records()
        self.assertIsNone(records[0]["ap"])
        minutes = [r for r in records if r["k"] == "min"]
        self.assertEqual(len(minutes), 1)
        self.assertFalse("web" in minutes[0])
        self.assertFalse("web" in out[0])
        self.assertEqual(self._events(), [])

    def test_crash_switches_the_access_point_off(self):
        hw = FakeHw()
        a = self._app(hw, (_after(100, _raise(ValueError("task"))),), wifi={"ap": AP})
        self.assertEqual(app.start(a), app.RESET)
        self.assertEqual((hw.aps[0].on, a.web.running), (False, False))

    def test_status_json_carries_the_mesh_and_no_secret(self):
        hw = FakeHw()
        ext = record.encode_extension(3920, 1)
        out = []

        def deliver(a):
            on_report = a.stack.routing.on_report
            on_report(PRESENCE, 9, Presence(FIRST_REPORT, ext, parent=0), None)
            on_report(PRESENCE, 12, Presence(0, b"", parent=9), None)
            on_report(NODE_INFO, 9, NodeInfo(b"\x00" * 6, 0x0003, "relay"), None)

        a = self._app(
            hw,
            (_after(50, deliver), _fetch(450, b"/status.json", out)),
            name="base",
            wifi={"ap": AP},
        )
        self.assertEqual(app.start(a), app.STOPPED)
        code, headers, body = out[0]
        self.assertEqual((code, headers["content-type"]), (200, "application/json"))
        j = json.loads(body)
        self.assertEqual((j["box"]["name"], j["box"]["web"]["requests"]), ("base", 0))
        nodes = j["mesh"]["nodes"]
        self.assertEqual([n["address"] for n in nodes], [9, 12])
        self.assertEqual((nodes[0]["name"], nodes[0]["battery"]), ("relay", 3920))
        self.assertEqual((nodes[1]["parent"], nodes[1]["hops"]), (9, 2))
        text = body.decode()
        for secret in _secrets(a.cfg):
            self.assertFalse(secret in text, secret)

    def test_minute_record_counts_the_requests(self):
        hw = FakeHw()
        out = []

        def stations(a):
            a.ap.joined = 2

        def unreadable(a):
            a.ap.stations_error = OSError(5)

        a = self._app(
            hw,
            (
                _after(50, stations),
                _fetch(200, b"/status.json", out, stop=False),
                _after(1100, unreadable),
                _after(2500, lambda a: a.stop()),
            ),
            wifi={"ap": AP},
        )
        self.assertEqual(app.start(a), app.STOPPED)
        self.assertEqual(out[0][0], 200)
        minutes = [r["web"] for r in self._records() if r["k"] == "min"]
        self.assertEqual(len(minutes), 2)
        first, second = minutes
        self.assertEqual(len(first), 7)
        self.assertEqual((first[0], first[1], first[6]), (1, 0, 2))  # requests, errors, stations
        self.assertTrue(type(first[5]) is int and first[5] >= 1, first)  # longest stretch, ms
        self.assertEqual((second[0], second[6]), (1, None))  # stations unreadable
        self.assertEqual(_guard(hw).crashes, 0)
