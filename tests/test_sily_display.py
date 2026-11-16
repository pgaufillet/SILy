"""The OLED status display (sily.display): pages, row writer, rotation, modes.

Pages are composed from sample ``App.status()`` dicts (a gateway and a
node); the writer and the rotation run on `FakeOled` (``test_sily_stack``),
the ``ssd1306`` driver's surface over a real ``framebuf`` with its
controller's memory simulated, and on injected clocks.
"""

import asyncio
import unittest

from test_sily_stack import FakeOled

from mesh.runner import Clock
from sily import display, record, traffic
from sily.display import Display, Screen


def _neighbour(address, margin, etx, parent=0):
    row = [address, 128, margin, margin, etx, 100, 4, parent, 30, -90, -91, 55]
    return dict(zip(record.NEIGHBOUR_FIELDS, row))


def _origin(address, delivery, median_ms):
    node = dict(zip(traffic.FIELDS, [address, 2, 10, 10, 0, 0, 0, 0, 1, 5, median_ms, None, None]))
    node["delivery"] = delivery
    return node


def _counters(**fields):
    c = {
        "tx_frames": 120,
        "rx_frames": 456,
        "rx_crc": 0,
        "rx_auth": 0,
        "forwarded": 0,
        "retries": 0,
        "unreachable": 0,
        "queue": 2,
        "duty_ms": 4000,
        "duty_budget_ms": 324000,
        "journal_lines": 1234,
        "journal_errors": 0,
        "items_lost": 0,
    }
    c.update(fields)
    return c


def gateway_status(**fields):
    s = {
        "role": "gateway",
        "address": 0,
        "name": "base",
        "boot": 12,
        "battery": 3950,
        "mode": "configuration",
        "attached": True,
        "heap": 123456,
        "time_s": 3725,
        "parent": None,
        "rank": 0,
        "grounded": True,
        "neighbours": [_neighbour(3, 125, 128), _neighbour(1, -35, 192)],
        "foreign": [0, 0],
        "counters": _counters(),
        "traffic": {
            "role": "receiver",
            "nodes": [_origin(3, 0.75, 120), _origin(12, 1.0, 123456), _origin(5, None, None)],
            "malformed": 0,
            "items_lost": 0,
            "nodes_beyond": 0,
        },
    }
    s.update(fields)
    return s


def node_status(**fields):
    s = {
        "role": "node",
        "address": 3,
        "name": "box3",
        "boot": 2,
        "battery": None,
        "mode": "deployment",
        "attached": False,
        "heap": 98000,
        "time_s": 59,
        "parent": None,
        "rank": 255,
        "grounded": False,
        "neighbours": [],
        "foreign": [1, 4],
        "counters": _counters(),
        "traffic": {
            "role": "sender",
            "period_s": 30,
            "size": 10,
            "submitted": 123,
            "accepted": 120,
            "refused": 3,
            "failed": 1,
            "refusals": [[1, 3]],
            "failures": [[3, 1]],
        },
    }
    s.update(fields)
    return s


