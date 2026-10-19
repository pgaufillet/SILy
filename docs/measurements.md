# Bench measurements

Measurements of the link layer on real boards: radio adapter, ESP32
platform, frame security. Procedure: `tools/bench.py` (host side, drives two
boards over their serial ports; see its docstring), with the current `src/`
deployed on both boards:

```sh
~/git/t3s3-micropython/.venv/mpremote/bin/python tools/bench.py \
    --a /dev/serial/by-id/<board A> --b /dev/serial/by-id/<board B> --out results.json
```

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
