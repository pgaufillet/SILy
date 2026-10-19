"""Discrete-event scheduler: simulated time in integer milliseconds."""

import gc
import heapq

# Bytes allocated between garbage collections while the scheduler runs. The
# unix port's allocator looks for room for a multi-block object from the
# lowest free block on, which only single-block allocations move forward:
# without a threshold, the heap is collected only when full, and with a large
# heap (32 MB in `make test`) each such allocation scans most of what was
# allocated since the last collection. A run then spends most of its time in
# the allocator. Collecting every megabyte keeps the scans short; when the
# heap is collected does not change what the simulation computes.
GC_THRESHOLD = 1 << 20


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
        never lets time advance); exceeding it raises RuntimeError. The
        garbage collector's threshold is `GC_THRESHOLD` during the run.
        """
        queue = self._queue
        count = 0
        threshold = gc.threshold()
        gc.threshold(GC_THRESHOLD)
        try:
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
        finally:
            gc.threshold(threshold)
        if until > self.now:
            self.now = until
