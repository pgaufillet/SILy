"""LoRa time on air against the airtime budget (MESH_DESIGN §4, §16)."""

import unittest

from mesh.airtime import (
    low_data_rate,
    snr_limit_x10,
    symbols_x4,
    time_on_air_ms,
    time_on_air_us,
)

# MESH_DESIGN §16 "Time on air (ms)", output of the budget script (BW 125 kHz,
# CR 4/5, preamble 8, explicit header, CRC on), rounded to the nearest ms.
_TABLE = {
    23: (62, 113, 206, 371, 823, 1483),  # beacon
    17: (51, 93, 165, 330, 659, 1319),  # presence x1
    197: (313, 553, 984, 1804, 3936, 7217),  # presence x30
    27: (67, 123, 226, 412, 823, 1647),  # punch x1
    255: (400, 707, 1250, 2296, 5001, 9019),  # punch x17, maximum frame
}

# Exact values (µs) of the budget script's formula for SF7..SF12.
_EXACT_US = {
    17: (51456, 92672, 164864, 329728, 659456, 1318912),
    20: (56576, 102912, 185344, 370688, 741376, 1318912),
    255: (399616, 707072, 1250304, 2295808, 5001216, 9019392),
}


class BudgetFigures(unittest.TestCase):
    def test_twenty_bytes(self):
        # MESH_DESIGN §4: 20 B at 125 kHz, CR 4/5, preamble 8: 57 / 371 / 1319 ms.
        for sf, ms in ((7, 57), (10, 371), (12, 1319)):
            self.assertEqual((time_on_air_us(20, sf) + 500) // 1000, ms)

    def test_section_16_table(self):
        for length, row in _TABLE.items():
            for i, ms in enumerate(row):
                us = time_on_air_us(length, 7 + i)
                self.assertEqual((us + 500) // 1000, ms, "%d B SF%d" % (length, 7 + i))

    def test_exact_microseconds(self):
        for length, row in _EXACT_US.items():
            for i, us in enumerate(row):
                self.assertEqual(time_on_air_us(length, 7 + i), us)

    def test_ms_rounded_up_for_accounting(self):
        for length in (0, 1, 17, 20, 23, 27, 100, 197, 254, 255):
            for sf in range(7, 13):
                us = time_on_air_us(length, sf)
                ms = time_on_air_ms(length, sf)
                self.assertTrue(ms * 1000 >= us > (ms - 1) * 1000, "%d B SF%d" % (length, sf))
        self.assertEqual(time_on_air_ms(17, 7), 52)  # 51.456 ms

    def test_longer_frames_take_longer(self):
        for sf in range(7, 13):
            previous = 0
            for length in range(256):
                us = time_on_air_us(length, sf)
                self.assertTrue(us >= previous)
                previous = us
            self.assertTrue(time_on_air_us(255, sf) > time_on_air_us(0, sf))


class Parameters(unittest.TestCase):
    def test_low_data_rate_optimisation(self):
        # On when a symbol lasts 16 ms or more: SF11-12 at 125 kHz, SF12 at 250 kHz.
        self.assertFalse(low_data_rate(10, 125000))
        self.assertTrue(low_data_rate(11, 125000))
        self.assertTrue(low_data_rate(12, 125000))
        self.assertFalse(low_data_rate(11, 250000))
        self.assertTrue(low_data_rate(12, 250000))
        self.assertTrue(low_data_rate(7, 7800))
        self.assertFalse(low_data_rate(12, 500000))

    def test_custom_bandwidth(self):
        # Budget script at bw=7800 Hz: 255 B at SF12 lasts 144541.54 ms.
        self.assertEqual(time_on_air_ms(255, 12, bandwidth_hz=7800), 144542)
        self.assertEqual(time_on_air_us(20, 7, bandwidth_hz=250000), 56576 // 2)

    def test_crc_header_preamble_coding_rate(self):
        # 17 B at SF7 (152 payload bits, 6 blocks): 50.25 symbols of 1024 µs.
        self.assertEqual(time_on_air_us(17, 7), 51456)
        # Without CRC or header: 136 or 132 bits, 5 blocks: 45.25 symbols.
        self.assertEqual(time_on_air_us(17, 7, crc=False), 46336)
        self.assertEqual(time_on_air_us(17, 7, header=False), 46336)
        # 4 more preamble symbols.
        self.assertEqual(time_on_air_us(17, 7, preamble=12), 55552)
        # CR 4/8: 6 blocks of 8 symbols.
        self.assertEqual(time_on_air_us(17, 7, coding_rate=8), 69888)
        self.assertEqual(symbols_x4(17, 7), 201)

    def test_empty_payload_still_has_symbols(self):
        # 0 B at SF12: 8 - 48 + 16 + 20 bits < 0: no payload block, 8 symbols.
        self.assertEqual(symbols_x4(0, 12), 17 + 4 * (8 + 8))
        # 1 B at SF12: 4 bits, one block (budget script: 827392 µs).
        self.assertEqual(time_on_air_us(1, 12), 827392)
        # 0 B at SF7: 16 bits, one block (budget script: 25856 µs).
        self.assertEqual(time_on_air_us(0, 7), 25856)
        # 0 B at SF12 without header nor CRC: -40 bits, still no block.
        self.assertEqual(symbols_x4(0, 12, crc=False, header=False), 17 + 4 * 16)

    def test_out_of_range(self):
        self.assertRaises(ValueError, time_on_air_ms, 20, 6)
        self.assertRaises(ValueError, time_on_air_ms, 20, 13)
        self.assertRaises(ValueError, time_on_air_ms, 256, 7)
        self.assertRaises(ValueError, time_on_air_ms, -1, 7)
        self.assertRaises(ValueError, time_on_air_ms, 20, 7, 125001)
        self.assertRaises(ValueError, time_on_air_ms, 20, 7, 125000, 9)


class Limits(unittest.TestCase):
    def test_demodulation_limits(self):
        self.assertEqual(
            [snr_limit_x10(sf) for sf in range(7, 13)], [-75, -100, -125, -150, -175, -200]
        )
        self.assertRaises(ValueError, snr_limit_x10, 13)
