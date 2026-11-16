"""Verdicts of the per-box hardware check (sily.boxcheck), without hardware."""

import unittest

from sily.boxcheck import (
    BATTERY_MAX_MV,
    BATTERY_MIN_MV,
    BATTERY_SPREAD_MV,
    FAIL,
    OBSERVE,
    PAGE_COLUMNS,
    PAGE_ROWS,
    PASS,
    battery_verdict,
    box_name,
    page_lines,
    summary,
    uart_verdict,
)

# repr of an ESP32 machine.UART (format of ports/esp32/machine_uart.c).
_UART = (
    "UART(1, baudrate=%d, bits=8, parity=%s, stop=1, tx=%d, rx=%d, rts=-1, cts=-1, "
    "txbuf=256, rxbuf=256, timeout=0, timeout_char=0, irq=0)"
)


def _uart(baudrate=38400, parity="None", tx=43, rx=44):
    return _UART % (baudrate, parity, tx, rx)


class Battery(unittest.TestCase):
    def test_steady_cell_passes_with_its_mean(self):
        status, detail = battery_verdict([3900, 3910, 3920, 3905])
        self.assertEqual(status, PASS)
        self.assertTrue(detail.startswith("3909 mV (spread 20 mV)"), detail)

    def test_range_bounds_are_inclusive(self):
        self.assertEqual(battery_verdict([BATTERY_MIN_MV] * 3)[0], PASS)
        self.assertEqual(battery_verdict([BATTERY_MAX_MV] * 3)[0], PASS)

    def test_below_range_fails_naming_the_switch(self):
        status, detail = battery_verdict([BATTERY_MIN_MV - 1, 3050])
        self.assertEqual(status, FAIL)
        self.assertIn("switched off", detail)

    def test_above_range_fails(self):
        self.assertEqual(battery_verdict([4300, BATTERY_MAX_MV + 1])[0], FAIL)

    def test_unsteady_reading_fails_within_range(self):
        status, detail = battery_verdict([3600, 3600 + BATTERY_SPREAD_MV + 1])
        self.assertEqual(status, FAIL)
        self.assertIn("unsteady", detail)
        self.assertEqual(battery_verdict([3600, 3600 + BATTERY_SPREAD_MV])[0], PASS)

    def test_no_battery_declared_is_reported_not_judged(self):
        status, detail = battery_verdict([4100, 3300, 4200], battery=False)
        self.assertEqual(status, OBSERVE)
        self.assertTrue(detail.startswith("3867 mV (spread 900 mV)"), detail)


class Uart(unittest.TestCase):
    def test_expected_settings_pass(self):
        status, detail = uart_verdict(_uart(), 1, 43, 44, 38400)
        self.assertEqual(status, PASS)
        self.assertEqual(detail, "UART1 38400 baud 8N1, TX GPIO43, RX GPIO44")

    def test_baud_rate_within_tolerance(self):
        self.assertEqual(uart_verdict(_uart(38461), 1, 43, 44, 38400)[0], PASS)
        self.assertEqual(uart_verdict(_uart(38900), 1, 43, 44, 38400)[0], FAIL)
        self.assertEqual(uart_verdict(_uart(115200), 1, 43, 44, 38400)[0], FAIL)

    def test_wrong_pins_parity_or_id_fail(self):
        status, detail = uart_verdict(_uart(rx=38), 1, 43, 44, 38400)
        self.assertEqual(status, FAIL)
        self.assertIn("rx=38", detail)
        self.assertEqual(uart_verdict(_uart(parity="0"), 1, 43, 44, 38400)[0], FAIL)
        self.assertEqual(uart_verdict(_uart().replace("(1,", "(2,"), 1, 43, 44, 38400)[0], FAIL)

    def test_unreadable_text_fails(self):
        self.assertEqual(uart_verdict("<UART>", 1, 43, 44, 38400)[0], FAIL)


class Page(unittest.TestCase):
    def test_version_box_then_results(self):
        lines = page_lines("0.2.0", "A1B2C3", [("radio", PASS, "x"), ("uart", FAIL, "y")])
        self.assertEqual(lines, ["SILy 0.2.0", "box A1B2C3", "radio PASS", "uart FAIL"])

    def test_long_name_keeps_the_status_whole(self):
        lines = page_lines("0.2.0", "A1B2C3", [("a-very-long-item-name", OBSERVE, "")])
        self.assertEqual(len(lines[2]), PAGE_COLUMNS)
        self.assertTrue(lines[2].endswith(" OBSERVE"), lines[2])

    def test_page_fits_the_display(self):
        results = [("item%d" % i, PASS, "") for i in range(12)]
        lines = page_lines("0.2.0.dev0-with-a-long-suffix", "A1B2C3", results)
        self.assertEqual(len(lines), PAGE_ROWS)
        for line in lines:
            self.assertTrue(len(line) <= PAGE_COLUMNS, line)


class Summary(unittest.TestCase):
    def test_pass_counts_items_and_lists_what_to_look_at(self):
        results = [("radio", PASS, ""), ("uart", PASS, ""), ("led", OBSERVE, "")]
        self.assertEqual(summary(results), "SUMMARY: PASS, 2 items; by eye: led")

    def test_any_failure_fails_and_is_named(self):
        results = [("radio", FAIL, ""), ("uart", PASS, ""), ("wifi", FAIL, "")]
        self.assertEqual(summary(results), "SUMMARY: FAIL radio, wifi")


class BoxName(unittest.TestCase):
    def test_last_three_bytes_in_upper_case_hex(self):
        self.assertEqual(box_name(b"\x48\xca\x43\x0a\xb2\xc3"), "0AB2C3")
