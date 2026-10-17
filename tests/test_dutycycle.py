"""Duty-cycle accounting and port priorities (MESH_DESIGN §6, §11, R16)."""

import unittest

from mesh import ports
from mesh.dutycycle import DutyCycle, retained_size
from mesh.ports import PRIORITY_LOW, PRIORITY_PERIODIC, PRIORITY_REPAIR, priority
from sim.platform import FakePlatform, FakeRetained
from sim.prng import XorShift32

_MIN = 60000
_HOUR = 3600000
_BUDGET = 324000  # EU profile
_P1 = 324000
_P2 = 259200
_P3 = 194400


def _filled(used, now=0, **options):
    """A window holding used ms charged at now."""
    window = DutyCycle(_BUDGET, **options)
    window.start(now, used)
    window.done(now, 0)
    return window


class PortPriority(unittest.TestCase):
    def test_mesh_ports(self):
        self.assertEqual(priority(ports.PRESENCE), PRIORITY_PERIODIC)
        self.assertEqual(priority(ports.TOPOLOGY), PRIORITY_LOW)
        self.assertEqual(priority(ports.ACK), PRIORITY_REPAIR)
        self.assertEqual(priority(ports.ERROR), PRIORITY_REPAIR)
        self.assertEqual(priority(ports.NODE_INFO), PRIORITY_LOW)
        for port in (ports.ECHO, ports.TRACEROUTE, ports.MANAGEMENT):
            self.assertEqual(priority(port), PRIORITY_LOW)
        self.assertEqual(priority(ports.CLASS2_CONTROL), PRIORITY_REPAIR)

    def test_dodag_information_depends_on_trickle_reset(self):
        self.assertEqual(priority(ports.DODAG_INFO), PRIORITY_PERIODIC)
        self.assertEqual(priority(ports.DODAG_INFO, trickle_reset=True), PRIORITY_REPAIR)
        # The flag concerns port 9 only.
        self.assertEqual(priority(ports.TOPOLOGY, trickle_reset=True), PRIORITY_LOW)

    def test_application_ports(self):
        for port in (16, 17, 100, 127):
            self.assertEqual(priority(port), PRIORITY_REPAIR)
        for port in (128, 200, 255):
            self.assertEqual(priority(port), PRIORITY_LOW)

    def test_reserved_and_invalid_ports(self):
        for port in (0, 11, 12, 15, 256, -1):
            self.assertRaises(ValueError, priority, port)


class Allowances(unittest.TestCase):
    def test_fractions_of_the_budget(self):
        window = DutyCycle(_BUDGET)
        self.assertEqual([window.allowance(p) for p in (1, 2, 3)], [_P1, _P2, _P3])
        self.assertRaises(ValueError, window.allowance, 0)
        self.assertRaises(ValueError, window.allowance, 4)

    def test_empty_window_allows_every_priority(self):
        window = DutyCycle(_BUDGET)
        for p in (1, 2, 3):
            self.assertTrue(window.allows(0, p, 9019))
            self.assertEqual(window.available(0, p), window.allowance(p))

    def test_refusal_by_port_priority(self):
        # 400 ms frames (255 B at SF7) until just above the priority-3 allowance.
        window = DutyCycle(_BUDGET)
        now = 0
        while window.used(now) + 400 <= _P3:
            self.assertTrue(window.allows(now, priority(ports.TOPOLOGY), 400))
            window.start(now, 400)
            window.done(now + 400, 400)
            now += 1000
        self.assertEqual(window.used(now), _P3 - _P3 % 400)
        self.assertFalse(window.allows(now, priority(ports.TOPOLOGY), 400))
        self.assertFalse(window.allows(now, priority(200), 400))
        self.assertTrue(window.allows(now, priority(ports.PRESENCE), 400))
        self.assertTrue(window.allows(now, priority(ports.ACK), 400))

    def test_allowance_boundaries(self):
        for p, allowance in ((1, _P1), (2, _P2), (3, _P3)):
            window = _filled(allowance - 1000)
            self.assertTrue(window.allows(1, p, 1000))
            self.assertFalse(window.allows(1, p, 1001))
            self.assertEqual(window.available(1, p), 1000)
            if p > 1:
                self.assertTrue(window.allows(1, p - 1, 1001))

    def test_over_budget_refuses_all(self):
        window = _filled(_P1)
        for p in (1, 2, 3):
            self.assertFalse(window.allows(0, p, 1))
            self.assertEqual(window.available(0, p), 0)


