# Mesh Design

Status: the design is complete; this document describes it as it stands.
The implementation is in progress: the link layer (frame format, security,
duty cycle, queues, medium access) and upward routing with repair and
class 1 are implemented and tested on the host and on two boards; downward
routing, class 2, supervision queries and key change are not implemented
yet. Parts of the routing and timing rules are checked with TLA+ models
(`spec/README.md`). Points that still need a measurement or a decision are
listed in §18.

The document has two parts. **Part I** describes the mesh as an
application-agnostic component, which may become a project of its own; its
code is the isolated package `mesh/` of the SILy repository, with no
dependency on SILy (a host test enforces the import rule), so that it can be
extracted later while sharing one repository and one test harness meanwhile.
**Part II** is the SILy profile: traffic model, airtime budget and periods
for SILy's use. Part I must not depend on Part II.

All figures come from `tools/mesh_budget.py` (CPython 3, no dependency):
change its parameters and re-run instead of editing tables by hand.

Vocabulary:

- **Node**: any board of the network. **Root**: the node towards which
  routes converge (the SILy Gateway); one per network.
- **Graph**: the physical topology; a **link** joins two nodes within radio
  range of each other, with a measured quality (margin, delivery ratio).
- **DODAG** (Destination-Oriented Directed Acyclic Graph, RPL, RFC 6550): the
  routing structure built on the graph. Every link used for routing is
  oriented towards the root, there is no cycle, and a node may have
  **several parents** (unlike a tree, where each node has exactly one).
- **Rank**: a node's distance to the root, computed from link qualities; it
  strictly increases away from the root, which forbids loops.
- **Parent set**: the neighbours of lower rank a node can forward to;
  **preferred parent**: the one it uses while it works.
- **Upward**: towards the root; **downward**: from the root; **P2P**: from
  node to node.
- **Storing / non-storing**: whether downward routes are stored in every
  node or known only by the root (§10).
- **ETX** (expected transmission count): average number of transmissions
  needed to get a frame across a link, 1 / delivery ratio; 1 is a perfect
  link. The routing metric (§9).
- **DODAG version**: generation number issued by the root; a new version
  rebuilds the ranks (**global repair**) and carries new network-wide mesh
  parameters (§9).
- **Advertisement**: a frame sent only to announce its sender's DODAG version
  and rank; every other frame announces them too, in its header (§11).
- **Trickle** (RFC 6206): the timer deciding when a node sends an
  advertisement. It runs in intervals I; in each one the node sends at a
  random time in [I/2, I), unless suppressed (k), then I doubles, up to
  **Imax** (here the presence period, beyond which presence frames advertise
  anyway: no advertisement at all in steady state). An event (new version,
  rank increase, poisoning, solicitation, rank error) resets I to **Imin**.
- **Imin**: Trickle's shortest interval, hence the delay within which
  neighbours react to an event; 32 slots (§8): 1.6 s at SF7, 5.3 s at SF9,
  42 s at SF12. A new version crosses 8 hops in 8 × Imin. Smaller is faster
  but squeezes every neighbour's reaction into Imin / 2 and raises
  collisions.
