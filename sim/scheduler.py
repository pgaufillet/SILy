"""Discrete-event scheduler: simulated time in integer milliseconds."""

import heapq


class Scheduler:
    """Time-ordered queue of calls; equal times run in scheduling order.

    ``now`` is the simulated time: the time of the call being run, or the end
    of the last ``run``. Calls scheduled during a run at the current time run
    in the same run, after those already queued for that time.
    """

    def __init__(self, start=0):
        self.now = start
        self._queue = []
        self._seq = 0
        self.executed = 0

    def at(self, time, fn, *args):
        """Schedule fn(*args) at time (>= now); return a handle for cancel."""
        if time < self.now:
            raise ValueError("time in the past")
        entry = [time, self._seq, fn, args]
        self._seq += 1
        heapq.heappush(self._queue, entry)
        return entry

    def after(self, delay, fn, *args):
        """Schedule fn(*args) delay ms from now."""
        return self.at(self.now + delay, fn, *args)

    def cancel(self, handle):
        """Cancel a scheduled call (no effect if it already ran)."""
        handle[2] = None

    def pending(self):
        """Return the number of queued calls, cancelled ones included."""
        return len(self._queue)

    def run(self, until, limit=None):
        """Run the calls due up to until (inclusive), then set now to until.

        limit bounds the number of calls run (a guard against a core that
        never lets time advance); exceeding it raises RuntimeError.
        """
        queue = self._queue
        count = 0
        while queue and queue[0][0] <= until:
            entry = heapq.heappop(queue)
            fn = entry[2]
            if fn is None:
                continue
            self.now = entry[0]
            count += 1
            if limit is not None and count > limit:
                raise RuntimeError("event limit reached at %d ms" % self.now)
            self.executed += 1
            fn(*entry[3])
        if until > self.now:
            self.now = until