class Pages(unittest.TestCase):
    def assertFits(self, status):
        for lines in display.pages(status):
            self.assertEqual(len(lines), display.ROWS, lines)
            for line in lines:
                self.assertTrue(len(line) <= display.COLS, line)

    def test_gateway_state(self):
        self.assertEqual(
            display.page(gateway_status(), 0),
            [
                "GW0 base     1/4",
                "attached",
                "parent -",
                "rank 0",
                "battery 3.95 V",
                "configuration",
                "",
                "",
            ],
        )

    def test_node_state(self):
        lines = display.page(node_status(), 0)
        expected = [
            "N3 box3      1/4",
            "DETACHED",
            "parent -",
            "rank -",
            "battery -",
            "deployment",
        ]
        self.assertEqual(lines[:6], expected)
        lines = display.page(node_status(attached=True, parent=1, rank=384, grounded=False), 0)
        self.assertEqual(lines[1:4], ["attached (float)", "parent 1", "rank 384"])

    def test_neighbours(self):
        self.assertEqual(
            display.page(gateway_status(), 1)[1:5],
            [" adr     dB  ETX", " 1     -3.5  1.5", " 3     12.5  1.0", ""],
        )

    def test_no_neighbour(self):
        self.assertEqual(
            display.page(node_status(), 1)[1:4], [" adr     dB  ETX", "none heard", ""]
        )

    def test_many_neighbours_parents_first(self):
        rows = [_neighbour(a, 10 * a, 128, 1 if a in (7, 9) else 0) for a in range(10, 0, -1)]
        rows[-1]["margin"] = None  # address 1: no measurement yet
        lines = display.page(node_status(neighbours=rows, parent=7, attached=True), 1)
        self.assertEqual(
            lines[2:],
            [
                "*7      7.0  1.0",
                "+9      9.0  1.0",
                " 1        -  1.0",
                " 2      2.0  1.0",
                " 3      3.0  1.0",
                "+5 more",
            ],
        )

    def test_gateway_traffic(self):
        self.assertEqual(
            display.page(gateway_status(), 2)[1:6],
            ["node   dlv%   ms", "3      75.0  120", "12    100.0 123s", "5         -    -", ""],
        )
        empty = gateway_status(traffic={"role": "receiver", "nodes": []})
        self.assertEqual(display.page(empty, 2)[2], "none received")
        many = [_origin(a, 1.0, 50) for a in range(1, 9)]
        lines = display.page(gateway_status(traffic={"role": "receiver", "nodes": many}), 2)
        self.assertEqual((lines[6], lines[7]), ("5     100.0   50", "+3 more"))

    def test_node_traffic(self):
        self.assertEqual(
            display.page(node_status(), 2)[1:7],
            [
                "test traffic",
                "every 30 s, 10 B",
                "sent         123",
                "accepted     120",
                "refused        3",
                "failed         1",
            ],
        )
        lines = display.page(node_status(traffic=None), 2)
        self.assertEqual(lines[1:3], ["test traffic off", ""])

    def test_health(self):
        self.assertEqual(
            display.page(gateway_status(), 3),
            [
                "GW0 base     4/4",
                "boot 12",
                "up 1:02:05",
                "heap 123456",
                "duty 1.2%",
                "queue 2",
                "journal 1234",
                "tx 120 rx 456",
            ],
        )
        c = _counters(journal_errors=3, duty_ms=324000)
        lines = display.page(gateway_status(counters=c), 3)
        self.assertEqual((lines[4], lines[6]), ("duty 100.0%", "jnl 1234 err 3"))

    def test_truncation(self):
        status = gateway_status(name="a-very-long-name", address=0)
        self.assertEqual(display.page(status, 0)[0], "GW0 a-very-l 1/4")
        status = node_status(name="rés", address=65534)
        self.assertEqual(display.page(status, 0)[0], "N65534 r?s   1/4")
        big = _counters(tx_frames=123456789, rx_frames=987654321, journal_errors=12345)
        rows = [_neighbour(65534, -999, 300 * 128, 1)]
        status = node_status(address=65534, counters=big, time_s=10**7, neighbours=rows)
        self.assertFits(status)
        self.assertEqual(display.page(status, 3)[7], "tx 123456789 rx ")
        self.assertFits(gateway_status())
        self.assertFits(node_status())

    def test_status_before_the_node_is_built(self):
        status = {"role": "node", "address": 3, "name": "box3", "attached": False, "boot": 1}
        self.assertFits(status)
        self.assertEqual(display.page(status, 3)[1:3], ["boot 1", "up -"])


def _ticks(step_us):
    """A microsecond clock advancing step_us at each read."""
    t = [-step_us]

    def ticks():
        t[0] += step_us
        return t[0]

    return ticks


def _diff(a, b):
    return a - b


