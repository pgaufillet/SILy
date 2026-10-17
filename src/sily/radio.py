"""Binding of the board's SX1262 adapter to the mesh radio contract.

`board.sx1262.Sx1262` reports through callbacks and knows nothing of the
mesh (package isolation, ``tests/test_layering.py``); `MeshRadio` turns its
callbacks into the events of ``mesh.radio`` and its carrier sense into the
``CHANNEL`` event. `lora_config` gives the driver configuration of a regional
profile and spreading factor (MESH_DESIGN §4, §14); `link_source` reads the
link source of a raw frame for the neighbour filter (§1).
"""

from mesh.frame import SOURCE_OFFSET
from mesh.radio import CHANNEL, RX, RX_ERROR, TX_DONE, Radio

PREAMBLE = 8  # symbols (§4; the driver's default is 12), as mesh.airtime assumes
SYNC_WORD = 0x1424  # private LoRa sync word 0x12 on the SX1262 (§14)

# Bandwidth in Hz -> the driver's "bw" key.
_BANDWIDTHS = {
    7800: "7.8",
    10400: "10.4",
    15600: "15.6",
    20800: "20.8",
    31250: "31.25",
    41700: "41.7",
    62500: "62.5",
    125000: "125",
    250000: "250",
    500000: "500",
}


def lora_config(profile, sf, tx_power_dbm):
    """Return the micropython-lib ``lora_cfg`` of a profile, SF and TX power.

    Frequency, bandwidth and coding rate come from the profile; explicit
    header and CRC on are the driver's defaults. The caller validates the
    settings first (``mesh.profile.validate``). Raises ValueError for a
    bandwidth the driver does not know.
    """
    bw = _BANDWIDTHS.get(profile.bandwidth_hz)
    if bw is None:
        raise ValueError("bandwidth")
    hz = profile.frequency_hz
    # Integer kHz when exact: MicroPython floats are single precision.
    return {
        "freq_khz": hz // 1000 if hz % 1000 == 0 else hz / 1000,
        "sf": sf,
        "bw": bw,
        "coding_rate": profile.coding_rate,
        "preamble_len": PREAMBLE,
        "syncword": SYNC_WORD,
        "output_power": tx_power_dbm,
    }


def link_source(frame):
    """Return the link source of a frame (header bytes 3-4, little-endian).

    None if the frame is too short to hold it. For
    `mesh.radio.NeighbourFilter`, before any authentication.
    """
    if len(frame) < SOURCE_OFFSET + 2:
        return None
    return frame[SOURCE_OFFSET] | frame[SOURCE_OFFSET + 1] << 8


class MeshRadio(Radio):
    """The `mesh.radio.Radio` of an SX1262 adapter (any object with its API).

    adapter: has ``transmit(frame)``, ``busy()`` and the callbacks
    ``on_receive``, ``on_crc_error``, ``on_transmitted``, which this object
    sets. Events go to ``sink``, set by the runner.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        adapter.on_receive = self._received
        adapter.on_crc_error = self._crc_error
        adapter.on_transmitted = self._transmitted

    def transmit(self, frame):
        self.adapter.transmit(frame)

    def sense(self):
        self.sink((CHANNEL, self.adapter.busy()))

    def _received(self, frame, rssi, snr):
        self.sink((RX, frame, rssi, snr))

    def _crc_error(self, rssi, snr):
        self.sink((RX_ERROR, rssi, snr))

    def _transmitted(self, duration_ms):
        self.sink((TX_DONE, duration_ms))
