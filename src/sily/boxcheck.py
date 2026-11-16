"""Per-box hardware check, run on each box after flashing and deploying.

From the host (docs/hardware.md), or at the board's REPL::

    make box-check PORT=/dev/ttyACM0
    from sily import boxcheck; boxcheck.run()

Prints a header line with the box's name, then one line per item, ``STATUS
ITEM: DETAIL`` with STATUS ``PASS``, ``FAIL`` or ``OBSERVE`` (what only a
person can judge, looking at the box), then one ``SUMMARY`` line, last:
``SUMMARY: PASS`` when no item failed. Items:

- ``firmware``: the SILy firmware is flashed (its board name);
- ``software``: the deployed SILy version and the key derivation count,
  which every box of a network must share (`KDF_ITERATIONS`);
- ``radio``: the SX1262 answers after its reset;
- ``battery``: the battery voltage, judged by `battery_verdict`;
- ``uart``: the SRR receiver's UART opens at 38400 baud 8N1 on its pins, as
  the driver reports them back (`uart_verdict`); the detail tells whether
  something drives the receive line high (a powered receiver does), not
  judged;
- ``wifi``: an open access point ``SILy-check-<box>`` starts, with its
  address; it stays up during the hold (below), to be seen from a phone;
- ``display`` (OBSERVE): the test page (`page_lines`: version, box, the
  results above) shows during the hold; FAIL if the display does not answer
  on I2C;
- ``led`` (OBSERVE): the user LED blinks once a second during the hold;
- ``cleanup``: after the hold of ``hold_s`` seconds, the board is left as at
  boot: LED off, access point off, display off, UART closed, radio asleep.

The box's name is the last three bytes of its MAC address (``unique_id``),
on the page and in the SSID. Hardware modules are imported by `run` only, so
that the verdicts run on the host.
"""

import sys
from time import sleep_ms

import sily

PASS = "PASS"
FAIL = "FAIL"
OBSERVE = "OBSERVE"

# Network-wide PBKDF2 count (mesh.kdf.DEFAULT_ITERATIONS, docs/measurements.md).
KDF_ITERATIONS = 6000

# One Li-ion cell: about 3.0 V empty (its protection and the board's 3.3 V
# regulator give up around there) to 4.2 V full (the charger's end voltage),
# widened at the top by the reading's accuracy (a few per cent).
BATTERY_MIN_MV = 3000
BATTERY_MAX_MV = 4400
# Largest spread of the readings of a battery, each the mean of several ADC
# reads; the charger's output without a battery moves by more.
BATTERY_SPREAD_MV = 150
BATTERY_READINGS = 10
BATTERY_GAP_MS = 50

UART_TOLERANCE = 0.01  # baud rate reported by the driver vs requested

PAGE_COLUMNS = 16  # 8 x 8 pixel font on 128 x 64 pixels
PAGE_ROWS = 8
SSID_PREFIX = "SILy-check-"


def box_name(mac):
    """Return the box's name: the last three bytes of mac, upper-case hex."""
    return "".join("%02X" % b for b in mac[-3:])


