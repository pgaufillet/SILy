"""The application of a box: boot decision, mesh node, LED, stop and crash paths.

``main.py`` calls `boot` at every boot. It prints ``SILY <version>``, then:

- the start guard (``sily.guard``) may skip this boot (one-shot flag, set
  from the REPL by ``sily.hw.skip_next_boot``): ``SILY SKIP``, back to the
  REPL;
- safe mode, when the guard counts `guard.MAX_CRASHES` crashes without
  10 minutes of stable run, or ``/config.json`` is missing or invalid
  (``sily.config``): one line ``SILY SAFE field: problem`` per reason, the
  LED blinks fast (`SAFE_BLINK_MS`) and `boot` returns: the REPL is free;
- otherwise the application runs under asyncio (`App`) until stopped.

Recovery: Ctrl-C (``mpremote`` sends one when it connects) stops the
application cleanly (radio in standby, LED off, ``SILY STOP``) and leaves
the board at the REPL; safe mode leaves it there too. ``make configure``
writes a new configuration, clears the guard and resets the board; a box
switched off and on retries after crashes (a power-on clears the count).

Any other exception (a task, the runner, the node's assembly such as a
silent SX1262) is a crash: ``SILY FATAL`` with the traceback, the crash
counted, the radio and LED stopped, then after `CRASH_PAUSE_MS` (Ctrl-C
works: the board stays at the REPL) the board resets. There is no hardware
watchdog: it would also reset a board stopped at the REPL.

`App` runs the node (``sily.stack``) and the tasks around it: the radio
adapter, a monitor polling the mesh state once a second (no application
code inside the mesh's step) and driving the LED (on while attached, the
gateway always), and the stability timer clearing the crash count. Later
parts add their tasks with `App.add_task` before the start.
"""

import asyncio
import sys

import sily
from sily import config, guard
from sily.stack import Stack

CONFIG_PATH = "/config.json"
CRASH_PAUSE_MS = 5000
MONITOR_MS = 1000
SAFE_BLINK_MS = 100

# Outcomes of `boot` and `start` (the board only sees the console lines).
STOPPED = "stopped"  # Ctrl-C or App.stop: at the REPL
RESET = "reset"  # after a crash (on the host, the injected reset returned)

current = None  # the App last started (`close`)
console = None  # file of the console lines (None: standard output; tests set one)


def _out(*items):
    if console is None:
        print(*items)
    else:
        print(*items, file=console)


def close():
    """Stop the radio and LED of the application last started (idempotent).

    For host tools that connect to a box (their Ctrl-C has already stopped
    it).
    """
    if current is not None:
        current.close()


class App:
    """The application on configuration cfg (``sily.config``) and hardware hw.

    hw: ``sily.hw`` on the board. start_guard: the `guard.Guard` whose crash
    count `stable_ms` of running clears (None: no stability timer);
    monitor_ms: period of the monitor.
    """

    def __init__(
        self, cfg, hw, start_guard=None, stable_ms=guard.STABLE_MS, monitor_ms=MONITOR_MS
    ):
        self.cfg = cfg
        self.hw = hw
        self.guard = start_guard
        self.stable_ms = stable_ms
        self.monitor_ms = monitor_ms
        self.stack = None
        self.led = None
        self.attached = False
        self.error = None  # the first exception of a task
        self._factories = []
        self._closed = False

    def add_task(self, factory):
        """Start factory(app), a coroutine, once the node is built."""
        self._factories.append(factory)

    def build(self):
        """Assemble the node from the configuration (``sily.stack``)."""
        cfg = self.cfg
        hw = self.hw
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
            presence_s=cfg["presence_s"],
        )
        if cfg["led"]:
            self.led = hw.led()
            self.led.value(0)

    async def main(self):
        """Build the node, run it and its tasks until `stop`; raise a task's exception."""
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(self._task_error)
        try:
            self.build()
            stack = self.stack
            asyncio.create_task(stack.adapter.run())
            asyncio.create_task(self._monitor())
            if self.guard is not None:
                asyncio.create_task(self._stability())
            for factory in self._factories:
                asyncio.create_task(factory(self))
            await stack.runner.run()
        finally:
            loop.set_exception_handler(None)
        if self.error is not None:
            raise self.error

    def _task_error(self, loop, context):
        if self.error is None:
            self.error = context["exception"]
        self.stop()

    def stop(self):
        """End `main` at the runner's next wake-up."""
        if self.stack is not None:
            self.stack.runner.stop()

    def poll(self):
        """Read the mesh state (once a second) and show it."""
        attached = self.stack.routing.attached
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

    def close(self):
        """Stop the node: runner, radio in standby, LED off. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self.stack is not None:
            self.stack.close()
        if self.led is not None:
            self.led.value(0)


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
    decision = start_guard.decide(hw.power_on())
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


def safe_mode(hw, reasons):
    """Print the reasons, blink the LED fast; the caller returns to the REPL."""
    for reason in reasons:
        _out("SILY SAFE", reason)
    hw.blink(SAFE_BLINK_MS)


def _stop(app, hw):
    hw.kbd_intr(-1)  # a second Ctrl-C must not cut the cleanup short
    try:
        app.close()
    finally:
        hw.kbd_intr(3)
    _out("SILY STOP")


def _crash(app, hw, start_guard, error):
    _out("SILY FATAL", repr(error))
    if console is None:
        sys.print_exception(error)
    else:
        sys.print_exception(error, console)
    n = start_guard.crashed()
    try:
        app.close()
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
        safe_mode(hw, reasons)
        return decision
    return start(App(cfg, hw, start_guard, **options))
