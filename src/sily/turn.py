"""Turnaround of the gateway's immediate replies, timed while the box runs.

`Turn` sits between the tap (``sily.tap``) and the runner, on the gateway
only: the root of the mesh, whose immediate replies (reply window,
MESH_DESIGN §8) must go on air within the window of the node they answer.
It passes every event and action on unchanged and times each reply from
the radio adapter's timestamps (µs, ``board.sx1262.Sx1262``): from the
RxDone IRQ of a frame addressed to the root (``_irq_us`` when the frame
is reported) to the start (SetTx, ``_tx_us``) of the reply, the first
frame sent to that frame's source, or to every neighbour (a reply that
carries the acknowledgements of several sources), once the link engine
has counted a reply (``replies``). Frames to other destinations leave the
reply pending; a frame to the root left unanswered gives way to the next.

Its cost on the root's reply path: for a frame to the root, in the
adapter's reception callback, its destination and source, the IRQ
timestamp and the reply counter read; in the mesh's step, for each frame
sent while a reply is pending, its destination and the counter read and,
for the reply, once started, the SetTx timestamp read and one sample
stored. At most `SIZE` samples are kept between two minute records:
further replies count in the number and the maximum only. `Turn.minute`,
called by the minute record outside the step, sorts them.

The bench console's wrapper (``sily.bench``, ``timed``) times the same
interval, with more work per reply inside the step (the RxDone service
delay, collection and storage flags, the frame counter, a node's side) for
its ``turn`` report; it matches the reply by its source only, the bench
having one node. The two stay apart so that the boxes pay only the above.
"""

from array import array
from time import ticks_diff

from mesh.frame import BROADCAST, DEST_OFFSET, HEADER_SIZE, ROOT
from mesh.radio import RX, Radio
from sily.radio import link_source

SIZE = 256  # samples kept between two minute records


def _dest(data):
    return data[DEST_OFFSET] | data[DEST_OFFSET + 1] << 8


def _rank(values, percent):
    """Return the nearest-rank percentile of sorted values (as ``sily.traffic``)."""
    return values[(percent * len(values) + 99) // 100 - 1]


def _tenths(us):
    """Return µs in 0.1 ms, rounded."""
    return (us + 50) // 100


class Turn(Radio):
    """Wraps radio (a `mesh.radio.Radio`) of the root (module documentation).

    adapter: has ``_irq_us`` and ``_tx_us`` (``board.sx1262.Sx1262``);
    counters: the link engine's (``replies``); size: the samples kept.
    ``n``: the replies timed since the last `minute`; ``last``: the
    figures `minute` returned last.
    """

    def __init__(self, radio, adapter, counters, size=SIZE):
        self.radio = radio
        self.adapter = adapter
        self.counters = counters
        self.size = size
        self.samples = array("i", [0] * size)  # µs, the first size since the last minute
        self.n = 0
        self.top = 0  # the largest since the last minute (µs)
        self.last = None
        self._pending = False
        self._irq = 0
        self._source = None
        self._replies = 0
        radio.sink = self._on_event

    def transmit(self, frame):
        self.radio.transmit(frame)
        if self._pending:
            self._sent(frame)

    def sense(self):
        self.radio.sense()

    def _on_event(self, event):
        if event[0] == RX:
            self._received(event[1])
        self.sink(event)

    def _received(self, data):
        if len(data) < HEADER_SIZE or _dest(data) != ROOT:
            return
        self._irq = self.adapter._irq_us
        self._source = link_source(data)
        self._replies = self.counters.replies
        self._pending = True

    def _sent(self, frame):
        dest = _dest(frame)
        if (dest != self._source and dest != BROADCAST) or self.counters.replies == self._replies:
            return
        us = ticks_diff(self.adapter._tx_us, self._irq)
        self._pending = False
        i = self.n
        if i < self.size:
            self.samples[i] = us
        if i == 0 or us > self.top:
            self.top = us
        self.n = i + 1

    def minute(self):
        """Return [replies, median, 90th percentile, maximum] of the
        turnarounds since the last call (0.1 ms, nearest rank; the median
        and percentile of the first `size` when more came), None without
        one; start the next period."""
        n = self.n
        if not n:
            self.last = None
            return None
        kept = sorted(self.samples[: min(n, self.size)])
        self.n = 0
        self.last = [n, _tenths(_rank(kept, 50)), _tenths(_rank(kept, 90)), _tenths(self.top)]
        return self.last

    def figures(self):
        """Return the last `minute`'s figures for ``App.status`` (ms; None without a reply)."""
        last = self.last
        if last is None:
            return {"n": 0, "median_ms": None, "p90_ms": None, "max_ms": None}
        return {
            "n": last[0],
            "median_ms": last[1] / 10,
            "p90_ms": last[2] / 10,
            "max_ms": last[3] / 10,
        }
