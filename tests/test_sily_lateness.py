"""The loop's lateness probe (sily.lateness): counting, figures, the task.

`Probe.woke` is driven with given lateness values and a scripted
application clock; `Probe.run` with a scripted µs clock, under the unix
port's asyncio: its lateness depends on the script only, not on the real
sleeps. The wiring in the application is in ``test_sily_app``.
"""

import asyncio
import gc
import unittest
from time import ticks_add

from mesh.runner import Clock
from sily.lateness import BOUND_MS, PERIOD_MS, Probe

BOUND_US = BOUND_MS * 1000


class Now:
    """The application's clock: a time set by the test, its reads counted."""

    def __init__(self, t=0):
        self.t = t
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.t


class Ticks:
    """A µs clock returning the values of a script, then its last one."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def read(self):
        i = self.calls
        self.calls = i + 1
        return self.values[min(i, len(self.values) - 1)]


class Counting(unittest.TestCase):
    def setUp(self):
        self.now = Now(5000)
        self.probe = Probe(self.now.read)

    def test_defaults(self):
        self.assertEqual((self.probe.period_ms, self.probe.bound_us), (PERIOD_MS, BOUND_US))
        self.assertTrue(PERIOD_MS < BOUND_MS)  # a stall of twice the bound always over it

    def test_longest_lateness_and_the_time_of_its_wake(self):
        p = self.probe
        p.woke(800)
        self.now.t = 6000
        p.woke(12500)
        self.now.t = 7000
        p.woke(4000)  # shorter: the clock not read
        p.woke(12500)  # as long: the first one kept
        self.assertEqual((p.wakes, p.top_us, p.top_t), (4, 12500, 6000))
        self.assertEqual(self.now.reads, 2)

    def test_wakes_later_than_the_bound_and_the_deaf_time(self):
        p = self.probe
        for late in (BOUND_US, BOUND_US + 1, 100, BOUND_US + 45000):
            p.woke(late)
        self.assertEqual((p.over, p.deaf_us), (2, 45001))  # the bound itself not over

    def test_early_wake_counted_alone(self):
        p = self.probe
        p.woke(-700)  # asyncio's whole ms: before the due time in µs
        self.assertEqual((p.wakes, p.top_us, p.top_t, p.over, p.deaf_us), (1, 0, None, 0, 0))
        self.assertEqual(self.now.reads, 0)

    def test_minute_figures_then_the_next_window(self):
        p = self.probe
        self.now.t = 6000
        p.woke(BOUND_US + 1500)
        self.now.t = 6500
        p.woke(BOUND_US + 200000)
        p.woke(3000)
        # ms rounded up: 230.0 ms longest, 201.5 ms deaf.
        self.assertEqual(p.minute(), [3, 230, 6500, 2, 202, 2, 202])
        self.now.t = 9000
        p.woke(BOUND_US + 1)
        self.assertEqual(p.minute(), [1, 31, 9000, 1, 1, 3, 203])
        self.assertEqual(p.minute(), [0, 0, None, 0, 0, 3, 203])  # no wake

    def test_no_allocation_per_wake(self):
        # The application's clock, read at each new longest lateness: every
        # wake below is one, from early to over the bound.
        p = Probe(Clock().now)
        gc.collect()
        gc.disable()
        try:
            before = gc.mem_alloc()
            for late in range(-2000, 100000, 50):
                p.woke(late)
            after = gc.mem_alloc()
        finally:
            gc.enable()
        self.assertEqual(after, before)
        self.assertEqual((p.wakes, p.top_us), (2040, 99950))
        self.assertTrue(p.over > 1000, p.over)


class Running(unittest.TestCase):
    def _run(self, script, wakes, period_ms=1):
        """Run the probe on the scripted µs clock until it woke wakes times;
        return it and the application's clock."""
        now = Now(100)
        ticks = Ticks(script)
        probe = Probe(now.read, period_ms=period_ms, ticks_us=ticks.read)

        async def main():
            task = asyncio.create_task(probe.run())
            while probe.wakes < wakes:
                await asyncio.sleep_ms(0)
                now.t += 1
            task.cancel()

        asyncio.run(main())
        asyncio.new_event_loop()
        return probe, now

    def test_lateness_of_each_wake_across_the_wrap(self):
        top = ticks_add(0, -1)  # the last value of the port's ticks
        before = top - 400  # the due time wraps: 599
        script = [
            before,
            ticks_add(before, 1000 + 250),  # 0.25 ms late
            900,
            900 + 1000 + 45000,  # a 45 ms stall: 15 ms beyond the bound
            47000,
            47000 + 1000 - 600,  # early
        ]
        probe, now = self._run(script, 3)
        figures = (probe.wakes, probe.top_us, probe.over, probe.deaf_us)
        self.assertEqual(figures, (3, 45000, 1, 15000))
        t = probe.top_t
        self.assertTrue(100 < t < now.t, (t, now.t))  # read at that wake
        self.assertEqual(probe.minute(), [3, 45, t, 1, 15, 1, 15])

    def test_the_due_time_is_one_period_after_the_sleep(self):
        script = [1000, 1000 + 20000 + 40000]  # period 20 ms, 40 ms late
        probe, _ = self._run(script, 1, period_ms=20)
        self.assertEqual((probe.top_us, probe.over, probe.deaf_us), (40000, 1, 10000))


if __name__ == "__main__":
    unittest.main()
