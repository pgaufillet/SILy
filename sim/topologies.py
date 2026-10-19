"""Seeded topologies of large simulated networks (MESH_DESIGN §15, §16).

A `Topology` lists the medium names of its boards, the root first (mesh
addresses: `ROOT`, then ``0x0101``, ``0x0102``...), and its links, both ways,
each with an RSSI and an SNR; `Topology.apply` scripts them on a
`sim.net.Network` whose boards are added. Generators:

- `single_domain`: every board hears every other one, the root included:
  the single collision domain of the traffic model (§15);
- `relay_star`: the worst case of §15–§16: one relay one hop from the root,
  every other board one hop behind it, hearing the relay and each other,
  not the root;
- `geometric`: random geometric graph in the unit square, the root at its
  centre, two boards linked when closer than a radius chosen for a mean
  degree of about `DEGREE` before border effects; drawn again until
  connected. Boards out of each other's range are hidden terminals. RSSI
  and SNR fall with the distance (-70 to -100 dBm, 10 to 0 dB: a margin of
  10 dB or more above the SF12 limit, every link eligible, §9);
- `grid`: the grid of `tools/simbench.py`, the root at a corner, each board
  hearing its 4 neighbours.

Random draws come from their own generator (`sim.prng.XorShift32`), so that
a topology depends on its seed only, not on the network's draws.
"""

from mesh.frame import ROOT
from sim.prng import XorShift32

DEGREE = 8  # geometric graph: mean degree before border effects
_LABEL = 1 << 20  # derive() label of the topology generator (above board labels)
_TRIES = 1000  # geometric graph: draws before giving up on connectivity


def address(i):
    """Return the mesh address of board i (0: the root)."""
    return ROOT if i == 0 else 0x0100 + i


class Topology:
    """Boards and links of a network.

    kind: generator name; names: medium names (= mesh addresses), the root
    first; links: [(a, b, rssi, snr)], each linked both ways; hops: hop
    count of each name from the root (shortest path).
    """

    def __init__(self, kind, names, links):
        self.kind = kind
        self.names = names
        self.links = links
        self.hops = _hops(names, links)

    @property
    def depth(self):
        """Largest hop count from the root."""
        return max(self.hops.values())

    def degrees(self):
        """Return {name: number of neighbours}."""
        out = dict((n, 0) for n in self.names)
        for a, b, _, _ in self.links:
            out[a] += 1
            out[b] += 1
        return out

    def apply(self, net, losses=None):
        """Script the links on net (boards already added).

        losses: {(a, b): loss} for links lossy both ways (default none).
        """
        for a, b, rssi, snr in self.links:
            loss = 0.0
            if losses is not None:
                loss = losses.get((a, b), 0.0)
            net.link_both(a, b, loss, rssi, snr)


def _hops(names, links):
    adj = dict((n, []) for n in names)
    for a, b, _, _ in links:
        adj[a].append(b)
        adj[b].append(a)
    hops = {names[0]: 0}
    frontier = [names[0]]
    while frontier:
        nxt = []
        for n in frontier:
            for m in adj[n]:
                if m not in hops:
                    hops[m] = hops[n] + 1
                    nxt.append(m)
        frontier = nxt
    return hops


def _names(n):
    return [address(i) for i in range(n)]


def single_domain(n):
    """Every board hears every other one (§15)."""
    names = _names(n)
    links = []
    for i in range(n):
        for j in range(i + 1, n):
            links.append((names[i], names[j], -80, 10))
    return Topology("domain", names, links)


def relay_star(n):
    """One relay next to the root, the n - 2 others one hop behind it (§15, §16)."""
    names = _names(n)
    links = [(names[0], names[1], -80, 10)]
    for i in range(1, n):
        for j in range(i + 1, n):
            links.append((names[i], names[j], -80, 10))
    return Topology("star", names, links)


def geometric(n, seed, degree=DEGREE):
    """Random geometric graph of n boards, connected, the root at the centre."""
    rng = XorShift32(seed).derive(_LABEL)
    radius = (degree / (3.14159265 * n)) ** 0.5
    names = _names(n)
    for _ in range(_TRIES):
        points = [(0.5, 0.5)]
        for _ in range(n - 1):
            points.append((rng.next32() / 4294967296, rng.next32() / 4294967296))
        links = []
        for i in range(n):
            for j in range(i + 1, n):
                dx = points[i][0] - points[j][0]
                dy = points[i][1] - points[j][1]
                d = (dx * dx + dy * dy) ** 0.5
                if d < radius:
                    f = d / radius
                    links.append((names[i], names[j], round(-70 - 30 * f), round(10 - 10 * f)))
        t = Topology("geometric", names, links)
        if len(t.hops) == n:
            return t
    raise ValueError("no connected graph drawn")


def grid(n):
    """Grid of width ceil(sqrt(n)), the root at a corner, 4 neighbours each."""
    names = _names(n)
    width = 1
    while width * width < n:
        width += 1
    links = []
    for i in range(n):
        if i % width + 1 < width and i + 1 < n:
            links.append((names[i], names[i + 1], -80, 10))
        if i + width < n:
            links.append((names[i], names[i + width], -80, 10))
    return Topology("grid", names, links)


GENERATORS = {
    "domain": lambda n, seed: single_domain(n),
    "star": lambda n, seed: relay_star(n),
    "geometric": geometric,
    "grid": lambda n, seed: grid(n),
}


def make(kind, n, seed):
    """Return the topology kind ('domain', 'star', 'geometric', 'grid') of n boards."""
    return GENERATORS[kind](n, seed)
