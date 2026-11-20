# Hardware

The SILy box: a LilyGo T3S3 V1.2 (ESP32-S3FH4R2: 4 MB flash and 2 MB
PSRAM, both quad SPI; SX1262 radio; SSD1306 display) in a waterproof box,
with an 18650 Li-ion cell, an on/off switch between the cell and the board,
a USB-B panel connector wired to the board's USB-C, a window over the
display, the LoRa antenna, and a SportIdent SRR receiver module on a UART.
The board's buttons are out of reach in the closed box.

Sources: LilyGo's T3S3 SX1262 page (pin map, electrical notes) and the
T3S3 V1.2 schematic in <https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series>
(`docs/en/t3_s3_sx1262/t3_s3_sx1262_hw.md`, `schematic/T3_S3_V1.2.pdf`;
V1.2 and V1.3 have the same pins, per LilyGo); the firmware's pin names
(`firmware/SILY_T3S3/pins.csv`); the wiring of the boxes' earlier LoPy4
version.

## Pin plan

| GPIO | Use | In the code |
|---|---|---|
| 0 | BOOT button (strapping pin) | `Pin.board.BUTTON` |
| 1 | battery voltage through a 100 kΩ / 100 kΩ divider (ADC1) | `Pin.board.VBAT_SENSE` |
| 2, 11, 13, 14 | microSD card, SPI: MISO, MOSI, CS, SCK | `Pin.board.SD_*` |
| 3, 5, 6, 7 | SX1262 SPI: MISO (strapping pin), SCK, MOSI, CS | `Pin.board.LORA_*` |
| 8, 33, 34 | SX1262 reset, DIO1, BUSY | `Pin.board.LORA_*` |
| 17, 18 | SSD1306 display, I2C0: SCL, SDA (address 0x3C) | `Pin.board.OLED_*` |
| 19, 20 | USB D−, D+ (REPL, flashing) | — |
| 37 | user LED, active high | `Pin.board.LED` |
| 43 | SRR receiver: UART1 TX, to the receiver's RX | `board.t3s3.SRR_TX` |
| 44 | SRR receiver: UART1 RX, from the receiver's TX | `board.t3s3.SRR_RX` |

Not to use: 10 and 21 (on the second QWIIC socket, P3, but wired to the
radio module), 12 (on header JP2, also the microSD slot's DAT2), 26–32
(in-package flash and PSRAM), 45 and 46 (strapping pins). Free on the
headers: 15, 16, 35, 36, 38–42, 47, 48.

## SRR receiver

From the existing box wiring: the SRR receiver module runs on the board's
3.3 V and its UART is at 3.3 V logic, 38400 baud 8N1 (the LoPy4 version
powered it from its 3V3 and GND and read it with `UART(1,
baudrate=38400)` on UART1's default pins, P3 TX and P4 RX). SportIdent's
published SRR documents give neither its supply nor its interface level;
its current draw is to confirm. The application only reads; the TX line is
wired too, so that a later command to the receiver needs no reopening of
the box.

UART1 is on GPIO44 (RX) and GPIO43 (TX). These pins:

- are on the QWIIC socket P1 (4 pins, 1.0 mm pitch: 1 GND, 2 3.3 V,
  3 GPIO43, 4 GPIO44), which LilyGo names "Uart1" and marks free: one
  connector carries the receiver's four wires; also on header JP1;
- are neither strapping (0, 3, 45, 46) nor flash or PSRAM pins (26–32),
  nor used on the board: they are UART0's default pins, unused by this
  firmware, whose REPL is on USB.

Caveat: at every reset the ESP32-S3's ROM writes its boot messages on
GPIO43 (UART0 TX, 115200 baud) before the application opens UART1; the
receiver's RX gets them as noise, which does not touch the reading
direction. The alternative without it, on JP1 only: RX GPIO38 (pin 13),
TX GPIO39 (pin 10), set in `board/t3s3.py`.

## Box wiring

| SRR receiver | T3S3: QWIIC socket P1 | or header JP1 |
|---|---|---|
| GND | 1 GND | 2 or 4, GND |
| VCC (3.3 V) | 2 3.3 V (the ESP32's regulator) | 3, 3.3 V (the radio's regulator) |
| TX | 4 GPIO44 (UART1 RX) | 11 GPIO44 (U0RXD) |
| RX | 3 GPIO43 (UART1 TX) | 12 GPIO43 (U0TXD) |

- JP1 pin 1 (+5V) is the board's unregulated input rail (battery voltage
  on battery): not for the receiver.
- Battery: one 18650 Li-ion cell on the board's battery connector (JST PH
  2.0 mm), through the box's switch. The T3S3 has no battery protection
  (LilyGo): use a protected cell. The board charges it from USB (500 mA),
  only while the box's switch is on. The schematic shows a slide switch
  between the board's power path and its regulators: if fitted, leave it
  on.
- USB: the cable from the panel USB-B to the board's USB-C carries data
  (flashing, REPL, check).
- Antenna: the board's SMA connector to the box's 50 Ω bulkhead by a short
  SMA cable (under 10 cm, at most 2 dB loss), or the factory antenna
  directly. Never transmit without an antenna (LilyGo).

## Display

