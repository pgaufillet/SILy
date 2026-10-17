"""Test-only cores driving the simulator through the real core protocol.

Frames used here carry their link source in the first byte (`source_of`).
"""

from mesh.core import Core
from mesh.platform import randbelow
from mesh.radio import CHANNEL, RX, SENSE_ACTION, TX, TX_DONE


def source_of(frame):
    """Link source of a test frame (first byte), None if empty."""
    return frame[0] if frame else None


class ScriptCore(Core):
    """Emits scripted actions at scripted times and records every event.

    script: list of (time, action), in time order. events: list of
    (time, event) received.
    """

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.events = []

    def handle(self, now, event):
        if event is not None:
            self.events.append((now, event))
        while self.script and self.script[0][0] <= now:
            self.emit(self.script.pop(0)[1])
        self.set_deadline(self.script[0][0] if self.script else None)

    def received(self):
        """Return [(time, frame, rssi, snr)] of the RX events."""
        return [(t, e[1], e[2], e[3]) for t, e in self.events if e[0] == RX]

    def of_kind(self, kind):
        """Return [(time, event)] of the events of kind."""
        return [(t, e) for t, e in self.events if e[0] == kind]


class BeaconCore(Core):
    """Sends a frame every period + random jitter, with carrier sense.

    Frame: [address, sequence & 0xFF] + padding to length bytes. When the
    channel is busy, waits a random number of slots in [1, cw] and senses
    again. received: [(time, source, sequence, rssi, snr)].
    """

    def __init__(self, address, rng, period, jitter, slot, cw=4, length=12):
        super().__init__()
        self.address = address
        self.rng = rng
        self.period = period
        self.jitter = jitter
        self.slot = slot
        self.cw = cw
        self.length = length
        self.seq = 0
        self.sent = 0
        self.backoffs = 0
        self.received = []
        self._state = "start"

    def handle(self, now, event):
        if event is None:
            if self._state == "start":
                self._state = "wait"
                self.set_deadline(now + randbelow(self.rng, self.period))
            elif self._state == "wait" and now >= self._deadline:
                self._sense()
        elif event[0] == CHANNEL:
            if event[1]:
                self.backoffs += 1
                self._state = "wait"
                self.set_deadline(now + self.slot * (1 + randbelow(self.rng, self.cw)))
            else:
                frame = bytearray(self.length)
                frame[0] = self.address
                frame[1] = self.seq & 0xFF
                self.emit((TX, bytes(frame)))
                self._state = "tx"
        elif event[0] == TX_DONE:
            self.sent += 1
            self.seq += 1
            self._state = "wait"
            self.set_deadline(now + self.period + randbelow(self.rng, self.jitter))
        elif event[0] == RX:
            frame = event[1]
            self.received.append((now, frame[0], frame[1], event[2], event[3]))

    def _sense(self):
        self.emit(SENSE_ACTION)
        self._state = "sensing"
        self.set_deadline(None)
