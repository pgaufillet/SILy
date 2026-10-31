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
- `relay_star_domain`: the same links plus a weak one between the root and
  every other board (`WEAK_RSSI`, `WEAK_SNR`): the literal reading of §15,
  every board in one collision domain, the root heard and hearing everyone
  (carrier sense, collisions; no hidden terminal), but these links below
  the eligibility margin of a parent link (§9) at any SF from 7, so that
  every Node still routes through the relay (`Topology.hops` counts every
  link: depth 1, routes 2 hops deep);
- `relay_star_split`: the dimensioning variant of the star: two relays one
  hop from the root and hearing each other, the other boards split in two
  halves, each half hearing its own relay and each other, neither the root
  nor the other half (the load of the star shared by two relays whose
  subtrees are hidden from each other);
- `geometric`: random geometric graph in the unit square, the root at its
  centre, two boards linked when closer than a radius chosen for a mean
  degree of about `DEGREE` before border effects; drawn again until
  connected. Boards out of each other's range are hidden terminals. RSSI
  and SNR fall with the distance (-70 to -100 dBm, 10 to 0 dB: a margin of
  10 dB or more above the SF12 limit, every link eligible, §9);
- `grid`: the grid of `tools/simbench.py`, the root at a corner, each board
  hearing its 4 neighbours;
- `course`: a hand-placed orienteering course, not drawn: the root at the
  arena, the controls on three loops leaving it in three directions, 1 to
  3 hops deep, every control beyond hop 1 hearing two controls one hop
  closer (two possible parents), the loops out of each other's range
  (hidden terminals); links and their RSSI and SNR from the distance as in
  `geometric`. The nominal case; the others are limit cases.

