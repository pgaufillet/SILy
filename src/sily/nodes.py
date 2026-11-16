"""The gateway's view of the mesh, from the reports it receives (ports 1, 2, 5).

`Nodes` keeps one list per origin (fields below), updated with each decoded
report (``mesh.routing.Routing.on_report``) outside the mesh's step, with
the time it was queued: presence (``mesh.reports.Presence``: parent, flags,
the battery and operating mode of the application extension,
``sily.record``), topology (the entries, kept packed as on air, replaced by
each report) and node information (name, firmware; the MAC is left to the
journal). At most ``capacity`` origins: the reports of further ones count
in ``beyond`` only. Entries are never removed: a silent box stays, shown
silent; a box never heard is absent.

No report carries a rank: `hops` counts a node's hops to the root along
the parent chain of the presences (None when a parent on it is unknown or
the chain loops). `state` reads the age of the last presence (before the
first one, of the last report) against the root's presence period P:
``ok`` up to 2.5 P (the mesh's silence limit), ``late`` up to 4.5 P (the
root's "node silent" alarm), else ``silent``.

`row` returns a node as one dict of the web page's ``mesh.nodes`` (keys
`FIELDS`; ages in s, None when the event never came); `rows` yields them
one at a time by address, so that the caller can stream them with other
tasks running between.
"""

from mesh.frame import ROOT
from mesh.ports import NODE_INFO, PRESENCE, TOPOLOGY
from mesh.reports import FIRST_REPORT, MAX_ENTRIES, decode_topology, encode_topology
from sily.record import MODES, decode_extension

CAPACITY = 64  # origins (as the neighbour table and the test traffic)
OK_HALVES = 5  # ok up to 2.5 presence periods
LATE_HALVES = 9  # late up to 4.5 presence periods, then silent

# Fields of a node's list.
HEARD = 0  # ms, the last report of any kind
PRESENT = 1  # ms, the last presence (None: none yet)
PRESENCES = 2  # presences received
RESTARTS = 3  # presences flagged first report since the node's boot
PARENT = 4  # of the last presence
CHANGES = 5  # parent changes between two presences
CHANGED = 6  # ms, the last parent change (None: none seen)
FLAGS = 7  # of the last presence
BATTERY = 8  # mV, of the last presence (None: no 1-byte extension)
MODE = 9  # name (record.MODES), likewise
LINKED = 10  # ms, the last topology report
LINKS = 11  # its entries as on air (count byte first)
NAME = 12
FIRMWARE = 13  # 16-bit version: major, minor

# Keys of a node's dict (`Nodes.row`).
FIELDS = (
    "address",
    "name",
    "firmware",  # "major.minor"
    "state",  # ok, late, silent
    "heard_s",  # age of the last report of any kind
    "age_s",  # age of the last presence
    "presences",
    "restarts",
    "parent",
    "hops",
    "parent_changes",
    "parent_age_s",
    "flags",
    "battery",  # mV
    "mode",
    "topology_age_s",
    "links",  # [address, margin dB (-128 unknown), parent-set member 0/1, delivery %]
)


def _age(t, now):
    return None if t is None else (now - t) // 1000


def _links(packed):
    if packed is None:
        return []
    return [[a, m, 1 if member else 0, r] for a, m, member, r in decode_topology(packed)]


class Nodes:
    """The nodes heard by the gateway; period_s: the root's presence period."""

    def __init__(self, period_s, capacity=CAPACITY):
        self.period_s = period_s
        self.capacity = capacity
        self.beyond = 0  # reports of origins beyond capacity
        self._nodes = {}  # address -> list (fields above)

    def __len__(self):
        return len(self._nodes)

    def update(self, t, port, origin, report):
        """Account report (decoded) of port from origin, queued at t (ms)."""
        node = self._nodes.get(origin)
        if node is None:
            if len(self._nodes) >= self.capacity:
                self.beyond += 1
                return
            node = [t, None, 0, 0, None, 0, None, None, None, None, None, None, None, None]
            self._nodes[origin] = node
        node[HEARD] = t
        if port == PRESENCE:
            node[PRESENT] = t
            node[PRESENCES] += 1
            if report.flags & FIRST_REPORT:
                node[RESTARTS] += 1
            if node[PARENT] is not None and report.parent != node[PARENT]:
                node[CHANGES] += 1
                node[CHANGED] = t
            node[PARENT] = report.parent
            node[FLAGS] = report.flags
            ext = decode_extension(report.extension)
            node[BATTERY] = None if ext is None else ext[0]
            node[MODE] = None if ext is None else MODES[ext[1]]
        elif port == TOPOLOGY:
            node[LINKED] = t
            # Packed (4 B per entry); above MAX_ENTRIES, none of SILy's.
            node[LINKS] = encode_topology(report[:MAX_ENTRIES])
        elif port == NODE_INFO:
            node[NAME] = report.name
            node[FIRMWARE] = report.firmware

    def state(self, address, now):
        """Return "ok", "late" or "silent": the age of address's last presence at now."""
        node = self._nodes[address]
        at = node[PRESENT]
        twice = 2 * (now - (node[HEARD] if at is None else at))
        period = self.period_s * 1000
        if twice <= OK_HALVES * period:
            return "ok"
        return "late" if twice <= LATE_HALVES * period else "silent"

    def hops(self, address):
        """Return the hops of address to the root along the parent chain,
        None when a parent on it is unknown or the chain loops."""
        nodes = self._nodes
        n = 0
        while address != ROOT:
            node = nodes.get(address)
            if node is None or n >= len(nodes):  # unknown, or more hops than nodes: a loop
                return None
            address = node[PARENT]
            n += 1
        return n

    def row(self, address, now):
        """Return the dict of address at now (ms), keys `FIELDS`."""
        node = self._nodes[address]
        fw = node[FIRMWARE]
        return {
            "address": address,
            "name": node[NAME],
            "firmware": None if fw is None else "%d.%d" % (fw >> 8, fw & 0xFF),
            "state": self.state(address, now),
            "heard_s": _age(node[HEARD], now),
            "age_s": _age(node[PRESENT], now),
            "presences": node[PRESENCES],
            "restarts": node[RESTARTS],
            "parent": node[PARENT],
            "hops": self.hops(address),
            "parent_changes": node[CHANGES],
            "parent_age_s": _age(node[CHANGED], now),
            "flags": node[FLAGS],
            "battery": node[BATTERY],
            "mode": node[MODE],
            "topology_age_s": _age(node[LINKED], now),
            "links": _links(node[LINKS]),
        }

    def rows(self, clock):
        """Yield `row` of each node by address, clock() (ms) read for each.

        The addresses are those at the first row: a node added while the
        caller runs other tasks between rows waits for the next call.
        """
        for address in sorted(self._nodes):
            yield self.row(address, clock())
