#!/usr/bin/env python3
"""Airtime and duty-cycle budget of the SILy mesh (docs/MESH_DESIGN.md).

Plain CPython 3, no dependency: `python3 tools/mesh_budget.py [SUBTREE]`.
Every figure of MESH_DESIGN "Airtime budget" (§16), and the repair and
class 2 recovery tables of §9 and §12, come from this script; change the
parameters below and re-run rather than editing the tables by hand.
"""

import math
import sys

# --- Radio (EU profile defaults, MESH_DESIGN §4) ---------------------------
BW = 125_000  # Hz
CR = 1  # 4/5
PREAMBLE = 8  # symbols (driver default is 12, see MESH_DESIGN)
CRC = True  # LoRa payload CRC; turning it off is option A6 (MESH_DESIGN §14)
EXPLICIT_HEADER = True
SFS = range(7, 13)
MAX_FRAME = 255  # bytes, SX1262 payload limit

# --- Regulation (869.4-869.65 MHz, 10 %) ------------------------------------
DUTY_CYCLE = 0.10
WINDOW = 3600  # s, ETSI EN 300 220 observation period
MARGIN = 0.90  # fraction of the legal budget the firmware allows itself
BUDGET = DUTY_CYCLE * WINDOW * MARGIN  # s of TX per hour per board

# --- Frame and message format (bytes, MESH_DESIGN §11) ----------------------
FRAME_HEADER = 9  # versions/key epoch 1, link dst 2, link src 2, frame counter 3, rank 1
TAG = 4  # AES-128-CCM tag (MIC-32, MESH_DESIGN §14)
OVERHEAD = FRAME_HEADER + TAG
MSG_HEADER = 3  # control 1, port 1, length 1
LAST_HEADER = 2  # last message of a frame: length implicit
BUNDLE = 1  # element count, when several messages share one header
ADDR = 2
SEQ = 2  # message sequence, classes 1 and 2
# Upward elements as forwarded by a relay (origin explicit): origin, [sequence], payload.
# Presence: origin, preferred parent, flags, SILy extension (battery, mode).
PRESENCE_REC = ADDR + ADDR + 1 + 1
TOPO_REC = ADDR + 1 + 6 * 4  # topology: origin, count, 6 x (addr 2, margin 1, quality 1)
PUNCH_REC = ADDR + SEQ + 10  # punch, class 2: origin, sequence, SIPy punch
# The same on the first hop (mode 6): origin = link source, presence parent = link destination.
PRESENCE_FIRST = PRESENCE_REC - 2 * ADDR
TOPO_FIRST = TOPO_REC - ADDR
PUNCH_FIRST = PUNCH_REC - ADDR
# Port 9 DODAG information: flags 1, presence 2, topology 2, Imin 1, max rank
# increase 1, profile 1.
DODAG_INFO = 8
# Root beacon and Trickle advertisement: one DODAG information message.
BEACON = LAST_HEADER + DODAG_INFO
ACK_ENTRY = 4  # class 1 acknowledgement: link source 2, frame counter low 16 bits 2
ACK_PER_FRAME = 8  # entries per root acknowledgement frame
ACK_SHARE = 0.05  # max channel occupancy by root acknowledgement frames