class Writer(unittest.TestCase):
    def setUp(self):
        self.oled = FakeOled()
        self.screen = Screen(self.oled, _ticks(2500), _diff)

    def test_first_page_one_row_per_step(self):
        lines = display.page(gateway_status(), 0)
        steps = self.screen.update(lines)
        for row in range(display.ROWS):
            self.assertEqual(next(steps), row)
            self.assertEqual(len(self.oled.writes), row + 1)
            first, last, data = self.oled.writes[-1]
            self.assertEqual((first, last, len(data)), (row, row, 128))
        with self.assertRaises(StopIteration):
            next(steps)
        self.assertEqual(self.oled.ram, self.oled.buffer)  # as a whole frame would
        self.assertTrue(any(self.oled.ram[:128]))  # the header is drawn
        self.assertFalse(any(self.oled.ram[6 * 128 :]))  # blank lines

    def test_only_changed_rows(self):
        lines = display.page(gateway_status(), 3)
        list(self.screen.update(lines))
        before = bytes(self.oled.ram)
        n = len(self.oled.writes)
        self.assertEqual(list(self.screen.update(lines)), [])
        self.assertEqual(len(self.oled.writes), n)
        lines = display.page(gateway_status(time_s=3726), 3)
        self.assertEqual(list(self.screen.update(lines)), [2])
        self.assertEqual(self.oled.writes[n][:2], (2, 2))
        self.assertEqual(self.oled.ram, self.oled.buffer)
        self.assertEqual(self.oled.ram[:256], before[:256])
        self.assertNotEqual(self.oled.ram[256:384], before[256:384])

    def test_timings(self):
        self.assertIsNone(self.screen.stalls())
        list(self.screen.update(display.page(gateway_status(), 0)))
        # 2.5 ms per row; the refresh from the first row's start to the last's end
        self.assertEqual(self.screen.stalls(), (3, 38))
        self.assertIsNone(self.screen.stalls())
        list(self.screen.update(display.page(gateway_status(), 1)))
        self.assertEqual(self.screen.stalls(), (3, 28))  # rows 0 to 5; 6 and 7 stay blank

    def test_loop_free_between_rows(self):
        oled = self.oled
        d = Display(Screen(oled), gateway_status, Clock().now, refresh_ms=1000)
        seen = []

        async def main():
            task = asyncio.create_task(d.run())
            while len(oled.writes) < display.ROWS:
                seen.append(len(oled.writes))
                await asyncio.sleep_ms(0)
            task.cancel()

        asyncio.run(main())
        self.assertEqual(sorted(set(seen)), list(range(display.ROWS)))


class Rotation(unittest.TestCase):
    def _display(self, **options):
        self.t = [1000]
        self.oled = FakeOled()
        return Display(Screen(self.oled), gateway_status, lambda: self.t[0], **options)

    def test_pages_rotate(self):
        d = self._display(page_ms=5000)
        numbers = []
        for t in (0, 4999, 5000, 9999, 10000, 19999, 20000):
            numbers.append(d.lines(1000 + t)[0][-3:])
        self.assertEqual(numbers, ["1/4", "1/4", "2/4", "2/4", "3/4", "4/4", "1/4"])

    def test_window_ends(self):
        d = self._display(mode=display.WINDOW, window_ms=600000)
        self.assertIsNotNone(d.lines(1000 + 599999))
        self.assertIsNone(d.lines(1000 + 600000))
        d = self._display(mode=display.ALWAYS)
        self.assertIsNotNone(d.lines(10**9))

    def test_window_powers_off(self):
        oled = FakeOled()
        d = Display(
            Screen(oled),
            gateway_status,
            Clock().now,
            display.WINDOW,
            window_ms=150,
            page_ms=40,
            refresh_ms=20,
        )
        asyncio.run(d.run())  # returns at the end of the window
        self.assertFalse(oled.powered)
        self.assertFalse(d.lit)
        self.assertIsNone(d.error)
        self.assertEqual(d.screen.shown, [None] * display.ROWS)  # off: content unknown
        headers = set(bytes(w[2]) for w in oled.writes if w[0] == 0)
        self.assertTrue(len(headers) >= 3, len(headers))  # several pages shown

    def test_failure_stops_the_display(self):
        oled = FakeOled()
        oled.fail = OSError(5)
        errors = []
        d = Display(Screen(oled), gateway_status, Clock().now, on_error=errors.append)
        asyncio.run(d.run())  # returns, does not raise
        self.assertEqual(errors, [oled.fail])
        self.assertTrue(d.error is oled.fail)
        self.assertFalse(oled.powered)
        d.off()  # again: nothing
        self.assertEqual(oled.cmds.count(0xAE), 1)

    def test_status_error_stops_the_display(self):
        errors = []
        d = Display(Screen(FakeOled()), lambda: {}["x"], Clock().now, on_error=errors.append)
        asyncio.run(d.run())
        self.assertEqual(len(errors), 1)
        self.assertTrue(isinstance(errors[0], KeyError))
