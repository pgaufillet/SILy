# SILy

Meshed LoRa network for SportIdent SRR orienteering stations, based on the
LilyGo T3S3 (ESP32-S3 + SX1262), written in MicroPython with asyncio.

Punch path:

**SI BSF8-SRR** --SRR--> **SI SRR module** --UART--> **SILy Node**
--LoRa mesh--> **SILy Gateway** --WiFi + TCP--> **MeOS**.

Nodes and the Gateway run the same application; the role comes from the
configuration. Work in progress: the MicroPython version is being written.
The previous Arduino version (v0.1.0) is on branch `arduino` and tag
`arduino-v0.1.0`.

## Layout

- `src/` — what goes on the board's filesystem:
  - `main.py` — entry point run at boot;
  - `sily/` — the application;
  - `mesh/` — application-agnostic LoRa mesh; imports nothing from `sily`,
    `board` or `machine` (radio and platform are injected), so that it runs
    on the unix port and can be extracted later; its core is event-driven
    without I/O (time and events in, actions and next deadline out,
    `mesh/core.py`), driven by its asyncio runner on the board
    (`mesh/runner.py`) and by the simulator on the host; its design is
    described in `docs/MESH_DESIGN.md`;
  - `board/` — board support, the only hardware-specific code: T3S3 wiring,
    ESP32 platform of the mesh (NVS, RTC memory, random source), SX1262
    radio adapter (reception, transmission, carrier sense).
- `sim/` — host-only mesh simulator, not deployed: deterministic random
  generator, discrete-event scheduler, platform fakes, scripted radio medium
  (loss, asymmetry, partitions, collisions, half duplex, carrier sense), the
  host runner of mesh cores, and networks of link engines (`sim/net.py`:
  boards with reset and power loss, sniffer for replays); seeded topologies
  of large networks (`sim/topologies.py`), invariant checks observing a
  running network (`sim/invariants.py`) and a trace recorder writing
  everything a network plays as JSON Lines (`sim/trace.py`). Imports `mesh`
  only; goes with it if the mesh is extracted.
- `tests/` — host tests (`run.py` runner, `test_*.py` modules), scenario
  suites on several seeds (`seeds.py`) and the large-network suite
  (`large.py`), both outside `make test`.
- `tools/bench.py` — two-board bench measurements, run from the host over
  the serial ports; results in `docs/measurements.md`.
- `tools/simbench.py` — simulator benchmark: wall-clock time and digest of
  a grid network (`make simbench`); figures in `docs/measurements.md`.
- `tools/trace.py` — traces of the networks of a test class or of one
  large-network run (`make trace`); `tools/traceview.html` — their offline
  viewer (one HTML file, opened in a browser).
- `tools/mesh_budget.py` — airtime budget and repair times of the mesh
  design (CPython 3), the source of the figures of `docs/MESH_DESIGN.md`.
- `firmware/SILY_T3S3/` — firmware variant: the upstream MicroPython
  `LILYGO_T3_S3` board plus the frozen `lora-async` package.

## Development environment

Builds and tools come from the `t3s3-micropython` development environment
(MicroPython, ESP-IDF, out-of-tree board builds), expected in
`~/git/t3s3-micropython` (override with `T3S3=<path>`). There, run
`make submodules`, `make unix` (host MicroPython) and `make venv` (mpremote,
esptool, ruff, codespell).

## Commands

```sh
make test                      # host tests on the MicroPython unix port
make lint                      # ruff check, ruff format --check, codespell
make check                     # lint + test
make simbench NODES=30         # simulator wall clock for 10 simulated minutes
make large                     # large-network invariants, about 7 min (not in check)
make large RUN="lossy star 2"  # one run of it, with its full report
make trace TEST=test_dodag_scenario.DetachRejoin  # traces into build/trace
make trace RUN="forming course 1 14"  # trace of one large run (nominal course)
make firmware                  # build the SILY_T3S3 firmware
make flash PORT=/dev/ttyACM0   # flash it
make deploy PORT=/dev/ttyACM0  # copy src/ to the board and reset it
```

Host tests use micropython-lib's `unittest` from the MicroPython tree (through
`MICROPYPATH`, nothing installed). `make test` fails if a test fails, a test
module cannot be imported, or no test runs.

## Large-network suite

`make large` (`tests/large.py`) checks the routing layer at the size of the
traffic model of `docs/MESH_DESIGN.md` (§15): 30 nodes and the root at SF8 with the
*competition* periods (presence 30 s, topology 300 s, root beacon every
15 s, Imin 3.0 s; §16–§17) and a class 1 load of 1000 punches per hour over
the network plus one burst of 100 punches within a minute (10-byte
payloads, 27-byte frames). Topologies (`sim/topologies.py`, seeded): one
collision domain (`domain`), the worst case of §16 (`star`: one relay next
to the root, the 29 others one hop behind it, hearing the relay and each
other, not the root), the same star in one collision domain (`star-domain`:
the root also hears and is heard by every board, on links too weak to be
parent links, SNR 1 dB above the SF8 limit) and a random geometric graph
with hidden terminals, the root at its centre (`geometric`: mean degree
about 6, depth 3–5).
Single runs (`RUN=`) also take `grid` and `course`, outside the plan: a
hand-placed orienteering course of up to 14 Nodes on three loops from the
arena, 1 to 3 hops, two possible parents beyond hop 1, the loops hidden
from each other (the nominal case, e.g. `RUN="relay-death course 1 14"`).
Scenarios: `forming`, `relay-death` (the relay that forwarded the most is
switched off at 6 minutes), `global-repair` (new DODAG version at 6
minutes), `lossy` (30 % of the links lose 10–25 % of their frames). Each
run lasts 15 simulated minutes, the last 2.5 without submissions.