# --- Traffic scenario (MESH_DESIGN §15) -------------------------------------
NODES = 30  # Nodes in the network (of the order of tens)
SUBTREE = 30  # Nodes whose traffic crosses the busiest relay, itself included
# Topology: the busiest relay is one hop from the Gateway and the other
# SUBTREE - 1 Nodes are one hop behind it; SUBTREE = NODES is the worst case
# (terrain forcing all traffic through one relay). All boards share one
# collision domain: every last-hop frame converges on the single Gateway
# receiver, whatever the number of relays around it.
# s, a silent Node is reported within about this delay at SF <= 9 (about 135 s
# at SF7-8, 270 s at SF9: K_MISS = 4, below).
DETECT_TARGET = 270
JITTER = 0.10  # periodic timers jittered by +-10 % (MESH_DESIGN §7)
SILENCE_LIMIT = 2.5  # presence periods without a frame: neighbour silent, strict (§9)
# Missed presences before a Node is reported silent; with 3, no root threshold
# meets the three conditions below (MESH_DESIGN §16).
K_MISS = 4
ROOT_SILENCE = 4.5  # presence periods without a presence at the root: Node silent
# TLA+ spec/ (KMiss, Silence), no on-air delay: K_MISS - 1 lost presences
# tolerated needs ROOT_SILENCE > K_MISS x 1.1, K_MISS lost detected needs
# ROOT_SILENCE <= (K_MISS + 1) x 0.9, no false alarm after a relay failure
# needs ROOT_SILENCE > 1.1 + SILENCE_LIMIT + report delay (KMiss_Tr74).
assert K_MISS * (1 + JITTER) < ROOT_SILENCE <= (K_MISS + 1) * (1 - JITTER) + 1e-9
assert (1 + JITTER) + SILENCE_LIMIT < ROOT_SILENCE
PEAK_PUNCHES = 1000  # per hour through the busiest relay
BURST_PUNCHES = 100  # per minute (mass start), through the busiest relay

# --- Budget policy -----------------------------------------------------------
PRESENCE_SHARE = 0.30  # max share of the relay budget for presence reports
TOPO_SHARE = 0.10  # max share of the relay budget for topology reports
PRESENCE_LOAD = 0.15  # max whole-network channel load from presence + topology
# Share of PRESENCE_LOAD that sizes the presence period (presence + one beacon).
PRESENCE_SIZING = 0.75
BEACONS = 2  # root beacons per presence period: every half period (§9)
# Sizing rule: the presence period is sized with one root beacon
# per period (presence + beacon <= PRESENCE_SIZING x PRESENCE_LOAD), so that
# presence and topology periods and detection times do not change; the
# channel load counts every beacon (BEACONS per period) and must stay within
# PRESENCE_LOAD (presence + topology + beacons; flagged in table B otherwise).
BURST_LOAD = 0.50  # max channel load around the relay during a burst
SENSE_SYMBOLS = 6  # carrier-sense vulnerable window (preamble detection)
PERIODS = (15, 30, 45, 60, 90, 120, 180, 300, 600, 900, 1200, 1800, 3600)

# --- Routing and repair (MESH_DESIGN §9) -----------------------------------
DEPTH = 8  # maximum depth (MP13)
TRICKLE_IMIN = 32  # slots (slot = time on air of a first-hop presence frame, §8)
TRICKLE_K = 2  # Trickle redundancy constant
GLOBAL_REPAIR_MIN = 4  # presence periods between two DODAG versions, at least
K_PARENT = SILENCE_LIMIT  # presence periods without any frame from the preferred parent
N_TX = 3  # class 1 transmission attempts
# Class 1 timeout beyond the forwarded frame: forwarding delay 1 + backoff 4 + margin 1.
RTO_SLOTS = 6
DETACH_HOLD = 2  # Imin after poisoning before rejoining
# Answer window beyond Imin after the solicitation: solicitation 1 + backoff 4
# + answer 2 + step 1.
WINDOW_SLOTS = 8

# --- Service classes (MESH_DESIGN §12) ---------------------------------------
TAIL = 1 + 1 + 2 + 2  # presence tail: count, port, oldest unacknowledged, last sequence
C2_WINDOW = 64  # class 2 send window per flow (messages)
C2_ACK_EVERY = 32  # receiver acknowledges when its cumulative point advanced by this much


def toa(payload, sf, bw=BW, cr=CR, preamble=PREAMBLE):
    """LoRa time on air in seconds (Semtech SX1261/2 datasheet, 6.1.4)."""
    tsym = (1 << sf) / bw
    ldro = 1 if tsym > 0.016 else 0
    bits = 8 * payload - 4 * sf + 28 + (16 if CRC else 0) - (0 if EXPLICIT_HEADER else 20)
    nsym = 8 + max(math.ceil(bits / (4 * (sf - 2 * ldro))) * (cr + 4), 0)
    return (preamble + 4.25 + nsym) * tsym


