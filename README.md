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
    (`mesh/runner.py`) and by the simulator on the host;
  - `board/` — board support, the only hardware-specific code: T3S3 wiring,
    ESP32 platform of the mesh (NVS, RTC memory, random source), SX1262
    radio adapter (reception, transmission, carrier sense).
- `sim/` — host-only mesh simulator, not deployed: deterministic random
  generator, discrete-event scheduler, platform fakes, scripted radio medium
  (loss, asymmetry, partitions, collisions, half duplex, carrier sense), the
  host runner of mesh cores, and networks of link engines (`sim/net.py`:
  boards with reset and power loss, sniffer for replays). Imports `mesh`
  only; goes with it if the mesh is extracted.
- `tests/` — host tests (`run.py` runner, `test_*.py` modules).
- `tools/bench.py` — two-board bench measurements, run from the host over
  the serial ports; results in `docs/measurements.md`.
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
make firmware                  # build the SILY_T3S3 firmware
make flash PORT=/dev/ttyACM0   # flash it
make deploy PORT=/dev/ttyACM0  # copy src/ to the board and reset it
```

Host tests use micropython-lib's `unittest` from the MicroPython tree (through
`MICROPYPATH`, nothing installed). `make test` fails if a test fails, a test
module cannot be imported, or no test runs.

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