The SSD1306 (128 × 64 pixels, 8 pages of 8 pixel rows) is on I2C0 at
400 kHz, address 0x3C, driven by micropython-lib's `ssd1306` driver, frozen
in the firmware (`board.t3s3.make_oled`). Measured on a T3S3: a whole frame
(the driver's `show`, 1 KB) takes about 25 ms, one page row (128 bytes)
about 3 ms. The application (`src/sily/display.py`) therefore writes one
text line (one page row, 16 characters of the 8×8 font) at a time and only
the lines that changed, the asyncio loop free between two rows. The
controller keeps its image while powered: the application powers it off at
its stop and crash; a power-on starts it off.

## Watchdog

`machine.WDT` is the ESP-IDF task watchdog (`machine.WDT(0, timeout_ms)`;
MicroPython 1.29, ESP-IDF 5.5). Measured on a T3S3: once armed it cannot
be disarmed; it survives a soft reset (nothing feeding it, the board reset
one timeout later, with `machine.reset_cause()` `WDT_RESET`); a
`machine.Timer` callback calling `feed()` keeps it fed during a
`time.sleep_ms` and at the idle REPL (scheduled callbacks run there). A
soft reset runs `boot.py` but, from the raw REPL (`mpremote` without
`resume`, `tools/bench.py`), not `main.py`, and deletes the hardware
timers (their finaliser); a hardware reset (power, `machine.reset`, the
watchdog's own) clears the watchdog. Hardware timers: 0 for the safe
mode's blink (`src/sily/hw.py`), 1 for the watchdog's feed at the REPL
(`src/sily/watchdog.py`, which gives the timeout and the feeds).

## Per-box procedure

With several boards on one host, their ACM ports swap after resets: a
command meant for one board can reach another (a configuration once went
to the wrong board). Address each board by its USB serial number instead:
`mpremote devs` lists the boards connected (device, serial number, USB
identifiers), and `PORT=id:<serial>` works with every target that goes
through mpremote (`deploy`, `box-check`, `configure`, `journal`).
`make flash` goes through esptool, which takes a device path only, and the
board re-enumerates when esptool resets it: flash with that board alone on
USB.

The box closed, its switch on (battery connected), its USB to the host
(`PORT`, e.g. `/dev/ttyACM0`):

1. `make flash PORT=...` (the SILY_T3S3 firmware, `make firmware`): esptool
   resets the board into its ROM bootloader, which re-enumerates on USB as
   303a:1001 (Espressif USB JTAG/serial; under a virtual machine, pass that
   device through too); the board restarts into MicroPython after the
   write. No button is needed, unless the board no longer answers on USB
   (then open the box: BOOT held during reset).
2. `make deploy PORT=...`: copies `src/` to the board and resets it. A
   board without configuration then boots into safe mode (`SILY SAFE file:
   /config.json missing`, the LED blinking fast) and waits at the REPL;
   `make deploy` keeps an existing `/config.json`.
3. `make box-check PORT=...` (`src/sily/boxcheck.py`): one line per item,
   `PASS`, `FAIL` or `OBSERVE` (to judge by eye), then a `SUMMARY` line;
   make fails unless it reads `SUMMARY: PASS`. For `HOLD` seconds (15 by
   default), look through the window: the LED blinks once a second and the
   display shows the test page (version, box name, results); the access
   point `SILy-check-<box>` shows on a phone. The box name, the last three
   bytes of the MAC address, goes on the box's label. A board without
   battery: `BATTERY=0`. On USB without a battery, the charger's output
   reads 4.18–4.24 V, a spread of 67–134 mV over the check's 10 readings
   (4 boards), within both bounds of the battery item: a box declared
   with a battery passes on USB alone, battery missing or switched off,
   and a spread just above 150 mV on USB may be the charger alone. Check the battery with USB unplugged:
   the box then runs on it, and the display's first page or the box's
   status page (after step 4) gives its voltage.

| Item | PASS when |
|---|---|
| firmware | the board name says the SILy firmware: `LILYGO T3-S3 (SILy)` |
| software | key derivation default of 6000 iterations (shared by every box of a network); the SILy version is shown |
| radio | the SX1262 answers after its reset |
| battery | 10 readings over 0.5 s within 3000–4400 mV and within 150 mV of each other: one Li-ion cell, from empty (about 3.0 V, where its protection and the 3.3 V regulator give up) to full (4.2 V, the charger's end voltage), plus a few per cent of reading accuracy at the top. On USB without a battery the charger's output reads instead, and passes: see above |
| uart | the driver reports UART1 at 38400 baud 8N1 on GPIO43 and GPIO44; the line also tells whether a powered receiver drives the RX line (not judged) |
| wifi | the access point is up with an address (192.168.4.1) |
| display | OBSERVE: the test page shows; FAIL if nothing answers at 0x3C |
| led | OBSERVE: the LED blinks |
| cleanup | the board is left as at boot: LED, access point and display off, UART closed, radio asleep |

4. `make configure PORT=... NETWORK=<network file> BOX=<box file>`
   (`tools/box.py`, README *Application*): writes the box's
   `/config.json` (key derived on the host from the network file), clears
   the start guard and resets the board, which prints `SILY <version>` and
   starts the application: the LED lights once the box is attached to the
   mesh (at once on the gateway). With the box file's access point (on in
   the template, its password replaced), a phone sees `<prefix>-<name>`
   and the box's page at http://192.168.4.1/ (README *Application*).
5. After a run, `make journal PORT=... OUT=build/<box>`: stops the
   application, copies the box's journal (`/journal/`) into
   `OUT/journal`, resets the board and prints its summary per boot
   (README *Application*).

For a field measurement of the mesh, set `traffic.enabled` (period, size)
in the network file before step 4 of every box: the nodes send numbered
test messages, and the gateway's summary gives per node the delivery and
the latency above the best case (README *Application*); set it back to
false, and configure every box again, after the measurement.