def frame(k, rec):
    """Frame carrying k messages of rec bytes, alone or in one bundle."""
    if k == 1:
        return OVERHEAD + LAST_HEADER + rec
    return OVERHEAD + MSG_HEADER + BUNDLE + k * rec


def per_frame(rec):
    """Most messages of rec bytes one bundle frame can carry."""
    return (MAX_FRAME - OVERHEAD - MSG_HEADER - BUNDLE) // rec


def records_toa(n, rec, sf):
    """Airtime of n messages of rec bytes packed in as few frames as possible."""
    per_frame = globals()["per_frame"](rec)
    full, rest = divmod(n, per_frame)
    t = full * toa(frame(per_frame, rec), sf)
    return t + (toa(frame(rest, rec), sf) if rest else 0)


def period(cost_per_period, limit_per_hour, floor=0):
    """Smallest standard period whose hourly cost stays under the limit."""
    for p in PERIODS:
        if p >= floor and cost_per_period * WINDOW / p <= limit_per_hour:
            return p
    return math.inf


def periodic_costs(sf, rec, first):
    """Per-period airtime: busiest relay TX, whole-network channel occupancy.

    Every Node sends one frame per period carrying its own message and the
    messages received from its children since the previous period
    (aggregation): the busiest relay sends SUBTREE messages with explicit
    origins (rec bytes each), every other Node one first-hop message (first
    bytes, origin implicit).
    """
    relay = records_toa(SUBTREE, rec, sf)
    return relay, relay + (NODES - 1) * toa(frame(1, first), sf)


