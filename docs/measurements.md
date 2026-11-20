# Bench measurements

Measurements of the link layer on real boards: radio adapter, ESP32
platform, frame security. Procedure: `tools/bench.py` (host side, drives two
boards over their serial ports; see its docstring), with the current `src/`
deployed on both boards (`make deploy PORT=<board>`):

```sh
~/git/t3s3-micropython/.venv/mpremote/bin/python tools/bench.py \
    --a /dev/serial/by-id/<board A> --b /dev/serial/by-id/<board B> --out results.json
```

The root's reply turnaround runs apart, one spreading factor per run:
`--only reply --sf <7|8|9>`, and `--only reply_routing` with the mesh routing
on both boards.

The last section times the simulator on the host (`make simbench`).

## 2026-10-17 — two T3S3 boards, SF7

Conditions: two LilyGo T3S3 (ESP32-S3 at 240 MHz, SX1262), firmware
`SILY_T3S3` (MicroPython 1.29.0, frozen `lora-async`), application deployed
as `.py` source (not frozen). Boards on one desk, TX power −9 dBm: RSSI
−49 dBm, SNR +12 dB. EU profile: 869.525 MHz, BW 125 kHz, CR 4/5, preamble
8, explicit header, CRC on, sync word 0x1424. Bench key: PBKDF2 with 1000
iterations. Figures from one full run (A address 1, B address 2), the
carrier sense ones repeated five times.

### Platform

| Item | Result |
|---|---|
| RTC user memory (`RTC().memory()`) | **2048 B**; kept across soft reset and `machine.reset()`; the mesh uses 901 B (duty-cycle window 257 B, replay table 644 B) |
| NVS write + commit of the 4-byte counter record | mean 2.6 ms, max 15.8 ms (another run: 1.9 / 11.4 ms); `commit` alone 14 µs: ESP-IDF writes at `set` |
| PBKDF2-HMAC-SHA256, application loaded | 0.24–0.25 ms per iteration: **1 s ≈ 4 200, 2 s ≈ 8 400 iterations**; 6 000 (`kdf.DEFAULT_ITERATIONS`) → 1.44 s; 10 000 → 2.4 s |
| PBKDF2, fresh heap (after soft reset) | 0.113 ms per iteration (1 s ≈ 8 900) |
| Heap of the link engine (`open_link` + runner, empty queue) | 12.6 KB (15.8 KB in one run) |
| Heap of the bench node (modem, adapter, filter, engine, runner) | 15 KB |
| Heap of the imported modules (`sily.bench` and dependencies, source) | 72.9 KB; 1.96 MB free after start (PSRAM) |

The PBKDF2 rate halves once the application is loaded: small objects then
live in PSRAM instead of internal RAM. The loaded state is the one that
counts (the password is entered in the running application).

### Frame security (median of 31 frames)

| Frame | Seal | Open (auth + decrypt + replay) | Decode messages |
|---|---|---|---|
| 19 B | 1.20 ms | 1.34 ms | 0.22 ms |
| 63 B | 1.44 ms | 1.56 ms | 0.22 ms |
| 127 B | 1.79 ms | 1.89 ms | 0.24 ms |
| 254 B | 2.44 ms | 2.53 ms | 0.25 ms |

Maxima reach 11–13 ms (garbage collection, frame counter reservation in NVS).

### CPU per frame (36-byte frames, means of 20 echo exchanges)

| Path | Board A (echo sender) | Board B (echo responder) |
|---|---|---|
| Adapter `transmit` (SPI: standby, buffer, SetTx) | 4.7 ms | 4.7 ms |
| Adapter TX done (back to continuous reception) | 3.5 ms | 3.4 ms |
| Adapter carrier sense (IRQ status, RSSI) | 1.4 ms | 1.4 ms |
| Adapter reception (2 IRQs + RxDone, buffer, packet status) | 2.7 ms | 2.7 ms |
| Step CHANNEL idle (encode, seal, adapter `transmit`) | 8.2 ms | 8.1 ms |
| Step RX (open, decode, handler) | 4.0 ms | 8.0 ms (handler submits the reply: pack and sense included) |
| **TX path** (CHANNEL step + TX done step + adapter TX done) | 12.4 ms | 12.2 ms |
| **RX path** (adapter reception + RX step) | 6.8 ms | 10.6 ms |
| Driver only, per frame sent / received | 8.2 + 1.4 ms / 2.7 ms | same |