The checks (`sim/invariants.py`, each shown able to fail by
`tests/test_invariants.py`) sample every board each second and watch the
medium's transmissions: MP11 (no preferred-parent cycle longer than
7 (Imin + 8 slots)), MP10 (every node connected to the root has a route
within Imin + h (S + W) after start and S + 2 Imin + 8 slots + h (S + W)
after losing it, h its hop count, S the silence limit, W the answer window;
every node adopts a new version within 7 Imin + 8 slots per hop of its
depth), MP5 (every class 1 message delivered or reported failed, design
exceptions apart), MP9 (time on air within the duty-cycle budget in every
window, from the medium) and MP13 (tables and queues within their sizes).
The bounds and their derivations are in the docstrings.

The plan is every scenario × topology at 30 nodes on `LARGE_SEEDS` seeds
(default 3), plus one 64-node run per scenario on the star (the relay's
neighbour table full at 64 entries), `LARGE_JOBS` runs at a time (default 4):
about 7 minutes on the development host (8 cores). It prints the report of each failing
run (statistics of each check, violations with time, nodes and detail, the
command reproducing it), then a summary line, and fails if any run fails.

## Traces

`make trace` records what simulated networks play (`sim/trace.py`), for
an offline viewer or a script: `TEST=MODULE[.CLASS]` runs the test classes
of `tests/MODULE.py` and writes every network they build to its own file;
`RUN="SCENARIO TOPOLOGY SEED [NODES]"` plays one run of the large-network
suite and prints its report. Files go to `OUT` (default `build/trace`,
ignored by git), one JSON object per line: a header (seed, SF, profile,
airtime, nodes with their positions when the topology has some, links),
then every transmission with its decoded messages and its receivers,
the fate of each reception (ok, loss, collision, half duplex, cut),
carrier senses and backoffs, rank, preferred parent and parent set
changes, the counters a step changed (retries, acknowledgements, errors,
Trickle resets...), submissions, deliveries, failures, reboots, link
changes, invariant violations and scenario events; the format is in the
docstring of `sim/trace.py`. The recorder only observes: a run computes
the same with or without it. `make trace RUN="relay-death star 1"` writes
about 3.5 MB (36 000 records) and takes 5 to 10 % longer than the run; the
small networks of the scenario tests take about 1.6 times as long.

`tools/traceview.html` shows them: open it in a browser from the file
system (one file, no network, no server), then load one or several traces
with *Open traces…* or by dropping them on the page. One cursor on the
records and the simulated time drives every view, the state shown being
the state after the cursor's record and later events dimmed: step with
← → (journal events), Shift+← → (transmissions), ↑ ↓ (any record), [ ]
(selected node), , . (selected message), play with Space (0.01× to
1000×), jump to a time with T, or click the overview strip under the
toolbar (airtime and collisions of the whole run); `?` lists the
shortcuts.

- *Chronogram*: one lane per node (by address, DODAG tree or rank): its
  transmissions coloured by content (DODAG information, solicitation,
  presence, topology, acknowledgement, error, application data), carrier
  senses and backoffs, its receptions coloured by fate (ok, loss,
  collision, half duplex, cut), routing state changes, submissions,
  deliveries, failures, down periods; violations and scenario events on
  top. Wheel zooms, drag pans; a click selects a frame and its message.
- *Journal*: each record as a sentence, following the cursor; filters by
  kind, node and message; a click moves the cursor.
- *Messages*: the class 1/2 messages with their outcome; the lifecycle of
  the selected one lists every frame carrying it (hop, attempt), its
  reception at the next hop, the explicit (port 3 entry naming the frame)
  and passive (next hop's forward overheard) acknowledgements with their
  fate at the sender, late ones, errors, delivery or failure.
- *Graph*: the nodes (topology positions when the trace has them,
  otherwise the DODAG tree at the cursor), links, preferred parents, ranks,
  frames on air and the selected message's path.
- *Details*: every field of the selected frame, record or node, with
  names; the *Legend & concepts* tab explains the colours and the mesh
  mechanisms (MESH_DESIGN §8, §9, §11, §12).

## Bench

`sily.bench` runs one link engine on a board with a line console (state
dump, send, echo, flood, carrier sense survey, neighbour filter), started by
hand from the REPL, never at boot:

```python
from sily import bench

bench.run(1)  # address 1, SF7, bench password, -9 dBm
bench.run(2, allow=(1, 3))  # address 2, hears only 1 and 3
```

With the code deployed on two boards, `tools/bench.py` drives both and
records the measurements:

```sh
~/git/t3s3-micropython/.venv/mpremote/bin/python tools/bench.py --a <port A> --b <port B>
```

## References

- SportIdent SRR: <https://www.sportident.com>.
- MeOS: <http://www.melin.nu/meos/en/>.
- LilyGo T3S3: <https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series>.
- MicroPython: <https://micropython.org>; LoRa drivers:
  <https://github.com/micropython/micropython-lib/tree/master/micropython/lora>.

## License

GPL-3.0, see `LICENSE`.