def main():
    print(f"Budget per board: {BUDGET:.0f} s/h ({DUTY_CYCLE:.0%} x {MARGIN:.0%})")
    print(
        f"Scenario: {NODES} Nodes, busiest relay carries {SUBTREE}, "
        f"peak {PEAK_PUNCHES} punches/h, burst {BURST_PUNCHES}/min\n"
    )

    print(f"Time on air (ms), BW 125 kHz, CR 4/5, preamble {PREAMBLE}")
    n_punch = per_frame(PUNCH_REC)
    sizes = [
        ("beacon", OVERHEAD + BEACON),
        ("pres. x1", frame(1, PRESENCE_FIRST)),  # codespell:ignore pres
        ("pres. x30", frame(30, PRESENCE_REC)),  # codespell:ignore pres
        ("punch x1", frame(1, PUNCH_FIRST)),
        (f"punch x{n_punch}", frame(n_punch, PUNCH_REC)),
        ("max frame", MAX_FRAME),
    ]
    print("SF  " + "".join(f"{n:>11}" for n, _ in sizes))
    print("    " + "".join(f"{f'({b} B)':>11}" for _, b in sizes))
    for sf in SFS:
        print(f"{sf:<4}" + "".join(f"{toa(b, sf) * 1000:>11.0f}" for _, b in sizes))

    print("\nA. Individual heartbeats (44 B, no aggregation), relay share 30 %")
    print("SF  period(s)")
    for sf in SFS:
        cost = SUBTREE * toa(44, sf) + toa(OVERHEAD + BEACON, sf)  # SILy-Arduino-like heartbeat
        print(f"{sf:<4}{period(cost, PRESENCE_SHARE * BUDGET)}")

    periods = {}
    print("\nB. Split presence / topology with aggregation at relays")
    print(
        "SF  presnc  detect  topo   relay     punches/h        burst/min        "
        "load   load   false alarms/h  limited by"
    )
    print(
        "    (s)     (s)     (s)    left(s/h) x1      aggr     x1      aggr     "
        "idle   peak   ALOHA  CS"
    )
    for sf in SFS:
        t_bc = toa(OVERHEAD + BEACON, sf)
        s_relay, s_net = periodic_costs(sf, PRESENCE_REC, PRESENCE_FIRST)
        t_relay, t_net = periodic_costs(sf, TOPO_REC, TOPO_FIRST)
        # Presence: relay budget and channel load, one root beacon per period
        # (sizing rule above).
        live_r = period(s_relay, PRESENCE_SHARE * BUDGET)
        live_c = period(s_net + t_bc, PRESENCE_LOAD * PRESENCE_SIZING * WINDOW)
        live = max(live_r, live_c)
        topo = max(
            period(t_relay, TOPO_SHARE * BUDGET),
            period(t_net, PRESENCE_LOAD * (1 - PRESENCE_SIZING) * WINDOW),
            5 * live,
        )
        left = BUDGET - s_relay * WINDOW / live - t_relay * WINDOW / topo
        p1 = toa(frame(1, PUNCH_REC), sf)  # forwarded by the relay
        p1_first = toa(frame(1, PUNCH_FIRST), sf)  # sent by the punching Node
        pn = toa(frame(n_punch, PUNCH_REC), sf)
        load = (s_net + BEACONS * t_bc) / live + t_net / topo
        # Peak: PEAK_PUNCHES single-punch frames crossing two hops.
        peak = load + PEAK_PUNCHES * (p1_first + p1) / WINDOW
        # A Node is falsely reported silent when K_MISS consecutive presence
        # frames are lost (detection: ROOT_SILENCE periods after the last one).
        # Bounds: pure ALOHA (hidden terminals everywhere) and carrier sense
        # in one collision domain, where only frames that
        # start within SENSE_SYMBOLS of each other collide.
        alarms = []
        for a in (1, SENSE_SYMBOLS * (1 << sf) / BW / toa(frame(1, PRESENCE_FIRST), sf)):
            loss = 1 - math.exp(-2 * peak * a)
            alarms.append(NODES * WINDOW / live * loss**K_MISS)
        # Burst: each forwarded frame occupies the channel twice near the relay.
        b1 = BURST_LOAD * 60 / (p1_first + p1)
        bn = n_punch * BURST_LOAD * 60 / (2 * pn)
        periods[sf] = live
        flag = " channel" if live_c >= live_r else " relay"
        flag += "" if ROOT_SILENCE * live <= DETECT_TARGET else ", detection > target"
        flag += "" if load <= PRESENCE_LOAD else f", idle load > {PRESENCE_LOAD:.0%}"
        print(
            f"{sf:<4}{live:>6}  {ROOT_SILENCE * live:>6.0f}  {topo:>5}  {left:>7.0f}  "
            f"{left / p1:>7.0f}  {n_punch * left / pn:>7.0f}  {b1:>6.1f}  {bn:>7.1f}  "
            f"{load:>5.1%}  {peak:>5.1%}  {alarms[0]:>5.1f}  {alarms[1]:>6.3f}{flag}"
        )

    print("\nC. Pure ALOHA success probability exp(-2G) for channel load G")
    print(
        "  "
        + "  ".join(f"G={g:.0%}: {math.exp(-2 * g):.0%}" for g in (0.01, 0.05, 0.10, 0.20, 0.30))
    )

    print(
        f"\nD. Root acknowledgements (class 1, last hop), {ACK_PER_FRAME} entries per frame, "
        f"<= {ACK_SHARE:.0%} of the channel"
    )
    print(f"    and root beacon ({BEACONS} per presence period)")
    print("SF  frame(ms)  batching(s)  root budget(s/h)  beacon(s/h)")
    for sf in SFS:
        t = toa(OVERHEAD + LAST_HEADER + ACK_PER_FRAME * ACK_ENTRY, sf)
        w = t / ACK_SHARE
        bc = BEACONS * toa(OVERHEAD + BEACON, sf) * WINDOW / periods[sf]
        print(f"{sf:<4}{t * 1000:>9.0f}  {w:>11.1f}  {t * WINDOW / w:>16.0f}  {bc:>11.0f}")

    print(
        f"\nE. Repair times (s), depth {DEPTH}, Trickle Imin {TRICKLE_IMIN} slots k {TRICKLE_K}, "
        f"{N_TX} class 1 attempts, parent lost after {K_PARENT} silent presence periods"
    )
    print(
        "SF  Imin  RTO    RTO    switch, traffic  switch  detach  global  "
        "Trickle reset, per neighbourhood  node cost at max"
    )
    print(
        "          relay  root   parent  parent   idle    rejoin  wave    "
        "frames  first-interval load       global repair rate (s/h)"
    )
    print("                        relay   root")
    for sf in SFS:
        slot = toa(frame(1, PRESENCE_FIRST), sf)
        imin = TRICKLE_IMIN * slot
        live = periods[sf]
        rto_relay = toa(MAX_FRAME, sf) + RTO_SLOTS * slot
        ack = toa(OVERHEAD + LAST_HEADER + ACK_PER_FRAME * ACK_ENTRY, sf)
        rto_root = ack / ACK_SHARE + ack + RTO_SLOTS * slot
        # Trickle after a reset: intervals Imin, 2 Imin, ... up to the presence
        # period; above it presence frames advertise anyway. A node sends at a
        # random time in the second half of each interval unless it already
        # heard TRICKLE_K consistent frames: about TRICKLE_K advertisements per
        # neighbourhood and interval, the first ones squeezed into Imin / 2.
        n_int = math.ceil(math.log2(live / imin)) + 1 if live > imin else 1
        adv = toa(OVERHEAD + BEACON, sf)
        peak = TRICKLE_K * adv / (imin / 2)
        node = n_int * adv * WINDOW / (GLOBAL_REPAIR_MIN * live)
        rejoin = (1 + DETACH_HOLD + 1) * imin + WINDOW_SLOTS * slot
        # Towards the root, one more root timeout after the last attempt for
        # its late acknowledgement (§12).
        print(
            f"{sf:<4}{imin:>4.1f}  {rto_relay:>5.1f}  {rto_root:>5.1f}  {N_TX * rto_relay:>6.1f}  "
            f"{(N_TX + 1) * rto_root:>6.1f}  {K_PARENT * live:>6.0f}  {rejoin:>6.1f}  "
            f"{DEPTH * imin:>6.1f}  {TRICKLE_K * n_int:>6}  {peak:>8.0%}  {node:>28.1f}"
        )

    print(
        f"\nF. Class 2 recovery (s), worst depth {DEPTH}; a hop costs at most one class 1 timeout"
    )
    print("SF  NACK    lost message,       lost last message   presence period with a tail")
    print("    delay   after the gap seen  (tail, idle flow)   on every Node (s, transient)")
    for sf in SFS:
        slot = toa(frame(1, PRESENCE_FIRST), sf)
        live = periods[sf]
        rto_relay = toa(MAX_FRAME, sf) + RTO_SLOTS * slot
        ack = toa(OVERHEAD + LAST_HEADER + ACK_PER_FRAME * ACK_ENTRY, sf)
        rto_root = ack / ACK_SHARE + ack + RTO_SLOTS * slot
        nack = N_TX * rto_root
        # NACK down then message up again, each hop at most one timeout.
        trip = 2 * DEPTH * rto_relay
        # Idle flow: the tail rides on the presence report after one silent
        # period, i.e. at most two periods after the last message.
        tail = 2 * live + trip
        s_relay, s_net = periodic_costs(sf, PRESENCE_REC + TAIL, PRESENCE_FIRST + TAIL)
        t_bc = toa(OVERHEAD + BEACON, sf)
        heavy = max(
            period(s_relay, PRESENCE_SHARE * BUDGET),
            period(s_net + t_bc, PRESENCE_LOAD * PRESENCE_SIZING * WINDOW),
        )
        print(f"{sf:<4}{nack:>5.1f}  {nack + trip:>10.0f}  {tail:>19.0f}  {heavy:>18}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        SUBTREE = int(sys.argv[1])
    main()
