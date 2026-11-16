"""Radio wrapper dropping a share of the frames of chosen link sources.

A test instrument of the box application (the box file's ``drop``,
``sily.config``): boards on one bench all hear each other, so no frame is
lost and the link engine never retransmits; dropping a share of a
neighbour's frames on purpose gives retransmissions, the root's immediate
replies to a relaying child's retransmission (MESH_DESIGN §8) and repairs
with a few boards. Unlike a real weak link, carrier sense still hears the
dropped frames.

`Drop` sits between the neighbour filter and the taps (``sily.stack``):
it passes actions on, and every event except the ``RX`` events whose link
source has a rate, of which it ignores that many percent at random. A
frame whose source cannot be read passes on (the filter or the link
engine decides). What follows it (tap, turnaround, link engine) sees the
frames as a lossy link would deliver them.

Its cost per frame received: the link source read, one dictionary look-up
and, for a source with a rate, one draw (`percent`: MicroPython's
``random``, seeded from the hardware random source at boot, no
allocation).
"""

import random

from mesh.radio import RX, Radio


def percent():
    """Return a uniform integer in 0..99."""
    return random.randrange(100)


class Drop(Radio):
    """Wraps radio (a `mesh.radio.Radio`) and drops frames of the sources of rates.

    rates: address -> percent (0 to 100: 0 drops none, 100 every one);
    source_of(frame): the link source of a frame, None if unreadable (as
    `mesh.radio.NeighbourFilter`'s); draw(): a uniform integer in 0..99, a
    frame being dropped when it is below the source's rate. ``dropped``:
    address -> frames dropped since made, for every address of rates.
    """

    def __init__(self, radio, rates, source_of, draw=percent):
        self.radio = radio
        self.rates = rates
        self.source_of = source_of
        self.draw = draw
        self.dropped = {address: 0 for address in rates}
        radio.sink = self._on_event

    def transmit(self, frame):
        self.radio.transmit(frame)

    def sense(self):
        self.radio.sense()

    def _on_event(self, event):
        if event[0] == RX:
            source = self.source_of(event[1])
            rate = self.rates.get(source)
            if rate is not None and self.draw() < rate:
                self.dropped[source] += 1
                return
        self.sink(event)

    def minute(self):
        """Return the ``drp`` field of the minute record: [[address, dropped], ...] by address."""
        return [[address, self.dropped[address]] for address in sorted(self.dropped)]
