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
    `mesh/core.py`), driven by an asyncio runner on the board and by the
    simulator on the host;
  - `board/` — board support, the only hardware-specific code.
- `sim/` — host-only mesh simulator, not deployed: deterministic random
  generator, discrete-event scheduler, platform fakes, scripted radio medium
  (loss, asymmetry, partitions, collisions, half duplex, carrier sense) and
  the host runner of mesh cores. Imports `mesh` only; goes with it if the mesh
  is extracted.
- `tests/` — host tests (`run.py` runner, `test_*.py` modules).
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

## References

- SportIdent SRR: <https://www.sportident.com>.
- MeOS: <http://www.melin.nu/meos/en/>.
- LilyGo T3S3: <https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series>.
- MicroPython: <https://micropython.org>; LoRa drivers:
  <https://github.com/micropython/micropython-lib/tree/master/micropython/lora>.

## License

GPL-3.0, see `LICENSE`.