Packing and sensing before a transmission add 3–4 ms (timer step). Echo
round trip: 20/20 replies, median 198 ms (196–219), for 2 × 77.8 ms on air:
about 42 ms of processing on the two boards.

### Carrier sense (A senses every 30 ms while B sends 20 frames of 219 B)

| Situation | Busy / senses | By IRQ window | By RSSI only |
|---|---|---|---|
| Idle channel | 0 / 50 (RSSI −104 dBm, floor −104 dBm) | 0 | 0 |
| B transmitting, five runs | 96, 95, 97, 93, 77 / 100 | 95, 93, 95, 92, 52 | 1, 2, 2, 1, 25 |

Idle senses fall in the gaps between B's frames (about 20 ms per 370 ms).
The run with 25 RSSI-only senses and 23 idle ones was not reproduced
(foreign interference suspected; the window counted no stale end). With B
flooding, A's own link engine sensed busy 4–21 times in 6–23 senses,
backed off, gave up 0–3 times, and sent its 2 frames afterwards; A
received 39 of B's 40 frames (half duplex: A transmitted twice meanwhile).

Noise floor: board A −104 dBm, board B −111/−112 dBm (spike: −112 dBm).

### Time on air (B, 40 frames of 219 B)

| Computed (driver formula) | Measured (SetTx → TxDone IRQ) | Accounted (duty cycle) |
|---|---|---|
| 13 936.6 ms (348.4 ms per frame) | 13 962.1 ms (349.0–349.2 ms) | 14 000 ms (350 ms) |

The measured duration exceeds the computed one by 0.6–0.8 ms (36-byte
frames: 77.8 ms measured for 76.8 ms computed); the duty cycle charges the
larger of both, rounded up to the ms, so the accounted time is always at
least the measured one.

### Error counters (option A6)

| Test | Receiver A |
|---|---|
| B sends 5 frames under another password | 5 authentication failures, 0 frames accepted, 0 CRC errors; sender 2 counted, flagged as known before |
| B sends 5 raw frames of 100 B cut after 40 ms | 5 CRC errors (radio and link counters), 0 authentication failures, 0 header errors |
| Neighbour filter allowing only 3, B sends 3 frames | 3 filtered, 0 accepted; after `filter 2`, the next frame is accepted |

## 2026-10-17 — routing smoke test, two T3S3 boards, SF7

Same boards and settings; `tools/bench.py --only routing`: board A runs the
bench console as the root (address 0, `routing=True`, presence period 10 s,
topology 60 s), board B as a node (address 2, `routing=True`), both with
`mesh.routing.Routing` (DODAG, upward forwarding, reports).

| Item | Result |
|---|---|
| B attached | rank 8, preferred parent 0 (the root), DODAG version 0 adopted from the root's information; 6.2 s after B's bench start (key derivation, solicitation, answer) |
| Root reports | `PRESENCE 2 0 1` (parent 0, flag *first report*), then `TOPOLOGY 2 [[0, 20, true, 100]]` (margin 20 dB, parent-set member, 100 % delivery), `NODEINFO 2 dc5475db9604 bench2`, then `PRESENCE 2 0 0` one period later |
| Counters | no rank error, loop, refusal or malformed report on either board |
| Heap of the bench node with routing (`heap_engine`) | 31.3 KB (12.6–15.8 KB with static routes) |

## 2026-11-14 — two T3S3 boards, SF7–SF9: baseline of the current code

Conditions: the boards and settings of 2026-10-17 (A `b8f862d91b8c`, B
`dc5475db9604`; same firmware, WiFi off, TX power −9 dBm: RSSI −48 dBm, SNR
+12 dB; bench key with 1000 iterations). Code: `src/mesh/` and `src/board/`
of commit b7daf82, deployed after removing the boards' `mesh`, `sily` and
`board` directories, as `.py` source, then compiled to `.mpy` (`mpy-cross`
1.29.0, `main.py` kept as source) for the memory, key derivation and CPU
figures. The noise floor was about 3 dB higher than on 2026-10-17 on both
boards (A −100/−101 dBm, B −107 to −110 dBm): the room, not the code.
Figures from one run of each test unless stated.

### Memory and flash

