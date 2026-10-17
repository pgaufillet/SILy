"""Radio contract between the mesh core and a radio adapter (MESH_DESIGN §1).

The core never touches the radio: it emits **actions**, the runner hands them
to the adapter with `execute`, and the adapter reports **events**, which the
runner feeds back to the core (protocol in `mesh.core`). The same contract is
implemented by the SX1262 adapter on the board and by the scripted medium on
the host.

Actions and events are tuples whose first item is a kind; actions and events
use disjoint kinds, so that a mix-up is detected.

Actions (core -> adapter):

- ``(TX, frame)``: transmit ``frame`` (bytes, 1 to 255 bytes) now. The adapter
  leaves reception, transmits, returns to reception and reports ``TX_DONE``.
  The core issues no other action to the radio (``TX`` or ``SENSE``) before
  that ``TX_DONE``: a board transmits one frame at a time (§7).
- ``(SENSE,)``: carrier sense (§8). The adapter samples the channel and
  reports ``CHANNEL``; busy if a frame is being received (preamble detected or
  header valid, without RxDone yet) or if the instantaneous RSSI is above the
  noise floor + 10 dB. ``SENSE_ACTION`` is a preallocated instance.

The radio receives whenever it does not transmit (nodes always listen, no
sleep scheduling in v1): there is no action to start receiving.

Events (adapter -> core):

- ``(RX, frame, rssi, snr)``: a frame was received with a valid LoRa CRC;
  ``rssi`` in dBm and ``snr`` in dB (numbers) are the link measurements.
- ``(RX_ERROR, rssi, snr)``: a frame was received with a bad CRC (counted by
  the core, §13; option A6 compares this with authentication failures).
- ``(TX_DONE, duration_ms)``: the transmission requested by ``TX`` is over;
  ``duration_ms`` is the measured time on air (TX start to TX-done IRQ), which
  the duty-cycle accounting uses when longer than the computed one (§6).
- ``(CHANNEL, busy)``: result of ``SENSE``; ``busy`` is a bool.

Backoff (§8) needs no radio support: the core waits through its own deadline
(`mesh.core`) and senses again.
"""

# Actions, core -> adapter.
TX = 1
SENSE = 2

# Events, adapter -> core.
RX = 16
RX_ERROR = 17
TX_DONE = 18
CHANNEL = 19

SENSE_ACTION = (SENSE,)

MAX_FRAME = 255


class Radio:
    """Radio adapter interface (documentation base, no behaviour).

    ``sink`` is set by the runner to a callable taking one event; the adapter
    calls it for every event, in order. On the board it is called from the
    runner's task, never from an interrupt handler.
    """

    sink = None

    def transmit(self, frame):
        """Start transmitting frame; report ``(TX_DONE, duration_ms)``."""
        raise NotImplementedError

    def sense(self):
        """Start a carrier sense; report ``(CHANNEL, busy)``."""
        raise NotImplementedError


def execute(radio, action):
    """Hand a radio action to radio; return False if action is not a radio one.

    Runners call it for each action returned by a core, in order, and handle
    the kinds it does not know (none yet).
    """
    kind = action[0]
    if kind == TX:
        radio.transmit(action[1])
    elif kind == SENSE:
        radio.sense()
    else:
        return False
    return True


class NeighbourFilter(Radio):
    """Radio wrapper ignoring frames from link sources outside a set (§1).

    Debug configuration: builds multi-hop topologies with real boards on one
    bench. Wraps any `Radio` and is itself a `Radio`: actions pass through,
    events pass through except ``RX`` events whose link source is not in
    ``allowed`` (a set, which may be changed at run time). ``source_of(frame)``
    returns the link source of a frame, or None if it cannot be read (the frame
    format belongs to the codec, so it is injected); such frames are ignored
    too. ``filtered`` counts the ignored frames.
    """

    def __init__(self, radio, allowed, source_of):
        self.radio = radio
        self.allowed = allowed
        self.source_of = source_of
        self.filtered = 0
        radio.sink = self._on_event

    def transmit(self, frame):
        self.radio.transmit(frame)

    def sense(self):
        self.radio.sense()

    def _on_event(self, event):
        if event[0] == RX:
            source = self.source_of(event[1])
            if source is None or source not in self.allowed:
                self.filtered += 1
                return
        self.sink(event)
