"""Simulated network of link engines on the scripted medium.

A `Network` holds the scheduler, the medium and the boards of one run, all
derived from one seed. A `Board` is a `mesh.link.LinkEngine` built by
`mesh.link.open_link` on its own platform fakes (frame counter in storage,
duty-cycle window and replay table in retained memory) with the network key,
run by a `sim.node.Node` on a medium port. Boards can be reset (soft reset,
watchdog) or lose power inside a running simulation: the engine is rebuilt
from what the platform kept, after an optional time off the air.

A `Sniffer` is an attacker's (or an observer's) radio on the medium: it
records every frame it hears and can transmit any bytes (replays).
"""

from mesh import profile as profiles
from mesh.airtime import time_on_air_ms
from mesh.link import open_link
from mesh.radio import RX
from sim.medium import Medium
from sim.node import Node
from sim.platform import FakePlatform
from sim.prng import XorShift32
from sim.scheduler import Scheduler


class Network:
    """Boards sharing one channel; every random draw comes from seed.

    key: network frame key (16 B), the default of every board; sf, profile:
    radio settings (EU profile by default); budget_ms, window_ms: duty-cycle
    budget and window of every board (profile values by default; scaled down
    to keep runs short); sense_ms, capture_db, trace: medium options.
    """

    def __init__(
        self,
        seed,
        key,
        sf=7,
        profile=None,
        budget_ms=None,
        window_ms=None,
        sense_ms=1,
        capture_db=None,
        trace=False,
    ):
        self.rng = XorShift32(seed)
        self.key = key
        self.sf = sf
        self.profile = profile if profile is not None else profiles.get(profiles.EU_869)
        self.budget_ms = budget_ms
        self.window_ms = window_ms
        self.scheduler = Scheduler()
        bw = self.profile.bandwidth_hz
        cr = self.profile.coding_rate
        self.medium = Medium(
            self.scheduler,
            lambda n: time_on_air_ms(n, sf, bw, cr),
            self.rng.derive(0),
            capture_db,
            sense_ms,
            trace,
        )
        self.boards = {}
        self._labels = 0

    def _label(self):
        self._labels += 1
        return self._labels

    def add(self, name, address=None, key=None, next_hop=None, reroute=None, routing=None):
        """Add a board on medium port name; mesh address defaults to name.

        next_hop, reroute: routing functions of its engines (`mesh.link.LinkEngine`);
        routing: function(engine) -> routing component plugged into each
        engine built (``board.routing``), e.g. a `mesh.dodag.Dodag`.
        """
        board = Board(
            self,
            name,
            name if address is None else address,
            self.key if key is None else key,
            next_hop,
            self._label(),
            reroute,
            routing,
        )
        self.boards[name] = board
        return board

    def sniffer(self, name):
        """Add a `Sniffer` on medium port name."""
        return Sniffer(self, name)

    def link_both(self, a, b, loss=0.0, rssi=-80, snr=10):
        """Link two medium ports both ways."""
        self.medium.link_both(a, b, loss, rssi, snr)

    def full_mesh(self, names, loss=0.0):
        """Link every pair of names both ways."""
        names = list(names)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                self.medium.link_both(names[i], names[j], loss)

    def at(self, time, fn, *args):
        """Schedule fn(*args) at time."""
        return self.scheduler.at(time, fn, *args)

    def run(self, until, limit=None):
        """Run the simulation up to until (ms)."""
        self.scheduler.run(until, limit)

    @property
    def now(self):
        return self.scheduler.now


class Board:
    """One simulated board: platform fakes, link engine, runner, medium port.

    ``engine`` is the current engine (None while the board is off; a reboot
    builds a new one, whose counters start from zero), ``routing`` the
    component the routing function plugged into it (or None); ``inbox`` records
    (time, origin, port, payload) for the ports given to `listen`, and
    ``failures`` (time, dest, port, seq, reason) for the failure callback,
    across reboots; ``boots`` counts the engines built.
    """

    def __init__(self, net, name, address, key, next_hop, label, reroute=None, routing=None):
        self.net = net
        self.name = name
        self.address = address
        self.key = key
        self.next_hop = next_hop
        self.reroute = reroute
        self.make_routing = routing
        self.routing = None
        self.platform = FakePlatform(net.rng.derive(label))
        self.port = net.medium.attach(name)
        self.inbox = []
        self.failures = []
        self.boots = 0
        self.engine = None
        self.node = None
        self._ports = []
        self._boot()

    def _boot(self):
        net = self.net
        engine = open_link(
            self.address,
            self.key,
            self.platform.storage,
            self.platform.retained,
            self.platform.random,
            net.profile,
            net.sf,
            self.next_hop,
            self.reroute,
            budget_ms=net.budget_ms,
            window_ms=net.window_ms,
        )
        for port in self._ports:
            engine.bind(port, self._record)
        engine.on_failure(self._failed)
        if self.make_routing is not None:
            self.routing = self.make_routing(engine)
        self.engine = engine
        self.node = Node(net.scheduler, engine, self.port)
        self.boots += 1
        self.node.start()

    def _record(self, origin, msg, header):
        self.inbox.append((self.net.now, origin, msg.port, bytes(msg.payload)))

    def _failed(self, dest, port, seq, reason):
        self.failures.append((self.net.now, dest, port, seq, reason))

    def listen(self, *ports):
        """Record the messages delivered on application ports into inbox."""
        for port in ports:
            self._ports.append(port)
            if self.engine is not None:
                self.engine.bind(port, self._record)

    def received(self, port=None):
        """Return the payloads of inbox (on port if given), in order."""
        return [r[3] for r in self.inbox if port is None or r[2] == port]

    def inbox_from(self, port=None):
        """Return [(origin, payload)] of inbox (on port if given), in order."""
        return [(r[1], r[3]) for r in self.inbox if port is None or r[2] == port]

    def submit(self, port, cls, dest, payload, replace=False):
        """Submit through the engine and step it; None while the board is off."""
        if self.engine is None:
            return None
        result = self.engine.submit(port, cls, dest, payload, replace)
        self.node.wake()
        return result

    def reset(self, off_ms=0):
        """Soft reset: retained memory kept, uncommitted storage lost."""
        self._down()
        self.platform.reset()
        self.net.scheduler.after(off_ms, self._boot)

    def power_loss(self, off_ms=0):
        """Power loss: retained memory cleared, uncommitted storage lost."""
        self._down()
        self.platform.power_loss()
        self.net.scheduler.after(off_ms, self._boot)

    def _down(self):
        if self.node is not None:
            self.node.stop()
        self.node = None
        self.engine = None
        self.routing = None


class Sniffer:
    """A raw radio on the medium: records frames, transmits arbitrary bytes.

    ``frames`` lists (time, frame) of every frame received.
    """

    def __init__(self, net, name):
        self.net = net
        self.name = name
        self.port = net.medium.attach(name)
        self.port.sink = self._on_event
        self.frames = []

    def _on_event(self, event):
        if event[0] == RX:
            self.frames.append((self.net.now, event[1]))

    def inject(self, frames, start, gap):
        """Transmit each frame of frames, the first at start, then every gap ms."""
        t = start
        for f in frames:
            self.net.scheduler.at(t, self.port.transmit, f)
            t += gap
