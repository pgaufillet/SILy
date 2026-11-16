"""Radio wrapper recording the link measurements of every source heard.

`Tap` sits between the neighbour filter and the runner (``sily.stack``'s
``wrap``): it passes every event on unchanged and, for each frame received
(``RX``), counts it under its link source, read from the header before any
authentication (``sily.radio.link_source``): foreign networks and frames
that later fail authentication count too. Per source: frames since boot,
last RSSI (dBm) and SNR, and their sums since the last `Tap.restart` (the
minute record's means). At most ``capacity`` sources: frames of further
ones count in ``overflow`` only.
"""

from mesh.radio import RX, Radio
from sily.radio import link_source

CAPACITY = 64  # sources (the neighbour table's capacity)

# Fields of a source's list.
FRAMES = 0  # since boot
RSSI = 1  # last, dBm
SNR = 2  # last, quarter dB
RSSI_SUM = 3  # since the last restart
SNR_SUM = 4  # quarter dB
COUNT = 5  # frames summed


def _quarters(snr):
    return int(snr * 4 + (0.5 if snr >= 0 else -0.5))


class Tap(Radio):
    """Wraps radio (a `mesh.radio.Radio`); ``sources``: address -> list (fields above)."""

    def __init__(self, radio, capacity=CAPACITY, source_of=link_source):
        self.radio = radio
        self.capacity = capacity
        self.source_of = source_of
        self.sources = {}
        self.overflow = 0
        radio.sink = self._on_event

    def transmit(self, frame):
        self.radio.transmit(frame)

    def sense(self):
        self.radio.sense()

    def _on_event(self, event):
        if event[0] == RX:
            self._heard(event)
        self.sink(event)

    def _heard(self, event):
        source = self.source_of(event[1])
        if source is None:
            return
        row = self.sources.get(source)
        if row is None:
            if len(self.sources) >= self.capacity:
                self.overflow += 1
                return
            row = [0, 0, 0, 0, 0, 0]
            self.sources[source] = row
        rssi = int(event[2])
        snr = _quarters(event[3])
        row[FRAMES] += 1
        row[RSSI] = rssi
        row[SNR] = snr
        row[RSSI_SUM] += rssi
        row[SNR_SUM] += snr
        row[COUNT] += 1

    def figures(self, address):
        """Return [frames, last RSSI dBm, mean RSSI dBm, mean SNR 0.1 dB] of address.

        Means since the last `restart`, rounded; None without frames there.
        None when the source was never heard.
        """
        row = self.sources.get(address)
        if row is None:
            return None
        n = row[COUNT]
        if not n:
            return [row[FRAMES], row[RSSI], None, None]
        return [
            row[FRAMES],
            row[RSSI],
            (2 * row[RSSI_SUM] + n) // (2 * n),
            (5 * row[SNR_SUM] + n) // (2 * n),  # quarters -> tenths: x 10 / 4
        ]

    def restart(self):
        """Start new means (the minute record)."""
        for row in self.sources.values():
            row[RSSI_SUM] = 0
            row[SNR_SUM] = 0
            row[COUNT] = 0
