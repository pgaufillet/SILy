"""The application of a box: boot decision, mesh node, LED, stop and crash paths.

``main.py`` calls `boot` at every boot. It prints ``SILY <version>``, then:

- the start guard (``sily.guard``) may skip this boot (one-shot flag, set
  from the REPL by ``sily.hw.skip_next_boot``): ``SILY SKIP``, back to the
  REPL;
- safe mode, when the guard counts `guard.MAX_CRASHES` crashes without
  10 minutes of stable run, or ``/config.json`` is missing or invalid
  (``sily.config``): one line ``SILY SAFE field: problem`` per reason, the
  LED blinks fast (`SAFE_BLINK_MS`), a ``safe`` line with the reasons
  goes into the journal (`safe_mode`) and `boot` returns: the REPL is
  free;
- otherwise the application runs under asyncio (`App`) until stopped.

Recovery: Ctrl-C (``mpremote`` sends one when it connects) stops the
application cleanly (radio in standby, LED off, ``SILY STOP``) and leaves
the board at the REPL; safe mode leaves it there too. ``make configure``
writes a new configuration, clears the guard and resets the board; a box
switched off and on retries after crashes (a power-on clears the count).

Any other exception (a task, the runner, the node's assembly such as a
silent SX1262) is a crash: ``SILY FATAL`` with the traceback, the crash
counted, the radio and LED stopped, then after `CRASH_PAUSE_MS` (Ctrl-C
works: the board stays at the REPL) the board resets.

A hang is caught by the hardware watchdog (``sily.watchdog``), which the
application arms at its start (`watchdog.TIMEOUT_MS`, in the ``boot``
record) and feeds from a task of its loop; safe mode and a skipped boot
never arm it. `App.close` hands the feed to a hardware timer: the REPL and
the host tools stay usable after a stop, and during a crash's pause. A
loop that stops turning resets the box; the next boot's reset cause is
``watchdog``, counted as a crash by the start guard.

`App` runs the node (``sily.stack``) and the tasks around it: the radio
adapter, a monitor polling the mesh state once a second (no application
code inside the mesh's step) and driving the LED (on while attached, the
gateway always), the stability timer clearing the crash count, and the
test traffic (``sily.traffic``): on the gateway, always, the accounting of
what the nodes send on its port; on a node whose configuration enables
it, the sender (its failures passed on by the journal writer); the OLED
display (``sily.display``) unless its mode is ``off``, started after the
boot record (no device, or a failure later: an ``ev`` ``display`` line and
the application goes on without it; powered off at stop and crash). With
``wifi.ap`` in the configuration, the WiFi access point ``<prefix>-<name>``
(WPA2) starts after the display, before the mesh's tasks, then the web
server (``sily.web``: the page and its JSON, `App.status` and, on the
gateway, `App.mesh`; the phone's time of each poll to `App.sample`); both
stay on until the stop or crash. A failure at
their start (an exception, no address, the port taken) is an ``ev``
``wifi`` line: the access point is switched off and the application goes
on without them. Later parts add their tasks with `App.add_task` before
the start, and their failure callbacks to ``failure_listeners``.

The field journal (``sily.journal``, records of ``sily.record``) starts a
new segment at each boot (boot number counted in the NVS namespace
``sily``; a boot in safe mode writes its ``safe`` line alone there): the
``boot`` record (with ``sily.build_id`` and the RTC's time when set), then
the ``clock`` lines anchoring its clock to the time of a phone showing the
page (``sily.utc``, the RTC set to each), a ``min`` record every
``journal.minute_s`` (the battery read then, and cached for the presence
extension), the ``ev`` events of the monitor's polls, the failures of own
messages and, on the gateway, the reports received; a ``stop`` or
``fatal`` line ends it. On the gateway, the writer also keeps the reports
in the view of the mesh (``sily.nodes``, `App.mesh`, for the web page).
The minute record carries the test traffic's figures (``tt``) when there
are some, the display's longest row write and refresh (``ow``, ``or``)
when it wrote, the web server's figures with the stations joined
(``web``) while it runs, on the gateway, the turnaround of its
immediate replies when it made some (``tr``, ``sily.turn``: a radio
wrapper beside the tap, timing each reply from the adapter's
timestamps) and, on every box, the loop's lateness (``lp``,
``sily.lateness``: a task started with the mesh's, its period and bound
in the boot record). The journal is synced after the boot record, every
``journal.sync_s`` and at stop and crash. The mesh core's callbacks (report
handler, failure callback, presence extension) run inside its step, on the
root's reply path: they only queue an item or return cached bytes; a task
formats and writes, yielding between items.
"""

