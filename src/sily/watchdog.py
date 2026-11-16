"""The hardware watchdog (board only): a hang resets the box.

``machine.WDT``, the ESP-IDF task watchdog: once armed it cannot be
disarmed and it survives a soft reset; only a hardware reset (power,
``machine.reset``, its own) clears it. A box it resets boots with the reset
cause ``watchdog`` in its ``boot`` record, counted as a crash by the start
guard (``sily.guard``: safe mode after repeated hangs). Who feeds it:

- the application (``sily.app``), which arms it at its start (`arm`; never
  in safe mode nor in a boot skipped on request), from a task of its
  asyncio loop every `FEED_MS`: a loop that stops turning (a task that
  never yields, a call blocked in C, a frozen chip) resets the box after
  `TIMEOUT_MS`;
- once the application stops (Ctrl-C, ``app.close``, a crash and its
  pause), hardware timer `TIMER` every `FEED_MS` (`idle`): its callback
  runs between bytecodes and at the idle REPL, so that the REPL and the
  host tools stay usable while a hang in C still resets the box;
- after a soft reset, which deletes that timer but leaves the watchdog
  armed, ``boot.py`` (`at_boot`): `idle` again, at once; it arms the
  watchdog first when no application did since the last hardware reset,
  which leaves the board as after a stop (armed, fed by the timer).

The timer is started once per interpreter and never stopped (a hardware
timer initialised again after ``deinit`` logs ESP-IDF errors): `arm` and
`idle` switch its callback's feed off and on.

`TIMEOUT_MS`: far above the longest legitimate stall of the loop (a
journal write up to about 310 ms, a display refresh 100 ms, a web server
stretch 73 ms, a garbage collection 25 ms) and above the start before the
first feed of the loop (journal, radio, boot record, display, access
point: a few seconds at most). A false reset costs a reboot and the
mesh's reattachment, and repeated ones safe mode; a hang without reset
loses the box for the rest of an outing: some seconds more before the
reset cost little.
"""

import machine

TIMEOUT_MS = 20000
FEED_MS = 1000
TIMER = 1  # hardware timer of the feed outside the application (0: sily.hw's blink)

_wdt = None
_timer = None
_idle = False  # the timer's callback feeds


def _tick(timer):
    if _idle:
        _wdt.feed()


def arm(timeout_ms=TIMEOUT_MS):
    """Arm the watchdog for timeout_ms and feed it, the timer's feed off;
    return it (``feed``)."""
    global _wdt, _idle
    _idle = False
    _wdt = machine.WDT(0, timeout_ms)
    _wdt.feed()
    return _wdt


def idle():
    """Feed the watchdog from the timer every `FEED_MS` until the next `arm`
    (armed for `TIMEOUT_MS` first when it was not, in this interpreter)."""
    global _wdt, _timer, _idle
    if _wdt is None:
        _wdt = machine.WDT(0, TIMEOUT_MS)
    _wdt.feed()
    _idle = True
    if _timer is None:
        _timer = machine.Timer(TIMER)
        _timer.init(mode=machine.Timer.PERIODIC, period=FEED_MS, callback=_tick)


def at_boot():
    """At every boot (``boot.py``): after a soft reset, `idle`."""
    if machine.reset_cause() == machine.SOFT_RESET:
        idle()
