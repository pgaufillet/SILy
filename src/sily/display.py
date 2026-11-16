"""The OLED status display of a box: pages of ``App.status``, written row by row.

The box is closed (no button, the display seen through a window): the
pages rotate by themselves, `PAGE_MS` each, the page shown refreshed every
`REFRESH_MS`. A page is `ROWS` lines of `COLS` characters (the 8x8 font of
``framebuf``); its first line is the box's identity (``GW`` the gateway,
``N`` a node, address, name) and the page number:

1. state: attached or ``DETACHED`` (``float``: attached without the
   root's ground), preferred parent, rank, battery voltage, operating mode;
2. neighbours, parents first (``*`` the preferred one, ``+`` another
   member of the parent set): address, margin in dB, ETX; as many as fit,
   then how many more;
3. test traffic (``sily.traffic``): on the gateway, per node, delivery %
   and median latency above the best case (ms); on a node, its period and
   size, messages sent, accepted, refused, failed;
4. health: boot number, uptime, free heap, duty cycle used (% of the
   budget), transmit queue, journal lines (``jnl`` with its errors when
   there are some), frames sent and received.

The display shares the asyncio loop with the mesh, whose root answers
frames within tens of ms: a whole frame (``SSD1306.show``, about 25 ms on
I2C at 400 kHz) would hold the loop too long. `Screen` writes only the rows
whose text changed, one 8-pixel row per step (`write_row`: the
controller's column and page windows set to that row, then its 128 bytes
of the frame buffer, about 3 ms), the loop free between two rows. It keeps
the longest row write and refresh (from the start of the first row written
for a page to the end of the last, the other tasks' turns included) for
the minute record (``ow``, ``or``).

Modes (``display.mode`` of the configuration): ``always``; ``window``, on
for ``window_min`` minutes after the start of the application, then
powered off; ``off``, the display never touched (nor in safe mode). A
failure (no device at start, an I2C error or any exception later) leaves
the application running without display (``sily.app`` journals it).

The page composition (`page`, `pages`) is pure; the driver is injected
(``sily.hw.display``: ``ssd1306.SSD1306_I2C``, frozen in the firmware).
"""

import asyncio
import time

from mesh.frame import RANK_INFINITE
from mesh.neighbours import ETX_ONE
from sily.config import GATEWAY

COLS = 16  # characters of a line (128 pixels, 8x8 font)
ROWS = 8  # lines of a page (64 pixels): one controller page each
PAGE_COUNT = 4
PAGE_MS = 5000  # time a page stays
REFRESH_MS = 1000  # period of the refresh of the page shown
ROW_PAUSE_MS = 0  # pause between two row writes (0: the other ready tasks run)

ALWAYS = "always"
WINDOW = "window"
OFF = "off"

# SSD1306 commands (``ssd1306`` driver of micropython-lib).
_SET_COL_ADDR = 0x21
_SET_PAGE_ADDR = 0x22
_COLUMNS = 128  # columns of the controller (narrower displays are centred)

_ROOM = ROWS - 2  # table rows of a page: identity and column titles above
_TABLE = "%-6s%5s%5s"  # columns of the neighbour and traffic tables


# --- Page composition (pure) -----------------------------------------------------------


def _ascii(text):
    """Return text with characters outside printable ASCII as ``?`` (the font's)."""
    out = ""
    for c in text:
        out += c if " " <= c <= "~" else "?"
    return out