| Item | 2026-10-17 | `.py` | `.mpy` |
|---|---|---|---|
| Heap of the imported modules (`sily.bench` and dependencies) | 72.9 KB | **134.4 KB** | 133.4 KB |
| Heap of the link engine (`open_link` + runner, static routes) | 12.6 KB (15.8 KB) | **36.1 KB** | 36.1 KB |
| Heap of the bench node (static routes) | 15 KB | 38.5 KB | 38.4–38.6 KB |
| Heap of the link engine with routing (`heap_engine`), root A / node B | – / 31.3 KB | **39.1 / 38.5 KB** | same |
| Heap of the bench node with routing, root A / node B | – | 40.9 / 40.3 KB | same |
| Free heap after start (PSRAM) | 1.96 MB | 1.93 MB | 1.93 MB |
| Application files (`board`, `mesh`, `sily`, `main.py`) | – | 400 KB (`mesh` 358 KB) | 85 KB (21 %) |

The link engine alone now takes 23.5 KB more than on 2026-10-17, and the
routing only 2.4–3 KB on top of it (18.7 KB then): what the mechanisms added
since allocate is mostly allocated with the engine (not broken down here).
Compiled modules save flash and the compilation at import, not heap: the
bytecode is in RAM either way.

### Platform and key derivation

| Item | 2026-10-17 | `.py` | `.mpy` |
|---|---|---|---|
| NVS write + commit of the counter record, mean / max | 2.6 / 15.8 ms | 2.38 / 11.3 ms | same |
| PBKDF2, application loaded, per iteration (two runs) | 0.24–0.25 ms | 0.197–0.207 ms | **0.299–0.308 ms** |
| 6 000 iterations (`kdf.DEFAULT_ITERATIONS`), timed | 1.44 s (computed) | 1.19 s | **1.75 s** |
| PBKDF2, fresh heap (after soft reset) | 0.113 ms | 0.112 ms | 0.113 ms |

RTC user memory unchanged (2048 B, kept across both resets). The derivation
takes about 1.5 times as long with compiled modules, reproduced in two runs and two
direct timings; presumably more of its small objects land in PSRAM, as
between a fresh heap and the loaded application.

### Frame security and CPU per frame (`.py`)

Seal, open and decode medians within 0.06 ms of 2026-10-17 for every frame
size; the open maxima reach 19.9–20.5 ms (127 and 254 B; 11–13 ms then).

| Path (means of 20 echo exchanges, A / B) | 2026-10-17 | `.py` | `.mpy` |
|---|---|---|---|
| Step CHANNEL (encode, seal, adapter `transmit`) | 8.2 / 8.1 ms | 8.6 / 8.5 ms | 8.4 / 8.6 ms |
| Step RX | 4.0 / 8.0 ms | 4.6 / 7.9 ms | 4.6 / 8.1 ms |
| **TX path** | 12.4 / 12.2 ms | 13.0 / 12.8 ms | 12.9 / 12.8 ms |
| **RX path** | 6.8 / 10.6 ms | 7.3 / 10.7 ms | 7.3 / 10.5 ms |
| Driver only, per frame sent / received | 8.2 + 1.4 / 2.7 ms | 8.35 + 1.35 / 2.73 ms | same |

Means without one collection of 20–26 ms in the run (with it: RX path of A
8.4 ms in `.py`, of B 11.7 ms in `.mpy`). Echo round trip: 20/20 replies,
median 201 ms (199–222), about 46 ms of processing on the two boards (42 ms
then). Packing and sensing before a transmission: 3.6 ms.

### Carrier sense, time on air, error counters, routing

| Carrier sense (A senses every 30 ms while B sends 20 frames of 219 B), five runs | 2026-10-17 | 2026-11-14 |
|---|---|---|
| Idle channel, busy / senses | 0 / 50 | 0 / 50 in each run |
| B transmitting, busy / 100 senses | 96, 95, 97, 93, 77 | 94, 96, 95, 92, 96 |
| of which by IRQ window | 95, 93, 95, 92, 52 | 90, 93, 95, 91, 96 |
| of which by RSSI only | 1, 2, 2, 1, 25 | 4, 3, 0, 1, 0 |
| A's engine while B floods: busy / senses, give-ups | 4–21 / 6–23, 0–3 | 1–21 / 3–23, 0–3 |
| A received of B's 40 frames | 39 | 39, 39, 39, 40, 40 |