class MixedFrames(unittest.TestCase):
    def setUp(self):
        # 190 s used: a priority-3 message may still add 4.4 s, priority 2 69.2 s.
        self.window = _filled(190000)

    def test_low_priority_message_cannot_ride_beyond_its_allowance(self):
        w = self.window
        # Frame with a priority-1 message: 5 s fits priority 1.
        frame = w.admit(1, PRIORITY_REPAIR, PRIORITY_REPAIR, 5000)
        self.assertEqual(frame, PRIORITY_REPAIR)
        # A priority-3 message making it 5.5 s exceeds the priority-3 allowance.
        self.assertEqual(w.admit(1, frame, PRIORITY_LOW, 5500), 0)
        # A priority-2 one fits.
        self.assertEqual(w.admit(1, frame, PRIORITY_PERIODIC, 5500), PRIORITY_PERIODIC)

    def test_frame_priority_lowers_to_its_least_urgent_message(self):
        w = self.window
        frame = w.admit(1, PRIORITY_REPAIR, PRIORITY_LOW, 4000)
        self.assertEqual(frame, PRIORITY_LOW)
        # A priority-1 message may not make the frame exceed the allowance of
        # the priority-3 message already in it.
        self.assertEqual(w.admit(1, frame, PRIORITY_REPAIR, 4401), 0)
        self.assertEqual(w.admit(1, frame, PRIORITY_REPAIR, 4400), PRIORITY_LOW)

    def test_empty_frame_takes_the_message_priority(self):
        w = self.window
        self.assertEqual(w.admit(1, PRIORITY_REPAIR, PRIORITY_LOW, 4400), PRIORITY_LOW)
        self.assertEqual(w.admit(1, PRIORITY_REPAIR, PRIORITY_LOW, 4401), 0)


class MeasuredDuration(unittest.TestCase):
    def test_longer_measurement_counts(self):
        window = DutyCycle(_BUDGET)
        window.start(0, 400)
        self.assertEqual(window.used(0), 400)
        window.done(430, 430)
        self.assertEqual(window.used(430), 430)

    def test_shorter_measurement_keeps_computed(self):
        window = DutyCycle(_BUDGET)
        window.start(0, 400)
        window.done(390, 390)
        self.assertEqual(window.used(390), 400)
        # The next frame's measurement is compared with its own computed time.
        window.start(1000, 100)
        window.done(1120, 120)
        self.assertEqual(window.used(1120), 520)

    def test_excess_charged_when_measured(self):
        # Start in the first bin, end in the second: the excess expires a bin later.
        window = DutyCycle(_BUDGET)
        window.used(0)  # bins aligned on time 0
        window.start(_MIN - 100, 400)
        window.done(_MIN + 400, 500)
        self.assertEqual(window.used(_HOUR + _MIN - 1), 500)
        self.assertEqual(window.used(_HOUR + _MIN), 100)
        self.assertEqual(window.used(_HOUR + 2 * _MIN), 0)


class SlidingWindow(unittest.TestCase):
    def test_charge_counts_at_least_one_hour_at_most_one_bin_more(self):
        for at in (0, 1, 30000, _MIN - 1):
            window = DutyCycle(_BUDGET)
            window.used(0)  # bins aligned on time 0
            window.start(at, 1000)
            window.done(at, 0)
            self.assertEqual(window.used(at + _HOUR), 1000, "charged at %d" % at)
            self.assertEqual(window.used(at + _HOUR + _MIN), 0, "charged at %d" % at)

    def test_bins_expire_one_by_one(self):
        window = DutyCycle(_BUDGET)
        window.start(0, 100)
        window.start(10 * _MIN, 200)
        window.start(59 * _MIN + 59999, 400)
        self.assertEqual(window.used(_HOUR), 700)
        self.assertEqual(window.used(_HOUR + _MIN), 600)
        self.assertEqual(window.used(_HOUR + 11 * _MIN - 1), 600)
        self.assertEqual(window.used(_HOUR + 11 * _MIN), 400)
        # Charged 1 ms before the end of its bin: counts one hour and 1 ms.
        self.assertEqual(window.used(2 * _HOUR - 1), 400)
        self.assertEqual(window.used(2 * _HOUR), 0)

    def test_budget_frees_up_after_the_hour(self):
        window = _filled(_P3)
        self.assertFalse(window.allows(_HOUR, PRIORITY_LOW, 1))
        self.assertTrue(window.allows(_HOUR + _MIN, PRIORITY_LOW, 1))

    def test_next_release_is_the_next_bin_boundary(self):
        # Bins start at the first call.
        window = _filled(1000, now=30000)
        t = window.next_release(30000)
        self.assertEqual(t, 30000 + _MIN)
        self.assertEqual(window.next_release(t - 1), t)
        self.assertEqual(window.next_release(t), t + _MIN)
        # The charge leaves at a release time, never between two.
        releases = 1
        while True:
            self.assertEqual(window.used(t - 1), 1000)
            if window.used(t) == 0:
                break
            t = window.next_release(t)
            releases += 1
        self.assertEqual((t, releases), (30000 + 61 * _MIN, 61))

    def test_long_silence_clears_the_window(self):
        window = _filled(5000)
        self.assertEqual(window.used(10 * _HOUR + 123), 0)
        window.start(10 * _HOUR + 200, 50)
        self.assertEqual(window.used(10 * _HOUR + 300), 50)
        self.assertEqual(window.used(11 * _HOUR), 50)
        self.assertEqual(window.used(11 * _HOUR + _MIN), 0)

    def test_steady_traffic_stays_within_budget(self):
        # A sender charging whenever allowed never exceeds the budget over
        # any one-hour interval, checked bin by bin.
        window = DutyCycle(_BUDGET)
        sent = []
        now = 0
        while now < 3 * _HOUR:
            if window.allows(now, PRIORITY_REPAIR, 9019):
                window.start(now, 9019)
                sent.append(now)
            now += 9100
        self.assertTrue(len(sent) > 70)
        for i, first in enumerate(sent):
            in_hour = [t for t in sent[i:] if t < first + _HOUR]
            self.assertTrue(len(in_hour) * 9019 <= _BUDGET)

    def test_window_shape(self):
        window = DutyCycle(_BUDGET, window_ms=600000, bins=10)
        window.start(0, 10)
        self.assertEqual(window.used(600000), 10)
        self.assertEqual(window.used(660000), 0)
        self.assertRaises(ValueError, DutyCycle, _BUDGET, 3600001, 60)
        self.assertRaises(ValueError, DutyCycle, _BUDGET, 3600000, 0)


