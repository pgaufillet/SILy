"""UTC for the field journal: the application's clock anchored to a phone's.

The boards have no time source: every journal line carries ``t``, ms of the
application's clock since the board's boot (``sily.record``). A phone
showing the box's page sends its own time with each poll (``sily.web``),
ms since the Unix epoch (1970-01-01 UTC; the phone's clock is set by its
network): a sample. `Anchor` decides which samples become anchors: the
first of a boot, then one every `PERIOD_MS` at most while samples keep
coming, and at once a sample more than `JUMP_MS` away from the UTC the last
anchor predicts (the phone's clock changed). The application journals each
anchor (a ``clock`` line: its ``t``, ``utc``) and sets the board's RTC to
it. An analysis maps a line's ``t`` to UTC by the latest anchor before it,
``utc + t - t_anchor``; the one-way delay of the request (tens of ms on
the access point) is not corrected.

The RTC (``sily.hw``) keeps its time across a soft reset, ``machine.reset``
and a watchdog reset, not across a power cut: it then restarts at
2000-01-01 (measured on a board). Besides the anchors, ``make configure``
sets it to the host's UTC before the reset that starts the application
(``tools/box.py``: within a few ms of the host's clock). A boot reads it
into its ``boot`` record (``utc``) when `valid`; that reading anchors the
boot until its first sample. A node without anchor is placed through the
gateway's clock offsets (``sily.traffic``, ``tools/survey.py``).

The RTC is read and set through its calendar tuple (``machine.RTC().datetime``:
year, month, day, weekday from 0 on Monday, hours, minutes, seconds,
microseconds), never through seconds since the port's epoch: MicroPython's
ESP32 port counts ``time.time()`` from 2000-01-01 (`EPOCH_2000_S` after the
Unix epoch), its unix port from 1970-01-01. `to_datetime` and
`from_datetime` convert (proleptic Gregorian calendar, UTC). On the board,
ms since the epoch exceed a small integer: each sample allocates a few
long integers.

Common subset: board, unix port and CPython (``tools/journal_summary.py``).
"""

MIN_MS = 1735689600000  # 2025-01-01 00:00 UTC: an earlier time is not set
MAX_MS = 4102444800000  # 2100-01-01 00:00 UTC: excluded
PERIOD_MS = 600000  # at most one anchor per period while samples keep coming
JUMP_MS = 2000  # a sample further from the estimate: anchored at once
EPOCH_2000_S = 946684800  # 2000-01-01 00:00 UTC, the ESP32 port's epoch
DAY_MS = 86400000


def valid(ms):
    """Return ms (since the Unix epoch) within [`MIN_MS`, `MAX_MS`), else None."""
    if ms is None or ms < MIN_MS or ms >= MAX_MS:
        return None
    return ms


def days(year, month, day):
    """Return the days from 1970-01-01 to the date (month and day from 1)."""
    if month <= 2:
        year -= 1
        month += 9
    else:
        month -= 3
    era = year // 400
    yoe = year - era * 400
    doe = yoe * 365 + yoe // 4 - yoe // 100 + (153 * month + 2) // 5 + day - 1
    return era * 146097 + doe - 719468


def date(n):
    """Return (year, month, day) of the date n days after 1970-01-01."""
    n += 719468
    era = n // 146097
    doe = n - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year = yoe + era * 400
    return (year + 1 if month <= 2 else year), month, day


def to_datetime(ms):
    """Return the RTC tuple of ms since the Unix epoch."""
    n = ms // DAY_MS
    rest = ms - n * DAY_MS
    year, month, day = date(n)
    s = rest // 1000
    weekday = (n + 3) % 7  # 1970-01-01: a Thursday
    return (year, month, day, weekday, s // 3600, s // 60 % 60, s % 60, rest % 1000 * 1000)


def from_datetime(dt):
    """Return the ms since the Unix epoch of an RTC tuple (its weekday ignored)."""
    s = dt[4] * 3600 + dt[5] * 60 + dt[6]
    return days(dt[0], dt[1], dt[2]) * DAY_MS + s * 1000 + dt[7] // 1000


def text(ms):
    """Return ms since the Unix epoch as ``YYYY-MM-DD hh:mm:ss.mmm`` (UTC)."""
    dt = to_datetime(ms)
    return "%04d-%02d-%02d %02d:%02d:%02d.%03d" % (
        dt[0],
        dt[1],
        dt[2],
        dt[4],
        dt[5],
        dt[6],
        dt[7] // 1000,
    )


def read(rtc):
    """Return the time of rtc (``machine.RTC``) in ms since the Unix epoch."""
    return from_datetime(rtc.datetime())


def write(rtc, ms):
    """Set rtc (``machine.RTC``) to ms since the Unix epoch."""
    rtc.datetime(to_datetime(ms))


class Anchor:
    """The application's clock (ms) anchored to UTC (ms since the Unix epoch).

    ``t``, ``utc``: the last anchor, None before one; ``samples``: the
    anchors taken from samples (`sample`), the RTC's reading at boot
    (`start`) not counted.
    """

    def __init__(self, period_ms=PERIOD_MS, jump_ms=JUMP_MS):
        self.period_ms = period_ms
        self.jump_ms = jump_ms
        self.t = None
        self.utc = None
        self.samples = 0

    def start(self, t, utc):
        """Anchor at utc, the RTC's reading at t (None: not set)."""
        if utc is not None:
            self.t = t
            self.utc = utc

    def estimate(self, t):
        """Return the UTC at t by the last anchor, None without one."""
        if self.utc is None:
            return None
        return self.utc + (t - self.t)

    def sample(self, t, utc):
        """Take a sample, utc at t; return True when it becomes the anchor:
        the first sample, `period_ms` or more after the last anchor, or more
        than `jump_ms` away from its estimate."""
        if (
            self.samples
            and t - self.t < self.period_ms
            and abs(utc - self.estimate(t)) <= self.jump_ms
        ):
            return False
        self.t = t
        self.utc = utc
        self.samples += 1
        return True