The 2026-10-17 run with 25 senses busy by RSSI only is not reproduced; the
idle senses still fall in the gaps between B's frames. Time on air (B, 40
frames of 219 B): measured 13 961.6–13 970.1 ms in five runs for
13 936.6 ms computed, accounted 14 000–14 008 ms, never below the measured
time. Error counters (wrong password, cut frames, neighbour filter): the
same counts as on 2026-10-17. Routing smoke test: the same reports and
counters; B attached after 2.2 s (6.2 s then), adopting the root's DODAG
information heard before it solicited it.

### Root's reply turnaround

`--only reply` (`reply_routing`: both boards with the mesh routing). B sends
probes at random gaps, each one frame to the root carrying an upward message
of another node (so that the root counts B as relaying) and B's own
presence: the root answers each at once and B keeps its reply window
(MESH_DESIGN §8). Root measure: from the RxDone IRQ of B's frame to the
SetTx of the reply. B's estimate: from its TxDone to the reply's RxDone
minus the reply's computed time on air. Margin: what is left of B's reply
window when the reply is received. The root forces a collection in every
10th turnaround. The design gives the turnaround one backoff slot (time on
air of a 17 B frame) of the window's two slots of slack. Code: the commit
that adds this test (no change to `src/mesh/` or `src/board/`).

| Run | Samples, natural / forced collection | Root, natural: median / p95 / p99 / max | Root, forced collection: median / max | B's estimate, max | B's margin, min | Slot / slack |
|---|---|---|---|---|---|---|
| SF7 | 360 / 40 | 20.8 / 23.1 / 25.0 / 34.5 ms | 42.3 / 44.5 ms | 46.6 ms | 85 ms | 52 / 104 ms |
| SF8 | 135 / 15 | 20.7 / 23.7 / 29.3 / 32.3 ms | 45.2 / 46.7 ms | 50.2 ms | 194 ms | 93 / 186 ms |
| SF9 | 135 / 15 | 20.8 / 22.3 / 32.2 / 37.6 ms | 45.0 / 46.1 ms | 53.1 ms | 385 ms | 165 / 330 ms |
| SF7, routing | 142 / 15 | 23.0 / 25.9 / 30.1 / 37.0 ms | 47.7 / 49.0 ms | 51.0 ms | 79 ms | 52 / 104 ms |

- No turnaround above one slot; B's margin always positive. Every probe
  answered at once (replies = probes = samples in the static runs); with
  routing, one probe of 150 lost on the air (the root was sending a beacon),
  none to the turnaround, and B's periodic presences answered at once too.
- At SF7: RxDone service 3.1 ms, RX step 8.35 ms (sensing included), CHANNEL
  step 8.6 ms, asyncio wake-ups about 1–1.5 ms. The routing adds about
  2.1 ms per reply (its hooks and the bench's `PRESENCE` print).
- Tail: a frame counter block written to NVS (every 256 frames, on both
  boards at once here) +12–17 ms, also delaying B's frame (B's margin 85
  instead of 111 ms at SF7); a collection +21.5–25 ms; the first frame after
  start 24–32 ms. A collection and a counter block in one turnaround (about
  60 ms at SF7 by addition: above one slot, below two) was not observed.
- B's estimate exceeds the root's measure by a near-constant 2.1 / 3.5 /
  6.9 ms at SF7 / 8 / 9: the RxDone delay after the last symbol on both
  boards, and the reply on air 0.63 ms longer than computed.

### Against the simulator's assumptions

| Item | Simulator (`sim/`) | Boards |
|---|---|---|
| Root's immediate reply turnaround | 1 ms (`sense_ms`) | median 21 ms (23 with routing), max 38 ms, 42–49 ms with a collection |
| Start of the sender's reply window | its frame's start on air | about 8 ms before it (the CHANNEL step), 21 ms when a counter block is written |
| Steps and collections | no time | 8–9 ms per CHANNEL or RX step; collections 20–26 ms |
| Time on air | computed | +0.63–0.70 ms |
| Carrier sense during a frame | every audible frame busy | 92–96 % busy; the idle senses fall between frames |

Not measured here (a third board needed): the start of the window of a
child that overhears its parent's frame to the root, and back-to-back
reply windows.

