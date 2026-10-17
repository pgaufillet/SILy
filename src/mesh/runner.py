"""asyncio runner of a mesh core on MicroPython (runner side of `mesh.core`).

The board counterpart of the host simulator's runner (``sim.node``): it owns
time and the radio, the core owns the protocol. It needs ``asyncio`` and
``time`` only (no ``machine``): the radio adapter is injected, any
`mesh.radio.Radio`.

- `Clock` turns ``time.ticks_ms``, which wraps, into the monotonic, never
  wrapping milliseconds of the core protocol. It must be read at least once
  per half period of the ticks (about 6 days on MicroPython); the runner
  reads it at least every `MAX_WAIT_MS`.
- `Runner` steps the core once at start, then with every radio event in
  arrival order, and with no event when its deadline is due or after `wake`
  (a submission made outside ``step``). The radio adapter calls ``sink``
  from its own task (never from an interrupt handler): events are queued and
  the runner's task steps the core, so that a step never runs inside another
  one (a carrier sense reports its result during the step that asked for it).
  Between steps the runner sleeps until the deadline or the next event.

``cpu`` holds the processor time of the steps, actions included, per event
kind (``None``: time-only steps): [count, total µs, maximum µs].
"""

import asyncio
from time import ticks_diff, ticks_ms, ticks_us

from mesh.radio import execute

MAX_WAIT_MS = 60000


class Clock:
    """Monotonic ms from a wrapping ticks function (``time.ticks_ms``).

    Starts at the first ticks value read; ``ticks_diff`` must match ticks.
    """

    def __init__(self, ticks=ticks_ms, diff=ticks_diff):
        self._ticks = ticks
        self._diff = diff
        self._last = ticks()
        self._now = self._last

    def now(self):
        """Return the current time in monotonic ms."""
        t = self._ticks()
        self._now += self._diff(t, self._last)
        self._last = t
        return self._now


class Runner:
    """Runs core with radio in an asyncio task (`run`)."""

    def __init__(self, core, radio, clock=None):
        self.core = core
        self.radio = radio
        self.clock = clock if clock is not None else Clock()
        self.steps = 0
        self.cpu = {}
        self._events = []
        self._woken = False
        self._stopped = False
        self._flag = asyncio.Event()
        radio.sink = self._post

    def _post(self, event):
        self._events.append(event)
        self._flag.set()

    def wake(self):
        """Step the core soon: after a call made outside ``step`` (a submission)."""
        self._woken = True
        self._flag.set()

    def stop(self):
        """End `run` at its next wake-up."""
        self._stopped = True
        self._flag.set()

    def _step(self, event):
        t0 = ticks_us()
        radio = self.radio
        for action in self.core.step(self.clock.now(), event):
            if not execute(radio, action):
                raise ValueError("unknown action %r" % (action,))
        self.steps += 1
        dt = ticks_diff(ticks_us(), t0)
        kind = None if event is None else event[0]
        meter = self.cpu.get(kind)
        if meter is None:
            meter = self.cpu[kind] = [0, 0, 0]
        meter[0] += 1
        meter[1] += dt
        if dt > meter[2]:
            meter[2] = dt

    async def run(self):
        """Drive the core until `stop`."""
        events = self._events
        flag = self._flag
        self._step(None)
        while not self._stopped:
            while events:
                self._step(events.pop(0))
            if self._woken:
                self._woken = False
                self._step(None)
                continue
            deadline = self.core.deadline()
            delay = MAX_WAIT_MS
            if deadline is not None:
                delay = deadline - self.clock.now()
                if delay <= 0:
                    self._step(None)
                    continue
                if delay > MAX_WAIT_MS:
                    delay = MAX_WAIT_MS
            flag.clear()
            try:
                await asyncio.wait_for_ms(flag.wait(), delay)
            except asyncio.TimeoutError:
                pass