Random draws come from their own generator (`sim.prng.XorShift32`), so that
a topology depends on its seed only, not on the network's draws.
"""

from mesh.frame import ROOT
from sim.prng import XorShift32

DEGREE = 8  # geometric graph: mean degree before border effects
_LABEL = 1 << 20  # derive() label of the topology generator (above board labels)
_TRIES = 1000  # geometric graph: draws before giving up on connectivity
COURSE_RANGE = 0.2  # course: radio range, in the unit square
# Star domain: the weak links of the root. SNR -9 dB: a margin of 1 dB above
# the SF8 limit (-10 dB), -1.5 dB at SF7, under the 3 dB of an eligible
# parent link from the first frame (§9).
WEAK_RSSI = -120
WEAK_SNR = -9

# Course: places of the Nodes, the root at the arena (0.5, 0.52). Loops N
# (up), W (left), E (right); ordered by hop count, the loops in turn, so
# that the first places of any course keep a parent for each Node and two
# for each Node beyond hop 1. Every link is at most 0.85 of the range,
# every other pair at least 1.15 of it (no link on the edge of the range).
_COURSE = (
    (0.54, 0.68),  # N hop 1
    (0.34, 0.47),  # W hop 1
    (0.62, 0.41),  # E hop 1
    (0.46, 0.68),  # N hop 1
    (0.38, 0.40),  # W hop 1
    (0.66, 0.48),  # E hop 1
    (0.51, 0.81),  # N hop 2
    (0.23, 0.46),  # W hop 2
    (0.66, 0.31),  # E hop 2
    (0.42, 0.78),  # N hop 2
    (0.27, 0.33),  # W hop 2
    (0.73, 0.40),  # E hop 2
    (0.41, 0.91),  # N hop 3
    (0.12, 0.36),  # W hop 3
)


def address(i):
    """Return the mesh address of board i (0: the root)."""
    return ROOT if i == 0 else 0x0100 + i


class Topology:
    """Boards and links of a network.

    kind: generator name; names: medium names (= mesh addresses), the root
    first; links: [(a, b, rssi, snr)], each linked both ways; hops: hop
    count of each name from the root (shortest path); positions: {name: (x,
    y)} in the unit square for the generators placing the boards
    (`geometric`, `grid`, `course`), otherwise None (for display, e.g. `sim.trace`).
    """

    def __init__(self, kind, names, links, positions=None):
        self.kind = kind
        self.names = names
        self.links = links
        self.positions = positions
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


def relay_star_domain(n):
    """The star, plus weak links between the root and the n - 2 others (§15)."""
    t = relay_star(n)
    names = t.names
    links = t.links + [(names[0], names[i], WEAK_RSSI, WEAK_SNR) for i in range(2, n)]
    return Topology("star-domain", names, links)


def relay_star_split(n):
    """Two relays next to the root and each other, the n - 3 others in two halves (§15, §16).

    Relays ``0x0101`` and ``0x0102``; the others in order, the first half
    (the larger one when n - 3 is odd) behind ``0x0101``, the second
    behind ``0x0102``. Links as in `relay_star`.
    """
    names = _names(n)
    links = [
        (names[0], names[1], -80, 10),
        (names[0], names[2], -80, 10),
        (names[1], names[2], -80, 10),
    ]
    rest = list(range(3, n))
    half = (len(rest) + 1) // 2
    for relay, group in ((1, rest[:half]), (2, rest[half:])):
        for k, i in enumerate(group):
            links.append((names[relay], names[i], -80, 10))
            for j in group[k + 1 :]:
                links.append((names[i], names[j], -80, 10))
    return Topology("star2b", names, links)


def geometric(n, seed, degree=DEGREE):
    """Random geometric graph of n boards, connected, the root at the centre."""
    rng = XorShift32(seed).derive(_LABEL)
    radius = (degree / (3.14159265 * n)) ** 0.5
    names = _names(n)
    for _ in range(_TRIES):
        points = [(0.5, 0.5)]
        for _ in range(n - 1):
            points.append((rng.next32() / 4294967296, rng.next32() / 4294967296))
        links = _ranged(names, points, radius)
        t = Topology("geometric", names, links, dict(zip(names, points)))
        if len(t.hops) == n:
            return t
    raise ValueError("no connected graph drawn")


def _ranged(names, points, radius):
    # Links between the points closer than radius, weaker with the distance.
    links = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            dx = points[i][0] - points[j][0]
            dy = points[i][1] - points[j][1]
            d = (dx * dx + dy * dy) ** 0.5
            if d < radius:
                f = d / radius
                links.append((names[i], names[j], round(-70 - 30 * f), round(10 - 10 * f)))
    return links


def grid(n):
    """Grid of width ceil(sqrt(n)), the root at a corner, 4 neighbours each."""
    names = _names(n)
    width = 1
    while width * width < n:
        width += 1
    links = []
    positions = {}
    for i in range(n):
        if i % width + 1 < width and i + 1 < n:
            links.append((names[i], names[i + 1], -80, 10))
        if i + width < n:
            links.append((names[i], names[i + width], -80, 10))
        span = max(width - 1, 1)
        positions[names[i]] = ((i % width) / span, (i // width) / span)
    return Topology("grid", names, links, positions)


def course(n):
    """Orienteering course of n boards: the root and the first n - 1 places.

    Up to 15 boards (14 Nodes: 6 at hop 1, 6 at hop 2, 2 at hop 3); a
    smaller n keeps the first places (see `_COURSE`), a larger one raises
    ValueError.
    """
    if not 1 <= n <= len(_COURSE) + 1:
        raise ValueError("course of 1 to %d boards" % (len(_COURSE) + 1))
    names = _names(n)
    points = [(0.5, 0.52)] + list(_COURSE[: n - 1])
    links = _ranged(names, points, COURSE_RANGE)
    return Topology("course", names, links, dict(zip(names, points)))


GENERATORS = {
    "domain": lambda n, seed: single_domain(n),
    "star": lambda n, seed: relay_star(n),
    "star-domain": lambda n, seed: relay_star_domain(n),
    "star2b": lambda n, seed: relay_star_split(n),
    "geometric": geometric,
    "grid": lambda n, seed: grid(n),
    "course": lambda n, seed: course(n),
}


def make(kind, n, seed):
    """Return the topology kind (a key of `GENERATORS`) of n boards."""
    return GENERATORS[kind](n, seed)