def _tenths(value):
    """Return value (tenths, int) as ``12.5``; None as ``-``."""
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    value = -value if value < 0 else value
    return "%s%d.%d" % (sign, value // 10, value % 10)


def _etx(value):
    if value is None:
        return "-"
    tenths = (value * 10 + ETX_ONE // 2) // ETX_ONE
    return ">99" if tenths >= 1000 else _tenths(tenths)


def _volts(mv):
    if mv is None:
        return "-"
    return "%d.%02d V" % (mv // 1000, mv % 1000 // 10)


def _dash(value):
    return "-" if value is None else str(value)


def _uptime(s):
    if s is None:
        return "-"
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def _header(status, index):
    role = "GW" if status.get("role") == GATEWAY else "N"
    left = "%s%s %s" % (role, _dash(status.get("address")), _ascii(status.get("name") or ""))
    right = "%d/%d" % (index + 1, PAGE_COUNT)
    left = left[: COLS - len(right) - 1]
    return left + " " * (COLS - len(left) - len(right)) + right


def _table(rows, line):
    """Return the lines of rows (as many as fit, then ``+N more``), each made by line(row)."""
    if len(rows) > _ROOM:
        shown = rows[: _ROOM - 1]
        return [line(r) for r in shown] + ["+%d more" % (len(rows) - len(shown))]
    return [line(r) for r in rows]


def _state(s):
    if s.get("attached"):
        attached = "attached" if s.get("grounded", True) else "attached (float)"
    else:
        attached = "DETACHED"
    rank = s.get("rank")
    return [
        attached,
        "parent " + _dash(s.get("parent")),
        "rank " + ("-" if rank is None or rank == RANK_INFINITE else str(rank)),
        "battery " + _volts(s.get("battery")),
        s.get("mode") or "",
    ]


def _neighbours(s):
    preferred = s.get("parent")

    def order(n):
        if n["address"] == preferred:
            return (0, n["address"])
        return (1 if n.get("parent") else 2, n["address"])

    def line(n):
        if n["address"] == preferred:
            mark = "*"
        else:
            mark = "+" if n.get("parent") else " "
        return _TABLE % (mark + str(n["address"]), _tenths(n.get("margin")), _etx(n.get("etx")))

    rows = sorted(s.get("neighbours") or [], key=order)
    lines = [_TABLE % (" adr", "dB", "ETX")]
    if not rows:
        return lines + ["none heard"]
    return lines + _table(rows, line)


def _delay(ms):
    if ms is None:
        return "-"
    return str(ms) if ms < 100000 else "%ds" % (ms // 1000)


def _traffic(s):
    t = s.get("traffic")
    if t is None:
        return ["test traffic off"]
    if t.get("role") == "receiver":

        def line(n):
            d = n.get("delivery")
            pct = "-" if d is None else "%.1f" % (d * 100)
            return _TABLE % (n["address"], pct, _delay(n.get("median_ms")))

        nodes = t.get("nodes") or []
        lines = [_TABLE % ("node", "dlv%", "ms")]
        if not nodes:
            return lines + ["none received"]
        return lines + _table(nodes, line)
    return [
        "test traffic",
        "every %d s, %d B" % (t.get("period_s", 0), t.get("size", 0)),
        "%-9s%7d" % ("sent", t.get("submitted", 0)),
        "%-9s%7d" % ("accepted", t.get("accepted", 0)),
        "%-9s%7d" % ("refused", t.get("refused", 0)),
        "%-9s%7d" % ("failed", t.get("failed", 0)),
    ]


def _health(s):
    c = s.get("counters") or {}
    budget = c.get("duty_budget_ms")
    used = c.get("duty_ms")
    if used is None:
        duty = "-"
    elif budget:
        duty = _tenths(used * 1000 // budget) + "%"
    else:
        duty = "%d ms" % used
    lines = _dash(c.get("journal_lines"))
    if c.get("journal_errors"):
        journal = "jnl %s err %d" % (lines, c["journal_errors"])
    else:
        journal = "journal " + lines
    return [
        "boot " + _dash(s.get("boot")),
        "up " + _uptime(s.get("time_s")),
        "heap " + _dash(s.get("heap")),
        "duty " + duty,
        "queue " + _dash(c.get("queue")),
        journal,
        "tx %s rx %s" % (_dash(c.get("tx_frames")), _dash(c.get("rx_frames"))),
    ]


_PAGES = (_state, _neighbours, _traffic, _health)


def page(status, index):
    """Return page index (0 to `PAGE_COUNT` - 1) of status: `ROWS` lines of at most `COLS`."""
    lines = [_header(status, index)]
    for text in _PAGES[index](status):
        lines.append(text[:COLS])
    while len(lines) < ROWS:
        lines.append("")
    return lines[:ROWS]


def pages(status):
    """Return every page of status (``App.status``): a list of `ROWS`-line lists."""
    return [page(status, i) for i in range(PAGE_COUNT)]


# --- Writer ------------------------------------------------------------------------------


def write_row(oled, row, data):
    """Send data, the frame buffer bytes of one 8-pixel row, to that row of oled.

    The ``ssd1306`` driver's ``show`` sends the whole buffer; here the
    controller's column window is set to the display's width and its page
    window to that one row (horizontal addressing, set by the driver's
    initialisation), then the row's bytes follow.
    """
    x0 = (_COLUMNS - oled.width) // 2
    for cmd in (_SET_COL_ADDR, x0, x0 + oled.width - 1, _SET_PAGE_ADDR, row, row):
        oled.write_cmd(cmd)
    oled.write_data(data)


class Screen:
    """Lines of text on an SSD1306 driver, written only where they changed.

    oled: a ``framebuf.FrameBuffer`` (MONO_VLSB, ``buffer``, ``width``)
    with the driver's ``write_cmd``, ``write_data`` and ``poweroff``;
    ticks_us, ticks_diff: the microsecond clock of the timings.
    """

    def __init__(self, oled, ticks_us=time.ticks_us, ticks_diff=time.ticks_diff):
        self.oled = oled
        self.ticks_us = ticks_us
        self.ticks_diff = ticks_diff
        width = oled.width
        view = memoryview(oled.buffer)
        self._rows = [view[r * width : (r + 1) * width] for r in range(ROWS)]
        self.shown = [None] * ROWS  # text of each row on the screen; None: unknown
        self.row_us = 0  # longest row write since the last `stalls`
        self.refresh_us = 0  # longest refresh since the last `stalls`
        self.written = 0  # rows written since the last `stalls`

    def update(self, lines):
        """Write the rows of lines (missing ones blank) whose text changed.

        A generator writing one row per step and yielding its number: the
        caller gives the loop to the other tasks between steps.
        """
        todo = []
        for row in range(ROWS):
            text = lines[row] if row < len(lines) else ""
            if text != self.shown[row]:
                todo.append(row)
        start = None
        for row in todo:
            text = lines[row] if row < len(lines) else ""
            t0 = self.ticks_us()
            if start is None:
                start = t0
            self._draw(row, text)
            t1 = self.ticks_us()
            self.shown[row] = text
            self.written += 1
            self.row_us = max(self.row_us, self.ticks_diff(t1, t0))
            if row == todo[-1]:
                self.refresh_us = max(self.refresh_us, self.ticks_diff(t1, start))
            yield row

    def _draw(self, row, text):
        oled = self.oled
        y = row * 8
        oled.fill_rect(0, y, oled.width, 8, 0)
        oled.text(text, 0, y, 1)
        write_row(oled, row, self._rows[row])

    def stalls(self):
        """Return (row_ms, refresh_ms), the longest row write and refresh since
        the last call (ms, rounded up), and restart them; None if no row was written."""
        if not self.written:
            return None
        out = ((self.row_us + 999) // 1000, (self.refresh_us + 999) // 1000)
        self.row_us = 0
        self.refresh_us = 0
        self.written = 0
        return out

    def off(self):
        """Power the display off (its content is kept by the controller, unknown here)."""
        self.shown = [None] * ROWS
        self.oled.poweroff()


class Display:
    """The rotation of the pages of status() on screen (a `Screen`).

    now: the application's clock (ms); mode `ALWAYS` or `WINDOW` (off after
    window_ms from the creation); on_error(exception): called once if the
    display fails, which then stops.
    """

    def __init__(
        self,
        screen,
        status,
        now,
        mode=ALWAYS,
        window_ms=0,
        page_ms=PAGE_MS,
        refresh_ms=REFRESH_MS,
        on_error=None,
    ):
        self.screen = screen
        self.status = status
        self.now = now
        self.mode = mode
        self.window_ms = window_ms
        self.page_ms = page_ms
        self.refresh_ms = refresh_ms
        self.on_error = on_error
        self.start = now()
        self.lit = True  # False once powered off or failed
        self.error = None

    def lines(self, t):
        """Return the page to show at t (ms), None once the window is over."""
        elapsed = t - self.start
        if self.mode == WINDOW and elapsed >= self.window_ms:
            return None
        return page(self.status(), elapsed // self.page_ms % PAGE_COUNT)

    def off(self):
        """Power the display off, if lit (never raises: the application's stop)."""
        if not self.lit:
            return
        self.lit = False
        try:
            self.screen.off()
        except Exception:  # an I2C error: nothing more to do
            pass

    async def run(self):
        """Task: show the pages until the window ends or the display fails."""
        try:
            while True:
                lines = self.lines(self.now())
                if lines is None:
                    self.off()
                    return
                for _ in self.screen.update(lines):
                    await asyncio.sleep_ms(ROW_PAUSE_MS)
                await asyncio.sleep_ms(
                    self.refresh_ms - (self.now() - self.start) % self.refresh_ms
                )
        except Exception as e:  # the application goes on without display
            self.error = e
            self.off()
            if self.on_error is not None:
                self.on_error(e)