import asyncio
import gc
import io
import sys

import sily
from mesh.runner import Clock
from sily import (
    config,
    display,
    guard,
    journal,
    lateness,
    nodes,
    record,
    traffic,
    turn,
    utc,
    watchdog,
    web,
)
from sily.stack import Stack
from sily.tap import Tap

CONFIG_PATH = "/config.json"
CRASH_PAUSE_MS = 5000
MONITOR_MS = 1000
SAFE_BLINK_MS = 100
WRITER_MS = 200  # period of the journal writer's look at the queue
QUEUE_ITEMS = 64  # callback items waiting for the writer
BOOT_KEY = "boots"  # boot number, NVS namespace guard.NAMESPACE
WATCHDOG_RESET = "watchdog"  # hw.reset_cause() after a reset by the watchdog
SAFE_JOURNAL_KB = 1 << 20  # bound of the journal for the safe line: no segment removed

# Kinds of the queued items.
_REPORT = 0  # (_REPORT, t, port, origin, report)
_FAIL = 1  # (_FAIL, t, dest, port, seq, reason)

# Outcomes of `boot` and `start` (the board only sees the console lines).
STOPPED = "stopped"  # Ctrl-C or App.stop: at the REPL
RESET = "reset"  # after a crash (on the host, the injected reset returned)

current = None  # the App last started (`close`)
console = None  # file of the console lines (None: standard output; tests set one)


def _out(*items, end="\n"):
    if console is None:
        print(*items, end=end)
    else:
        print(*items, end=end, file=console)


def next_boot(store):
    """Count one more boot in store (``get``, ``set``, ``commit``); return its number."""
    value = store.get(BOOT_KEY)
    n = 1
    if value:
        n = int.from_bytes(value, "little") + 1
    store.set(BOOT_KEY, n.to_bytes(4, "little"))
    store.commit()
    return n


def close():
    """Stop the application last started: journal closed, radio, LED (idempotent).

    For host tools that connect to a box (their Ctrl-C has already stopped
    it: ``make journal``).
    """
    if current is not None:
        current.close()