- **k** (Trickle redundancy constant): a node skips its advertisement in an
  interval if it has already heard k frames of its own version from
  neighbours: about k advertisements per neighbourhood and interval, whatever
  the number of neighbours. k = 2, so that one lost frame does not deprive a
  neighbourhood (RPL's default, 10, suits cheap 802.15.4 frames).
- **MaxRankIncrease**: how far a node may worsen its rank within one DODAG
  version: never above L + 56, L being the best rank it advertised in that
  version (56 = 7 perfect hops). Beyond, it detaches (rank 255) and rejoins
  instead of following ever worse routes; this stops "count to infinity",
  where two nodes with stale views pick each other as parent and raise their
  ranks in turn. A new version resets L (§9).
- **Flow**: the messages of one (origin, destination, port); classes 1 and 2
  number them with a 16-bit sequence (§11).
- **Passive acknowledgement**: a sender learns that the next hop received a
  message by overhearing it forward that message; no frame is spent (§9, §12).
- **NACK** (negative acknowledgement): a class 2 receiver's request for the
  sequences missing from a flow (§12).
- **Cumulative point** C: every sequence of a flow up to C has been received
  (or declared gone); a cumulative acknowledgement lets the sender free them
  (§12).
- **Tail**: [oldest unacknowledged, last] sequence of an idle class 2 flow,
  announced by the sender so that the receiver detects a lost last message
  (§12).

# Part I — Generic mesh

## 1. Scope and layering

The mesh carries opaque application messages between boards sharing one LoRa
channel. Any node can take the root role; exactly one root per network (no
root redundancy).

**Graph and DODAG.** The network is a **graph**: a board usually hears
several neighbours. Routing overlays a **DODAG** on it, not a tree: each node
keeps a **parent set**, forwards upward through its **preferred parent**, and
switches at once to the next parent when delivery to the preferred one fails
(class 1 retries exhausted). The rank rule makes any parent ranked below
the node's lowest rank since it attached loop-free, so this **local
repair** needs no exchange. The root knows the
**whole graph** (topology reports give every link with its quality; presence
reports give each node's parent set) and computes downward and P2P routes on
it (§10). Full link-state routing at every node (OLSR-like) is rejected: every
node would have to flood its links to the whole network, too costly in
airtime.

Layers, bottom-up:

- **Radio** — SX1262 driver behind a small interface (send, receive, carrier
  sense, link measurements), so that a scripted medium can replace it (MP23,
  MP24). A **neighbour filter** (debug configuration: frames from link
  sources outside a list are ignored) builds multi-hop topologies with real
  boards on one bench.
- **Platform** — clock, persistent storage (NVS on the ESP32), memory kept
  across resets (RTC on the ESP32), random source: injected into the mesh, so
  that it runs on the MicroPython unix port (MP23).
- **Link** — frame header, hop-by-hop protection (§14), carrier sense and
  backoff (§8), duty-cycle accounting by priority (§6), fair queueing (§7),
  neighbour table and link estimation, hop-by-hop reliability (class 1),
  aggregation of queued messages per next hop into one frame.
- **Routing** — DODAG towards the root with parent sets and local repair,
  downward and peer-to-peer routes (§10), broadcast, repair, presence and
  topology reports to the root, diagnostics.
- **Transport services** — service classes 0/1/2 (MP4), end-to-end
  reliability (class 2), multiplexing by port.
- **Applications** (outside the mesh) — SILy: punches, application status,
  mode commands, each on its own port.

The SILy-Arduino version had the same separation: LoRaMesher (routing,
addresses, HELLO) below, `LoraMgr` adding a one-byte packet type and
dispatching to callbacks, the heartbeat being an application reading the
routing table. Routing information and liveness belong to the mesh;
application state (mode, last punch sequence) never appears in mesh headers.

**Test levels** (MP23), to extend if they prove insufficient: (1) host unit
tests of the pure logic (frame coding, CCM vectors, route computation,
parent and rank, Trickle, class 2 receiver, replay table, duty-cycle
accounting) with injected clock and storage; (2) host multi-node scenarios
on a scripted medium (relay reboot with a queued message, replay after a
power loss, loop during repair, flooding node, partition and return);
(3) real boards, multi-hop on one bench through the neighbour filter, for
every radio measurement. There is no radio propagation model: real boards
give the radio behaviour, the host covers scale, reproducible fault
scenarios and regression.

## 2. Properties

Properties sought from the mesh. Each one is tracked until it is assured by a documented mechanism and verified by a
test or a measurement. **Status**: *sought* (required, mechanism not yet
designed), *designed* (mechanism in this document), *verified* (test or
measurement recorded).

| Id | Property | Status | Mechanism / verification |
|---|---|---|---|
| **Services** ||||
| MP1 | Any-to-any unicast: node → root with **implicit root address**, root → node, node ↔ node (**P2P**, explicit addresses); network-wide broadcast | designed | §9 (upward, broadcast), §10 (downward/P2P, route computation), §11 (addressing modes) |
| MP2 | Application-agnostic: opaque payloads multiplexed by an 8-bit port; the mesh never interprets payloads; applications act on the mesh only through documented parameters | designed | layering (§1); relays never read application payloads (§11) |
| MP3 | Transparent to applications: they address nodes (or the root implicitly), never routes; topology changes appear only as delay or explicit failure | designed | §12: applications address nodes or the root; topology changes show as delay or explicit failure |
| MP4 | Service classes chosen per message: **class 0** datagram (at most once, optional replace-by-newer); **class 1** hop-by-hop reliable (passive ack by overhearing the next hop, piggybacked ack on the last hop, 3 attempts, held during route repair); **class 2** end-to-end reliable (exactly once, unordered; NACK on sequence gaps, tail announcement, lazy cumulative ack, sender buffer) — SILy's punch recovery made generic | designed | §12: class 0 (replace by newer), class 1 (passive and explicit acknowledgements, 3 attempts, holding), class 2 (class 1 per hop + NACK, tail, lazy cumulative acknowledgement, send window) |
| MP5 | **No silent loss**: in classes 1 and 2 a message is delivered, or its sender gets an explicit failure (no route, queue or buffer full, duty-cycle budget, timeout) | designed | §12: failures at submission and from the network (errors are class 1); class 2 kept until acknowledged, *gone* declared at sender reboot; exceptions: duplicates possible across a receiver reboot; in class 1, a message lost by a relay that reboots after acknowledging it (class 2 recovers it), and a message failed by a relay cut off from the root when its origin is cut off too (no link, no failure can travel; class 2 recovers it) |
| MP6 | Explicit limits: maximum payload announced (≈ 200 B), no fragmentation in v1 | designed | §11: 200 B announced |
| MP7 | Priorities chosen by the application, mapped to duty-cycle allowances; fairness between forwarded and local traffic | designed | §6: priority implicit in the port (application ports 16–127 high, 128–255 low), allowances per priority; §7: round robin by origin within a priority, per-origin queue quota with back-pressure |
| MP8 | Compact headers: fields implicit when deducible from context (root address upward, root origin downward), SCHC-style | designed | §11: implicit root, first-hop compression, implicit last length, bundles |
| **Robustness** ||||
| MP9 | Duty-cycle compliance guaranteed whatever the applications do; mesh control traffic bounded, its budget known per SF | designed | §6; budget §16 (SILy) |
| MP10 | Self-forming, self-healing: no configuration beyond network key and radio parameters; repair within a bounded time stated per SF | designed | §9: local repair by parent set, detach and rejoin, global repair by DODAG version; repair times per SF (§9 table) |
| MP11 | Loop freedom, or loops detected and broken immediately (rank / feasibility condition, data-path validation, hop limit); *immediately* = negligible against the system's time step, the presence period | designed | §9: RPL rank rule, rank following within L + 56, poisoning before rejoin, data-path validation with duplicate cache; no hop limit needed. No loop with at most one consecutive loss per link (TLA+ `DODAG`); beyond, a loop ends by a parent switch or at the L + 56 bound: about 1 s with traffic, at most about 7 Imin idle (0.4–0.6 presence period at SF7–12), without loss of class 1/2 messages (custody): negligible against the presence period, hence accepted |
| MP12 | Stability: hysteresis, no route flapping; link quality (margin, delivery ratio) preferred over hop count; asymmetric links detected | designed | §9: ETX over 16-frame windows, margin eligibility with 3/6 dB hysteresis, ETX ≤ 4 eligibility (waived, for reachability alone, only on the link to the root and the last candidate's), parent switch threshold, rank hysteresis; asymmetric links flagged by the root |
| MP13 | Bounded resources: sized tables (default 64 nodes, depth 8), bounded queues and buffers, documented degradation beyond | designed | §12 (resource table and degradation), §9 (neighbour table), §10 (graph, depth) |
| MP14 | Reboot tolerance: a rebooted node or root rejoins without operator action; no persistent state required except security counters and the duty-cycle window (§6) | designed | §9: a node solicits at boot and rejoins; the root listens one presence period and issues the next DODAG version (no persistent routing state); persistent state limited to the frame counter and class 2 sequences in NVS, replay table and duty-cycle window in RTC memory (§6, §14) |
| MP15 | Partition tolerance: an isolated node stays audible, is reported as soon as a neighbour hears it and rejoins automatically; class 1 holds messages during repair; no long-term store-and-forward (application journals do that) | designed | §9: a node without route solicits once per presence period and appears in its neighbours' topology reports; §12: class 1 and 2 held 2 presence periods |
| MP16 | Coexistence: several networks on one channel (network identity / key); foreign LoRa traffic filtered (private sync word) | designed | §14: private sync word filters LoRaWAN; other networks fail authentication, counted and reported; two networks in range tolerated, not supported (shared capacity) |
| MP17 | Security: only authenticated nodes inject traffic or routing information; replay protection; limits of a network-wide shared key documented | designed | §14: AES-128-CCM on every frame (4-byte tag), nonce from address and lifelong frame counter, replay table, PBKDF2 key derivation, staged key switch, next key wrapped under the current key for distribution by the application; threat model and limits of the shared key |
| MP27 | Regulatory compliance by construction: channels, bandwidth, power, duty cycle, maximum transmission time and mandatory listen-before-talk come from a **regional profile** (data, not code) selected by configuration; radio settings outside the profile are refused; the mesh enforces the profile's access rules whatever the applications do | designed | §4: profile table, configuration validated against it, custom profile flagged as the operator's responsibility; §6 duty cycle, §8 listen-before-talk where mandatory, §9 profile identifier checked between nodes |
| **Supervision** ||||
| MP18 | Local observability on every node: neighbours (margin, delivery ratio, age), routes, parent, queues, duty-cycle use, counters (TX, RX, drops by cause, authentication failures, backoffs), route-change log with causes | designed | §13: neighbours, routing state, queues, duty cycle, counters by cause, route-change log |
| MP19 | Remote supervision: presence and topology reports to the root (a mesh function; they feed the application's mesh map); remote query of a node's mesh state | designed | §13: presence and topology reports, root alarms (subtree silent), management query (port 8) |
| MP20 | Diagnostics: ping (round trip, hop count), traceroute (path with per-link quality) | designed | §13: echo (port 6) with hop counts, traceroute (port 7) with per-link margin and cost |
| MP21 | Explainable behaviour: documented parameters derived from SF, adjustable at run time (e.g. longer periods in SILy *retrieval*); the reason for a parent choice can be shown | designed | §9: Imin, timeouts derived from SF, parameters disseminated with the DODAG version; §13: reason for the preferred parent, route-change causes |
| MP22 | Misconfiguration detection: wrong key seen by neighbours as "unknown sender, authentication failed"; duplicate address and duplicate root detected and reported. A wrong SF cannot be detected over radio (check by WiFi or display) | designed | §13: authentication failures per sender, address conflict flag, second root, foreign profile, unknown version; wrong SF undetectable (§5) |
| **Engineering** ||||
| MP23 | Host-testable: protocol logic runs on the MicroPython unix port; multi-node scenarios on a scripted medium (loss, asymmetry, partition, reboot); no radio propagation model (radio behaviour is measured on real boards) | designed | §1: radio and platform interfaces, neighbour filter for multi-hop on real boards, test levels; §10: route computation a pure function of the root's graph; §14: replay after a power loss as a host scenario |
| MP24 | Radio isolated behind a small interface (host tests, portability to other boards) | designed | §1: send, receive, carrier sense, link measurements |
| MP25 | Versioned protocol: version field; other versions rejected cleanly and reported | designed | §11: 2-bit protocol version with an escape value to an extended version byte; other versions rejected and counted |
| MP26 | Reusable: no dependency on SILy; candidate for a separate project | designed | §1 layering; Part I independent of Part II, SILy appears in Part I as examples only |

Non-goals (v1): radio sleep scheduling or duty-cycled listening (nodes always
receive: the SX1262 draws about 5 mA in continuous reception, little next to
the awake ESP32); several roots; fragmentation; time synchronisation (SILy:
punch times come from the SportIdent stations).

## 3. State of the art and borrowings

No standard fits as is (survey of October 2026):

- **No LoRa mesh standard exists.** No IETF RFC or active draft on LoRa mesh
  routing; LoRaMesher was presented at IETF 122 (GAIA, March 2025) as a talk,
  not a draft; the only IETF LoRa draft (header compression, 2016) has expired
  and notes that LoRaWAN has no routing nor multi-hop. LoRaWAN is a star; its
  Relay extension adds a single hop.
- **IPv6 low-power stacks** (RPL + 6LoWPAN, Thread, Wi-SUN) target 802.15.4
  at 250 kb/s: headers and control messages too verbose for a 10 % duty cycle
  at a few kb/s, no MicroPython implementation. Pymesh (used by SIPy) was
  OpenThread over LoRa: it worked at SF7 but was a heavy black box, abandoned
  with Pycom.
- **Community LoRa meshes** target human messaging, not collection towards a
  sink, and are complete C++ firmwares, not embeddable libraries:
  **Meshtastic** (managed flooding with hop limit; since 2.6, direct messages
  follow a next hop learned from a first flood, falling back to flooding);
  **MeshCore** (dedicated repeaters, path discovery by flooding then source
  routing); **Reticulum** (strong cryptography, heavy announces, CPython).
- **LoRaMesher** (SILy-Arduino) is a minimal distance vector: hop-count
  metric, full routing table in every HELLO — too costly in airtime and too
  poor in link quality for the properties above.

The mesh is therefore designed for SILy, as **a simplified RPL for LoRa at
10 % duty cycle**: proven mechanisms are reused, ICMPv6/IPv6 encodings are
replaced by a compact binary format with implicit fields. Each mechanism of
this document cites its source and what was changed.

| Need | Source | What is borrowed |
|---|---|---|
| Topology, upward/downward routes, P2P | **RPL** (RFC 6550) | DODAG rooted at the root, parent set with preferred parent; rank rule against loops; local repair by switching parent, global repair by version number; storing vs non-storing modes (§10) |
| Parent choice | **MRHOF** (RFC 6719), **CTP** (TinyOS) | ETX metric with hysteresis; CTP data-path validation (a packet going up towards a higher cost reveals a loop, detected without extra traffic) |
| Loop freedom during repair | **Babel** (RFC 8966) | Considered and rejected: feasibility condition with a root-issued sequence number; the RPL rank rule is kept (§9) |
| Link estimation, asymmetric links | **CTP** (4-bit link estimator), **Thread** (link quality in/out exchanged between neighbours) | Combine beacon reception and class 1 acknowledgements: ETX measured at no airtime cost |
| Control traffic cost | **Trickle** (RFC 6206) | Route advertisements fast after a change, increasingly spaced when stable, suppressed when neighbours already said the same; presence stays periodic for supervision |
| State dissemination | **Trickle** / Drip (TinyOS) | Versioned value disseminated to every node, including late or rebooted ones |
| Flooding when needed | **Meshtastic** | Rebroadcast delay from SNR (farthest node first, others suppress); its hop limit is replaced by duplicate detection (§9) |
| Compact headers | **6LoWPAN / SCHC** (RFC 8724) | Implicit fields deduced from context (root address upward) |
| Service classes | **CoAP** (CON/NON, RFC 7252), **MQTT-SN** QoS 0/1/2, **NORM** (RFC 5740), **SCHC** ACK-on-Error | At most once / at least once / exactly once semantics; NACK-based reliability with tail announcement (class 2) |
| Security (§14) | **IEEE 802.15.4 / Thread**, **LoRaWAN** | CCM with a nonce from (address, frame counter) and a 4-byte MIC; persistent frame counters; Thread's key switch on the first frame received under the next key |
| Supervision | **RPL non-storing**, **Thread Network Diagnostics** | Root rebuilds the whole graph and DODAG; diagnostic queries as TLVs; ping, traceroute |

Sources: [LoRaMesher at IETF 122 GAIA](https://datatracker.ietf.org/meeting/122/materials/slides-122-gaia-loramesher-for-lora-mesh-networks-00),
[draft-vilajosana-lpwan-lora-hc-00](https://datatracker.ietf.org/doc/html/draft-vilajosana-lpwan-lora-hc-00),
[LoRaMesher](https://github.com/LoRaMesher/LoRaMesher),
[MeshCore](https://en.wikipedia.org/wiki/MeshCore),
[Meshtastic vs MeshCore (2026)](https://adrelien.com/meshtastic-vs-meshcore-i-run-both-heres-the-honest-truth-2026/);
RFC 6550, 6206, 6719, 6997, 7252, 8724, 8966, 5740. Meshtastic 2.6 and
MeshCore details come from secondary sources, to be checked against the
projects' documentation before relying on them.

## 4. Regulatory frame

Source: ARCEP draft decision on short-range devices (public consultation of
11 September 2026), consistent with EU decision 2019/1345 / ERC Rec 70-03.
The decision in force (ARCEP 2014-1263 as amended) is to be checked before
release (§18).

| Band (MHz) | Power | Access | Use |
|---|---|---|---|
| 869.4–869.65 | 500 mW ERP | duty cycle ≤ 10 % (or LBT + AFA) | **v1 channel: 869.525 MHz, BW 125 kHz** (one channel fits) |
| 865.6–865.8, 866.2–866.4, 866.8–867.0, 867.4–867.6 | 500 mW ERP, data networks only, ≤ 200 kHz | adaptive power control required (or LBT + mitigation); duty cycle ≤ 10 % for network access points, **≤ 2.5 % otherwise** | not used in v1 |
| rest of 865–868, 868.0–868.6 | 25 mW ERP | 1 % (or LBT + AFA) | not used |

- Duty cycle is measured over a one-hour observation period (ETSI EN 300 220).
- The four 86x.x channels are limited to 2.5 % for nodes, so v1 runs the whole network on 869.525 MHz at
  10 %; the firmware allows itself 90 % of it: **324 s of transmission per
  hour per board**.
- LBT + AFA (polite spectrum access instead of duty cycle) is not claimed:
  AFA needs several channels and the conformance requirements of EN 300 220
  are out of reach. Listen-before-talk is still used, for collision avoidance
  only (§8).
- LoRa parameters: BW 125 kHz, CR 4/5, explicit header, CRC on, **preamble 8
  symbols** (the micropython-lib driver defaults to 12; set `preamble_len`
  explicitly). The time-on-air formula of `tools/mesh_budget.py` (Semtech
  SX1261/2 datasheet) gives 57 / 371 / 1319 ms for 20 B at SF7 / 10 / 12.

### Regional profiles (MP27)

The firmware complies **by construction** only for what it controls, and
only if the radio configuration cannot leave the profile:

- **Enforced by the mesh**: duty cycle (§6), and in other profiles maximum
  transmission time and mandatory listen-before-talk.
- **Enforced by configuration validation**: the profile fixes
  frequency, bandwidth and coding rate and bounds the TX power; the operator
  chooses the profile, the SF, the TX power and the antenna gain, and settings
  outside the profile are refused. ERP depends on the antenna: the profile
  limit is checked against TX power + antenna gain (configuration parameter)
  − 2.15 dB. In the EU g3 sub-band, 22 dBm (SX1262 maximum) stays under
  500 mW ERP up to about 7 dBi of antenna gain.
- **Custom profile**: frequency, bandwidth (7.8–125 kHz),
  coding rate (4/5–4/8), TX power (−9 to 22 dBm, SX1262 limits) and duty
  cycle entered by the operator, for use outside the supported regions; the
  spreading factor stays 7–12 under every profile. Compliance is then the **operator's responsibility**:
  the local UI flags it, and the profile identifier 0xFF in the DODAG
  information message (§9) lets the root show it on the map and in its alarms
  (§13). The mesh still enforces the duty cycle entered (§6). Timers derived
  from the slot (§8) follow the actual time on air, but the periods of §16
  and §17 assume 125 kHz and CR 4/5: under a custom profile the operator sets
  the presence and topology periods (computed with `mesh_budget.py`).
- **Out of the firmware's reach**: conformity of the hardware itself
  (RED / EN 300 220 emissions: LilyGo's CE marking), and the decision in force
  (ARCEP 2014-1263 as amended, not yet read, §18).

A profile is a table: channel(s) with frequency, maximum bandwidth, maximum
power (ERP or EIRP), and access rules (duty cycle and its window, maximum
transmission time, LBT threshold and sensing time, minimum pause, hopping
requirement). The mesh reads it in three places: duty-cycle accounting (§6),
carrier sense (§8; mandatory LBT turns it from collision avoidance into a
regulatory check with the profile's threshold and sensing time), and frame
construction (the maximum transmission time caps the aggregated frame size
per SF, hence the maximum payload of MP6). The airtime budget (§16) is
recomputed per profile; it is already bounded by channel load rather than by
the duty cycle, so periods change little where no duty cycle applies, and a
self-imposed allowance is kept anyway for fairness.

v1 ships the **EU 869.525 MHz** profile (identifier 1) and the custom
profile (identifier 0xFF); identifier 0 is unused. Other regions
differ in ways that matter to the design (to verify from the texts before any
implementation):

| Region | Main constraints | Impact |
|---|---|---|
| UK | Same sub-band and 10 % rule (Ofcom IR 2030) | profile data only |
| USA (FCC §15.247, 902–928 MHz) | no duty cycle; with < 250 kHz channels, frequency hopping over ≥ 50 channels with ≤ 0.4 s average occupancy per 20 s; digital modulation (DTS) needs ≥ 500 kHz bandwidth | a single 125 kHz channel is not allowed: either LoRa 500 kHz on one channel (≈ 6 dB less sensitivity than 125 kHz) or synchronised hopping, which the v1 single-channel mesh does not support |
| Japan (ARIB STD-T108, 920 MHz) | carrier sense mandatory for most station classes, limits on transmission time and pauses | LBT becomes regulatory (§8 thresholds from the profile), frame duration capped |
| Australia / NZ (915–928 MHz) | class licence, power limits, no duty cycle | profile data, possibly 500 kHz or hopping as for the USA (to check) |

Multi-channel operation (hopping, or the four EU 86x.x data channels with
adaptive power control) is a separate evolution: it needs every node to agree
on a channel schedule, which touches the whole mesh.

Sources: [47 CFR §15.247](https://www.law.cornell.edu/cfr/text/47/15.247),
[RAK certification guide](https://docs.rakwireless.com/product-categories/software-apis-and-libraries/rui3/certification-guide),
[ARIB STD-T108 overview](https://www.arib.or.jp/english/std_tr/telecommunications/desc/std-t108.html).

## 5. Spreading factor

**v1: one fixed SF for the whole network, set by configuration** (same
value on every board, e.g. through a JSON configuration import). No SF
change at run time. Rationale: stabilise everything else first; changing
SF on the fly adds transient states (timed switch, boards missing it, search
for the network) before losses and margin stability are even measured, and
gains capacity, not reliability.

- **Margin display (v1).** The receiver of a frame estimates the link SNR
  (packet SNR when below +5 dB, otherwise RSSI minus the measured noise
  floor, since the SX1262 SNR saturates) and reports it in its topology
  report. The root shows, per link, the margin `SNR − limit(SF)` with
  demodulation limits −7.5, −10, −12.5, −15, −17.5, −20 dB for SF7–12
  (Semtech datasheet), and the **lowest SF for which every node keeps a route
  whose links all have a margin ≥ M** (default 10 dB: forest, bodies, rain,
  antenna orientation), together with the airtime budget at that SF (§16 for
  SILy). The operator uses it to choose the SF during deployment; nothing is
  automatic.
- **Lost node (v1)**: route repair at the same SF only. A node without a
  route keeps listening and reports itself as soon as a neighbour hears it;
  the root alerts the operator.
- **Why the lowest SF matters**: SF7 has ~20× the capacity of SF12 (§16).
  The goal remains an adaptive SF chosen by the mesh during deployment,
  deferred until losses and margins are measured on the bench and in the
  field.

### Adaptive SF (deferred, not in v1)

Recorded so the v1 design does not preclude it:

- One SF for the whole network: the SX1262 receives one SF at a time, so a
  board cannot change SF alone; all boards follow a timed, disseminated
  command "SF *s* from *t* + Δ" (relays correct Δ by their forwarding delay).
- A board that hears no valid frame for some presence periods stays at the
  last known SF and makes periodic excursions to the higher SFs; it thus
  finds the network after a switch it missed (self-healing).
- A silent node may be lost (fading) or dead; a step-up helps only in the
  first case. Hint: weak margin before vanishing → fading likely; abrupt
  silence with a comfortable margin → failure likely.
- Policy while the network is in use is open; stability comes first.
- Per-link SF (each node listening on its own SF) would avoid slowing the
  whole network, at the cost of scheduling and of overhearing-based routing.

## 6. Duty-cycle accounting

- **Sliding window**: 60 full bins of 60 s plus the current bin, holding the
  airtime sent (ms); a charge counts for at least one hour (with 60 bins
  including the current one, it would count only 59–60 minutes).
  Before each transmission, `sum(bins) + toa(frame) ≤ allowance(priority)`;
  the frame is charged at TX start, so a reset during transmission still
  counts it.
- **Time on air**: computed with the formula of §4 before sending; after TX
  done, the measured duration (TX start to TX-done IRQ) is accounted if longer.
- **Priorities and allowances** (fraction of the budget; 324 s in the EU
  profile):
  1. repair and reliability: acknowledgements (port 3), errors (port 4),
     class 2 control (port 10), poisoning, solicitations and advertisements
     after a Trickle reset (port 9); high-priority application messages
     (ports 16–127; SILy: punches, mode commands): 100 %;
  2. presence reports (port 1), periodic advertisements and the root beacon
     (port 9): 80 %;
  3. topology reports (port 2), node information (port 5), diagnostics
     (ports 6–8), low-priority application messages (ports 128–255): 60 %.
  A refused priority-2/3 report is dropped (the next period replaces it); a
  refused class 1/2 message stays queued and is aggregated when budget frees
  up, or fails explicitly on timeout (MP5).
- **Priority implicit in the port**: a relay deduces the
  priority of a forwarded message from its port, so the format carries no
  priority field (MP8); an application chooses its priority by choosing its
  port (MP7). Port 9 is link-local, never forwarded: its sender knows whether
  it advertises after an event (priority 1) or periodically (priority 2).
- **Mixed frames**: a message joins an aggregated frame (§7) only if the
  frame, with it, stays within the allowance of the message's own priority;
  low-priority messages cannot ride on high-priority frames beyond their
  allowance.
- **Reboot**: the window is kept in RTC memory (survives soft resets and the
  watchdog, not power loss). After a power loss the board starts with an
  empty window; residual risk accepted (rare, at most one hour slightly
  above budget).
- **Root**: same accounting.
- **From the profile**: the duty-cycle limit and its window come from the
  regional profile (§4): 10 % over one hour in the EU profile (2.5 % would
  apply to the 86x.x data channels); a custom profile carries the operator's
  value. The mesh allows itself 90 % of the limit.

## 7. Queues and scheduling

Fairness between forwarded and local traffic (MP7) rests on round robin
between origins and a per-origin queue quota: a node flooding the network
gets its share, not its relays' capacity.

- One transmit queue per board. Order: **priority** (from the port, §6)
  first; within a priority, **round robin by origin**, the node's own messages
  counting as one origin whatever their port; age within an origin. When a
  frame or the duty-cycle allowance cannot take every queued message, a relay
  thus shares its capacity equally between its own traffic and each origin of
  its subtree, whatever their volume.
- **Per-origin quota**: when the queue holds more than half its size (32 of
  64 messages, §12), a message from an origin that already holds 16 is
  refused: a forwarded class 1/2 message is not acknowledged (its sender keeps
  it and retries; the back-pressure reaches the origin, whose class 2 window
  fills and refuses submissions, MP5); a forwarded class 0 message is
  dropped; a local submission is refused. A node flooding the network gets
  its share, not its relays' capacity. Refusals are counted per origin (§13).
- **Strict priority between levels**: under sustained priority-1 load,
  priority-3 messages wait. Periodic reports replace older ones in the queue
  (class 0, replace by newer, §12), so waiting costs no memory.
- A board transmits at most one frame at a time and returns to receive
  immediately (half duplex).
- Periodic timers (presence, topology, root beacon) are jittered by ±10 % so
  that boards switched on together do not stay synchronised.
- A relay waits a random delay in [0, ToA(presence frame)] before forwarding,
  to desynchronise relays that heard the same frame.
- **Opportunistic aggregation**: when a board gets the channel, it packs the
  queued messages bound to the next hop of the head of the queue into one
  frame, in queue order, up to 255 B (or the profile's maximum transmission
  time, §4) and within the allowances (§6). No
  added latency at low load; throughput scales under bursts. Messages are
  opaque to relays (mesh message header only), so relays unpack and repack
  frames, hence **hop-by-hop protection**. With a single network key shared by
  every board (§14), end-to-end encryption would add no confidentiality
  towards relays.

## 8. Medium access

- **Carrier sense** before each transmission, the channel being busy if
  either:
  - the radio is receiving a frame: PreambleDetected or HeaderValid IRQ seen
    without RxDone/timeout yet (these IRQs must be added to the driver's IRQ
    mask, as CAD_DONE was in the platform spike). This also covers frames below the
    noise floor that RSSI cannot see;
  - instantaneous RSSI (GetRssiInst) is above the noise floor + 10 dB. Noise
    floor measured at boot and refreshed when idle (platform spike: −112 dBm median;
    bench: −104 and −112 dBm on two boards, `docs/measurements.md`).
- CAD is not used (platform spike: preamble only, poor detection).
- **Backoff**: when busy, wait a random number of slots in [1, CW], slot =
  ToA of a presence frame at the current SF; CW starts at 4, doubles up to 32;
  after 6 attempts a priority-2/3 report is dropped, a class 1/2 message (and
  a priority-1 class 0 message) is retried later: a new procedure after one
  more backoff of up to 32 slots. The slot is the time on air of a 17 B frame
  (SILy presence size), fixed whatever the presence extension.
- **Hidden terminals** remain (nodes that hear the relay but not each other).
  Accepted: class 1/2 recover losses, presence losses are absorbed by the
  silence threshold (`K_MISS`); measured loss rates decide whether more is
  needed (§18). Two hidden senders whose frames collided at their common
  next hop time out together: a class 1 retry waits a random delay after
  its timeout (§12), so that their retries do not collide again; its range
  doubles while the sender's frames keep timing out.

## 9. Routing and repair

Upward routing is a simplified RPL (RFC 6550): DODAG
rooted at the root, rank rule against loops, local repair by parent set,
global repair by DODAG version. The metric follows MRHOF (RFC 6719), loop
detection on the data path follows CTP, control timers follow Trickle
(RFC 6206). Choices: RPL rank rule rather than Babel's feasibility
condition (one destination, the root, which is what RPL is designed for;
Babel needs per-destination sequence requests), with the other candidates
tested against the node's lowest rank (DUAL's feasibility condition, no
sequence number); ETX metric with the margin
as an eligibility filter (link quality rather than hop count, MP12); global
repair on demand only (each one resets Trickle in every neighbourhood,
finding 4 below); parent declared lost when silent (2.5 presence periods:
silence limit, below), which costs no airtime.
Figures come from section E of `mesh_budget.py`.

### Link estimation

Every node keeps a **neighbour table** (64 entries at most, MP13; when full,
the stalest entry that is not a parent is evicted; an entry not heard for 4
presence periods is removed): address, DODAG version and rank from its last
frame, last frame counter, reception window, margin, time last heard,
acknowledgement window and the time of its newest failure.

- **Inbound delivery ratio** `d_in`: frames received / frames sent by the
  neighbour, from the gaps of its frame counter (§11) over its last 16 counter
  values. Every frame counts, whatever its link destination: the channel is
  shared. A gap above 16 (long silence, reboot) restarts the window. No
  airtime.
- **Acknowledgement window** `p_ack` = acknowledged / samples over the last
  16 samples, **one sample per frame** sent to the neighbour (next hop, low
  16 bits of the frame counter), whatever the number of messages it carries:
  a success at the first acknowledgement of one of its class 1/2 messages
  (an explicit entry acknowledges them all, §12; a **passive
  acknowledgement**, the node overhearing the parent forward one of them
  within its forwarding delay, §7, proves the frame was received), a
  failure when they all time out (§12). Towards the root, the failure is
  decided one root timeout after the timeout, and an acknowledgement of the
  frame arriving before (late, §12) makes it a success instead; **one failed
  sample per batching delay**: a timeout gives no failed sample if its
  frame ended within one batching delay (§16) of the frame of the last
  failed sample towards the root. The root acknowledges in batches, so one
  lost batch frame leaves several frames of a relay unacknowledged at once:
  it is one loss event, and one failed sample per frame would make
  congestion at the root read as a bad link (rank jump of the relay, TLA+
  `AckWindow_rootbatch_P4`). Successes are still sampled per frame; the
  attempts of one message are more than a root timeout apart, longer than
  the batching delay, so each failed attempt is still one sample. Cost:
  separate frame losses within one batching delay count once, so a lossy
  link to the root reads slightly better. **Presence reports** are included,
  matched by origin: a presence sent to a preferred parent other than the
  root, in a frame without class 1/2 message, is one sample, a success when
  the node overhears the parent forward a presence of its origin, a failure
  after the class 1 timeout towards a relay (§12: a forward later than that,
  behind priority-1 traffic or the parent's own allowance, counts as a
  failure); no retry. When the parent is the root, which forwards nothing,
  the root's class 1 acknowledgements (§16) play this part when there is
  class 1 traffic.
- **Failure ageing**: once the newest failure of a window is a silence limit
  old (2.5 presence periods), the failures leave the window and its
  successes are kept. A link that failed during a cut and is heard again
  thus reads good again within 2.5 periods of its last failure, without
  any frame sent to it (TLA+ `LinkCut`, P3: the bound is exact, idle or
  busy, presence samples or not).
- **ETX** (expected transmissions, MRHOF): `1 / p_ack` once the link has **4
  samples** (overhearing the forward tests both directions); `1 / d_in²`
  before (symmetry assumed until the link is used). **Latch**: once an entry
  has held 4 samples, `p_ack` is used whenever its window is not empty
  (after an ageing too), so that the estimate does not jump between `d_in`
  and `p_ack`; a new entry clears it. With these rules (TLA+ `AckWindow`)
  one lost acknowledgement never moves the advertised rank of a relay on a
  perfect link (4 is the smallest count that ensures it, 3 keeps it below
  rank + 8), and a bad link becomes ineligible after 4 consecutive failures
  on a fresh window, 13 whatever its history (13 lost presences, i.e. 13
  periods, when presence is the only traffic). The link to the root, and
  the preferred parent's while it is the node's last candidate, are judged
  by their reachability instead (below).
- **Silence limit**: a neighbour not heard for **2.5 presence periods** is
  silent (strict: heard less than 2.5 periods ago, it is not). Every node
  sends at least one frame per period jittered by ±10 % (§7: presence), so
  one lost frame is tolerated while the on-air delay of the sender's
  periodic frames (queue, carrier sense, duty cycle) varies by up to T/4,
  and two lost frames are detected while it varies by less than T/5 (TLA+
  `Silence`, both bounds tight); between the two, two losses may go
  undetected until the next frame. The root sends its beacon every half
  period: three lost beacons are tolerated (§9, Trickle and control
  traffic).
- **Margin**: SNR margin above the demodulation limit of the SF (§5),
  averaged over the last 8 frames. A link is **eligible** as a parent link if
  its margin is ≥ 3 dB and its ETX ≤ 4, except that the link to the root
  and the preferred parent's while it is the node's **last candidate** are
  judged by reachability instead of ETX (below); once
  below 3 dB, it becomes eligible again at 6 dB (hysteresis, MP12). Among
  equal path costs, the larger margin wins.
- **Reachability** (RPL neighbour unreachability detection style): a link
  judged by it is ineligible while **unreachable**: the node's last **4**
  failed samples
  towards the neighbour are consecutive (the samples of the acknowledgement
  window; towards the root one per batching delay: one lost batch counts
  once) **and** no acknowledgement of the neighbour came for one silence
  limit, measured from the last one (an explicit entry of the neighbour
  for the node, whatever the frame; a late one of the root, §12; a passive
  one, the node overhearing the neighbour forward its message, with or
  without a sample), or from the entry's creation. Any acknowledgement of
  the neighbour ends the run and makes the link reachable again; beacons
  and other frames do not. The probes are the node's own class 1
  transmissions towards the neighbour (fresh attempts while it is kept,
  §12) and its presence reports to a relay parent: no extra frame.
  Reachability is read at each selection (the root's beacons give one
  every half period, a relay's presence one every period). Failure ageing
  ends the run with the failures it removes (the link then gets 4 more
  before it is left), the time of the last acknowledgement stays: a node
  that stops sending to the neighbour (it left it) sees it eligible again
  a silence limit after its last failure, as with ETX. Taking a relay as
  the preferred parent restarts its reachability, as at the entry's
  creation (below).
  - **The root and the last candidate**: the link to the root, and the
    preferred parent's while no other neighbour is a candidate (drops
    included; the other candidates do not depend on the preferred parent,
    so they are found first, with the ordinary rule), are not tested
    against ETX ≤ 4: reachability alone. Under hidden terminals the root's
    batches collide at a root child (D28): its ETX to the root climbs
    above 4 while the root still answers now and then, and ETX alone
    would remove the root, the root child's only candidate under L,
    detaching its subtree. The same holds for a relay that is a hop-2
    node's only candidate (the 30-node star, D28: the Nodes behind the
    one relay drop it after collisions at it and detach). A dead uplink
    to such a parent, still heard, is left about one silence limit after
    its last acknowledgement (§9 repair times), at any ETX. The ETX used
    for the **link cost** of such a link **is capped at 4** (cost 32): a
    reachable parent at ETX above 8 would otherwise cost more than L + 56
    and leave the candidates through the bound. Decided at each
    selection, never remembered: once the node has switched away from it,
    the old parent needs ETX ≤ 4 again (TLA+ `DODAG`, `Elig = "reach"`;
    relays: `Exhaust = "keepboth"`, `Elig = "reachall"`).
  - **Why only the last candidate** (MP12): waiving ETX ≤ 4 on every link,
    with every cost capped at 32, would make a link of ETX 10 cost as one
    of ETX 4 and stay a candidate; it would lose preference only to a
    candidate cheaper by the switch threshold (12), so a preferred parent
    at ETX 10 would be kept over an alternative one hop deeper at ETX 2
    (gain 8), and a fresh selection could pick it. While the node has
    another candidate, the ETX rule moves it there instead; it costs a
    detach only when there is none, which is where reachability replaces
    it.
  - **Restart at a parent change**: a relay taken as the preferred parent
    (by ETX ≤ 4, or as the first one) gets 4 failed samples and a silence
    limit of probes before it is judged unreachable, its run and the time
    of its last acknowledgement restarted. Otherwise a link back after a
    cut, or a last candidate left unreachable while its ETX was still ≤ 4
    (a dead uplink is left before its ETX passes 4), would be taken at the
    rejoin and left at the next selection, before its first probe (the
    presence sent at once after the change) is acknowledged. Over a dead
    uplink the node still leaves and takes the relay again, once per
    silence limit and rejoin, until its ETX passes 4 or the relay is no
    longer heard; before, a drop after 3 attempts did so at every message.
- **Asymmetric links** (MP12): locally, a parent heard well (high `d_in`) but
  forwarding poorly (low `p_ack`) loses preference through its ETX (as the
  node's last candidate it is kept while reachable: no better link is
  there); at the root, both directions of a link come from the two ends' topology reports
  (each reports its inbound ratio), and links whose ratios differ by more than
  30 points are flagged on the map.

### Rank, parent set and preferred parent

- **Rank** (1 byte): 0 = root, 255 = infinite (no route, or poisoned). One
  hop of ETX 1 costs 8 (RPL MinHopRankIncrease); **link cost** = round(8 ×
  ETX), from 8 to 32 (on a link judged by reachability alone, the root's
  or the last candidate's, the ETX capped at 4, above). **Path cost** through a neighbour m = rank(m) + link
  cost(m); above 254, m is not usable. The depth limit (8, MP13) is enforced
  by the root (§10); with ETX ≤ 3.9 per hop, 8 hops fit in the rank byte.
- **Candidates**: neighbours with the same DODAG version, a rank **strictly
  lower than L**, the lowest rank the node has advertised since it attached
  or adopted its DODAG version (never above its current rank; any finite
  rank for a node without route), heard within the silence limit (2.5
  presence periods, strict), through an eligible link. The **preferred parent** stays a candidate at any finite
  rank (**rank following**, the RPL rule): when its rank rises to the
  node's or above (it fell back on a worse parent), the node keeps it and
  follows the increase, within the L + 56 bound (below), instead of
  detaching with its subtree while its parent stays attached.
- **Parent set**: the 3 candidates of lowest path cost. **Preferred parent**:
  the first one. The node switches preferred parent only when another
  candidate's path cost is lower by at least **12** (1.5 hops, the MRHOF
  switch threshold scaled to this unit), or when the preferred parent leaves
  the set (MP12).
- **Advertised rank** = path cost through the preferred parent. It is
  updated when it differs from the advertised value by 4 or more (half a hop)
  or when the preferred parent changes, so that ETX noise does not ripple
  down the DODAG. A followed increase is at least one link cost (8): it is
  always advertised, resets Trickle, and leaves L unchanged.
- After a change of preferred parent, the node sends its presence report at
  once: the root learns the new path (§10) and the path is tested.
- Precise rules: candidates other than the preferred parent
  (followed at any rank) are compared with **L**, not with the current
  rank (loop freedom below);
  "heard within the silence limit" is strict (age < 2.5 periods); a preferred
  parent outside the 3 lowest costs has left the set; the L + 56 bound
  excludes candidates (the node detaches only when none is left); L
  follows the advertised rank's decreases, and the members other than the
  preferred parent not below L leave the set, so that it never holds a
  member the next selection would refuse; a parent
  dropped after failed attempts stays out until a new frame of it is heard
  (the root, and the preferred parent while it is the last candidate, are
  not dropped while they are candidates apart from drops, §12);
  equal cost and margin go to the lower address.

### Loop freedom (MP11)

- **Rank rule** (RPL): every parent has a strictly lower rank, so ranks
  strictly decrease along any upward path and no loop exists while the views
  are consistent.
- **Bounded rank increase**: within one DODAG version a node never advertises
  a rank above `L + 56`, L being the lowest rank it advertised since it
  attached or adopted that version (56 = 7 hops, the MaxRankIncrease of
  Contiki-NG's RPL). The node follows its preferred parent's rank increase
  within `L + 56`; beyond, the preferred parent is no longer usable and the
  node switches or detaches.
- **Local repair below L**: a move to another parent only uses neighbours
  whose advertised rank is strictly lower than L, not than the node's
  current rank. Every rank the node has advertised since it attached is at
  least L, and a descendant's rank is above every rank of its parent it
  used (one link cost, at least 8, more than the rank hysteresis), so a
  neighbour below L cannot be a descendant, whatever the descendants have
  heard or not yet sent: this **local repair** needs no exchange. A rank
  increase not yet advertised, or not yet heard by the descendants, cannot
  close a cycle. Tested against the current rank instead, a neighbour
  between L and the new rank may be a child still heard at its rank from
  before the increase: taking it closes a cycle with no frame lost (two
  siblings whose link costs rise, each still hearing the other at its old
  rank, take each other; TLA+ `DODAG_chain_current_loop`). The rank last on
  air fails the same way: the children's ranks reach the node later than
  its rank reaches them. This is the feasibility condition of DUAL (EIGRP),
  L playing the feasible distance and the detach, hold and rejoin its
  active state. Cost: fewer local repairs and more detaches, since a node
  whose rank has risen above L moves only to a neighbour below L, otherwise
  it follows its preferred parent up to the bound or detaches and rejoins
  (L restarting); the first local repair after a parent loss is unchanged
  when the node was at its lowest rank (L is then its current rank). The
  argument assumes no node holds a view of an ancestor older than that
  ancestor's last attach, which the detach below ensures with at most one
  consecutive loss per link.
- **Detach (poisoning)**: when no candidate remains, the node advertises rank
  255 at once (Trickle reset, after a random delay of at most 4 slots, as its
  solicitations: siblings detaching together otherwise collide and may
  loop), keeps listening and waits **2 Imin**, so that
  its descendants hear the poison and switch parent or detach in turn. It
  then sends a solicitation **without selecting** and stays detached until
  the solicitation is on air (queue, duty cycle); it rejoins only through
  neighbours heard **after the solicitation**, at any rank (L restarts).
  The solicitation opens an **answer window** of Imin + 8 slots (Trickle
  below) during which the node selects nothing; at its close it takes the
  **best** candidate heard after the solicitation (lowest path cost, the
  switch threshold playing no part), or with none the first candidate
  heard afterwards. Taking the first candidate heard (an earlier rule)
  could keep a worse path for good, a better
  candidate heard later gaining less than the switch threshold (TLA+
  `DODAG_rejoin_current_best`; `Rejoin = "window"` checks RejoinBest). A
  node adopting another version in its window also waits for the close
  (selecting at the adoption violates RejoinBest). The solicitation (rank
  255) is a second poison: a descendant that lost the poison hears it and
  detaches instead of answering. With at most one consecutive loss per link no loop
  forms (TLA+ `DODAG`, `spec/README.md`: line and subtree graphs); two
  consecutive losses on one link (poison and solicitation) may let the node
  rejoin through a descendant. Such a loop is **not broken at once by the
  rank rule**: each member follows its preferred parent's increase (rank
  following). It ends when a member switches to a candidate better by the
  switch threshold, or when the ranks have climbed past `L + 56` and a
  member detaches (the RPL count to the bound, TLA+
  `DODAG_line_follow_witness_bound`): 7 increases of one hop, each
  advertised at once with traffic (data-path validation below), about one
  Imin each when idle (Trickle reset on the increase), so about 7 Imin.
- **Data-path validation** (CTP, RPL §11.2): every frame carries its sender's
  rank. A relay receiving an upward message from a sender of the same version
  whose rank is **not higher** than its own detects an inconsistency: it
  resets its Trickle timer and advertises at once, at most once per Imin
  (otherwise its neighbours' frames could suppress the advertisement, and its
  rank would not reach the sender within Imin), counts a
  rank error (presence flag, §11; MP18), drops a class 0 message, and forwards
  a class 1 or 2 message. A class 1 or 2 message that comes back (in the
  duplicate cache, or its origin is the node) has looped: it is dropped and
  counted as a loop only while the node still holds a copy, otherwise taken
  again (custody, §12), so that a loop never loses it.
- No hop limit is needed: upward loops end by the checks above (a switch, or
  the `L + 56` bound within about 7 Imin); until then a looped class 1 or 2
  message may travel around the loop more than once (custody: a copy that
  comes back after its holder handed it over is taken again), never lost;
  downward routes are finite source routes (§10), broadcasts are
  deduplicated.
- Across versions, ranks are not compared: an upward message from another
  version is forwarded; an older sender triggers a Trickle reset, a newer one
  makes the relay **behind** (DODAG version below): it keeps its preferred
  parent once that parent has moved to the newer version, at its own rank,
  and solicits until it adopts the version. A **detached** node adopts
  another version only from a DODAG information heard **after its
  solicitation**, the rule of the rejoin: during the hold a descendant that
  lost the poison may still advertise, in an older version too, and adopting
  it at once would attach the node through its own child, a loop without
  any loss (TLA+ `DODAG_diamond_versions_adopt`; a new version ends neither
  the hold nor that rule).

### DODAG version and global repair

- The **DODAG version** (4 bits, frame header §11) is issued by the root
  only. A node adopts a newer version when it receives the DODAG information
  message (port 9, below) of that version from a neighbour of finite rank. It
  then takes parents of the new version only (never of an older one),
  restarts L and resets its Trickle timer. Upward traffic keeps flowing during
  the change.
- **Lagging behind a version** (no detach for lagging: one lost
  advertisement must not detach a subtree): a
  node that hears a frame of a newer version from a neighbour of finite rank
  without its DODAG information (a lost advertisement) is behind. It keeps
  selecting in its own version and keeps its **preferred parent once that
  parent has moved to the newer version** (finite rank, heard within the
  silence limit, not dropped after failed attempts), at the node's own rank
  (ranks of different versions are not compared); it forwards through it in
  the old version and solicits with backoff (Trickle below) until it adopts
  the version. Its other candidates keep its own version. If the preferred
  parent is lost meanwhile, the node falls back on a candidate of its own
  version or detaches; after its solicitation it adopts the version it then
  hears (TLA+ `DODAG`, `Behind = "keep"`, `Adopt = "fresh"`: no loop with at
  most one consecutive loss per link; one lost advertisement no longer
  detaches a subtree, `DODAG_line_versions_lag`).
- The version also numbers the **network-wide mesh parameters** carried by
  the DODAG information message: a parameter change (e.g. the periods of an
  operating mode, §17) is a new version. Configuration and routing
  generation are thus checked in every frame header at no cost.
- The root issues a new version (**global repair**) only on demand: at
  **boot** (it listens for one presence period and takes the
  version after the newest one heard, any value if none: no persistent
  state); on **operator command**; on a **parameter change**; on
  **inconsistency** (rank-error flags in 3 consecutive presence reports of
  one node, or a node still unreachable after rerouting, §10). At
  most one version every **4 presence periods**.
- **Serial arithmetic on 4 bits** compares versions less than 8 apart. That
  suffices: a node keeps a finite rank only while it hears a parent, so a node
  cut off from the root detaches within 2.5 presence periods; frames of rank 255
  do not propagate their version, and a detached node adopts any other version
  it hears after its solicitation. Connected nodes follow a new version within
  8 Imin (table below) when no frame of the exchange is lost, a few Imin more
  per lost frame (global repair wave, below), far less than 4 presence
  periods. A node behind solicits at least once per presence period
  and adopts at the first answer heard: lagging by two versions would take
  every answer lost for 4 presence periods while its parent stays heard,
  falling 8 versions behind (incomparable) 28 periods.

### Trickle and control traffic

- Every frame advertises its sender's version and rank (§11), so the content
  of an RPL DIO travels for free. Trickle only decides when a node sends a
  frame **for advertisement alone**: a frame to every neighbour (0xFFFF)
  holding one DODAG information message (23 B).
- **Imin = 32 slots** (§8): 1.6 s at SF7, 5.3 s at SF9, 42 s at SF12. **Imax
  = the presence period**: above it the periodic presence frame advertises
  anyway, so in steady state Trickle sends **nothing**. Redundancy **k = 2**:
  in each interval the node sends at a random time in the second half, unless
  it already sent a frame (any frame advertises) or heard 2 frames of its own
  version.
- **Reset to Imin** on: adoption of a new version; a frame of an older
  version from a neighbour of finite rank; a solicitation; an increase of the
  node's own rank, or its poisoning; a rank error; the first parent acquired
  after boot or detach.
- **Solicitation**: a DODAG information message with an empty payload, sent
  to every neighbour by a node without route: at boot after a random delay in
  [0, Imin], at the end of the detach hold, then once per presence period
  while it has no route. The neighbours reset their Trickle timers and answer
  within Imin. Each of these solicitations opens an **answer window** of
  **Imin + 8 slots** from the time it goes on air, during which the node
  selects no parent: an answer leaves in [Imin/2, Imin) after the
  solicitation is received (at most 1 slot on air), then waits at most one
  busy carrier sense (a backoff of at most CW_MIN = 4 slots, §8) and takes
  at most 1.25 slots on air (23 B): 1 + 4 + 2 slots, plus 1 for the
  scheduling step (the solicitation's own random delay of at most 4 slots
  comes before it goes on air). At the close the node takes the best
  candidate heard after the solicitation that ended its hold, or with none
  the first candidate heard afterwards. A solicitation during the hold
  opens no window, one during an open window (another version heard) does
  not extend it. At boot the first DODAG information is still adopted at
  once (parameters); the parent waits for the close of the window if one
  is open (a DODAG information heard before the boot solicitation goes out
  is adopted with its parent at once). An isolated node thus stays audible: any neighbour that hears
  it lists it in its topology report (MP15).
- A node **behind a newer version** solicits **with backoff** until it
  adopts it: at once (at most once per Imin), then after Imin, the gap
  doubling up to the presence period (10 to 19 Imin at SF7–12): at most 5
  solicitations in the first presence period, then one per period, the rate
  of a node without route. Each one, like any frame of the node (older
  version), resets the Trickle timers of its newer neighbours, which answer
  within Imin. A detach ends them (the hold's solicitation follows).
- The **root beacon** (§16) is the root's advertisement, priority 2, on its
  own timer: every **half presence period** jittered by ±10 % (§7),
  re-armed from each firing. It is not a Trickle advertisement: Trickle
  resets on the root (solicitation, older version, new version) send their
  priority-1 advertisements and neither postpone nor suppress it, so that
  the root's frames are at most 0.55 T apart while no beacon is lost. A
  child of the root, whose silence limit stays 2.5 T, then tolerates
  **three consecutive lost beacons** while the beacon's on-air delay varies
  by up to T/4 (4 × 0.55 T + T/4 < 2.5 T; TLA+ `Silence`, `Silence_half_*`);
  four may go undetected until the next beacon, five are detected while the
  delay varies by up to T/5. With one beacon per period, two lost beacons
  left a gap of up to 3.3 T and detached the root's child with its subtree.
  The half period keeps the detection bounds unchanged (the parent silence
  limit 2.5 T and the root alarm at 4.5 T, `K_MISS` = 4); a silence limit of
  its own for a child of the root would raise false root alarms, and a
  beacon every 2/3 period tolerates two lost beacons only (`spec/README.md`).
  A new version re-arms it on the new period.
- **DODAG information** (port 9, link-local): flags 1 B (bit 0 topology
  report on change, as in *deployment*; others reserved), presence period
  2 B (s), topology period 2 B (s), Imin 1 B (slots), maximum rank increase
  1 B, regional profile identifier 1 B (§4; 0xFF custom); 8 B. Its version is
  the frame header's. A node that receives another regional profile
  identifier does not adopt it and reports a misconfiguration (MP22).
- Precise rules: at Imax nodes send no advertisement (presence
  frames advertise), nor does the root's Trickle timer (its beacon has its
  own timer, above); after a reset caused by a
  solicitation, an older neighbour or a new version, only port 9 messages
  count for suppression (a header alone cannot transfer a version); the
  poison is sent once (a lost one is covered by the solicitation that ends
  the hold, above); a node selects no parent before its first DODAG
  information; a detached node ignores solicitations and older
  versions; versions 8 apart are incomparable (counted, ignored); presence
  period and Imin of 0 are malformed. Trickle advertisements have priority 1
  (each follows a reset), the root beacon priority 2; an advertisement merged
  with a queued root beacon keeps priority 1.

### Repair times per SF (MP10)

| SF | Imin | switch with traffic, parent relay | switch with traffic, parent root | left with traffic, last candidate relay | switch, idle | detach + rejoin | global repair wave (8 hops) | Trickle frames per neighbourhood per reset | node cost at the maximum global repair rate (s/h) |
|---|---|---|---|---|---|---|---|---|---|
| 7 | 1.6 | 12.0 | 92 | 108 | 75 | 7.0 | 13 | 12 | 11 |
| 8 | 3.0 | 21.6 | 92 | 108 | 75 | 12.6 | 24 | 10 | 17 |
| 9 | 5.3 | 38.4 | 183 | 216 | 150 | 22.4 | 42 | 10 | 15 |
| 10 | 10.6 | 76.1 | 366 | 432 | 300 | 44.8 | 84 | 10 | 14 |
| 11 | 21.1 | 153.5 | 915 | 1080 | 750 | 89.7 | 169 | 10 | 12 |
| 12 | 42.2 | 304.0 | 1830 | 2160 | 1500 | 179.4 | 338 | 10 | 11 |

(Times in seconds, from section E of `mesh_budget.py`; the "with
traffic" columns are upper bounds, retry delays at their maximum from the
first timeout after an acknowledgement; "switch, idle" is the silence
limit, 2.5 presence periods; "global repair wave" assumes no frame of the
exchange lost on any hop, see below.)

- **Switch with traffic, parent relay**: a relay preferred parent, when
  the node has another candidate, is dropped
  after 3 class 1 attempts without acknowledgement (§12), the last two
  after retry delays of up to 64 then 128 slots (3.3 then 6.7 s at SF7,
  10.6 then 21.1 s at SF9; up to 256 slots each when other frames of the
  node time out meanwhile, §12). Timeout through a relay: time on air of a 255-byte frame + 6 slots
  (forwarding delay 1, backoff 4, margin 1): 0.7 s at SF7, 2.2 s at SF9.
- **Switch with traffic, parent root**: the root is not dropped after
  failed attempts while it is heard and its link eligible (§12): it is
  left once its link is unreachable (§9 link estimation: 4 consecutive
  failed samples and one silence limit without acknowledgement of the
  root), or at the silence limit if it is not heard at all ("switch,
  idle"), whichever comes first. From the last acknowledgement: the
  decision of the 4th failed sample (the attempts of one message go a root
  timeout and a retry delay apart, with one more root timeout after each
  round of 3, late acknowledgement, §12, and a failure is decided one root
  timeout after its timeout: 6 root timeouts and 3 retry delays at most,
  64, 128 then 256 slots, 37 s at SF7, 67 s at SF8) or the silence limit,
  whichever comes last: the silence limit at every SF; then the next
  selection, at the latest the root's next beacon (0.55 period): 3.05
  presence periods, above "switch, idle", which counts no selection
  delay. Root timeout: its acknowledgement batching delay (§16) + one
  acknowledgement frame + 6 slots: 2.2 s at SF7, 7.5 s at SF9. Slower than
  a drop after 3 attempts would be (4 root timeouts: 9 s at SF7, 30 s at
  SF9), but a root child does not detach, its subtree following, after
  frames lost to collisions at the root, nor after its ETX to the root
  climbs above 4 while the root's batches collide at it but the root still
  answers within the silence limit (hidden terminals, D28), the root being
  its only candidate under L (TLA+ `DODAG`, Exhaust = "keeproot", Elig =
  "reach"). Before reachability (ETX ≤ 4), 13 failed samples from a full
  window: 331 s at SF8.
- **Left with traffic, last candidate relay**: a relay preferred parent
  that is the node's last candidate is kept the same way (§12): left once
  its link is unreachable, or at the silence limit if it is not heard at
  all. From its last acknowledgement: the decision of the 4th failed
  sample (at its timeout: 3 attempts, then a fresh one after the keep, 4
  relay timeouts and 3 retry delays, 64, 128 then 256 slots, 26 s at SF7,
  47 s at SF8) or the silence limit, whichever comes last: the silence
  limit at every SF; then the next selection, at the latest the relay's
  next presence (1.1 period): 3.6 presence periods, then the detach and
  rejoin (no other candidate). Before (a drop after 3 attempts, as
  "switch with traffic, parent relay"), the node detached after mere
  collisions at the relay (hidden terminals: the 30-node star, D28)
  (TLA+ `DODAG`, Exhaust = "keepboth", Elig = "reachall"). A node with
  presence reports only probes the relay once per period: 4 failed
  presences and a silence limit, 4.4 periods at most, then the selection.
- **Switch, idle**: no frame from the preferred parent for 2.5 presence
  periods, the silence limit (a parent sends at least its own presence
  report each period, the root its beacon each half period).
- **Detach + rejoin**, after the detection above: poison within Imin, hold
  2 Imin, then the answer window of the solicitation, Imin + 8 slots.
- **Global repair wave**: one Imin per hop over 8 hops when no frame of a
  hop's exchange is lost (the table). A rank change reaches the children
  within one Imin. Losses are not rare, and the protocol causes most of
  them (seed runs of the host scenarios): a parent sends its presence at once after
  adopting (its preferred parent is selected again), so its child is
  behind before the parent's advertisement arrives and solicits; the
  solicitation also resets the child's own children, still in the old
  version, whose answers fall in the window of the parent's advertisement
  ([Imin/2, Imin) of both resets) and collide with it at the child (parent
  and grandchild are hidden terminals): about 2 × ToA(23 B) / (Imin/2) ≈ 15 %
  per hop whose node has children, at every SF (Imin is in slots). The
  child, behind, solicits with backoff: each frame of the exchange lost
  (the parent's advertisement at the child, the child's solicitation at the
  parent) costs at most the next backoff gap (Imin, then doubling), so that
  a hop with n losses takes at most **(2^(n+1) − 1) Imin** (Imin + access
  without loss), without detach. Measured on the SF7 line root — A — B — C
  — D (host scenario, 400 seeds): advertisement lost at the child in 14.5 %
  of the hops whose node has a child (B, C), 0.5 % of the others; per hop
  median 0.8 Imin, 1.5 to 5.8 Imin after such a loss; wave of 4 hops median
  3.3 Imin, 90th percentile 4.6 Imin, maximum 8.7 Imin, above 4 Imin in 24 %
  of the waves. Over 8 hops (6 relays with children) about 60 % of the
  waves would meet at least one such loss: the table's 8 Imin is a typical
  figure, not a bound. The children's answers are of no use to the node
  behind (its own version): a solicitation of finite rank (only a node
  behind sends one) answered by newer neighbours only would remove this
  collision (open point, §18).

Findings:

1. **With traffic, local repair takes seconds** through a relay at SF7–9
   (at most 12–38 s, retry delays included). The node switches to the next
   parent of its set without any exchange. Next to the root, the root is
   kept while heard and eligible: a failed link to it is left within 92 s
   at SF7–8, 183 s at SF9, about one silence limit after the root's last
   acknowledgement (unreachability, or the silence limit if the root is
   not heard). A relay that is the node's last candidate is kept the same
   way: left within 108 s at SF7–8, 216 s at SF9, then the detach.
2. **Idle, local repair is bounded by the silence limit**, 2.5 presence
   periods (75 s at SF7–8, 150 s at SF9), and costs no airtime. The children
   of a failed relay re-attach and send their presence before the root
   declares them silent, so that the root reports the relay alone (§16):
   the root hears nothing from a child for at most 1.1 + 2.5 periods plus the
   report delay after its parent change (TLA+ `KMiss`: no false alarm from a
   root threshold of 3.7 periods with a report within 1/20 period), against
   a root threshold of 4.5 periods (`K_MISS` = 4, §16): the
   presence after the change has less than 0.9 period to reach the root.
   Without frame loss only: in the worst phase, losing that presence too
   still raises a short false alarm.
3. **Every repair is shorter than the silence detection** (§16) at every SF;
   at SF7–9 they all fit the detection targets (135 s at SF7–8, 270 s at
   SF9, §15).
4. **Control traffic stays bounded**: about 10 advertisements per
   neighbourhood per reset, about 15 % of the channel during the first Imin /
   2 only; at one global repair every 4 presence periods (the maximum rate), at
   most 17 s/h per node, 5 % of its budget (priority 2, §6).

### Broadcast

- Modes 4 and 5 (§11) are flooded: every node relays each new broadcast
  once. Duplicates are recognised by (origin, port, sequence), the root's
  broadcasts by (port, sequence); entries are kept for at least one presence
  period.
- The relay delay depends on the SNR of the received copy: the weaker the
  signal, the shorter the delay, within [0, 8 slots], so the farthest nodes
  relay first (Meshtastic); a node cancels its relay when it has heard 2
  relays of the same broadcast meanwhile (k = 2, as Trickle).
- Cost: at most one frame per node per broadcast. Broadcasts are rare (SILy:
  mode changes) and class 0; applications confirm delivery themselves (§12).

## 10. Downward and P2P routing: storing vs non-storing

Upward routing is the same in both modes: each node forwards towards its
preferred parent in the DODAG, falling back on the other parents of its set
(§1). The modes (RPL "mode of operation") differ in how a message travels
**down**: root → node,
and the second half of a node → node (P2P) message.

- **Storing** (RPL MOP 2): every node keeps a table "destination in my
  subtree → child". Instead of RPL's DAO messages, the table can be learned at
  no airtime cost by noting the origin of the upward messages a node forwards
  (every node sends a presence report at least once per presence period). A
  P2P message goes up until a node has the destination in its subtree, then
  down.
- **Non-storing** (RPL MOP 1): only the root knows the routes, from the
  parent set carried in each presence report (the DAO equivalent) and the
  links of the topology reports: it routes on the graph, not only on the
  DODAG. Root → node messages
  carry a **source route**; a P2P message goes up to the root, which re-emits
  it with a source route.
- **Both**: direct delivery when the destination is a neighbour; class 1
  hop-by-hop delivery; an error goes back towards the root (and the sender)
  when a hop fails.

| Criterion | Storing | Non-storing |
|---|---|---|
| Control airtime | none, if tables are learned from forwarded upward messages (an RPL-style DAO would add traffic) | none: the presence report carries the parent set, needed for the map anyway |
| Downward header | destination only | + source route: 1 B + 2 B per intermediate hop (depth 3: +5 B, depth 8: +15 B). Class 2 NACK at depth 3: 72 → 82 ms at SF7, 1.81 → 1.97 s at SF12. 30 lazy cumulative acks per round: 330 → 450 B |
| P2P path | up to the common ancestor, then down: short within a branch | always through the root: depth(A) + depth(B) hops, crossing twice the area around the root, which bounds the network capacity (§16) |
| Memory | each node: a table of its subtree (≤ 64 entries, a few KB in MicroPython; negligible on a 2 MB heap) | root only (graph of ≤ 64 nodes) |
| Consistency during repair | distributed: after a parent change, the common ancestor of the old and new paths updates on the first upward message through the new path; stale entries on the old branch expire; downward messages sent meanwhile are lost (class 1/2 retries) | single source of truth: the root updates on one report; a source route through a stale but still audible link keeps working; a dead hop returns an error and the root reroutes on another path of the graph |
| Supervision, explainability | the root learns the graph from presence and topology reports anyway, but the path actually used depends on distributed tables that can transiently differ from the root's view | the root knows and chooses every downward path: traceroute and "why this path" are direct (MP20, MP21) |
| Root outage | P2P inside a subtree keeps working while routes hold | no downward traffic and no P2P at all |
| Testability | consistency needs multi-node simulation | route computation is a pure function of the root's graph (unit tests); forwarding is stateless |
| Implementation | per-node tables with expiry, up/down decision at every hop | source-route header (built at the root, consumed hop by hop), graph at the root |
| Evolution | — | **optimised P2P**: the root knows every reported link (the graph, not only the DODAG) and can return an optimised source route A → B that A caches and uses directly (cf. P2P-RPL RFC 6997, Meshtastic/MeshCore path learning) |

Impacts:

- **SILy**: downward traffic is small (class 2 NACKs and lazy cumulative acks,
  supervision queries; mode changes are broadcast); P2P has no v1 use beyond
  diagnostics. The source-route overhead is marginal; the root detour does not
  matter.
- **Generic mesh**: P2P-heavy uses (e.g. sensor → actuator in the same area)
  would pay the root detour in non-storing mode until optimised P2P exists,
  and lose P2P entirely during a root outage.
- **Robustness**: non-storing has one place where routes are decided, so
  fewer distributed transient states to get wrong; storing survives a root
  outage better, which matters little to SILy (no root, no punches to MeOS).

**Decision: non-storing in v1** (single source of truth,
supervision and explainability, testability), with direct delivery to
neighbours; **optimised P2P is a planned evolution**. The message format keeps
the routing header optional (flag), so that optimised P2P routes or a storing
mode can be added later without a format change. Revisit if P2P traffic
becomes significant.

### Route computation at the root

- **Graph**: nodes from presence and node-information reports; directed
  links with their inbound delivery ratio and margin from topology reports
  (each node reports the links towards itself); preferred-parent edges from
  presence reports.
- **Default route** to a node: the reverse of its chain of preferred
  parents. Upward traffic is using it, so it is validated.
- **Alternative route**, when a link of the default route is marked failed:
  lowest cost path on the graph, link cost 8 × ETX as in §9 (ETX from both
  directions when both ends report the link, symmetric otherwise), eligible
  links only (margin, §9), at most 8 hops (7 intermediate hops in the routing
  header, MP13).
- **Failure**: a relay that cannot forward a source-routed message sends an
  **error** (port 4, §11) to the root. The root marks the link failed until a
  later report shows it again, then computes an alternative route and sends
  class 1 and 2 messages again (§12). A
  destination absent from the graph, beyond 8 hops or without any route is an
  explicit failure to the application (MP5); if it persists after rerouting,
  the root issues a global repair (§9).
- Route computation is a pure function of the root's graph: unit tests on
  the host (MP23).

## 11. Addressing and frame format

Security fields are specified in §14. Integers are little-endian (as the SIPy punch and MeOS records); sequence numbers and the
DODAG version use serial-number arithmetic (RFC 1982).

### Addresses

- 16-bit addresses. **0x0000 is the root**, whichever board holds the role;
  **0xFFFF** means all neighbours (link destination) or all nodes
  (broadcast). A node's address is the low 16 bits of its MAC (0x0000 and
  0xFFFF remapped), as LoRaMesher did in SILy-Arduino, overridable by
  configuration.
- Collisions: about 0.7 % probability for 30 boards. The root detects them
  from the node-information message (full MAC) and reports them, as well as a
  second root (frames from 0x0000 that are not its own) (MP22). Uniqueness
  also matters to security: the AEAD nonce is built from the sender address
  and frame counter (§14).

### Frame

One LoRa packet, at most 255 B: **cleartext header** (authenticated as
associated data, §14), **encrypted payload** made of messages, **tag**.
Overhead: 9 B header + 4 B tag = **13 B**.

| Bytes | Field | Notes |
|---|---|---|
| 0 | versions | bits 7–6 protocol version (0 reserved, never valid; 1 this version; 2 next incompatible version; 3 escape: an extended version byte follows byte 0, for later versions, since four values could prove limiting and the escape costs nothing until needed; values not supported are rejected and counted, MP25); bit 5 key epoch (alternates at each key change, lets old and new keys coexist during the change, §14); bits 4–1 DODAG version (root-issued, incremented on global repair and parameter change, §9; serial arithmetic on 4 bits); bit 0 reserved (sent as 0, ignored) |
| 1–2 | link destination | next hop, or 0xFFFF (every neighbour) |
| 3–4 | link source | sender |
| 5–7 | frame counter | 24 bits, strictly increasing per sender for the board's whole life, never reset, even across key changes: persisted in NVS by blocks of 256 (§14); 2^24 frames last about 20 years of SILy use. Part of the AEAD nonce and replay check (§14); gaps give neighbours the delivery ratio (link estimation); identifies the frame for class 1 acknowledgements |
| 8 | rank | sender's rank (§9: 8 per hop of ETX 1); 0 = root, 255 = no route or poisoned |
| 9… | messages | encrypted |
| last 4 | tag | AES-128-CCM, 32 bits (MIC-32 as LoRaWAN and IEEE 802.15.4; forging requires online attempts at radio speed, about 2^31 frames on average, years of continuous emission, and shows in authentication-failure counters, MP22; §14) |

Every frame thus advertises its sender's routing state (DODAG version, rank):
the content of an RPL DIO travels in every frame for free; Trickle (§9) only
governs frames sent for advertisement alone. Frames are rebuilt at each hop:
a relay decrypts, keeps or forwards each message, and repacks the messages
bound to the same next hop (§7).

Rejected for the counter: transmitting 16 bits and reconstructing the high
part (LoRaWAN FCnt) — a neighbour hearing a node for the first time, or after
a long gap, cannot know the high part, which needs a resynchronisation
mechanism.

### Message

| Field | Size | Presence |
|---|---|---|
| control | 1 B | always: bits 7–5 addressing mode; bits 4–3 class; bit 2 routing header present; bit 1 bundle (count follows); bit 0 **last** (length omitted: the message extends to the tag) |
| port | 1 B | always: 0–15 mesh, 16–127 applications at high priority, 128–255 applications at low priority (priority, §6) |
| length | 1 B | unless *last* without bundle: payload length of one element |
| count | 1 B | bundle only: number of elements |
| then, per element: | | |
| addresses | 0, 2 or 4 B | by addressing mode; P2P: origin, then destination |
| sequence | 2 B | classes 1 and 2, and broadcasts |
| routing header | 1 + 2n B | flag set: count n, then the remaining intermediate hops of a source route; each hop checks that the first entry is its own, removes it and sends to the next one (to the destination when n = 0) |
| payload | length | opaque to the mesh for application ports |

Addressing modes:

| Mode | Addresses | Use |
|---|---|---|
| 0 link-local | none | concerns the link destination(s) only: acknowledgements, routing information |
| 1 upward | origin | to the root, implicit (MP8) |
| 2 downward | destination | from the root, implicit; source-routed (§10) |
| 3 P2P | origin, destination | up to the root, then source-routed down (§10); direct to a neighbour |
| 4 broadcast | origin | from a node to every node |
| 5 root broadcast | none | from the root to every node |
| 6 upward, first hop | none | to the root, origin = link source (the node's own message on its first hop); the first relay rewrites it in mode 1 |
| 7 | | reserved |

- **Bundle**: consecutive messages with the same control, port and length
  share one header (count > 1). It is how a relay forwards 30 presence reports
  or 17 punches from different origins in one frame at little cost
  (SCHC-like); single messages do without the count byte.
- **First-hop compression** (mode 6, *last* bit): the most frequent frames on
  the channel are each node's own single message to its parent (29 of 30
  presence frames per period); they carry neither origin nor length, and the
  presence report no parent (§ mesh ports). The relay restores the fields when
  it forwards, since it knows them (the origin is the link source, the parent
  is itself).
- **Decoding rules**: a bundle carries a count of at least 2; a
  bundle with *last* must end exactly at the tag; the final message need not
  carry *last* if it ends at the tag; a routing header is valid in modes 2
  and 3 only, with n ≤ 7 (§10, MP13); the escape version (3) is rejected
  until an extended version is defined; any malformed frame is dropped and
  counted, never partially processed.
- **Sequence**: 16 bits per flow (origin, destination, port): the class 2
  sequence number (the application no longer numbers its messages, the
  mesh does), the identity used by class 1 passive acknowledgement, and the
  duplicate filter of broadcasts. 8 bits were rejected: they need a window of
  at most 127 outstanding messages, fragile during partitions.
- **Class** 3 is reserved. Relays read control, length, addresses, sequence
  and routing header, never an application payload (MP2).
- **Maximum application payload**: 255 − 13 (frame) − 23 (P2P message, last,
  class 2, routing header of 7 hops) = 219 B; **200 B announced** (MP6).

### Mesh ports

| Port | Message | Mode, class | Payload |
|---|---|---|---|
| 1 | presence | upward (mode 6 on the first hop, 1 when forwarded), 0 | preferred parent 2 B (mode 1 only; in mode 6 it is the link destination); flags 1 B (bit 0 first report since boot, bit 1 queue overflow since last report, bit 2 duty-cycle use above 80 % since last report (seen at every transmission, kept until a presence goes on air: presence itself is refused above 80 %), bit 3 class 2 tails follow, bit 4 rank errors since last report (§9), bit 5 address conflict, bit 6 authentication failures since last report (§13), bit 7 next key loaded (§14)); if bit 3: tail count 1 B, then per idle flow not yet acknowledged: port 1 B, oldest unacknowledged sequence 2 B, last sequence 2 B (§12); application extension: the remaining bytes, opaque (length set by configuration; SILy: battery and mode, 1 B) |
| 2 | topology | upward (6 / 1), 0 | count 1 B; per neighbour: address 2 B, margin 1 B (signed dB, saturated, §5), quality 1 B (bit 7 parent-set member, bits 6–0 delivery ratio 0–100 %); parent set first, then best other links; 6 entries by default |
| 3 | acknowledgement | link-local, 0 | per acknowledged frame: link source 2 B, frame counter low 16 bits 2 B |
| 4 | error | upward (6 / 1), 1 | reason 1 B (1 next hop unreachable, 2 next hop not a neighbour, 3 no route, 4 queue full, 5 duty-cycle budget, 6 route beyond 8 hops); next hop 2 B; failed message: origin 2 B, destination 2 B, port 1 B, sequence 2 B (§10, §12; the root forwards it to the origin) |
| 5 | node information | upward (6 / 1), 0 | full MAC 6 B, protocol version 1 B, firmware version 2 B, name (remaining bytes, UTF-8, ≤ 16 B) |
| 6 | echo | any, 0 | kind 1 B (0 request, 1 response); identifier 2 B; hop count out 1 B; hop count back 1 B; padding (optional) (§13) |
| 7 | traceroute | any, 0 | kind 1 B (0 request, 1 response, bit 7 truncated); identifier 2 B; then per hop, out and back: address 2 B, margin of the received frame 1 B (signed dB), link cost 1 B (8 × ETX); 16 entries at most (§13) |
| 8 | management query / response | any, 1 | query: request identifier 1 B, object 1 B (0 counters, 1 neighbours, 2 routing state and parent set, 3 queues and duty cycle, 4 route-change log, 5 node information), offset 1 B; response: request identifier 1 B, object 1 B, offset 1 B, flags 1 B (bit 0 more follows), TLV entries ≤ 200 B (§13) |
| 9 | DODAG information | link-local, 0 | empty: solicitation; otherwise flags 1 B, presence period 2 B, topology period 2 B, Imin 1 B, maximum rank increase 1 B, regional profile identifier 1 B (§9) |
| 10 | class 2 control | reverse direction of the flow, 0 | kind 1 B, flow port 1 B, then: kind 0 acknowledgement / NACK (receiver → sender): cumulative point 2 B, then missing ranges (first 2 B, count 1 B), none for a pure acknowledgement; kind 1 tail (sender → receiver other than the root): oldest unacknowledged 2 B, last 2 B; kind 2 gone (sender → receiver): first 2 B, count 1 B (§12) |
| 0, 11–15 | reserved | | |

The presence report carries only the preferred parent: the root routes
downward along preferred parents and takes alternatives from the parent-set
bits of the topology reports (§10). The application extension of presence
lets an application piggyback a few opaque bytes on the periodic report
instead of sending its own periodic message (SILy: battery and operating
mode); the mesh neither reads nor interprets them. Node information (port 5)
goes with the first topology report and then with every 8th.

### Sizes (SILy cases)

| Frame | Bytes |
|---|---|
| presence of a node to its parent (mode 6, last) | 13 + 2 + 2 = 17 |
| 30 presence reports bundled by a relay | 13 + 4 + 30 × 6 = 197 |
| topology of a node, 6 neighbours (mode 6, last) | 13 + 2 + 25 = 40 |
| punch of a node, class 2 (mode 6, last; 10-byte SIPy punch) | 13 + 2 + 2 + 10 = 27 |
| 17 punches bundled by a relay | 13 + 4 + 17 × 14 = 255 |
| root acknowledgement, one entry | 13 + 2 + 4 = 19 |
| class 2 NACK, downward, depth 3 (last) | 13 + 2 + 2 + 5 + payload |

### Example: presence of node 0x3F2A to its parent 0x1B07 (17 B)

| Bytes | Value | Field |
|---|---|---|
| 0 | 0x40 + 2 × DODAG version | versions: protocol 1, key epoch 0, DODAG version in bits 4–1 |
| 1–2 | 07 1B | link destination = parent |
| 3–4 | 2A 3F | link source = origin |
| 5–7 | … | frame counter |
| 8 | … | rank |
| 9 | 0xC1 | control: mode 6, class 0, last |
| 10 | 0x01 | port: presence |
| 11 | 0x00 | presence flags |
| 12 | … | SILy extension (battery, mode) |
| 13–16 | … | tag |

### Compaction

Against the first draft of this section (header 11 B, tag 8 B): DODAG version
and protocol version packed into one byte, security level replaced by a
key-epoch bit (A1); 24-bit counter (A2); implicit length of the last message
(A3); first-hop compression (A4); 4-byte tag (A5, confirmed in
§14). A presence frame shrinks from 28 to 17 B, a single-punch frame from 36
to 27 B. Option A6 (LoRa payload CRC off, relying on the tag as LoRaWAN
downlinks do; saves 16 bits on air, i.e. 10–30 % of a small frame) is kept
open until the corruption rate is measured (§18).

## 12. Service classes

Choices: class 2 runs over class 1 at every hop (local retries fix most
losses within seconds); a class 2 message is kept until acknowledged (no
default timeout: nothing is lost silently); a receiver without state
resynchronises from the sender's tail [oldest unacknowledged, last] (nothing
is written to flash on reception); sender sequences persist by blocks, and
sequences lost at a sender reboot are declared *gone*. Figures come from section F of
`mesh_budget.py`.

### Common rules

- The application chooses the class of each message (MP4). Classes 1 and 2
  carry a 16-bit sequence per flow (origin, destination, port) (§11).
- No class guarantees order: messages are delivered as they arrive.
- **Explicit failures** (MP5), reported to the application with the message
  identity and a reason:
  - at submission: payload too long (MP6), transmit queue full, class 2 send
    window full, class 1 or 2 requested for a broadcast;
  - later: class 1 or 2 error received from the network (port 4, reasons in
    §11); class 2 per-flow timeout, if the application set one; on the
    receiving side, messages declared *gone* by their sender.
- **Transparency** (MP3): applications address nodes or the root, never
  routes; a topology change shows only as delay, or as one of the failures
  above.

### Class 0 — datagram

- Sent once per hop: no acknowledgement, no retry, no error message (losses
  are counted locally, §13).
- **Replace by newer** (optional, per port): a queued class 0 message is
  replaced by a newer one with the same origin and port. The mesh's own
  presence and topology reports use it.
- Broadcasts are class 0 in v1 (§9); class 1 or 2 broadcasts are refused.
  Applications confirm themselves when they need to (SILy: each node confirms
  its mode in the presence application extension, and the Gateway
  repeats the broadcast until every node has confirmed).

### Class 1 — hop by hop

- Every hop keeps the message until the next hop acknowledges it, with up to
  **3 attempts** per next hop. Timeout (§9): time on air of a 255-byte frame
  + 6 slots; when the next hop is the root, the root's acknowledgement
  batching delay is added (§16).
- **Retry delay**: after a timeout the retry waits a random delay, uniform
  in [0, w), drawn once per frame so that its messages are retried
  together. Two senders hidden from each other whose frames collided at
  their common next hop time out together: retried at the timeout, they
  would collide again at every attempt (host simulator, before the delay:
  about 70 % of such retries collided again, mostly with the same partner,
  against a third of all frames). The delay moves the retry only: timeouts
  and failure decisions stay anchored on the end of the frame.
- **Retry backoff**: w is 32 slots doubled per frame of the node timed out
  since its last acknowledgement (any next hop), that one included, at
  most 3 times: 64, 128, then 256 slots (3.3, 6.7, 13.3 s at SF7; 6.0,
  11.9, 23.8 s at SF8; 10.6, 21.1, 42.2 s at SF9). Any acknowledgement of
  one of its frames (explicit, passive, or late from the root) resets the
  count. Binary exponential backoff, as 802.11 and 802.15.4, against
  congestion collapse: with a fixed range, sustained collisions between
  hidden terminals kept the channel at a relay about 90 % busy, retries a
  third of the air time, never slowing down (30-node star of the host
  simulator). The cap keeps every delay below one presence period (256
  slots: 13 s at SF7, 24 s at SF8, 42 s at SF9, 84 s at SF10, 169 s at
  SF11, 338 s at SF12; a fourth doubling would exceed it from SF8, 48 s
  against 30 s), so that the holding of 2 periods (MP15) and the silence
  limits keep their meaning. A base of 32 slots rather than one scaled to
  the timeout: the relay timeout (1.8 frames of 255 B) spreads two retries
  too little, and on 30-node networks of the host simulator the scaled
  range gave more retries and detaches. Cost: a failed link is left later
  (§9 repair table), and a lost message takes longer to recover (class 2
  NACK delay below).
- **Passive acknowledgement**: the sender overhears the next hop forwarding
  the message (identified by origin, destination, port and sequence).
- **Explicit acknowledgement** (port 3, link-local), sent by:
  - the final destination: the root batches up to 8 entries per frame (§16),
    any other node acknowledges at once;
  - a relay that accepted the message but will not forward it within half a
    timeout (message held during repair, duty-cycle budget, queue);
  - a node that receives a duplicate (the sender missed the acknowledgement).
  One entry (link source, low 16 bits of the frame counter) acknowledges
  every class 1 and 2 message of the frame. Each frame is one sample of the
  next hop's acknowledgement window, whatever its messages; towards the
  root, at most one failed sample per batching delay, one lost batch being
  one loss (§9).
- **Late root acknowledgement**: under load the root's medium access can
  delay its batched acknowledgement beyond the timeout. Towards the root
  the retry still goes after the timeout (and its delay), but the sender
  keeps the frame counters of the message's last 3 transmissions: an
  acknowledgement of any of them removes the message, whether it waits for
  an acknowledgement, is queued for its retry or is held. A frame's failed
  sample is decided one root timeout after its timeout, and an
  acknowledgement of that frame arriving before makes it a success
  instead; after the last attempt the sender waits one more root timeout
  before the upward failure below. Ignored, late acknowledgements would
  fail samples although no frame was lost and, with a drop of the root
  after three attempts, drop it (TLA+ `DODAG_line_lateignore_drop`;
  accepted: `DODAG_*_lateack_*`).
- **Duplicate cache** on every node: (origin, destination, port, sequence),
  128 entries, each kept for at least 2 presence periods. A duplicate is
  always acknowledged. **Custody**: the final destination drops it; any
  other node drops it only while it still holds a copy (queued, waiting for
  an acknowledgement, or held during repair) and otherwise takes it again
  as a new arrival (forwarded, or held), counted. A message that comes back
  through a transient loop to a relay that has handed it over is thus never
  dropped without a copy left (MP5; TLA+ `DODAG`). A copy waiting for the
  acknowledgement of the node the duplicate comes from does not count:
  that node took it, and its acknowledgement may still arrive. Cost: after
  a lost acknowledgement, the retransmission reaching a relay that has
  already forwarded the message travels the rest of the path once more;
  the destination filters it.
- **Upward failure**: after 3 attempts a relay parent is dropped (§9) and
  the message goes to the next parent with fresh attempts. **The root is
  kept** while it is a candidate apart from the drop (heard within the
  silence limit, link eligible, of the node's version, within the bound),
  and so is **the preferred parent when it is the node's last candidate**
  (no other candidate: dropping it would detach the node): one root
  timeout after the last attempt towards the root (late acknowledgement,
  above), after the last timeout towards a relay, the message gets fresh
  attempts towards it, after a retry delay: these transmissions are the
  probes of the parent's reachability. Only the silence limit or the
  link's ineligibility (its margin, or unreachable: 4 consecutive failed
  samples and a silence limit without acknowledgement of the parent, §9;
  not ETX above 4) removes it. The keep is decided at each exhaustion,
  never remembered: a candidate heard since (a dropped neighbour heard
  again, a neighbour whose rank fell below L) makes the next exhaustion
  drop the kept relay, unless it takes its place by the switch threshold
  first. Under the test against L the root is a root child's only
  candidate, and a relay often a hop-2 node's: a drop would be a detach,
  its subtree following, after mere collisions at the parent (TLA+
  `DODAG_line_drop_rootloss`, `DODAG_line_reachetx_relaydrop`; kept:
  `DODAG_*_keeproot_*`, `DODAG_*_reachall_*`, no silent loss and the root
  never dropped with up to 3 losses in any pattern, left over a dead
  link), and so would the parent link's ETX above 4 after the parent's
  acknowledgements collide at the node (D28, `DODAG_line_etx_detach`,
  `DODAG_line_keepbothetx_detach`; reachability: `DODAG_*_reach_*`,
  `DODAG_*_reachall_*`). Cost: a failed link to such a parent is left
  later, about one silence limit after its last acknowledgement (§9
  repair table).
- **Downward failure** (source route): error to the root (port 4, reason 1 or
  2); the root reroutes (§10) and sends the message again, being its origin
  (or, for P2P, the origin of the downward half).
- **Holding during repair** (MP15): a node without route keeps class 1 and 2
  messages for **2 presence periods**, longer than a detach and rejoin at
  every SF (§9). Then it drops them and queues an error (reason 3) for the
  root. After a detach, the queued upward class 1/2 messages, those waiting
  for an acknowledgement included, are held; queued class 0 messages are
  dropped.
- **Errors are class 1** (port 4, upward). The root forwards an error about an
  upward or P2P message to its origin. An error about an error is never
  generated, only counted. Class 1 thus delivers the message, or its origin
  gets an explicit failure (MP5), **except** when a relay that already
  acknowledged the message reboots before forwarding it: the message is
  then lost silently (accepted: custody records in RTC memory would cost
  writes per message and still not survive a power loss); and when a relay cut off from the root fails a
  held message (end of holding) while its origin is cut off too: the error
  cannot reach the origin, held in turn then dropped (TLA+
  `DODAG_line_partition_MP5Live`). Accepted: with no link
  at all nobody can communicate, not even a failure. Applications that
  cannot accept these risks use class 2, whose end-to-end recovery covers
  them.
- A frame holding a refused message is not acknowledged at all (one entry
  per frame): its other, delivered messages may then be retried (duplicates,
  filtered) or reported failed although delivered (a false failure, never a
  silent loss).
- Identity of a link-local class 1 message: (link source, link destination,
  port, sequence).

### Class 2 — end to end

Class 2 is class 1 at every hop, plus recovery between
the **sender** (origin) and the **receiver** (destination). The message is
delivered exactly once to the receiver, unordered. This is SILy's punch
recovery made generic:
hop-by-hop retries fix most losses on the spot, so the end-to-end recovery
only handles the rest (a relay rebooting with messages in its queue, holding
expired, a whole path lost).

- **Sender**:
  - consecutive sequences per flow, persisted by blocks of 64 (as the frame
    counter, §14); at boot it jumps to the next block;
  - a **send window** of 64 unacknowledged messages per flow, kept in a
    buffer; when the window is full, submissions are refused (back-pressure,
    MP5);
  - **no timeout by default**: a message is kept until acknowledged;
    the application may set a timeout per flow, which then reports the
    failure.
- **Receiver**, per flow: a cumulative point C (every sequence ≤ C received or
  declared gone) and a bitmap of the sequences received in (C, C + 64].
  Each new message is delivered at once; duplicates are dropped.
- **NACK**: a received sequence above the highest one so far reveals a gap.
  After the **NACK delay** (3 timeouts towards the root and 2 retry
  delays, 64 then 128 slots: the time a message retried on the last hop
  needs to arrive), the
  receiver sends a NACK with C and the missing ranges (port 10). It repeats
  it every presence period while a gap remains.
- **Tail**: a flow with unacknowledged messages, and none sent since the
  previous presence report, announces its tail [oldest unacknowledged, last].
  Towards the root, the tail rides on the presence report (§11). Towards any
  other destination, it is a port 10 message, once per presence period. The
  tail reveals a lost last message, and resynchronises a receiver that lost
  its state.
- **Lazy cumulative acknowledgement** (port 10, C without ranges): sent when C
  has advanced by 32 since the last acknowledgement, or upon a tail when every
  message up to the tail's last one has arrived. NACKs carry C as well. The
  sender frees every message ≤ C and stops announcing the tail. No message is
  acknowledged individually: an acknowledgement per message would roughly
  double the airtime per hop.
- **Resending**: on a NACK, the sender resends the missing messages from its
  buffer, with their sequence. Sequences it no longer holds (lost at its own
  reboot) are declared **gone** (port 10). The receiver then moves C past
  them and reports the loss to its application: nothing is lost silently on
  either side. An application keeping its own journal may submit those
  messages again (SILy: from the punch journal of each board; the Gateway
  deduplicates punches on their content).
- **Receiver reboot**: the receiver has no state for the flow. The flow's
  next tail gives [oldest, last]; the receiver sets C = oldest − 1 and NACKs
  what it lacks. Messages delivered before the reboot but not yet
  acknowledged may then be delivered a second time. Class 2 is therefore
  exactly-once **except across a receiver reboot**; applications that need
  strict uniqueness deduplicate on content (as SILy does for punches). Nothing is written to
  flash on reception.

Recovery times (worst depth 8, a hop costing at most one class 1 timeout):

| SF | NACK delay (s) | lost message, after the gap is seen (s) | lost last message, from its sending (s) | presence period if every node carried a tail (s) |
|---|---|---|---|---|
| 7 | 16.6 | 28 | 71 | 30 |
| 8 | 30.5 | 51 | 80 | 45 |
| 9 | 54.0 | 90 | 156 | 90 |
| 10 | 105.5 | 174 | 308 | 180 |
| 11 | 216.2 | 359 | 743 | 300 |
| 12 | 422.0 | 693 | 1471 | 600 |

- At SF7–9, a lost message is recovered within 28 to 90 s of the gap being
  seen, and a lost last message within the detection targets (135 s at
  SF7–8, 270 s at SF9, §15).
- **Tails must stay transient.** If every presence report carried one (6 B),
  the presence period at SF9 would rise from 60 to 90 s, and detection from
  270 to 405 s. Hence a tail is only carried by an idle, unacknowledged flow,
  and the receiver acknowledges as soon as the tail shows the flow complete:
  about one tail per flow per burst.

### Queues and resources (MP13)

| Resource | Size (default) | When exceeded |
|---|---|---|
| transmit queue | 64 messages; above 32, 16 per origin (§7) | origin over its quota refused as below; local submission refused; forwarded class 0 dropped (lowest priority, oldest first); forwarded class 1/2 not accepted (no acknowledgement, the sender retries or switches parent); presence flag *queue overflow* |
| neighbour table | 64 entries | stalest entry that is not a parent evicted (§9) |
| duplicate cache | 128 entries | oldest evicted; a late duplicate may then be forwarded, and the class 2 receiver drops it |
| class 2 send window | 64 messages per flow, 8 flows per node | submission refused |
| class 2 receiver | 64-sequence bitmap per flow | sequences beyond C + 64 are dropped unacknowledged (the sender's window prevents them) |
| root graph | 64 nodes, depth 8 | new nodes not routed downward; alarm *graph full* (§13) |

Reading of the table: the class 2 send window is the sender's own
buffer, separate from the transmit queue; a class 1/2 message occupies a queue
entry (counted in the size and the quota) only until the next hop
acknowledges it. Only an arriving forwarded class 0 message evicts, and only
a queued class 0 message of the same or lower priority, oldest first; local
and class 1/2 arrivals are refused, never evict.

With these sizes, a node's mesh state is a few tens of KB of MicroPython
heap (out of 2 MB of PSRAM on the T3S3). Measured on the bench: 31 KB for a
bench node with routing, 73 KB for the imported modules of the bench
application as source (`docs/measurements.md`); the state in a full-size
network remains to be measured (§18).

## 13. Supervision and diagnostics

### Local observability (MP18, MP21)

Every node exposes its mesh state through a local API (SILy: web UI over
WiFi):

- **Neighbours** (§9): delivery ratio in, passive acknowledgement ratio, ETX,
  margin, rank, version, age; the **parent set** with path costs, and the
  reason for the preferred parent (lowest path cost, kept by hysteresis,
  margin as tie-break) (MP21).
- **Routing state**: rank, L, DODAG version, Trickle interval, detached or
  not.
- **Queues**: transmit queue by priority and origin, quota refusals per
  origin (§7), class 2 windows and receivers per flow, held messages.
- **Duty cycle**: use over the window, per priority, refusals.
- **Counters** since boot: frames sent and received per port; drops by cause
  (queue full, origin quota, duty-cycle budget, holding expired, duplicate,
  rank error, loop, version rejected, authentication failure, replay, foreign
  profile); replay-table evictions; class 1 attempts and parent switches;
  class 2 NACKs, resends, gone; channel busy and backoffs.
- **Route-change log**: the last 32 events with time and cause (attempts
  exhausted, parent silent, better parent, new version, detach, rejoin).
- **Authentication failures and replays per sender address** (MP22, §14).
- **Security state** (§14): fingerprints of the current and next keys, grace
  period, frame counter.

### Remote supervision (MP19)

- **Presence and topology reports** (§11) feed the root's graph (§10), which
  the application displays (SILy: mesh map).
- **Root alarms**: node silent (`K_MISS` = 4 missed presences: no presence
  for 4.5 presence periods, §16); **subtree
  silent** (a relay and its descendants silent together: one alarm, §16);
  address conflict; second root; foreign regional profile; custom profile in
  use (compliance under the operator's responsibility, §4); authentication
  failures (senders never authenticated: wrong key or foreign network; senders
  authenticated before: key switch missed, §14); nodes ready for a key switch;
  asymmetric link; repeated rank errors, and the global repairs
  issued; graph full.
- **Management query** (port 8, class 1, from the root or any node): a
  request names an object (0 counters, 1 neighbour table, 2 routing state and
  parent set, 3 queues and duty cycle, 4 route-change log, 5 node
  information) and an offset; the response returns it as TLV entries of at
  most 200 B, paged by offset. Read-only in v1: network-wide parameters
  change only through the DODAG version (§9).

### Diagnostics (MP20)

- **Echo** (port 6, class 0, any addressing mode): every relay increments the
  hop count of the current leg; the responder returns the identifier, the
  outward hop count and the optional padding (to test a frame size). The
  origin measures the round trip on its own clock (no time synchronisation).
- **Traceroute** (port 7, class 0): every relay, and the destination, append
  an entry (address, margin of the frame received, link cost) on the way out
  and on the way back; at most 16 entries. In non-storing mode, a P2P
  traceroute shows the path through the root (§10).
- Relays may modify these payloads: they belong to mesh ports, and frames are
  decrypted and rebuilt at every hop anyway (§11).

### Misconfiguration (MP22)

- **Wrong key**: neighbours count authentication failures per sender address;
  a presence flag alerts the root, the management query gives the addresses;
  the key fingerprints shown in each board's UI locate the mistyped password
  (§14).
- **Duplicate address**: a node receiving a frame whose link source is its own
  address raises a presence flag; the root also sees two MACs for one address
  in node-information reports (§11).
- **Second root**: a root hearing frames from 0x0000 that it did not send
  raises an alarm.
- **Foreign regional profile** (§9) and **unknown protocol version** (MP25)
  are counted and reported.
- A **wrong SF** cannot be detected over the radio (§5).

## 14. Security

Choices: one **network key shared** by every board, its limits documented
(the adversaries in scope are outsiders without a board, and
misconfiguration); **4-byte tag**; key change by a **staged switch** over
the air; the next key **distributed** by the root's application, wrapped
under the current key (SILy: over WiFi); replays possible once after a
receiver's **power loss**, a residual risk accepted (bounded effects,
below).

### Threat model (MP17)

- **Assets**, by importance: integrity of messages and routing information
  (a forged punch or route is worse than a lost one); availability;
  confidentiality of payloads (low for SILy: results are published live).
- **In scope**: an eavesdropper; a transmitter without the key (a competitor
  injecting punches, a prankster disturbing the network); a foreign network on
  the same channel; a misconfigured board (wrong password).
- **Out of scope**: jamming (no defence on a single channel; it shows as
  silence and the root reports it, §13); an insider holding a configured
  board or the password (limits below); attacks on the hardware.
- **Reference level** for SILy: SportIdent itself (SI cards and SRR stations
  are not authenticated). SILy must not make cheating easier than with
  SportIdent; the finish readout remains the reference.

Properties obtained:

- **S1** Only key holders inject frames: every frame is authenticated,
  advertisements, solicitations and acknowledgements included; no frame is
  processed before its tag is verified.
- **S2** Replays are rejected (frame counter, below).
- **S3** Messages are encrypted. The frame header stays in clear (addresses,
  counter, rank, versions): an eavesdropper can map the network and its
  traffic volume, not read punches. Accepted.
- **S4** Failures are visible: authentication failures and replays are
  counted per sender and reported (§13, MP22).

### Algorithm

**AES-128-CCM** (NIST SP 800-38C, RFC 3610), as IEEE 802.15.4 CCM* and
LoRaWAN: key 16 B, nonce 13 B (length field L = 2), tag 4 B. Associated data:
the frame header (bytes 0–8, §11); plaintext: the messages; the ciphertext has
the plaintext's length. CCM is composed from `cryptolib` (ECB for the counter
mode, CBC for the CBC-MAC), validated in the platform spike against NIST and RFC 3610
vectors: 1.7 ms per 32-byte frame, 5.4 ms per 240-byte frame at 240 MHz.
`cryptolib` is also available on the MicroPython unix port, so host tests
run the same CCM code.
Every frame heard is decrypted, whatever its link destination (overheard
frames feed passive acknowledgements and link estimation, §9): 200 frames per
minute cost about 1 % of a core.

**Tag: 4 bytes** (MIC-32 as LoRaWAN and IEEE 802.15.4).
Each forgery attempt succeeds with probability 2^-32 and has to be
transmitted: about 2^31 frames on average, years of continuous emission at
SF7, every failure counted and reported. A successful guess gains little: the
replay check forces a fresh frame counter, hence a fresh nonce, so the
decrypted content is uncontrolled and almost always rejected by message
parsing. Offline, the tag lets an eavesdropper test password guesses (a
second frame confirms a candidate): the strength of the password matters
(key derivation, below). The frame format and the budget (§16) are unchanged.

### Nonce

13 bytes: **link source** (2 B) ‖ **frame counter** (3 B), as in the header,
then 8 zero bytes. CCM is broken if a nonce repeats under one key (the
keystream repeats: the XOR of two plaintexts leaks; authenticity weakens), so
(link source, frame counter) must never repeat:

- **The frame counter of a board increases for the board's whole life**,
  across reboots, power losses and key changes; it is never reset (below).
- **Addresses are unique in the network.** A collision of MAC-derived
  addresses (0.7 % for 30 boards, §11) is a static property of the fleet:
  the same two boards collide at every event. The root reports it at the
  first bench test (address conflict, §13) and the operator overrides one
  address. Until then, the two boards reuse nonces: the address-conflict
  alarm must be cleared before any use in a race.
- Rejected: the full MAC in the nonce (802.15.4 style). A receiver would need
  each neighbour's MAC before decrypting its first frame, hence an extra
  exchange.

### Frame counter persistence

- Kept in **NVS** (`esp32.NVS`, its own flash partition), not in the
  configuration: it survives reboots, power losses, factory reset and a
  reformatted file system.
- **Blocks of 256**: the record holds the first counter of the next block. It
  is written before the block's first frame (in advance, once half of the
  current block is used, so that no transmission waits for flash: a write
  and commit takes 2–2.6 ms on average and up to 16 ms on the bench,
  `docs/measurements.md`). At boot,
  the board starts at the recorded value and records the next block. A reset
  or power loss can skip up to about 1.5 blocks (≤ 512 counters, since the
  next block is recorded at the half-block point), never repeat one. The
  last counter sent is 2^24 − 2; then the node stops sending (alarm).
- The same record holds the class 2 sender sequences (blocks of 64, §12).
- **Lifetime**: 2^24 frames. A busy relay sends about 500 frames per hour;
  10 h a day, 50 days a year, plus up to 512 counters per reboot (20 reboots
  a day), uses about 0.76 million counters a year: about 20 years. A board reaching
  2^24 − 1 stops transmitting and reports it.
- **Wear**: one write per 256 frames and one per boot, i.e. about one write
  per 2 h for an ordinary node, 2 per hour for a busy relay; NVS levels wear.
- **Full flash erase** (`esptool erase_flash`, firmware reinstallation): the
  record is lost. The board then starts at a random block in [0, 2^23), which
  makes a reuse unlikely without excluding it. Procedure: change the network
  password (key switch, below) after a full erase of a board.

### Replay protection

- Every receiver keeps, per sender (link source), the highest frame counter
  authenticated so far, and accepts a frame only if its counter is **strictly
  greater**. A sender transmits one frame at a time, so its frames never
  arrive out of order. The check follows the tag verification; a replay is
  dropped, counted per sender (§13) and not used for link estimation.
- **Replay table**, separate from the neighbour table: a neighbour entry
  expires after 4 silent presence periods (§9), a replay entry does not.
  128 entries (twice the default network size, MP13), the least recently
  heard sender evicted when full (degradation: an evicted sender's frames can
  be replayed once; evictions are counted).
- Kept in **RTC memory** with the duty-cycle window (§6): it survives soft
  resets and the watchdog. 4 B + 128 × 5 B for the table and 257 B for the
  window (901 B in all), within the 2 KB of RTC user memory of
  MicroPython on the ESP32 (2048 B measured on the T3S3, kept across soft
  reset and `machine.reset()`, `docs/measurements.md`).
- **After a power loss** (residual risk accepted) the table
  is empty and the first frame of each sender sets its baseline. An attacker
  who recorded frames can replay old ones once each: every replay raises the
  baseline, the genuine frames, with higher counters, are accepted again as
  soon as they arrive. Effects: old messages delivered again (duplicates, which
  class 2 already allows across a receiver reboot, §12; SILy deduplicates
  punches on content); stale advertisements, superseded by the sender's
  next genuine frame. Application commands must carry their own freshness
  (SILy: mode commands carry a sequence persisted with the mode). Covered
  by a host scenario on the scripted medium (MP23).
- **Wormhole**: a frame recorded in one place and retransmitted elsewhere, to
  receivers that never heard its sender, is genuine and accepted; it creates
  a phantom link. Class 1 acknowledgements fail across it and its ETX removes
  it from parent sets (§9). No further defence.

### Key derivation

- **Master key** = PBKDF2-HMAC-SHA256(password in UTF-8, salt = `mesh/1` ‖
  network name, iterations N, 32 B). The network name is a configuration item
  (empty by default): two networks with the same password still get different
  keys, and a dictionary precomputed for one network is useless for another.
  N is set so that derivation takes 1–2 s on the board: N = 6000, 1.44 s
  measured on a T3S3 with the application loaded (0.24 ms per iteration;
  half that on a freshly reset board, `docs/measurements.md`). N is network-wide. Derivation
  happens when the password is entered, not at each boot.
- From the master key (HMAC-SHA256 with a label per use; not HKDF-Expand,
  which would append a counter byte):
  **frame key** = first 16 B of HMAC(master, `frame`); **key fingerprint** =
  first 4 B of HMAC(master, `fingerprint`), shown in hexadecimal in the local
  UI. Comparing fingerprints finds a mistyped password without revealing it
  (MP22). **Wrapping key** = first 16 B of HMAC(master, `key-wrap`) (key
  distribution, below).
- **Storage**: the configuration keeps the frame key and the fingerprint, not
  the password. JSON export omits the key; JSON import accepts a
  password, derived on import (mass deployment).
- **Strength**: from one recorded frame, an attacker tests guesses offline.
  With N = 10 000, a GPU tests in the order of 10^5–10^6 guesses per second: a
  40-bit password falls within weeks on one GPU, an 80-bit one never. The UI
  offers a generated password (16 base32 characters, 80 bits) and refuses
  fewer than 12 characters.
- **No flash encryption** in v1: the key can be read from a captured board's
  flash (limits below).

### Key change (key epochs)

Staged switch (Thread's key switch on the first frame
received under the next key):

- A board holds its **current** key (the epoch bit e of the header, §11) and
  at most one other: the **next** key (bit 1 − e) before a switch, the
  **previous** key (bit 1 − e) for a grace period after it.
- A board sends under its current key. On reception the epoch bit selects the
  key; a frame without a matching key is an authentication failure.
- **Switch**: a frame that authenticates under the next key proves that its
  sender has switched. The receiver switches too: the next key becomes
  current, the former current key becomes previous, accepted for a **grace
  period** of 4 presence periods and then erased. Only a key holder can thus
  trigger a switch. A new next key can be loaded after the grace period only.
- The frame counter continues across switches (nonce uniqueness holds even if
  a key is used again later).
- **Procedure**:
  1. The next key reaches every board: distributed by the root's application
     (below; SILy: over WiFi, nothing to do on the Nodes), or entered locally
     (password through the UI or JSON). A board holding a next key sets the
     presence flag **next key loaded** (bit 7, §11); the root shows how many
     nodes are ready.
  2. The operator commands the switch at the root, normally once every node is
     ready. The root switches and resets its Trickle timer, as for a new DODAG
     version.
  3. Each node switches on the first frame it authenticates under the next
     key, from any neighbour: the switch crosses the network as a version
     does, within one Imin per hop (§9). Frames still sent under the old key
     are accepted during the grace period: no outage.
  4. A node that was off keeps the next key and switches on the first frame it
     hears. A node without the next key fails authentication and appears
     silent; the root reports it (§13) and the operator configures it.
- **Uses**: a new key per event (recommended, and cheap with distribution:
  an older recording loses its value), after a full flash erase of a board
  (above), after the loss of a board (the switch then proceeds without it; it
  must not hold the next key, so the next key is then entered locally, not
  distributed).

### Key distribution

Key changes must not require a manipulation on every board. The mesh
provides the cryptographic part; the transport belongs to the application,
which runs on the root (SILy: the Gateway, over WiFi):

- **Generation**: the root generates the next key as a random password of
  26 base32 characters (128 bits), so that offline guessing no longer applies
  once the network has switched to it. Its master key is derived as above;
  the password can be shown on explicit request in the root's authenticated
  administration, to configure a new or erased board by hand. An
  operator-chosen password remains possible.
- **Wrapped next key** (mesh API at the root): version 1 B, fingerprint of the
  current key 4 B, random nonce 13 B, next master key 32 B encrypted with
  AES-128-CCM under the current wrapping key, tag 16 B (no airtime
  constraint): 66 B. The associated data are the version, the current
  fingerprint and a 16-byte nonce chosen by the requesting board, so that a
  recorded answer cannot be replayed.
- **Loading** (mesh API at a node): the node checks the current fingerprint,
  then the tag, then loads the result as its next key. A valid tag also
  proves that the sender holds the current key: a fake access point cannot
  impose a key.
- **Trust**: whoever holds the current key can obtain the next one. The
  distribution therefore cannot exclude a captured board: the
  root's application offers the wrapped key only while the operator has
  opened the distribution, and after the loss of a board the next key is
  entered locally on the remaining ones. Per-board enrolment with revocation
  is an evolution (limits below).

### Coexistence (MP16)

- The radio uses the **private LoRa sync word** (0x12; 0x1424 on the SX1262):
  LoRaWAN traffic (public sync word) is filtered by the radio itself.
- Frames of other networks using the private sync word, another network of
  this mesh included, fail authentication. They are counted per sender and
  cannot be told from a misconfigured board. The root reports them in two
  groups: senders that never authenticated (wrong key or foreign network) and
  senders that did before (a key switch missed).
- Two networks within range share the channel capacity, which the budget
  (§16) does not include: tolerated, not supported in v1. Separate networks
  need separate channels (regional profiles, §4).

### Reception order

1. LoRa CRC (unless option A6, below), then protocol version (MP25).
2. Key selected by the epoch bit; CCM verification and decryption. Failure:
   counted per sender (MP22), frame dropped.
3. Replay check (above). Failure: counted per sender, frame dropped.
4. Only then: neighbour table and link estimation (counter gap, rank,
   version, §9), duplicate address check (link source equal to the node's own,
   §13), key switch, and the messages (link destination: own address or
   0xFFFF; other frames are overheard for passive acknowledgements, §9).

### Option A6 (LoRa CRC off)

v1 keeps the LoRa payload CRC on. With CRC off (saves 16 bits on air, §11),
the tag still rejects corrupted frames, with a false acceptance of 2^-32 per
corrupted frame (better than the 16-bit CRC), but noise and a wrong key then
both show as authentication failures; separating them relies on their
pattern (wrong key: every frame of a sender fails; noise: sporadic failures).
Decided after measuring the corruption rate on the bench (§18).

### Limits of the shared network key (MP17)

- **Any holder of a configured board or of the password is a full insider**:
  it decrypts everything, can impersonate any node (the link source is not
  bound to a key), and can forge messages and routing information (attract
  traffic with a low rank, then drop it). Detection is only indirect: class 2
  gaps, rank errors, an inconsistent graph at the root.
- **A captured board yields the key** (flash not encrypted): change the
  password on every other board (key switch above).
- **Header in clear**: topology and traffic volume visible (S3).
- **Jamming** and selective jamming: out of scope, visible as silence.
- **Offline guessing**: protection rests on the password's strength.
- **Key distribution** reaches any holder of the current key: a lost board
  cannot be excluded by it.
- Evolutions: per-node keys (the root holds the master key, each node a key
  derived from its MAC, with end-to-end authentication of the origin);
  per-board enrolment and revocation for key distribution; flash encryption
  and secure boot of the ESP32-S3 (they complicate over-the-air updates).

### Security state

| State | Where | Survives |
|---|---|---|
| frame counter, class 2 sender sequences | NVS, by blocks | reboot, power loss, factory reset; not a full flash erase (random restart) |
| current, next or previous key; fingerprints | configuration (flash) | reboot, power loss (a reboot restarts the grace period); erased by factory reset |
| replay table (with the duty-cycle window, §6) | RTC memory | soft reset, watchdog; not a power loss |

# Part II — SILy profile

## 15. Traffic model

Scenario, parameters of `tools/mesh_budget.py`:

- 30 Nodes (networks of the order of tens of nodes), one Gateway (root) with one radio.
- Peak 1000 punches/h through the busiest relay; bursts of 100 punches/min
  (mass start, relays).
- A silent Node (or a lost last punch) must be reported within about
  135 s at SF7–8 and 270 s at SF9 in *competition* (the consequence of
  `K_MISS` = 4, §16).
- Worst case for a relay: all 29 other Nodes one hop behind it, the relay one
  hop from the Gateway (`SUBTREE = 30`). All boards share one collision domain.

Message sizes follow §11. Compared with a first budget (records of 5, 21 and
14 B in 20 B of frame overhead), the compact layered format (13 B of frame
overhead, first-hop compression, bundling) shortens the small frames that
dominate the channel: presence periods shorten at SF8–10, so that
**detection within the targets holds up to SF9** (§16); aggregated punch
capacity is about the same, single-punch capacity higher.

## 16. Airtime budget

### Time on air (ms)

| SF | beacon 23 B | presence ×1 17 B | presence ×30 197 B | punch ×1 27 B | punch ×17 255 B |
|---|---|---|---|---|---|
| 7 | 62 | 51 | 313 | 67 | 400 |
| 8 | 113 | 93 | 553 | 123 | 707 |
| 9 | 206 | 165 | 984 | 226 | 1250 |
| 10 | 371 | 330 | 1804 | 412 | 2296 |
| 11 | 823 | 659 | 3936 | 823 | 5001 |
| 12 | 1483 | 1319 | 7217 | 1647 | 9019 |

(Beacon: the root's DODAG information message, §9. ×1: a node's own message
on its first hop; ×30 and ×17: bundles forwarded by a relay.)

### Naive heartbeat (rejected)

One 44-byte heartbeat per Node, forwarded hop by hop as is (SILy-Arduino
style): to keep heartbeats under 30 % of the busiest relay's budget, the
period must be 2 min at SF7, 10 min at SF9, 1 h at SF12. Incompatible with the
detection target beyond SF7.

### Retained scheme: split presence / topology, aggregation at relays

Policy: presence ≤ 30 % and topology ≤ 10 % of a relay's budget; presence +
topology ≤ 15 % channel load, the root beacon included (two per presence
period, §9); topology period ≥ 5 × presence period; a Node is reported silent
after 4 missed presences (`K_MISS` = 4: no presence for 4.5 presence periods,
finding 2 below). The presence period is sized on presence + one beacon per
period ≤ 75 % of the 15 % (topology the other 25 %); the second beacon is
counted in the channel load only, so that periods and detection times stay
those of one beacon per period (it takes the presence share to 103 % at SF8,
the channel load stays at 13.9 %).

| SF | presence period (s) | detection (s) | topology period (s) | relay budget left (s/h) | punches/h, 1 per frame | punches/h, aggregated | burst/min, 1 per frame | burst/min, aggregated | channel load idle | channel load at peak |
|---|---|---|---|---|---|---|---|---|---|---|
| 7 | 30 | 135 | 180 | 259 | 3871 | 11002 | 225 | 638 | 9 % | 12 % |
| 8 | 30 | 135 | 300 | 228 | 1849 | 5486 | 122 | 361 | 14 % | 21 % |
| 9 | 60 | 270 | 600 | 239 | 1056 | 3248 | 66 | 204 | 12 % | 25 % |
| 10 | 120 | 540 | 900 | 238 | 579 | 1764 | 36 | 111 | 13 % | 36 % |
| 11 | 300 | 1350 | 3600 | 259 | 287 | 882 | 17 | 51 | 10 % | 58 % |
| 12 | 600 | 2700 | 3600 | 249 | 151 | 469 | 9 | 28 | 11 % | 102 % |

("Channel load idle" counts presence, topology and two root beacons per
presence period; "at peak" adds 1000 single-punch frames per hour over two
hops. Burst capacity keeps the channel around the relay under 50 %.)

### Root acknowledgements (class 1, last hop)

The root forwards nothing, so class 1 cannot rely on passive acknowledgement
on the last hop: the root batches acknowledgements (8 entries per frame) and
keeps them under 5 % of the channel. Batching delay: 1.8 s at SF7, 6.2 s at
SF9, 46 s at SF12; at most 180 s/h of the root's budget, reached only under
saturating traffic (1000 single-punch frames/h at SF7 cost about 51 s/h).
The root beacon, two per presence period, takes 15 to 27 s/h of the root's
budget besides (27 s/h at SF8).
Class 1 retransmission timeouts must exceed this delay (§9). Under load the
root's medium access can delay a batch beyond the timeout; such a late
acknowledgement is still accepted (§12).

### Findings

1. **The shared channel is the bottleneck, not the relay duty cycle.** With
   aggregation, every presence period is limited by channel load, because all
   last-hop frames converge on the root's single receiver. Spreading the
   traffic over several first-hop relays (`mesh_budget.py 10`: 3 relays of 10)
   barely changes the periods (presence periods unchanged, topology periods
   shorter); it mainly frees relay budget (≈ 280–295 s/h left). The
   single-relay case stays the worst case for a relay's own duty cycle.
2. **Detection within the targets holds up to SF9**: 135 s at SF7 and SF8,
   270 s at SF9; it degrades to 9 min at SF10 and 45 min
   at SF12. Faster detection at high SF would exceed a sane channel load.
   The root threshold, 4.5 periods after the last presence heard, follows
   from the ±10 % jitter (TLA+ `KMiss`, `Silence`, no on-air delay):
   tolerating 3 lost presences needs more than 4 × 1.1 = 4.4 periods,
   detecting 4 at most 5 × 0.9 = 4.5, avoiding a false alarm after a relay
   failure more than 1.1 + 2.5 periods plus the report delay (§9). With
   `K_MISS` = 3 the band is empty (tolerating 2 losses needs ≥ 3.35 periods,
   detecting 3 ≤ 3.6, no false alarm ≥ 3.7). At 4.5 periods an on-air delay
   variation of the presence frames below 0.1 period keeps 3 losses
   tolerated, but any variation may let 4 consecutive losses pass
   unreported (5 are reported while it stays within 0.9 period).
3. **1000 punches/h needs SF ≤ 9 with one punch per frame, SF ≤ 10 with
   aggregation**; SF11–12 cannot carry it (the channel saturates). Bursts of
   100/min need SF ≤ 8, or SF ≤ 10 with aggregation.
4. **Loss rate is the main unknown.** At the peak load the presence loss rate
   lies between the carrier-sense bound (frames collide only when they start
   within ~6 symbols, one collision domain) and pure ALOHA (hidden terminals
   everywhere). False "silent Node" reports per hour (4 consecutive lost
   presences): SF7 0.002–8.0, SF9 0.05–43, SF12 0.9–103. The real figure
   must be measured on the bench and in the field; `K_MISS` and
   the periods are configuration parameters.

### Design consequences

- **Presence and topology are separate mesh reports.** Presence (small)
  every presence period; neighbour list every topology period, and on change
  in *deployment*. SILy's battery and mode ride on the presence application
  extension (§11); the last punch sequence is the class 2 tail carried by
  presence until acknowledged (MP4).
- **Opportunistic aggregation** of opaque messages per next hop (§7), with
  bundling of same-size messages and first-hop compression (§11).
- **Routing information travels in frame headers** (version, rank); only the
  root emits a periodic beacon, every half presence period (its own jittered
  timer, §9: three consecutive lost beacons tolerated by its children),
  counted in the 15 % channel load; no flooded routing beacons (other
  advertisements: Trickle, §9). Broadcasts are rare.
- **Any authenticated frame proves its sender alive** to its neighbours; at
  the root, any upward message proves its origin alive, so a node that sent
  traffic recently may skip its presence report.
- **Root-side diagnosis**: a lost relay frame drops many reports at once; the
  Gateway reports "relay X and its subtree silent" rather than 30 separate
  alarms; with parent sets (§1), the subtree normally re-attaches by itself.

## 17. Periods per operating mode

Periods follow §16 for the current SF (`presence(SF)`, `topology(SF)`). The
mode manager (application, on the Gateway) sets them through the root's
run-time parameters (MP21); the root issues a new DODAG version carrying them
in the DODAG information message (§9), so every node applies them:

| Mode | Presence | Topology | Notes |
|---|---|---|---|
| configuration | presence(SF) | topology(SF) | bench |
| deployment | presence(SF) | on change, at least topology(SF) | mesh map feedback while installing; link margins and recommended SF (§5) |
| competition | presence(SF) | topology(SF) | punches have priority (§6) |
| retrieval | 2 × presence(SF) | 2 × topology(SF) | only liveness matters; WiFi on for locating the board |

## 18. Open points

- Measure the frame loss rate under load on the bench (≥ 3 boards) and in
  the field, then set `K_MISS` and the periods (§16). Measure also the
  variation of the on-air delay of presence frames: the root alarm at 4.5
  periods tolerates three lost presences only while it stays below about
  T/20 (§16, finding 2).
- Adaptive SF (§5, deferred): reconsider once loss rates and margin
  stability are measured.
- Check the decision in force (ARCEP 2014-1263 as amended) for 869.4–869.65 MHz.
- Global repair wave (§9): a solicitation of finite rank (a node behind a
  newer version) answered by newer neighbours only, so that the node's own
  children no longer answer it in the window of its parent's advertisement
  (a collision in about 15 % of the hops whose node has children). To
  decide; then check it with the TLA+ `DODAG` model and the seed runs.
- Option A6 (§11, §14): LoRa payload CRC off, relying on the AEAD tag;
  measure the corruption rate on the bench and check that diagnostics
  still separate sporadic failures (noise) from systematic ones (wrong key).
- CPU under load: on two boards at SF7, sending takes 12.3 ms of CPU per
  frame and receiving 6.8 ms, about 4 % of one core at 200 frames per
  minute (`docs/measurements.md`); to check again under load.
- Link estimation (§9) to watch on the bench: the 16-frame window always
  ends on a received frame (inbound ratio biased up by ≤ 1/16); a gap > 16
  restarts the window, so a neighbour that went silent for more than 16 of
  its frames reads as a perfect link until new samples arrive; a link whose
  failures aged out reads good again until it is used (a one-way link heard
  well is retried every 2.5 periods); a bad link seen only through presence
  needs 13 lost presences to become ineligible; the link to the root is
  left only after a silence limit without its acknowledgement, so a root
  child keeps a root whose batches mostly collide at it (in the simulator's
  star with the root hidden, none reached the root child for longer than
  that during the burst: the root was left all the same, D28). Revisit if
  repairs choose bad links.
- Fairness parameters (quota 16 per origin above half a queue, §7): checked
  in a host scenario with a flooding node; to check on the bench.
- Parameters chosen on paper (Imin, k, MaxRankIncrease, attempts,
  timeouts, holding, windows, cache sizes): check on the bench, together
  with the memory footprint of the mesh state (§12).
