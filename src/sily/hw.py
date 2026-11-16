"""The board's hardware, as the application and the bench take it (board only).

A thin bundle over ``board.t3s3``, ``board.platform`` and
``board.sx1262``: the application (``sily.app``) receives this module as its
``hw`` argument, and the host tests a fake with the same names:

- `adapter(lora_cfg)`: the SX1262 reset, configured, wrapped in its adapter;
- `storage(namespace)`, `retained()`, `random()`: the mesh platform (NVS
  namespace ``mesh`` for the frame counter; ``sily`` for the start guard);
- `mac()`: the 6-byte MAC address (node information);
- `led()`: the user LED (one Pin, made once);
- `display()`: the OLED display (``ssd1306`` driver, cleared and on), None
  when no device answers on its I2C bus;
- `power_on()`: this boot follows a power-on (a brownout reads as one);
- `reset_cause()`: the cause of this boot, a word (`RESET_CAUSES`);
- `battery_mv()`: the battery voltage in mV (``board.t3s3``);
- `access_point(ssid, password)`: the WiFi access point started (WPA2),
  an `AccessPoint` (its address, the stations joined, off);
- `reset()`, `sleep_ms(ms)`, `kbd_intr(char)`: ``machine.reset``,
  ``time.sleep_ms``, ``micropython.kbd_intr``;
- `blink(period_ms)`: the LED toggled by a hardware timer every period_ms,
  until the next reset (safe mode: it goes on at the REPL).

From the REPL or a host tool: `skip_next_boot()` (the next boot returns to
the REPL at once, ``tools/bench.py`` before a hard reset) and
`clear_guard()` (crash count and skip flag cleared, ``tools/box.py
config``).
"""

import time

import machine
import micropython

from board import t3s3
from board.platform import HardwareRandom, NvsStorage, RtcRetained
from board.sx1262 import Sx1262
from sily.guard import NAMESPACE, Guard

BLINK_TIMER = 0  # hardware timer of the safe mode's blink

RESET_CAUSES = {
    machine.PWRON_RESET: "power",
    machine.HARD_RESET: "hard",
    machine.WDT_RESET: "watchdog",
    machine.DEEPSLEEP_RESET: "deepsleep",
    machine.SOFT_RESET: "soft",
}

_led = None
_blinker = None

sleep_ms = time.sleep_ms
kbd_intr = micropython.kbd_intr


def adapter(lora_cfg):
    """Return the SX1262 adapter configured by lora_cfg (RuntimeError: no answer)."""
    return Sx1262(t3s3.make_modem(lora_cfg))


def storage(namespace="mesh"):
    return NvsStorage(namespace)


def retained():
    return RtcRetained()


def random():
    return HardwareRandom()


def mac():
    return machine.unique_id()


def led():
    """Return the user LED (off when first made)."""
    global _led
    if _led is None:
        _led = t3s3.make_led()
    return _led


def display():
    """Return the OLED display, None if absent (``board.t3s3.make_oled``)."""
    return t3s3.make_oled()


def power_on():
    return machine.reset_cause() == machine.PWRON_RESET


def reset_cause():
    cause = machine.reset_cause()
    return RESET_CAUSES.get(cause, str(cause))


def battery_mv():
    return t3s3.battery_millivolts()


def reset():
    machine.reset()


class AccessPoint:
    """The WiFi access point as the application takes it (``network.WLAN``)."""

    def __init__(self, wlan):
        self.wlan = wlan

    def address(self):
        """Return its IPv4 address, ``0.0.0.0`` when it has none."""
        return self.wlan.ipconfig("addr4")[0]

    def stations(self):
        """Return the number of stations joined."""
        return len(self.wlan.status("stations"))

    def off(self):
        self.wlan.active(False)


def access_point(ssid, password):
    """Start the access point ssid, WPA2 with password; return its `AccessPoint`.

    Activated first, then configured (the order measured on the board).
    """
    import network

    wlan = network.WLAN(network.WLAN.IF_AP)
    wlan.active(True)
    wlan.config(ssid=ssid, key=password, security=network.WLAN.SEC_WPA2)
    return AccessPoint(wlan)


def blink(period_ms):
    """Toggle the LED every period_ms from a hardware timer, until a reset."""
    global _blinker
    pin = led()
    state = [0]

    def toggle(timer):
        state[0] ^= 1
        pin.value(state[0])

    _blinker = machine.Timer(BLINK_TIMER)
    _blinker.init(mode=machine.Timer.PERIODIC, period=period_ms, callback=toggle)


def skip_next_boot():
    """Make the next boot return to the REPL without starting the application."""
    Guard(storage(NAMESPACE)).skip_next()


def clear_guard():
    """Clear the crash count and the skip flag."""
    Guard(storage(NAMESPACE)).clear()
