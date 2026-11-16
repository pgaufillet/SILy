"""The application (sily.app) on fake hardware: boot decision, run, stop, crash.

The configuration is a real file under build/; the hardware is the
`FakeHw` of ``test_sily_stack`` (scripted radio adapter, simulator platform
fakes, LED and board controls recorded). Runs last a few hundred ms of
real time on the unix port's asyncio.
"""

import asyncio
import io
import json
import os
import unittest

from test_sily_stack import FakeHw

from mesh.ports import PRESENCE
from mesh.reports import Presence
from sily import app, guard, journal, record
from sily.config import check

DIR = "build/test_sily_app"  # relative to the repository root, where the tests run
PATH = DIR + "/config.json"
JOURNAL = DIR + "/journal"
KEY = "00112233445566778899aabbccddeeff"


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
        "fingerprint": "a1b2c3d4",
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