def battery_verdict(readings, battery=True):
    """Return (status, detail) of battery readings (mV) taken some time apart.

    battery: a battery is connected and switched on. Then PASS when every
    reading lies within BATTERY_MIN_MV..BATTERY_MAX_MV and they spread by at
    most BATTERY_SPREAD_MV, FAIL otherwise, with the likely causes. Without
    one (a board on USB alone), the reading is the charger's output: reported,
    not judged (OBSERVE).
    """
    n = len(readings)
    low = min(readings)
    high = max(readings)
    detail = "%d mV (spread %d mV)" % ((sum(readings) + n // 2) // n, high - low)
    if not battery:
        return OBSERVE, detail + ": no battery declared, not judged"
    if low < BATTERY_MIN_MV or high > BATTERY_MAX_MV:
        return FAIL, detail + (
            ": outside %d-%d mV (one Li-ion cell): battery switched off or not "
            "connected? (on USB alone this reads the charger's output) Otherwise "
            "a flat battery or the sense divider" % (BATTERY_MIN_MV, BATTERY_MAX_MV)
        )
    if high - low > BATTERY_SPREAD_MV:
        return FAIL, detail + (
            ": unsteady beyond %d mV: battery not connected? (the charger's "
            "output moves without one)" % BATTERY_SPREAD_MV
        )
    return PASS, detail


def uart_verdict(text, uart_id, tx, rx, baudrate):
    """Return (status, detail) of an ESP32 machine.UART's repr, text.

    PASS when the driver reports UART uart_id at baudrate (within
    UART_TOLERANCE), 8 bits, no parity, 1 stop bit, on GPIOs tx and rx.
    """
    items = text[text.find("(") + 1 : text.rfind(")")].split(", ")
    fields = {"id": items[0]}
    for item in items[1:]:
        pair = item.split("=", 1)
        if len(pair) == 2:
            fields[pair[0]] = pair[1]
    try:
        actual = int(fields.get("baudrate", ""))
    except ValueError:
        actual = 0
    expected = (
        ("id", str(uart_id)),
        ("bits", "8"),
        ("parity", "None"),
        ("stop", "1"),
        ("tx", str(tx)),
        ("rx", str(rx)),
    )
    wrong = ["%s=%s" % (key, fields.get(key)) for key, want in expected if fields.get(key) != want]
    if abs(actual - baudrate) > baudrate * UART_TOLERANCE:
        wrong.append("baudrate=%d" % actual)
    if wrong:
        return FAIL, "driver reports %s; expected UART%d %d baud 8N1, TX GPIO%d, RX GPIO%d" % (
            ", ".join(wrong),
            uart_id,
            baudrate,
            tx,
            rx,
        )
    return PASS, "UART%d %d baud 8N1, TX GPIO%d, RX GPIO%d" % (uart_id, actual, tx, rx)


def page_lines(version, box, results):
    """Return the test page: at most PAGE_ROWS lines of PAGE_COLUMNS characters.

    The version, the box's name, then one line per (name, status, detail) of
    results, the name cut so that the status stays whole.
    """
    lines = [("SILy " + version)[:PAGE_COLUMNS], ("box " + box)[:PAGE_COLUMNS]]
    for name, status, _ in results:
        lines.append(name[: max(0, PAGE_COLUMNS - len(status) - 1)] + " " + status)
    return lines[:PAGE_ROWS]


def summary(results):
    """Return the SUMMARY line of results, (name, status, detail) tuples."""
    failed = [name for name, status, _ in results if status == FAIL]
    observe = [name for name, status, _ in results if status == OBSERVE]
    if failed:
        line = "SUMMARY: FAIL %s" % ", ".join(failed)
    else:
        line = "SUMMARY: PASS, %d items" % sum(1 for r in results if r[1] == PASS)
    if observe:
        line += "; by eye: " + ", ".join(observe)
    return line


def _item(results, name, check, *args):
    try:
        status, detail = check(*args)
    except Exception as e:  # the item fails, the check goes on
        status, detail = FAIL, "%s: %s" % (type(e).__name__, e)
    results.append((name, status, detail))
    print("%-7s %s: %s" % (status, name, detail))


def _firmware():
    impl = sys.implementation
    detail = "%s, MicroPython %s" % (impl._machine, ".".join(str(v) for v in impl.version[:3]))
    if "(SILy)" in impl._machine:
        return PASS, detail
    return FAIL, detail + ": not the SILy firmware (make flash)"


def _software():
    from mesh import kdf

    detail = "SILy %s, key derivation %d iterations" % (sily.__version__, kdf.DEFAULT_ITERATIONS)
    if kdf.DEFAULT_ITERATIONS == KDF_ITERATIONS:
        return PASS, detail
    return FAIL, detail + ", expected %d" % KDF_ITERATIONS


def _radio(state):
    from board import t3s3

    state["modem"] = t3s3.make_modem()  # raises if the SX1262 does not answer
    return PASS, "SX1262 answered after its reset"


def _battery(battery):
    from board import t3s3

    readings = []
    for i in range(BATTERY_READINGS):
        if i:
            sleep_ms(BATTERY_GAP_MS)
        readings.append(t3s3.battery_millivolts())
    return battery_verdict(readings, battery)


def _uart(state):
    from machine import Pin

    from board import t3s3

    # Idle UART lines are high: with a pull-down, a high RX line is driven.
    probe = Pin(t3s3.SRR_RX, Pin.IN, Pin.PULL_DOWN)
    sleep_ms(2)
    driven = probe.value()
    probe.init(Pin.IN, pull=None)
    uart = state["uart"] = t3s3.make_srr_uart()
    status, detail = uart_verdict(
        repr(uart), t3s3.SRR_UART, t3s3.SRR_TX, t3s3.SRR_RX, t3s3.SRR_BAUDRATE
    )
    if driven:
        return status, detail + "; RX line driven high: a receiver is connected and powered"
    return status, detail + "; RX line not driven: no receiver connected, or not powered"


def _wifi(state, box):
    import network

    ap = state["ap"] = network.WLAN(network.WLAN.IF_AP)
    ssid = SSID_PREFIX + box
    ap.config(ssid=ssid, security=network.WLAN.SEC_OPEN)
    ap.active(True)
    for _ in range(50):
        if ap.active():
            break
        sleep_ms(100)
    address = ap.ipconfig("addr4")[0]
    if ap.active() and address != "0.0.0.0":
        return PASS, "access point %s, address %s" % (ssid, address)
    return FAIL, "access point %s not started (active %s, address %s)" % (
        ssid,
        ap.active(),
        address,
    )


def _display(state, box, results, hold_s):
    import ssd1306

    from board import t3s3

    i2c = t3s3.make_i2c()
    if t3s3.OLED_ADDRESS not in i2c.scan():
        return FAIL, "no device at I2C address 0x%02X" % t3s3.OLED_ADDRESS
    oled = ssd1306.SSD1306_I2C(t3s3.OLED_WIDTH, t3s3.OLED_HEIGHT, i2c, addr=t3s3.OLED_ADDRESS)
    state["oled"] = oled
    oled.fill(0)
    for row, line in enumerate(page_lines(sily.__version__, box, results)):
        oled.text(line, 0, 8 * row)
    oled.show()
    return OBSERVE, "the test page shows box %s and the results above for %d s" % (box, hold_s)


def _led(state, hold_s):
    from board import t3s3

    state["led"] = t3s3.make_led()
    return OBSERVE, "the LED blinks once a second for %d s" % hold_s


def _hold(state, hold_s):
    led = state.get("led")
    for i in range(2 * hold_s):
        if led is not None:
            led.value(1 - i % 2)
        sleep_ms(500)


def _clean(state):
    steps = (
        ("led", "LED off", lambda led: led.value(0)),
        ("ap", "access point off", lambda ap: ap.active(False)),
        ("oled", "display off", lambda oled: oled.poweroff()),
        ("uart", "UART closed", lambda uart: uart.deinit()),
        ("modem", "radio asleep", lambda modem: modem.sleep()),
    )
    done = []
    failed = []
    for key, what, action in steps:
        if key not in state:
            continue
        try:
            action(state[key])
            done.append(what)
        except Exception as e:  # the other steps still run
            failed.append("%s: %s: %s" % (what, type(e).__name__, e))
    if failed:
        return FAIL, "; ".join(failed)
    return PASS, ", ".join(done) if done else "nothing to restore"


def run(battery=True, hold_s=15):
    """Check the box and print the results; return the number of failed items.

    battery: a battery is connected and switched on (False: a board on USB
    alone, its reading not judged). hold_s: seconds of LED blinking, test page
    and access point before the cleanup.
    """
    import machine

    mac = machine.unique_id()
    box = box_name(mac)
    print("SILy box check: box %s, MAC %s" % (box, ":".join("%02X" % b for b in mac)))
    results = []
    state = {}
    try:
        _item(results, "firmware", _firmware)
        _item(results, "software", _software)
        _item(results, "radio", _radio, state)
        _item(results, "battery", _battery, battery)
        _item(results, "uart", _uart, state)
        _item(results, "wifi", _wifi, state, box)
        _item(results, "display", _display, state, box, list(results), hold_s)
        _item(results, "led", _led, state, hold_s)
        print("Holding %d s: LED, display, access point" % hold_s)
        _hold(state, hold_s)
    finally:
        _item(results, "cleanup", _clean, state)
        print(summary(results))
    return sum(1 for r in results if r[1] == FAIL)