## 2026-11-20 — one T3S3 board, the box application

Conditions: one board alone (no other box on the air), indoors, on the
laptop's USB, the box application at b3e3b13 deployed as `.py`, SF7,
−9 dBm, configured by `make configure` first as a node (never attached),
then as a gateway. Journal records every 10 s.

### Loop lateness (`sily.lateness`: 20 ms period, 30 ms bound)

| Run | Span | Wakes per 10 s | Longest, quiet records | Wakes over the bound | Deaf time |
|---|---|---|---|---|---|
| Node alone | 5:42 | 411–455 | 19–22 ms | 26 | 615 ms (0.18 %) |
| Gateway alone, page open ~2 min | 3:41 | 422–453 | 16–25 ms | 9 | 162 ms (0.07 %) |

- About 2640 wakes per minute. The quiet records' longest lateness comes
  at one phase of each second: the display's refresh (once a second).
- Over the bound, by the figures of the same record: the journal's record
  write (`jw` 43–141 ms: lateness 44–140 ms), the most frequent; a garbage
  collection (35–39 ms, no write in the record); the web server's
  stretch with the page open (36–72 ms: lateness up to 69 ms); a display
  refresh (93 ms, once).
- Per wake, at the REPL (3 × 2000): `asyncio.sleep_ms(0)` 178–193 µs, with
  the probe's own lines 201–217 µs: about 0.9 % of the CPU at about 44
  wakes per second. The probe alone on a loop for 10 s: 499 wakes, longest
  lateness 3 ms: the idle loop's 100 Hz tick did not delay the wakes here
  (board on USB).

### Clock

- `make configure`: RTC check +0 to +2 ms (five runs).
- Running, without a reset or a page: within the readings' ±2–3 ms over 69
  and 151 s; a `machine.reset` stepped it by at most 1 ms.
- A phone opening the page sets the RTC (a boot's first sample is an
  anchor, `sily.utc`): the gateway read 30 ms ahead of the laptop's clock
  four minutes later (anchor departure +42 ms), the node 73 ms ahead
  seven minutes later (anchor departure −19 ms): the phone's clock and the
  request's delay replace `make configure`'s setting. `tools/bench.py`'s
  platform test moved it by 0.6 s.

### Gateway page

Load about 3 s on a phone (page 29 KB); the server's longest stretch 38 ms
during the load, 19–37 ms per record while the page polls.

## 2026-10-19 — simulator speed, host

`make simbench NODES=<n>` (`tools/simbench.py`, see its docstring): grid of
n boards, the root at a corner, each board hearing its 4 grid neighbours;
routing with presence period 10 s and topology period 60 s; one board in 7
sends a class 1 message to the root every minute. 10 simulated minutes,
seed 1, MicroPython 1.29.0 unix port with the 32 MB heap of `make test`.
The last column is the same run without the simulator's allocation measures
(below); both give the same digest.

| Nodes | Events | Receptions | Wall clock | Per event | Without the measures |
|---|---|---|---|---|---|
| 16 | 24 613 | 9 986 | 2.5 s | 100 µs | 30.0 s |
| 30 | 60 421 | 25 715 | 6.9 s | 115 µs | 92.8 s |
| 64 | 163 763 | 74 015 | 23.5 s | 145 µs | 242.8 s |

Events grow faster than nodes: the grid deepens, and reports cross more
hops. Time spent (30 and 64 nodes alike): mesh cores 80 % (no function
above 4 %), medium 10 %, scheduler and the rest 10 %. In the interpreter
(callgrind, 30 nodes, 2 simulated minutes), 42 % of the instructions are the
allocator's search for free blocks and 14 % garbage collection (53
collections, each sweeping the whole 32 MB heap).

The allocator looks for room for a multi-block object from the lowest free
block on, a point only single-block allocations move forward. With nothing
collecting the 32 MB heap before it is full, each such allocation scanned
most of what was allocated since the last collection: 90 % of the time
(callgrind, 16 nodes). Measures: the scheduler collects every megabyte
allocated while it runs (`sim.scheduler.GC_THRESHOLD`; 512 KB gains 5 to
10 % here but slows `make test` by 20 %), the fake retained memory is saved
in place instead of into a new 2 KB object per frame, and the medium keeps
each source's links instead of visiting every port per transmission.
