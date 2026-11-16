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
  - `sily/` — the application, and the per-box hardware check
    (`sily/boxcheck.py`, `make box-check`);
  - `mesh/` — application-agnostic LoRa mesh; imports nothing from `sily`,
    `board` or `machine` (radio and platform are injected), so that it runs
    on the unix port and can be extracted later; its core is event-driven
    without I/O (time and events in, actions and next deadline out,
    `mesh/core.py`), driven by its asyncio runner on the board
    (`mesh/runner.py`) and by the simulator on the host; its design is
    described in `docs/MESH_DESIGN.md`;
  - `board/` — board support, the only hardware-specific code: T3S3 wiring
    (radio, battery voltage, LED, display bus, SRR receiver's UART; pin
    plan, box wiring and per-box procedure in `docs/hardware.md`), ESP32
    platform of the mesh (NVS, RTC memory, random source), SX1262 radio
    adapter (reception, transmission, carrier sense).
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
- `tools/campaign.py` — verdicts of a sweep or campaign from its records
  and seeds lines: hard rules (`bounds`), and the seed-paired comparison
  with a baseline or record identity (`compare`, `compare --exact`); the
  jobs of a campaign plan with their cost and wall time (`plan`, `make
  campaign-plan`; the standard plan `tools/campaign.txt`), from per-run
  costs (`tools/campaign_times.txt`) merged from records by `times`;
  `tools/campaign.sh` runs a campaign on a rented machine and fetches its
  verdicts (`make campaign`, `make campaign-fetch`; README *Campaigns*).
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
make push-check                # before a push: lint + messages and added lines of the unpushed commits
make simbench NODES=30         # simulator wall clock for 10 simulated minutes
make seeds                     # scenario suites on 500 seed shifts, about 10 min (not in check)
make large                     # large-network invariants, about 7 min (not in check)
make large RUN="lossy star 2"  # one run of it, with its full report
make large RUN="lossy star 2 30 steady"  # the same at another load
make sweep SWEEP="--seeds 1-300 --topologies star --loads steady"  # failure rates
make trace TEST=test_dodag_scenario.DetachRejoin  # traces into build/trace
make trace RUN="forming course 1 14"  # trace of one large run (nominal course)
make firmware                  # build the SILY_T3S3 firmware
make flash PORT=/dev/ttyACM0   # flash it
make deploy PORT=/dev/ttyACM0  # copy src/ to the board and reset it
make box-check PORT=/dev/ttyACM0  # per-box hardware check (docs/hardware.md)
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
run lasts 15 simulated minutes, the last 2.5 without submissions; an
overload run (below) goes on, still without submissions, while a board
holds class 1 messages, for at most 2.5 more (its recovery wants nothing
queued at the end).

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
The reports also give metrics that never fail a run: the false floats, the
detaches of relays and of leaves, the passive acknowledgements and those
overheard after the sender's relay timeout, the duplicates arriving before
the relay's copy went on air, and the class 1 failures split by the state
of their origin.

Loads (the fifth word of `RUN`): `s15` (default), the load above;
`steady`, the same rate without the burst; `idle`, no class 1 traffic.
Only at `s15` is the hidden-root `star` an overload case, judged on its
recovery; at the other loads it is judged like any topology.

### Sweeps

A pass on a few seeds says little about a rare failure. `make sweep`
plays the runs of `tests/large.py sweep-plan` (options in `SWEEP`:
`--seeds A-B`, default 1-100; `--scenarios`, `--topologies`, `--loads`,
comma-separated; `--nodes 30,64`), `SWEEP_JOBS` at a time (default 4),
records each run as one JSON file in `SWEEP_OUT` (default `build/sweep`;
a run recorded there is skipped, so an interrupted sweep resumes; the
report of a failing run beside it), then prints per variant, load,
scenario, topology and size the runs with each indicator (failed, any
violation, silent loss, duplicate, false float, relay detach, the same
without scenario event while connected, failure with
the origin connected, recovery failed, the root alone with messages
queued at the end of an overload run, a class 1 upward attempt to a hop
other than the next hop, each check violated) as k/n with a 95 % Wilson
interval, the totals of the per-run counts, and the commands reproducing the
failing runs. `make sweep-summary` prints that again;
`tests/large.py aggregate --csv DIR...` gives it as CSV.