class Retention(unittest.TestCase):
    def setUp(self):
        self.platform = FakePlatform(XorShift32(1))
        self.offset = 100

    def _window(self):
        return DutyCycle(_BUDGET, retained=self.platform.retained, offset=self.offset)

    def test_survives_reset_with_restarted_clock(self):
        window = self._window()
        window.used(5 * _HOUR)  # bins aligned on 5 h
        t = 5 * _HOUR + 30000  # 30 s into a bin
        window.start(t, 194000)
        window.done(t + 194000, 194000)
        self.assertFalse(window.allows(t + 194000, PRIORITY_LOW, 500))
        self.platform.reset()
        # The clock restarts at 0: the window resumes 30 s into its bin.
        restored = self._window()
        self.assertEqual(restored.used(0), 194000)
        self.assertFalse(restored.allows(0, PRIORITY_LOW, 500))
        self.assertTrue(restored.allows(0, PRIORITY_PERIODIC, 500))
        # It expires one hour + one bin after the start of its bin, as if no
        # time had passed during the reset (conservative).
        self.assertEqual(restored.used(_HOUR + _MIN - 30001), 194000)
        self.assertEqual(restored.used(_HOUR + _MIN - 30000), 0)

    def test_survives_reset_with_continuing_clock(self):
        window = self._window()
        window.start(0, 700)
        window.start(30 * _MIN, 300)
        self.platform.reset()
        restored = self._window()
        now = 30 * _MIN + 5000
        self.assertEqual(restored.used(now), 1000)
        # Bins keep their relative ages; the 5 s of the reset are not counted,
        # which delays expiry (conservative).
        self.assertEqual(restored.used(now + 31 * _MIN - 1), 1000)
        self.assertEqual(restored.used(now + 31 * _MIN), 300)

    def test_charges_after_restore_add_up(self):
        window = self._window()
        window.start(0, 1000)
        self.platform.reset()
        restored = self._window()
        restored.start(10, 500)
        self.platform.reset()
        self.assertEqual(self._window().used(0), 1500)

    def test_unsaved_charges_lost_by_reset_are_none(self):
        # Every charge is saved: a reset right after a start keeps it.
        window = self._window()
        window.start(0, 1234)
        self.platform.reset()
        self.assertEqual(self._window().used(0), 1234)

    def test_power_loss_starts_empty(self):
        window = self._window()
        window.start(0, 100000)
        self.platform.power_loss()
        self.assertEqual(self._window().used(0), 0)

    def test_corrupted_or_foreign_content_ignored(self):
        window = self._window()
        window.start(0, 100000)
        data = self.platform.retained.data
        data[self.offset + 20] ^= 1  # a slot, against the stored total
        self.assertEqual(self._window().used(0), 0)
        data[self.offset + 20] ^= 1
        self.assertEqual(self._window().used(0), 100000)
        # Another window shape does not read it.
        other = DutyCycle(_BUDGET, 600000, 10, self.platform.retained, self.offset)
        self.assertEqual(other.used(0), 0)
        data[self.offset] ^= 0xFF  # magic
        self.assertEqual(self._window().used(0), 0)

    def test_stays_in_its_region(self):
        retained = FakeRetained(400)
        for i in range(400):
            retained.data[i] = 0xA5
        size = retained_size()
        self.assertEqual(size, 13 + 61 * 4)
        window = DutyCycle(_BUDGET, retained=retained, offset=50)
        window.start(0, 777)
        data = retained.data
        self.assertTrue(all(b == 0xA5 for b in data[:50]))
        self.assertTrue(all(b == 0xA5 for b in data[50 + size :]))
        self.assertRaises(ValueError, DutyCycle, _BUDGET, 3600000, 60, retained, 400 - size + 1)

    def test_without_retained_memory(self):
        window = DutyCycle(_BUDGET)
        window.start(0, 10)
        self.assertEqual(window.used(0), 10)
