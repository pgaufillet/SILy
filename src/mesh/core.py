"""Core <-> runner protocol: an event-driven core without I/O.

A **core** holds protocol state and logic only: it never sleeps, never reads
a clock, never touches hardware. A **runner** owns time and I/O: on the board
an asyncio task driving the radio adapter, on the host a discrete-event
simulator driving many cores on a scripted medium in simulated time.

Protocol:

- ``actions = core.step(now, event)``: ``now`` is the current time in
  monotonic integer milliseconds, never wrapping and never decreasing from one
  call to the next (the runner converts its ticks). ``event`` is a radio event
  (`mesh.radio`) or None, meaning only that time has passed: the core runs
  whatever is due at ``now``. ``step`` returns the actions to execute, in
  order (radio actions through `mesh.radio.execute`); an empty sequence if
  none. The runner must not keep or modify the returned sequence.
- ``core.deadline()``: the time at which the core wants ``step(now, None)``
  next, or None if it waits for events only. Read after every ``step``. The
  runner calls ``step`` with ``now >= deadline`` as soon as it can; an earlier
  or extra call is harmless, a late one only delays the core.
- The runner calls ``step(now, None)`` once at start, before any event.
- Persistent storage, retained memory and the random source are injected
  objects with synchronous calls (`mesh.platform`), used inside ``step``.

`Core` is a base class implementing the protocol's bookkeeping; subclasses
implement ``handle``.
"""

_NO_ACTION = ()


class Core:
    """Base of a core: checks time, collects actions, keeps the deadline."""

    def __init__(self):
        self.now = None
        self._actions = []
        self._deadline = None

    def step(self, now, event):
        """Process event (or the passing of time) at now; return the actions."""
        if self.now is not None and now < self.now:
            raise ValueError("time went backwards")
        self.now = now
        self.handle(now, event)
        actions = self._actions
        if not actions:
            return _NO_ACTION
        self._actions = []
        return actions

    def deadline(self):
        """Return the time of the next wanted step, or None."""
        return self._deadline

    def emit(self, action):
        """Append an action to those returned by the current step."""
        self._actions.append(action)

    def set_deadline(self, time):
        """Set the time of the next wanted step (None: no timer)."""
        self._deadline = time

    def handle(self, now, event):
        """Subclass hook: process event (None: time passed) at now."""
        raise NotImplementedError