The scenario suites run on many seeds too: `make seeds` (`tests/seeds.py`)
plays the test classes of the DODAG, routing and class 1 scenario modules
on seed shifts 0 to `SEEDS` - 1 (default 500; shift k adds 1000 k to the
seed of every network, shift 0 is `make test`'s), in chunks of
`SEEDS_CHUNK` shifts, `SEEDS_JOBS` chunks at a time (default 8: about 10
minutes on the development host), each chunk writing one JSON line per
shift and class (verdict, failures, run time) into `build/seeds`
(`SEEDS_OUT`). It then prints the failing shifts per class with their
95 % Wilson interval and each failing (shift, class, test, last message)
with the command reproducing it alone (`make seeds SEEDS=1 SEEDS_FIRST=k
SEEDS_CLASS=MODULE.CLASS SEEDS_JOBS=1`: the shifts in turn, with the
unittest output), and fails with status 1 when a shift failed, 2 when a
shift is missing or a chunk crashed. Its parts serve other job runners:
`tests/seeds.py COUNT --first K --class NAME --json` (a chunk),
`tests/seeds.py plan COUNT CHUNK --first K` (the chunks) and
`tests/seeds.py aggregate --expect FIRST-LAST PATH...` (the verdict).

### Campaigns

A campaign runs the large-network runs, the scenario-suite shifts and the
full TLC tier of one plan together on a rented machine, then judges the
records. The plan is a file of job groups, one per line (`tools/campaign.txt`,
the standard one: every 30-node cell, the 64-node star and the steady and
idle loads of the star on 100 to 300 seeds, shifts 0 to 1999, the full TLC
tier): `large OPTIONS` (the runs of `tests/large.py sweep-plan OPTIONS`),
`seeds --shifts A-B --chunk N [MODULE...] [--class NAME]...`, `spec
[NAMES...]` (none: the full tier).

1. `make campaign-plan` (`CAMPAIGN_PLAN`, `CAMPAIGN_JOBS=32`) prints the
   jobs of each group (a run or shift already in an earlier group is
   dropped), their vCPU-hours and the wall time on that many vCPUs, from
   the per-run costs of the time table `tools/campaign_times.txt`: about
   90 vCPU-hours and a little over 3 hours on a 32-vCPU Linux instance for
   the standard plan, the TLC tier included. Jobs of a cell absent from the
   table are of unknown cost: listed, run first, the totals then lower
   bounds.
2. `make campaign CAMPAIGN_HOST=user@host` (`tools/campaign.sh`) expands
   the plan here into its job list, the longest first; copies `src sim
   tests tools` of this tree (label `current`) and of each tree of
   `CAMPAIGN_TREES="LABEL=DIR ..."` (variants, e.g. other worktrees: each
   doubles the cost) into `~/sily-campaign` there (`CAMPAIGN_DIR`), with
   the MicroPython binary; checks that the remote interpreter gives the
   local `make simbench` digest and the same report of one run; then starts,
   detached (`nohup`: a dropped ssh session does not stop it), the TLC tier
   at normal priority through `spec/remote.sh` (`SPEC_JOBS=8`
   configurations at a time) and, beside it, one pool of every other job
   for every label under `nice -n 10`, `CAMPAIGN_JOBS` at a time, each
   limited to an hour: large runs recorded into `out/LABEL/large`, seeds
   chunks into `out/LABEL/seeds`. A job already recorded there is skipped:
   start again to resume an interrupted campaign or retry failed jobs. A
   second start is refused while one runs, and so is a start onto a
   directory holding a label at another revision.
3. `make campaign-fetch CAMPAIGN_HOST=user@host` tells the progress (pool
   jobs recorded against the list, failed ones, TLC configurations done).
   Once both have ended, it copies the records, the plan and the job list
   into `build/campaign` (`CAMPAIGN_FETCH`) and the TLC output into its
   `spec/` (durations merged into `spec/durations.txt`), lists the failed
   jobs (timeout or crash), merges the run times of the label `current`
   into the time table, packs `records-REV.tar.xz` (records, seeds lines,
   plan, job list), and writes and prints the verdicts, each in its file
   there, `verdicts.txt` summing them up. It exits with the worst status
   (make shows it as `Error N`): 0 all passed; 1 a breach, a difference
   or a TLC outcome not the expected one; 2 a job without record, a
   missing shift or an input problem.
4. Delete the instance (`spec/README.md`).

The verdicts (`tools/campaign.py`, also usable by hand on any records):
`bounds` of each label, the hard rules: a silent class 1 loss, a
violation of MP11, MP9 or MP13, an overload run whose recovery failed
with a board other than the root still holding messages; listed apart,
the recovery failures on the root's queue alone, MP10 outside the overload
runs, each failing seeds (shift, class, test) with its reproduction
command, and the shifts missing from the plan's range. With
`BASELINE=` the records of an earlier campaign (a directory or its
`records-REV.tar.xz`), `compare` of its label `current`
(`BASELINE_LABEL`) with each label of this one, on the cells present on
both sides: runs paired by seed (exact McNemar on each yes/no indicator,
Wilcoxon signed-rank on each count; Fisher and Welch below 20 common
seeds), McNemar per test class on the common seeds shifts,
Benjamini-Hochberg over all, the findings at q < 0.01. A change meant to
keep behaviour (a refactoring) is judged instead by record identity on
the same seeds, `COMPARE=--exact`: the simulator is deterministic, so
every common record must be identical but for its wall time, revision
and label. A variant against this campaign's own `current`:
`tools/campaign.py compare build/campaign/out build/campaign/out --label
current:LABEL`. Keep each campaign's archive: it is the next one's
baseline.

The time table holds per cell the cost of one run in vCPU-seconds: the
mean wall time of the runs measured, times the vCPUs per pool job when
they ran (at most 1). Each fetch merges the cells measured into it
(`CAMPAIGN_TIMES`). The pool runs beside the TLC tier, at a lower
priority: the jobs that ran beside it are counted slower than they are,
a conservative cost. The TLC tier's line is set by hand from a full run.

The host: Ubuntu 24.04 or Debian 13 with rsync and Java 11 or later
(`apt install rsync default-jre-headless`), since the local MicroPython
binary is copied there and needs glibc 2.38 and libffi 8 (the header of
`tools/campaign.sh` tells how to build the port there otherwise); 10 GB
of disk are enough. Here: ssh, rsync, and tar with xz.

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
