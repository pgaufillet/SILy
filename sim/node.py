"""Host runner: binds a mesh core to a radio in simulated time.

The counterpart of the board's asyncio runner: it implements the runner side
of the `mesh.core` protocol on a `sim.scheduler.Scheduler`.
"""

from mesh.radio import execute


class Node:
    """Runs core with radio (a `mesh.radio.Radio`, e.g. a medium port).

    Feeds the core every radio event and a ``step(now, None)`` at its
    deadline, and executes the returned actions in order. ``steps`` counts
    the calls to ``core.step``. observers: objects whose ``stepped(node,
    event)`` is called after each step (actions executed, timer set), e.g.
    `sim.trace.Recorder`; they read the core's state, never change it.
    """

    def __init__(self, scheduler, core, radio, observers=()):
        self.scheduler = scheduler
        self.core = core
        self.radio = radio
        self.observers = observers
        self.steps = 0
        self._timer = None
        self._timer_at = None
        radio.sink = self._on_event

    def start(self):
        """First step, at the scheduler's current time."""
        self._step(None)

    def wake(self):
        """Step the core now: after a call made outside ``step`` (a submission)."""
        self._step(None)

    def stop(self):
        """Detach from the radio and cancel the timer (the board goes down)."""
        if self._timer is not None:
            self.scheduler.cancel(self._timer)
        self._timer = None
        self._timer_at = None
        self.radio.sink = None

    def _on_event(self, event):
        self._step(event)

    def _on_timer(self):
        self._timer = None
        self._timer_at = None
        self._step(None)

    def _step(self, event):
        now = self.scheduler.now
        self.steps += 1
        for action in self.core.step(now, event):
            if not execute(self.radio, action):
                raise ValueError("unknown action %r" % (action,))
        deadline = self.core.deadline()
        if deadline != self._timer_at:
            if self._timer is not None:
                self.scheduler.cancel(self._timer)
                self._timer = None
            self._timer_at = deadline
            if deadline is not None:
                self._timer = self.scheduler.at(max(deadline, now), self._on_timer)
        for o in self.observers:
            o.stepped(self, event)
