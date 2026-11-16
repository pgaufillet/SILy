"""The hardware watchdog (sily.watchdog) on a fake ``machine`` module.

Each test starts from a fresh interpreter's state (nothing armed, no
timer), as after a hardware reset or a soft reset.
"""

import unittest

from sily import watchdog

import run

BOOT_PY = run._dirname(__file__) + "/../src/boot.py"


class Machine:
    """What sily.watchdog uses of ``machine``, recorded: the ``WDT`` calls
    (id, timeout_ms) and feeds of the one watchdog, the timers made."""

    PWRON_RESET = 1
    HARD_RESET = 2
    WDT_RESET = 3
    SOFT_RESET = 5

    def __init__(self, cause=HARD_RESET):
        self.cause = cause
        self.wdts = []
        self.feeds = 0
        self.timers = []
        self.Timer = _timer_class(self)

    def WDT(self, id, timeout):
        self.wdts.append((id, timeout))
        return self

    def feed(self):
        self.feeds += 1

    def reset_cause(self):
        return self.cause


def _timer_class(machine):
    class Timer:
        PERIODIC = 1

        def __init__(self, id):
            self.id = id
            self.inits = []  # (mode, period) of each init
            self.deinits = 0
            self.callback = None
            machine.timers.append(self)

        def init(self, mode, period, callback):
            self.inits.append((mode, period))
            self.callback = callback

        def deinit(self):
            self.deinits += 1

        def fire(self):
            self.callback(self)

    return Timer


class Feeds(unittest.TestCase):
    def setUp(self):
        self.real = watchdog.machine
        self._fresh(Machine())

    def tearDown(self):
        watchdog.machine = self.real
        self._fresh(self.real)

    def _fresh(self, machine):
        watchdog.machine = self.m = machine
        watchdog._wdt = None
        watchdog._timer = None
        watchdog._idle = False

    def test_arm(self):
        w = watchdog.arm()
        self.assertEqual(self.m.wdts, [(0, watchdog.TIMEOUT_MS)])
        self.assertEqual(self.m.feeds, 1)
        w.feed()
        self.assertEqual(self.m.feeds, 2)
        self.assertEqual(self.m.timers, [])  # the application feeds it

    def test_idle_then_arm_switch_the_timer_feed(self):
        watchdog.arm()
        watchdog.idle()  # the application stopped
        self.assertEqual(len(self.m.timers), 1)
        timer = self.m.timers[0]
        self.assertEqual(timer.id, watchdog.TIMER)
        self.assertEqual(timer.inits, [(timer.PERIODIC, watchdog.FEED_MS)])
        fed = self.m.feeds
        timer.fire()
        timer.fire()
        self.assertEqual(self.m.feeds, fed + 2)
        watchdog.arm()  # started again from the REPL
        fed = self.m.feeds
        timer.fire()
        self.assertEqual(self.m.feeds, fed)  # its loop feeds it, not the timer
        watchdog.idle()
        fed = self.m.feeds
        timer.fire()
        self.assertEqual(self.m.feeds, fed + 1)
        # One timer, initialised once and never stopped.
        self.assertEqual((len(self.m.timers), len(timer.inits), timer.deinits), (1, 1, 0))
        self.assertEqual(self.m.wdts, [(0, watchdog.TIMEOUT_MS)] * 2)

    def test_soft_reset_feeds_it_again(self):
        self._fresh(Machine(Machine.SOFT_RESET))
        watchdog.at_boot()
        self.assertEqual(self.m.wdts, [(0, watchdog.TIMEOUT_MS)])
        self.assertEqual(len(self.m.timers), 1)
        fed = self.m.feeds
        self.m.timers[0].fire()
        self.assertEqual(self.m.feeds, fed + 1)

    def test_hardware_reset_leaves_it_alone(self):
        for cause in (Machine.PWRON_RESET, Machine.HARD_RESET, Machine.WDT_RESET):
            self._fresh(Machine(cause))
            watchdog.at_boot()
            self.assertEqual((self.m.wdts, self.m.timers), ([], []), cause)

    def test_timeout(self):
        # Above the loop's longest stall (a journal write, about 310 ms) and
        # its start before the first feed (a few seconds), with many feeds
        # within it; at most the half minute a hang may cost.
        self.assertTrue(10000 <= watchdog.TIMEOUT_MS <= 30000, watchdog.TIMEOUT_MS)
        self.assertTrue(watchdog.TIMEOUT_MS >= 10 * watchdog.FEED_MS)


class BootFile(unittest.TestCase):
    """``boot.py`` calls `watchdog.at_boot` and never raises: MicroPython
    does not run main.py after a boot.py that raised."""

    def setUp(self):
        self.saved = watchdog.at_boot
        with open(BOOT_PY) as f:
            self.source = f.read()

    def tearDown(self):
        watchdog.at_boot = self.saved

    def _run(self):
        printed = []
        exec(self.source, {"print": lambda *items: printed.append(items)})
        return printed

    def test_calls_at_boot(self):
        calls = []
        watchdog.at_boot = lambda: calls.append(1)
        self.assertEqual(self._run(), [])
        self.assertEqual(calls, [1])

    def test_failure_printed_not_raised(self):
        def fail():
            raise OSError(5)

        watchdog.at_boot = fail
        self.assertEqual(self._run(), [("SILY boot.py:", repr(OSError(5)))])
