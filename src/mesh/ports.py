"""Mesh ports and the transmit priority they imply (MESH_DESIGN §6, §11).

Ports 0–15 belong to the mesh, 16–127 to applications at high priority,
128–255 to applications at low priority (R14). The priority of a message is
implicit in its port, so that a relay deduces it for forwarded messages and
the format carries no priority field. Port 9 is link-local: its sender knows
whether it advertises after a Trickle reset (priority 1) or periodically
(priority 2).
"""

PRESENCE = 1
TOPOLOGY = 2
ACK = 3
ERROR = 4
NODE_INFO = 5
ECHO = 6
TRACEROUTE = 7
MANAGEMENT = 8
DODAG_INFO = 9
CLASS2_CONTROL = 10
APP_HIGH = 16  # first application port at high priority
APP_LOW = 128  # first application port at low priority

# Priorities, 1 the most urgent (duty-cycle allowances, §6; queue order, §7).
PRIORITY_REPAIR = 1  # repair and reliability, high-priority applications
PRIORITY_PERIODIC = 2  # presence, periodic advertisements, root beacon
PRIORITY_LOW = 3  # topology, node information, diagnostics, low-priority applications

# Mesh ports 0..10 -> priority; 0 = reserved (port 9 handled apart).
_MESH_PRIORITY = b"\x00\x02\x03\x01\x01\x03\x03\x03\x03\x00\x01"


def priority(port, trickle_reset=False):
    """Return the priority (1–3) of a message on port.

    trickle_reset: port 9 only, True for an advertisement, solicitation or
    poisoning sent after a Trickle reset, False for a periodic advertisement
    or the root beacon. Reserved ports (0, 11–15) and values outside 0–255
    raise ValueError: such messages are never sent nor forwarded.
    """
    if port >= APP_LOW:
        if port > 255:
            raise ValueError("port out of range")
        return PRIORITY_LOW
    if port >= APP_HIGH:
        return PRIORITY_REPAIR
    if port == DODAG_INFO:
        return PRIORITY_REPAIR if trickle_reset else PRIORITY_PERIODIC
    if 0 <= port < len(_MESH_PRIORITY) and _MESH_PRIORITY[port]:
        return _MESH_PRIORITY[port]
    raise ValueError("reserved port")
