"""Carrier sense bookkeeping of a LoRa receiver (MESH_DESIGN §8), pure logic.

The SX1262 adapter (`board.sx1262`) feeds these objects with what the radio
reports; they hold no hardware and run on the host for tests.

- `Reception`: a frame is being received from its PreambleDetected or
  HeaderValid IRQ until RxDone, a header error or a transmission. Each IRQ
  opens a bounded window, so that a lost end (an IRQ the receiver never
  raises, e.g. a false preamble detection) cannot keep the channel busy: after
  a preamble the header must follow within the preamble and header symbols,
  after a header the frame lasts at most the time on air of a maximum frame.
- `NoiseFloor`: median of recent instantaneous RSSI samples taken while
  nothing is being received; a sample above the floor + `THRESHOLD_DB` is
  energy on the channel, not noise, and is left out, unless such samples
  persist (`ADAPT_AFTER` in a row): the environment changed, the estimate
  starts again from the last sample.

Times are ``time.ticks_ms`` values, compared with ``ticks_diff``.
"""

from time import ticks_add, ticks_diff

THRESHOLD_DB = 10  # busy above the noise floor + 10 dB (§8)
WINDOW = 15  # samples of the noise floor median
ADAPT_AFTER = 16  # consecutive samples above the threshold that move the floor


class Reception:
    """Whether a frame is being received, from the receiver's IRQs.

    preamble_ms: longest time from PreambleDetected to HeaderValid;
    frame_ms: longest time from HeaderValid to RxDone. ``stale`` counts the
    windows that expired without an end (lost or false detections).
    """

    def __init__(self, preamble_ms, frame_ms):
        self.preamble_ms = preamble_ms
        self.frame_ms = frame_ms
        self.stale = 0
        self._until = None

    def preamble(self, now):
        """PreambleDetected at now: a header should follow."""
        self._until = ticks_add(now, self.preamble_ms)

    def header(self, now):
        """HeaderValid at now: the frame lasts at most frame_ms."""
        self._until = ticks_add(now, self.frame_ms)

    def end(self):
        """RxDone, header error, or the receiver left reception."""
        self._until = None

    def active(self, now):
        """Return True if a frame is being received at now."""
        until = self._until
        if until is None:
            return False
        if ticks_diff(until, now) > 0:
            return True
        self._until = None
        self.stale += 1
        return False


class NoiseFloor:
    """Noise floor estimate (dBm) from instantaneous RSSI samples.

    ``floor`` is None until the first sample. ``rejected`` counts the samples
    left out as channel activity.
    """

    def __init__(self, window=WINDOW, threshold_db=THRESHOLD_DB, adapt_after=ADAPT_AFTER):
        self.window = window
        self.threshold_db = threshold_db
        self.adapt_after = adapt_after
        self.floor = None
        self.rejected = 0
        self._samples = []
        self._above = 0

    def calibrate(self, samples):
        """Set the floor to the median of samples (at boot), dropping the history."""
        self._samples = list(samples)[-self.window :]
        self._above = 0
        self._update()

    def add(self, rssi):
        """Take a sample taken while nothing was being received."""
        if self.floor is not None and rssi > self.floor + self.threshold_db:
            self._above += 1
            if self._above < self.adapt_after:
                self.rejected += 1
                return
            self.calibrate((rssi,))  # the environment changed: start again
            return
        self._above = 0
        samples = self._samples
        samples.append(rssi)
        if len(samples) > self.window:
            samples.pop(0)
        self._update()

    def busy(self, rssi):
        """Return True if rssi is above the floor + threshold (no floor: False)."""
        return self.floor is not None and rssi > self.floor + self.threshold_db

    def _update(self):
        s = sorted(self._samples)
        self.floor = s[len(s) // 2] if s else None
