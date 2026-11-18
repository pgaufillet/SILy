"""Lateness of the asyncio loop, measured by a task that sleeps and wakes.

Runs on the board and on the unix port (``time.ticks_*``). Everything of a
box runs on one cooperative loop (``sily.app``): the mesh's steps and
radio handling, the journal's writes and syncs, the display, the web
server, garbage collections. While one of them holds the loop, the radio
is not served: a frame waits, a reply misses its slot (the root answers a
relaying child within one backoff slot, 52 ms at SF7, with a turnaround
of 21 ms median on the bench, docs/measurements.md). `Probe.run` sleeps
`PERIOD_MS` and, at each wake, measures its lateness: the time of the
wake minus the time it asked for, in µs whatever the period
(``ticks_us``, ``ticks_diff``: wrap-safe). The minute record carries its
figures (``lp``, ``sily.record``).

The probe complements the figures of the parts (``jw`` and ``js`` the
journal's, ``ow`` and ``or`` the display's, ``web``, ``tr``): they tell
which part held the loop, the probe what the loop as a whole cost the
radio, whoever held it. It reads and changes none of them; a stall shows
in it and in the figure of the part that made it, two views of the same
time, never added.

What a lateness means. A stall of S ms that covers a wake's due time
shows as a lateness between S - `PERIOD_MS` and S (the probe slept
through part of it: the rest of its sleep when the stall began), plus the
turns of the tasks due before the probe; a stall that ends before the due
time does not show. The longest lateness of a minute hence understates
its longest stall by less than a period. On the board, a wake of the idle
loop comes at the next FreeRTOS tick (100 Hz in the firmware's ESP-IDF
configuration: the idle loop waits in ``poll`` one tick at a time unless
an interrupt wakes it, as the radio's does): a lateness up to 10 ms is no
stall. ``asyncio`` counts its sleeps in whole ms: a wake may also come up
to 1 ms before the due time in µs, a negative lateness, counted as a wake
and nothing else.

The bound, `BOUND_MS`: the lateness a reply can absorb. A frame that
arrives as a stall starts is served when it ends; its reply still goes
within the slot if the stall lasts at most the slot minus the turnaround,
52 - 21 = 31 ms at SF7 (the slots are longer at SF8 and SF9: SF7's bound
is the strictest), rounded down to 30 ms: three ticks, so that the idle
tick alone never crosses it, and above a garbage collection (20 to 26 ms
on the board). A wake later than the bound counts ``over`` (one per such
stall: the probe wakes once when it ends), and its lateness beyond the
bound adds to the deaf time: for a stall, about the time during which a
frame that arrived would have its reply too late (understated by the
probe's sleep, as above).

The period, `PERIOD_MS`: each wake costs the loop the scheduler's turn
(the task queue, two ``poll`` calls, the coroutine resumed) and the few
lines of `Probe.woke`, about 3.5 µs on the unix port of a laptop. The
board runs such interpreted code 45 to 68 times slower (frame decode,
seal and open: 0.22, 1.20 and 1.34 ms on the board, docs/measurements.md,
4.9, 17.7 and 20.4 µs on the port; 51 to 109 times for PBKDF2, which
allocates), hence about 160 to 240 µs per wake on the board. At 20 ms, a
multiple of the tick (the wakes come every 20 or 30 ms), the probe costs
0.5 to 1.2 % of the CPU (2 % by the PBKDF2 ratio), and a reply's
turnaround about 0.2 ms when a wake falls in it. A shorter period buys
little: the stalls that matter are longer than the bound, and the idle
loop wakes on ticks anyway. At 1 ms the probe would wake at every tick
at least (100 per second, 1.5 to 2.5 %) and at each turn of a busy loop,
distorting what it measures.

Per wake (`Probe.woke`): small integers only, no list, no allocation (the
ticks, the lateness, counters; the deaf time summed in µs over the window
of one minute record stays below the board's 2^30 for windows up to about
17 minutes, ``journal.minute_s`` defaulting to 60 s), the application's
clock read only at a new longest lateness, nothing written: `Probe.minute`,
called by the minute record, returns the figures and starts the next
window.

On every box, gateway and nodes: ``sily.app`` starts the task with the
mesh's tasks (before them the radio is not served, and the start's own
work, journal, boot record, display, access point, is no stall of the
running mesh); never in safe mode, where the application does not run.
"""

import asyncio
from time import ticks_add, ticks_diff, ticks_us

PERIOD_MS = 20  # sleep between two wakes
BOUND_MS = 30  # lateness a reply absorbs: SF7's slot (52 ms) minus the turnaround (21 ms)


class Probe:
    """The loop's lateness (module documentation).

    now: the application's clock (ms, ``mesh.runner.Clock.now``), read at
    each new longest lateness; period_ms, bound_ms: as `PERIOD_MS` and
    `BOUND_MS`; ticks_us, ticks_diff, ticks_add: the µs clock. Since the
    last `minute`: ``wakes``, ``top_us`` the longest lateness and ``top_t``
    the time of its wake (None while no wake was late), ``over`` the wakes
    later than the bound and ``deaf_us`` their lateness beyond it; since
    the start: ``over_total`` and ``deaf_total_ms``.
    """

    def __init__(
        self,
        now,
        period_ms=PERIOD_MS,
        bound_ms=BOUND_MS,
        ticks_us=ticks_us,
        ticks_diff=ticks_diff,
        ticks_add=ticks_add,
    ):
        self.now = now
        self.period_ms = period_ms
        self.bound_us = bound_ms * 1000
        self.ticks_us = ticks_us
        self.ticks_diff = ticks_diff
        self.ticks_add = ticks_add
        self.wakes = 0
        self.top_us = 0
        self.top_t = None
        self.over = 0
        self.deaf_us = 0
        self.over_total = 0
        self.deaf_total_ms = 0

    async def run(self):
        """Sleep the period and count the lateness of each wake, forever."""
        ticks = self.ticks_us
        add = self.ticks_add
        diff = self.ticks_diff
        sleep_ms = asyncio.sleep_ms
        period = self.period_ms
        period_us = period * 1000
        while True:
            due = add(ticks(), period_us)
            await sleep_ms(period)
            self.woke(diff(ticks(), due))

    def woke(self, late):
        """Count a wake late µs after its due time (negative: early)."""
        self.wakes += 1
        if late > self.top_us:
            self.top_us = late
            self.top_t = self.now()
        if late > self.bound_us:
            self.over += 1
            self.deaf_us += late - self.bound_us

    def minute(self):
        """Return [wakes, longest lateness ms, t of its wake (None: no wake
        late), wakes over the bound, deaf ms] since the last call, then
        [wakes over the bound, deaf ms] since the start (ms rounded up; the
        totals sum the minutes' figures); start the next window."""
        deaf = (self.deaf_us + 999) // 1000
        self.over_total += self.over
        self.deaf_total_ms += deaf
        out = [
            self.wakes,
            (self.top_us + 999) // 1000,
            self.top_t,
            self.over,
            deaf,
            self.over_total,
            self.deaf_total_ms,
        ]
        self.wakes = 0
        self.top_us = 0
        self.top_t = None
        self.over = 0
        self.deaf_us = 0
        return out
