"""UTC for the field journal (sily.utc): calendar, validity, anchors, the RTC.

The calendar is checked on dates known from elsewhere (one of them measured
on a board: the ESP32 port's ``time.time()`` after setting its RTC) and day
by day over the valid range; the RTC through `FakeRtc` (``test_sily_stack``)
of both MicroPython epochs, as ``sily.hw`` reads and sets it.
"""

import unittest

from test_sily_stack import FakeRtc

from sily import utc

# 2026-11-17 15:30:00 UTC, a Tuesday: on a board, RTC().datetime() of this
# tuple, then time.time() == 848244600 (seconds since 2000-01-01).
BOARD_TUPLE = (2026, 11, 17, 1, 15, 30, 0, 0)
BOARD_S_2000 = 848244600
BOARD_MS = (BOARD_S_2000 + utc.EPOCH_2000_S) * 1000


class Calendar(unittest.TestCase):
    def test_known_dates(self):
        cases = (
            (0, (1970, 1, 1, 3, 0, 0, 0, 0)),  # a Thursday
            (utc.EPOCH_2000_S * 1000, (2000, 1, 1, 5, 0, 0, 0, 0)),  # a Saturday
            (utc.MIN_MS, (2025, 1, 1, 2, 0, 0, 0, 0)),
            (utc.MAX_MS, (2100, 1, 1, 4, 0, 0, 0, 0)),
            (1835481599999, (2028, 2, 29, 1, 23, 59, 59, 999000)),  # a leap day
            (BOARD_MS, BOARD_TUPLE),
        )
        for ms, dt in cases:
            self.assertEqual(utc.to_datetime(ms), dt, ms)
            self.assertEqual(utc.from_datetime(dt), ms, dt)

    def test_milliseconds_as_microseconds(self):
        dt = utc.to_datetime(BOARD_MS + 123)
        self.assertEqual(dt, BOARD_TUPLE[:7] + (123000,))
        self.assertEqual(utc.from_datetime(dt[:7] + (123999,)), BOARD_MS + 123)

    def test_day_by_day_over_the_valid_range(self):
        first = utc.days(2025, 1, 1)
        last = utc.days(2100, 1, 1)
        self.assertEqual((first, last), (utc.MIN_MS // utc.DAY_MS, utc.MAX_MS // utc.DAY_MS))
        self.assertEqual(utc.date(first), (2025, 1, 1))
        year, month, day = 2025, 1, 1  # counted here, month by month
        lengths = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
        wrong = []
        for n in range(first, last):
            if utc.date(n) != (year, month, day) or utc.days(year, month, day) != n:
                wrong.append((n, year, month, day))
            leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
            if day < lengths[month - 1] + (1 if leap and month == 2 else 0):
                day += 1
            elif month < 12:
                month, day = month + 1, 1
            else:
                year, month, day = year + 1, 1, 1
        self.assertEqual(wrong, [])
        self.assertEqual((year, month, day), (2100, 1, 1))
        self.assertEqual(utc.date(last), (2100, 1, 1))

    def test_text(self):
        self.assertEqual(utc.text(BOARD_MS + 7), "2026-11-17 15:30:00.007")


class Valid(unittest.TestCase):
    def test_range(self):
        for ms in (utc.MIN_MS, BOARD_MS, utc.MAX_MS - 1):
            self.assertEqual(utc.valid(ms), ms)
        for ms in (None, 0, utc.EPOCH_2000_S * 1000, utc.MIN_MS - 1, utc.MAX_MS):
            self.assertIsNone(utc.valid(ms), ms)


class Rtc(unittest.TestCase):
    """The RTC read and set as ``sily.hw`` does, on both epochs."""

    def test_set_then_read_on_both_epochs(self):
        for epoch, seconds in ((2000, BOARD_S_2000), (1970, BOARD_MS // 1000)):
            rtc = FakeRtc(epoch)
            utc.write(rtc, BOARD_MS + 250)
            self.assertEqual(rtc.us % 1000000, 250000)
            self.assertEqual(rtc.us // 1000000, seconds, epoch)
            self.assertTrue(rtc.time() - seconds in (0, 1), epoch)
            self.assertEqual(rtc.datetime()[:4], BOARD_TUPLE[:4])
            elapsed = utc.read(rtc) - BOARD_MS - 250
            self.assertTrue(0 <= elapsed < 500, (epoch, elapsed))

    def test_never_set_is_not_valid(self):
        # A power cut: the RTC restarts from its epoch (measured on a board).
        for epoch, start in ((2000, utc.EPOCH_2000_S), (1970, 0)):
            ms = utc.read(FakeRtc(epoch))
            self.assertTrue(0 <= ms - start * 1000 < 5000, (epoch, ms))
            self.assertIsNone(utc.valid(ms))


T0 = 5000  # ms of the application's clock at the first sample


class Anchor(unittest.TestCase):
    def test_none_before_an_anchor(self):
        a = utc.Anchor()
        self.assertIsNone(a.estimate(T0))
        a.start(T0, None)  # the RTC not set
        self.assertIsNone(a.estimate(T0))

    def test_rtc_at_boot_anchors_until_the_first_sample(self):
        a = utc.Anchor()
        a.start(200, BOARD_MS)
        self.assertEqual(a.estimate(T0), BOARD_MS + T0 - 200)
        self.assertEqual(a.samples, 0)
        # The first sample is anchored even when it agrees with the RTC.
        self.assertTrue(a.sample(T0, BOARD_MS + T0 - 200))
        self.assertEqual((a.t, a.samples), (T0, 1))

    def test_first_sample_then_one_per_period(self):
        a = utc.Anchor()
        self.assertTrue(a.sample(T0, BOARD_MS))
        self.assertEqual(a.estimate(T0 + 7000), BOARD_MS + 7000)
        late = utc.PERIOD_MS - 1
        for dt in (5000, 10000, late):  # the polls every 5 s, 30 ms of delay
            self.assertFalse(a.sample(T0 + dt, BOARD_MS + dt + 30), dt)
        self.assertEqual((a.t, a.utc), (T0, BOARD_MS))
        self.assertTrue(a.sample(T0 + utc.PERIOD_MS, BOARD_MS + utc.PERIOD_MS + 30))
        self.assertEqual((a.t, a.utc, a.samples), (T0 + utc.PERIOD_MS, BOARD_MS + 600030, 2))

    def test_a_jump_anchors_at_once(self):
        a = utc.Anchor()
        a.sample(T0, BOARD_MS)
        t = T0 + 5000
        for departure in (utc.JUMP_MS, -utc.JUMP_MS):  # at the bound: not a jump
            self.assertFalse(a.sample(t, BOARD_MS + 5000 + departure), departure)
        self.assertTrue(a.sample(t, BOARD_MS + 5000 + utc.JUMP_MS + 1))
        self.assertTrue(a.sample(t + 5000, BOARD_MS - 3600000))  # the phone an hour back
        self.assertEqual((a.utc, a.samples), (BOARD_MS - 3600000, 3))
        self.assertEqual(a.estimate(t + 6000), BOARD_MS - 3600000 + 1000)


if __name__ == "__main__":
    unittest.main()