class App:
    """The application on configuration cfg (``sily.config``) and hardware hw.

    hw: ``sily.hw`` on the board. start_guard: the `guard.Guard` whose crash
    count `stable_ms` of running clears (None: no stability timer);
    monitor_ms: period of the monitor; journal_dir, fs: the journal's
    directory and file system (``sily.journal``); web_port: the web
    server's port (``sily.web``); feed_ms: period of the watchdog's feed.
    """

    def __init__(
        self,
        cfg,
        hw,
        start_guard=None,
        stable_ms=guard.STABLE_MS,
        monitor_ms=MONITOR_MS,
        journal_dir=journal.DIR,
        fs=None,
        web_port=web.PORT,
        feed_ms=watchdog.FEED_MS,
    ):
        self.cfg = cfg
        self.hw = hw
        self.guard = start_guard
        self.stable_ms = stable_ms
        self.monitor_ms = monitor_ms
        self.journal_dir = journal_dir
        self.fs = fs
        self.web_port = web_port
        self.feed_ms = feed_ms
        self.clock = Clock()
        self.stack = None
        self.tap = None
        self.turn = None  # turn.Turn on the gateway: its replies timed
        self.probe = None  # lateness.Probe once the mesh's tasks start
        self.journal = None
        self.queue = journal.Queue(QUEUE_ITEMS)
        self.boot = None
        self.state = record.DETACHED
        self.battery = None  # mV, read every minute
        self.mode = 0  # operating mode (record.MODES), carried in the presence extension
        self.extension = record.encode_extension(None, 0)
        self.led = None
        self.attached = False
        self.error = None  # the first exception of a task
        self.traffic = None  # traffic.Receiver (gateway), traffic.Sender or None
        self.display = None  # display.Display, or None (mode off, absent)
        self.nodes = None  # nodes.Nodes on the gateway: the reports received
        self.ap = None  # the access point (sily.hw.access_point), or None
        self.web = None  # web.Web once started, or None
        self.wdt = None  # the hardware watchdog once armed (sily.watchdog)
        self.anchor = utc.Anchor()  # the clock anchored to UTC (sily.utc)
        self.failure_listeners = []  # fn(dest, port, seq, reason), outside the step
        self._factories = []
        self._closed = False

    def add_task(self, factory):
        """Start factory(app), a coroutine, once the node is built."""
        self._factories.append(factory)

    def open_journal(self):
        """Count this boot and start its journal segment."""
        self.boot = next_boot(self.hw.storage(guard.NAMESPACE))
        j = self.cfg["journal"]
        self.journal = journal.Journal(
            self.journal_dir,
            self.boot,
            self.clock.now,
            j["max_kb"],
            j["segment_kb"],
            j["sync_s"],
            self.fs,
        )
        self.journal.open()

    def _wrap(self, stack, radio):
        self.tap = Tap(radio)
        if not stack.engine.is_root:
            return self.tap
        self.turn = turn.Turn(self.tap, stack.adapter, stack.engine.counters)
        return self.turn

    def build(self):
        """Assemble the node from the configuration (``sily.stack``)."""
        cfg = self.cfg
        hw = self.hw
        self.read_battery()
        gateway = cfg["role"] == config.GATEWAY
        self.stack = Stack(
            hw,
            cfg["address"],
            bytes.fromhex(cfg["key"]),
            cfg["sf"],
            name=cfg["name"],
            profile=cfg["profile"],
            power=cfg["tx_power"],
            gain=cfg["antenna_gain"],
            allow=cfg["allow"],
            drop=config.drop_rates(cfg["drop"]),
            presence_s=cfg["presence_s"],
            extension=self._extension,
            on_report=self._on_report if gateway else None,
            wrap=self._wrap,
            clock=self.clock,
        )
        self.stack.engine.on_failure(self._on_failure)
        if cfg["led"]:
            self.led = hw.led()
            self.led.value(0)
        self.traffic = self._traffic()
        if gateway:
            self.nodes = nodes.Nodes(self.stack.routing.dodag.info.presence_s)

    def _traffic(self):
        """Set up the test traffic (module documentation); return it or None."""
        engine = self.stack.engine
        if engine.is_root:
            receiver = traffic.Receiver(
                self.clock.now, sf=self.stack.sf, profile=self.stack.profile
            )
            engine.bind(traffic.PORT, receiver.handler)
            self.add_task(lambda app: traffic.receive(receiver))
            return receiver
        t = self.cfg["traffic"]
        if not t["enabled"]:
            return None
        sender = traffic.Sender(
            self.submit,
            self.hw.random(),
            self.boot,
            t["period_s"] * 1000,
            t["size"],
            t["jitter_pct"],
        )
        self.failure_listeners.append(sender.failed)
        self.add_task(lambda app: traffic.send(sender, app.clock.now, lambda: app.attached))
        return sender

    def _display(self):
        """Start the display (module documentation); return it or None."""
        d = self.cfg["display"]
        if d["mode"] == display.OFF:
            return None
        error = "no device"
        try:
            oled = self.hw.display()
        except Exception as e:  # an I2C error: the application goes on without display
            oled = None
            error = repr(e)
        if oled is None:
            self._display_failed(error)
            return None
        shown = display.Display(
            display.Screen(oled),
            self.status,
            self.clock.now,
            d["mode"],
            d["window_min"] * 60000,
            on_error=lambda e: self._display_failed(repr(e)),
        )
        self.add_task(lambda app: shown.run())
        return shown

    def _display_failed(self, error):
        self.journal.write(self.clock.now(), "ev", {"e": "display", "err": error})

    async def _wifi(self):
        """Start the access point, then the web server (module documentation)."""
        ap = self.cfg["wifi"]["ap"]
        if ap is None:
            return
        ssid = config.ap_ssid(ap["prefix"], self.cfg["name"])
        mesh = self.mesh if self.nodes is not None else None  # the gateway's view
        try:
            self.ap = self.hw.access_point(ssid, ap["password"])
            address = self.ap.address()
            if address and address != "0.0.0.0":
                server = web.Web(self.status, mesh, self.sample, port=self.web_port)
                await server.start()
                self.web = server
                return
            error = "no address"
        except Exception as e:  # the application goes on without WiFi
            error = repr(e)
        self.journal.write(self.clock.now(), "ev", {"e": "wifi", "err": error})
        self._wifi_off()
        self.ap = None

    def _serving(self):
        return self.web is not None and self.web.running

    def _stations(self):
        try:
            return self.ap.stations()
        except Exception:  # the count only: the minute record goes on
            return None

    def _wifi_off(self):
        """Stop the web server, switch the access point off (never raises)."""
        if self.web is not None:
            try:
                self.web.stop()
            except Exception:  # nothing more to do
                pass
        if self.ap is not None:
            try:
                self.ap.off()
            except Exception:  # nothing more to do
                pass

    def submit(self, port, cls, dest, payload):
        """Submit an application message (``LinkEngine.submit``), then wake the runner."""
        result = self.stack.engine.submit(port, cls, dest, payload)
        self.stack.runner.wake()
        return result

    def read_battery(self):
        """Read the battery; cache it for the presence extension."""
        self.battery = self.hw.battery_mv()
        self.extension = record.encode_extension(self.battery, self.mode)

    async def main(self):
        """Build the node, run it and its tasks until `stop`; raise a task's exception."""
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(self._task_error)
        try:
            self.wdt = self.hw.arm_watchdog(watchdog.TIMEOUT_MS)
            asyncio.create_task(self._feed())  # from the first wait on
            self.open_journal()
            self.build()
            stack = self.stack
            crashes = 0 if self.guard is None else self.guard.crashes
            rtc = utc.valid(self.hw.utc_ms())
            now = self.clock.now()
            fields = record.boot(
                self.cfg,
                stack,
                self.hw.reset_cause(),
                crashes,
                sily.__version__,
                sily.build_id(),
                watchdog.TIMEOUT_MS,
                rtc,
                [lateness.PERIOD_MS, lateness.BOUND_MS],
            )
            self.journal.write(now, "boot", fields)
            self.anchor.start(now, rtc)
            self.journal.sync()
            self.display = self._display()
            await self._wifi()
            self.probe = lateness.Probe(self.clock.now)
            asyncio.create_task(self.probe.run())
            asyncio.create_task(stack.adapter.run())
            asyncio.create_task(self._monitor())
            asyncio.create_task(self._minutes())
            asyncio.create_task(self._writer())
            asyncio.create_task(self._syncer())
            if self.guard is not None:
                asyncio.create_task(self._stability())
            for factory in self._factories:
                asyncio.create_task(factory(self))
            await stack.runner.run()
        finally:
            loop.set_exception_handler(None)
        if self.error is not None:
            raise self.error

    def sample(self, ms):
        """Take a phone's time, ms since the Unix epoch (a poll of the page,
        ``sily.web``): at each anchor (``sily.utc``), a ``clock`` line and
        the RTC set."""
        now = self.clock.now()
        if self.anchor.sample(now, ms):
            self.journal.write(now, "clock", {"utc": ms})
            self.hw.set_utc(ms)

    # --- Callbacks of the mesh core: inside its step, queue only -----------------------

    def _extension(self):
        return self.extension

    def _on_report(self, port, origin, report, header):
        self.queue.put((_REPORT, self.clock.now(), port, origin, report))

    def _on_failure(self, dest, port, seq, reason):
        self.queue.put((_FAIL, self.clock.now(), dest, port, seq, reason))

    # --- Journal tasks -----------------------------------------------------------------

    def _write_item(self, item):
        if item[0] == _FAIL:
            self.journal.write(item[1], "ev", record.failure(item[2], item[3], item[4], item[5]))
            for listener in self.failure_listeners:
                listener(item[2], item[3], item[4], item[5])
        else:
            kind, fields = record.report(item[2], item[3], item[4])
            self.journal.write(item[1], kind, fields)
            if self.nodes is not None:
                self.nodes.update(item[1], item[2], item[3], item[4])

    async def _writer(self):
        queue = self.queue
        while True:
            while len(queue):
                self._write_item(queue.get())
                await asyncio.sleep_ms(0)
            await asyncio.sleep_ms(WRITER_MS)

    async def _syncer(self):
        while True:
            await asyncio.sleep_ms(self.journal.maybe_sync())

    async def record_minute(self):
        """Write the minute record, built in pieces (other tasks run between them)."""
        self.read_battery()
        now = self.clock.now()
        fields = {}
        for piece in record.minute(self.stack, now, self.tap):
            fields.update(piece)
            await asyncio.sleep_ms(0)
        j = self.journal
        fields["bat"] = self.battery
        fields["heap"] = gc.mem_free()
        fields["jw"], fields["js"] = j.stalls()
        fields["jq"] = self.queue.overflow
        fields["je"] = j.errors
        if self.display is not None:
            stalls = self.display.screen.stalls()
            if stalls is not None:
                fields["ow"], fields["or"] = stalls
        if self.traffic is not None:
            await asyncio.sleep_ms(0)
            tt = self.traffic.minute()
            if tt is not None:
                fields["tt"] = tt
        if self._serving():
            fields["web"] = self.web.minute() + [self._stations()]
        if self.turn is not None:
            await asyncio.sleep_ms(0)
            tr = self.turn.minute()
            if tr is not None:
                fields["tr"] = tr
        if self.probe is not None:
            fields["lp"] = self.probe.minute()
        j.write(now, "min", fields)

    async def _minutes(self):
        period = self.cfg["journal"]["minute_s"] * 1000
        while True:
            await asyncio.sleep_ms(period)
            await self.record_minute()

    def _task_error(self, loop, context):
        if self.error is None:
            self.error = context["exception"]
        self.stop()

    def stop(self):
        """End `main` at the runner's next wake-up."""
        if self.stack is not None:
            self.stack.runner.stop()

    def poll(self):
        """Read the mesh state (once a second): journal its changes, show it."""
        stack = self.stack
        cur = record.state(stack.engine, stack.routing)
        if cur != self.state:
            events = record.transitions(self.state, cur)
            self.state = cur
            now = self.clock.now()
            for name, fields in events:
                fields["e"] = name
                self.journal.write(now, "ev", fields)
        attached = cur[0] == 1
        if attached != self.attached:
            self.attached = attached
            if self.led is not None:
                self.led.value(1 if attached else 0)

    async def _monitor(self):
        while True:
            self.poll()
            await asyncio.sleep_ms(self.monitor_ms)

    async def _stability(self):
        await asyncio.sleep_ms(self.stable_ms)
        self.guard.stable()

    async def _feed(self):
        wdt = self.wdt
        while True:
            wdt.feed()
            await asyncio.sleep_ms(self.feed_ms)

    def status(self):
        """Return the state of the box as one dict (display, web page);
        ``utc``: the UTC now by the last anchor (``sily.utc``), ms since the
        Unix epoch, None without one."""
        cfg = self.cfg
        out = {
            "role": cfg["role"],
            "address": cfg["address"],
            "name": cfg["name"],
            "boot": self.boot,
            "battery": self.battery,
            "mode": record.MODES[self.mode],
            "attached": False,
            "heap": gc.mem_free(),
            "utc": self.anchor.estimate(self.clock.now()),
        }
        stack = self.stack
        if stack is None:
            return out
        e = stack.engine
        now = self.clock.now()
        s = record.state(e, stack.routing)
        rows, foreign = record.neighbours(e.neighbours, now, self.tap)
        c = e.counters
        j = self.journal
        out.update(
            {
                "time_s": now // 1000,
                "attached": s[0] == 1,
                "parent": s[1],
                "rank": s[2],
                "grounded": s[3] == 1,
                "neighbours": [dict(zip(record.NEIGHBOUR_FIELDS, row)) for row in rows],
                "foreign": foreign,
                "counters": {
                    "tx_frames": c.tx_frames,
                    "rx_frames": c.rx_frames,
                    "rx_crc": c.rx_crc,
                    "rx_auth": c.rx_auth,
                    "forwarded": c.forwarded,
                    "retries": c.retries,
                    "unreachable": c.unreachable,
                    "queue": len(e.queue),
                    "duty_ms": e.dutycycle.used(now),
                    "duty_budget_ms": e.dutycycle.budget_ms,
                    "journal_lines": 0 if j is None else j.lines,
                    "journal_errors": 0 if j is None else j.errors,
                    "items_lost": self.queue.overflow,
                },
            }
        )
        if self.traffic is not None:
            out["traffic"] = self.traffic.status()
        if self.turn is not None:
            out["turnaround"] = self.turn.figures()
        if stack.drop is not None:
            out["dropped"] = stack.drop.dropped
        if self._serving():
            out["web"] = self.web.figures()
        return out

    def mesh(self):
        """Return the gateway's view of the mesh (web page), None on a node.

        Keys: ``period_s`` (the root's presence period), ``beyond`` (reports
        of origins beyond the bound) and ``nodes``, a generator of one dict
        per node by address (`sily.nodes.Nodes.rows`: the caller may let
        other tasks run between them).
        """
        view = self.nodes
        if view is None:
            return None
        return {
            "period_s": view.period_s,
            "beyond": view.beyond,
            "nodes": view.rows(self.clock.now),
        }

    def close(self, kind="stop", fields=None):
        """Stop the node: the watchdog fed by its timer from now on (first:
        the cleanup, a crash's pause and the REPL after them), pending items
        and a last line of kind (None: none) journaled, the journal synced
        and closed, runner stopped, radio in standby, LED and display off,
        web server stopped, access point off. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.wdt is not None:
                self.hw.idle_watchdog()
            j = self.journal
            if j is not None:
                try:
                    queue = self.queue
                    while len(queue):
                        self._write_item(queue.get())
                    if kind is not None:
                        j.write(self.clock.now(), kind, fields)
                finally:
                    j.close()
        finally:
            if self.stack is not None:
                self.stack.close()
            if self.led is not None:
                self.led.value(0)
            if self.display is not None:
                self.display.off()
            self._wifi_off()


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def prepare(hw, start_guard, path=CONFIG_PATH):
    """Return (decision, config, reasons) of this boot.

    decision: `guard.START`, `guard.SKIP` or `guard.SAFE`; config: the
    checked configuration (None in safe mode); reasons: the safe mode's,
    ``field: problem`` strings.
    """
    decision = start_guard.decide(hw.power_on(), hw.reset_cause() == WATCHDOG_RESET)
    if decision == guard.SKIP:
        return decision, None, []
    reasons = []
    if decision == guard.SAFE:
        reasons.append(
            "crashes: %d without %d min of stable run (switch off and on to retry)"
            % (start_guard.crashes, guard.STABLE_MS // 60000)
        )
    text = _read(path)
    cfg = None
    if text is None:
        reasons.append("file: %s missing (make configure)" % path)
    else:
        cfg, errors = config.load(text)
        reasons.extend(errors)
    if reasons:
        return guard.SAFE, None, reasons
    return guard.START, cfg, reasons


def safe_mode(hw, reasons, journal_dir=journal.DIR, fs=None):
    """Print the reasons, blink the LED fast, journal them; the caller returns to the REPL.

    The ``safe`` line (``sily.record.safe``) opens this boot's segment,
    synced: it tells ``make journal`` why the box sat blinking. The
    configuration may be the problem: the journal takes its defaults,
    except its bound (`SAFE_JOURNAL_KB`: the line removes no segment, a
    box configured above the default keeps its older ones; the next boot
    that runs applies its own). Its failure is a console line only (the
    blink and the REPL stay). The watchdog is left alone: unarmed after a
    hardware reset, fed by its timer after a soft one (``boot.py``).
    """
    for reason in reasons:
        _out("SILY SAFE", reason)
    hw.blink(SAFE_BLINK_MS)
    error = None
    try:
        number = next_boot(hw.storage(guard.NAMESPACE))
        j = journal.Journal(journal_dir, number, Clock().now, SAFE_JOURNAL_KB, fs=fs)
        j.open()
        j.write(j.clock(), "safe", record.safe(reasons, sily.__version__, sily.build_id()))
        j.close()
        if j.errors:
            error = "%d errors" % j.errors
    except Exception as e:  # the blink and the REPL stay
        error = repr(e)
    if error is not None:
        _out("SILY SAFE journal not written:", error)


def _stop(app, hw):
    hw.kbd_intr(-1)  # a second Ctrl-C must not cut the cleanup short
    try:
        app.close()
    finally:
        hw.kbd_intr(3)
    _out("SILY STOP")


def _traceback(error):
    buf = io.StringIO()
    sys.print_exception(error, buf)
    return buf.getvalue()


def _crash(app, hw, start_guard, error):
    _out("SILY FATAL", repr(error))
    text = _traceback(error)
    _out(text, end="")
    n = start_guard.crashed()
    try:
        app.close("fatal", {"err": repr(error), "tb": text})
    except Exception as e:  # the reset below must still come
        _out("SILY FATAL close", repr(e))
    _out(
        "SILY RESET in %d s, crash %d (safe mode at %d; Ctrl-C: stay at the REPL)"
        % (CRASH_PAUSE_MS // 1000, n, guard.MAX_CRASHES)
    )
    try:
        hw.sleep_ms(CRASH_PAUSE_MS)
    except KeyboardInterrupt:
        _out("SILY STOP")
        return STOPPED
    hw.reset()
    return RESET


def start(app):
    """Run app until Ctrl-C, `App.stop` or a crash; return `STOPPED` or `RESET`."""
    global current
    current = app
    hw = app.hw
    asyncio.new_event_loop()  # drop the tasks of an earlier run
    try:
        asyncio.run(app.main())
    except KeyboardInterrupt:
        _stop(app, hw)
        return STOPPED
    except Exception as e:
        return _crash(app, hw, app.guard, e)
    finally:
        asyncio.get_event_loop().set_exception_handler(None)
        asyncio.new_event_loop()
    _stop(app, hw)
    return STOPPED


def boot(hw=None, path=CONFIG_PATH, **options):
    """Start the application, or not (see the module documentation).

    hw: the hardware bundle (``sily.hw`` when None); options: as `App`.
    Returns `guard.SKIP`, `guard.SAFE`, `STOPPED` or `RESET`.
    """
    if hw is None:
        from sily import hw
    _out("SILY", sily.__version__)
    start_guard = guard.Guard(hw.storage(guard.NAMESPACE))
    decision, cfg, reasons = prepare(hw, start_guard, path)
    if decision == guard.SKIP:
        _out("SILY SKIP: this boot skipped on request, the REPL is free")
        return decision
    if decision == guard.SAFE:
        safe_mode(hw, reasons, options.get("journal_dir", journal.DIR), options.get("fs"))
        return decision
    return start(App(cfg, hw, start_guard, **options))
