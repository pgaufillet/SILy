"""Regional profiles and radio configuration validation (MESH_DESIGN §4)."""

import unittest

from mesh import profile
from mesh.dutycycle import DutyCycle
from mesh.profile import (
    ANTENNA_GAIN,
    BANDWIDTH,
    CODING_RATE,
    CUSTOM,
    DUTY_CYCLE,
    EU_869,
    FREQUENCY,
    POWER_LIMIT,
    SF,
    TX_POWER,
    validate,
)


def _custom(frequency_hz=868100000, bandwidth_hz=125000, coding_rate=5, duty_cycle_bp=100):
    return profile.custom(frequency_hz, bandwidth_hz, coding_rate, duty_cycle_bp)


class EuProfile(unittest.TestCase):
    def setUp(self):
        self.eu = profile.get(EU_869)

    def test_channel_and_rules(self):
        eu = self.eu
        self.assertEqual(eu.ident, EU_869)
        self.assertEqual(eu.frequency_hz, 869525000)
        self.assertEqual(eu.bandwidth_hz, 125000)
        self.assertEqual(eu.coding_rate, 5)
        self.assertEqual(eu.duty_cycle_bp, 1000)
        self.assertEqual(eu.window_ms(), 3600000)
        self.assertFalse(eu.custom)

    def test_budget_is_90_percent_of_10_percent(self):
        # §4: 324 s of transmission per hour per board.
        self.assertEqual(self.eu.budget_ms(), 324000)

    def test_every_sf_at_full_power_with_dipole(self):
        for sf in range(7, 13):
            self.assertEqual(validate(self.eu, sf, 22, 2.15), [])
        self.assertEqual(validate(self.eu, 9, -9), [])

    def test_sf_outside_range(self):
        for sf in (6, 13, 0, 9.0, None):
            self.assertEqual(validate(self.eu, sf, 14), [SF])

    def test_tx_power_outside_radio(self):
        for power in (23, -10, 14.5, "14"):
            self.assertEqual(validate(self.eu, 9, power), [TX_POWER])

    def test_erp_limit_with_antenna_gain(self):
        # 500 mW ERP (26.99 dBm) = TX power + gain - 2.15 dB: 22 dBm up to
        # about 7 dBi (§4).
        self.assertEqual(validate(self.eu, 9, 22, 7), [])
        self.assertEqual(validate(self.eu, 9, 22, 7.14), [])
        self.assertEqual(validate(self.eu, 9, 22, 7.15), [POWER_LIMIT])
        self.assertEqual(validate(self.eu, 9, 22, 12), [POWER_LIMIT])
        # A lower TX power makes room for a larger antenna.
        self.assertEqual(validate(self.eu, 9, 17, 12), [])
        self.assertEqual(validate(self.eu, 9, 18, 12), [POWER_LIMIT])

    def test_bad_antenna_gain(self):
        self.assertEqual(validate(self.eu, 9, 14, "high"), [ANTENNA_GAIN])
        self.assertEqual(validate(self.eu, 9, 14, float("nan")), [ANTENNA_GAIN])

    def test_problems_accumulate(self):
        self.assertEqual(validate(self.eu, 5, 30, 3), [SF, TX_POWER])
        self.assertEqual(validate(self.eu, 5, 22, 10), [SF, POWER_LIMIT])

    def test_eirp_profile(self):
        # Same power figure as EIRP: no 2.15 dB credit for the dipole.
        eirp = profile.Profile(2, "test", 915000000, 125000, 5, 2000, profile.EIRP, 10000)
        self.assertEqual(validate(eirp, 9, 20, 0), [])
        self.assertEqual(validate(eirp, 9, 20, 0.01), [POWER_LIMIT])

    def test_lookup(self):
        self.assertTrue(profile.get(EU_869) is self.eu)
        self.assertEqual(profile.get(0x42), None)
        self.assertEqual(profile.get(CUSTOM), None)


class CustomProfile(unittest.TestCase):
    def test_flagged_and_identified(self):
        p = _custom()
        self.assertTrue(p.custom)
        self.assertEqual(p.ident, 0xFF)

    def test_operator_values_within_radio_accepted(self):
        for bw in (7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000):
            for cr in (5, 6, 7, 8):
                self.assertEqual(validate(_custom(bandwidth_hz=bw, coding_rate=cr), 12, 22), [])
        self.assertEqual(validate(_custom(frequency_hz=433175000), 7, -9), [])
        self.assertEqual(validate(_custom(duty_cycle_bp=10000), 7, 0), [])

    def test_power_left_to_the_operator(self):
        # No regional power limit: only the radio's range.
        self.assertEqual(validate(_custom(), 9, 22, 12), [])
        self.assertEqual(validate(_custom(), 9, 23, 0), [TX_POWER])

    def test_values_outside_radio_refused(self):
        self.assertEqual(validate(_custom(frequency_hz=2400000000), 9, 14), [FREQUENCY])
        self.assertEqual(validate(_custom(frequency_hz=100000000), 9, 14), [FREQUENCY])
        self.assertEqual(validate(_custom(bandwidth_hz=250000), 9, 14), [BANDWIDTH])
        self.assertEqual(validate(_custom(bandwidth_hz=100000), 9, 14), [BANDWIDTH])
        self.assertEqual(validate(_custom(bandwidth_hz=125000.0), 9, 14), [BANDWIDTH])
        self.assertEqual(validate(_custom(coding_rate=4), 9, 14), [CODING_RATE])
        self.assertEqual(validate(_custom(coding_rate=9), 9, 14), [CODING_RATE])
        self.assertEqual(validate(_custom(duty_cycle_bp=0), 9, 14), [DUTY_CYCLE])
        self.assertEqual(validate(_custom(duty_cycle_bp=10001), 9, 14), [DUTY_CYCLE])
        bad = _custom(frequency_hz=0, bandwidth_hz=0, coding_rate=0, duty_cycle_bp=0)
        self.assertEqual(
            validate(bad, 6, 99), [SF, TX_POWER, FREQUENCY, BANDWIDTH, CODING_RATE, DUTY_CYCLE]
        )

    def test_operator_duty_cycle_drives_the_budget(self):
        # 2.5 % entered: 90 % of 90 s per hour.
        p = _custom(duty_cycle_bp=250)
        self.assertEqual(p.budget_ms(), 81000)
        window = DutyCycle(p.budget_ms(), p.window_ms())
        self.assertEqual(window.allowance(1), 81000)
        self.assertEqual(window.allowance(3), 48600)
