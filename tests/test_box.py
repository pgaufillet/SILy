"""Host configuration of the boxes (tools/box.py): network checks and templates."""

import json
import unittest

from sily import config
from tools import box

NETWORK = {"password": "secret words", "network": "club", "iterations": 10, "sf": 7}
AP = {"prefix": "SILy", "password": "ap-words-7731"}


def _box(address, role="node", **fields):
    b = {"role": role, "address": address}
    b.update(fields)
    return b


class Network(unittest.TestCase):
    def test_valid_set(self):
        boxes = [("g.json", _box(0, "gateway")), ("a.json", _box(1)), ("b.json", _box(2))]
        self.assertEqual(box.check_boxes(boxes), [])
        self.assertEqual(box.check_boxes(boxes, NETWORK), [])

    def test_duplicate_address_refused(self):
        boxes = [("g.json", _box(0, "gateway")), ("a.json", _box(4)), ("b.json", _box(4))]
        self.assertEqual(box.check_boxes(boxes), ["address 4: in a.json, b.json"])

    def test_two_gateways_refused(self):
        boxes = [("g.json", _box(0, "gateway")), ("h.json", _box(0, "gateway"))]
        errors = box.check_boxes(boxes)
        self.assertTrue("gateway: 2 boxes (g.json, h.json), exactly one expected" in errors)

    def test_no_gateway_refused(self):
        errors = box.check_boxes([("a.json", _box(1))])
        self.assertEqual(errors, ["gateway: 0 boxes (none), exactly one expected"])

    def test_box_problems_named_by_file(self):
        boxes = [("g.json", _box(0, "gateway")), ("a.json", _box(1, sf=8))]
        errors = box.check_boxes(boxes, NETWORK, "net.json")
        self.assertEqual(errors, ["a.json: sf: set by the network file, not by a box"])

    def test_duplicate_ssid_refused(self):
        boxes = [
            ("g.json", _box(0, "gateway", wifi={"ap": AP})),
            ("a.json", _box(1, name="relay", wifi={"ap": AP})),
            ("b.json", _box(2, name="relay", wifi={"ap": AP})),
            ("c.json", _box(3, wifi={"ap": AP})),  # SILy-box3
            ("d.json", _box(4, name="relay")),  # no access point
        ]
        for network in (None, NETWORK):
            errors = box.check_boxes(boxes, network, "net.json")
            self.assertEqual(errors, ["SSID SILy-relay: in a.json, b.json"])
        other = dict(AP, prefix="Club")
        boxes[2] = ("b.json", _box(2, name="relay", wifi={"ap": other}))
        self.assertEqual(box.check_boxes(boxes), [])

    def test_boxes_checked_without_the_network_file(self):
        network_file = dict(NETWORK, presence_s=30)  # given as a box by mistake
        boxes = [
            ("g.json", _box(0, "gateway")),
            ("net.json", network_file),
            ("x.json", {"address": 3, "colour": "red"}),
        ]
        fields = ("iterations", "network", "password", "presence_s", "sf")
        self.assertEqual(
            box.check_boxes(boxes),
            ["net.json: %s: set by the network file, not by a box" % f for f in fields]
            + ["x.json: role: missing", "x.json: colour: unknown key"],
        )

    def test_chain_with_drop_rates(self):
        # Three boards in range of each other forced into a chain 0 - 2 - 1,
        # the relay losing 30 % of the gateway's frames.
        boxes = [
            ("g.json", _box(0, "gateway", allow=[2])),
            ("r.json", _box(2, allow=[0, 1], drop={"0": 30})),
            ("n.json", _box(1, allow=[2])),
        ]
        for network in (None, NETWORK):
            self.assertEqual(box.check_boxes(boxes, network, "net.json"), [])
        boxes[1] = ("r.json", _box(2, allow=[0, 1], drop={"0": 130, "3": 10, "01": 5}))
        for network in (None, NETWORK):
            self.assertEqual(
                box.check_boxes(boxes, network, "net.json"),
                [
                    "r.json: drop.0: out of range 0 to 100",
                    "r.json: drop.01: an address 0 to 65534 in decimal expected",
                    "r.json: drop.3: not in allow (never heard)",
                ],
            )

    def test_board_file_keeps_the_drop_rates(self):
        cfg, errors = box.merge(NETWORK, _box(2, drop={"0": 30, "1": 0}))
        self.assertEqual(errors, [])
        board, errors = config.load(json.dumps(cfg))  # as written to /config.json
        self.assertEqual(errors, [])
        self.assertEqual(config.drop_rates(board["drop"]), {0: 30, 1: 0})

    def test_network_problem_reported_once(self):
        network = dict(NETWORK)
        del network["password"]
        boxes = [("g.json", _box(0, "gateway")), ("a.json", _box(1))]
        errors = box.check_boxes(boxes, network, "net.json")
        self.assertEqual(errors, ["net.json: password: missing"])


class Templates(unittest.TestCase):
    def _load(self, name):
        obj, errors = box.load_json("tools/" + name)
        self.assertEqual(errors, [])
        return obj

    def test_template_password_refused(self):
        network = self._load("network.example.json")
        cfg, errors = box.merge(network, self._load("box.example.json"), "net.json")
        self.assertIsNone(cfg)
        self.assertEqual(
            errors,
            ["net.json: password: still the template's", "wifi.ap.password: still the template's"],
        )

    def test_template_ap_password_refused(self):
        template = self._load("box.example.json")
        cfg, errors = box.merge(NETWORK, template, "net.json")
        self.assertIsNone(cfg)
        self.assertEqual(errors, ["wifi.ap.password: still the template's"])
        located = box._located(errors, "net.json", "relay1.json")
        self.assertEqual(located, ["relay1.json: wifi.ap.password: still the template's"])
        boxes = [("g.json", _box(0, "gateway")), ("relay1.json", template)]
        for network in (None, NETWORK):
            self.assertEqual(
                box.check_boxes(boxes, network, "net.json"),
                ["relay1.json: wifi.ap.password: still the template's"],
            )

    def test_templates_merge_once_the_password_is_set(self):
        network = self._load("network.example.json")
        network["password"] = "secret words"
        network["iterations"] = 10
        template = self._load("box.example.json")
        template["wifi"]["ap"]["password"] = "ap-words-7731"
        cfg, errors = box.merge(network, template)
        self.assertEqual(errors, [])
        self.assertEqual(cfg["role"], "node")
        ap = cfg["wifi"]["ap"]  # on by default
        self.assertEqual(config.ap_ssid(ap["prefix"], cfg["name"]), "SILy-relay1")


class Push(unittest.TestCase):
    def test_copy_then_guard_cleared_then_rtc_read(self):
        cmd = box.push_commands("mpremote", "/dev/ttyACM0", "/tmp/x/config.json")
        self.assertEqual(cmd[:3], ["mpremote", "connect", "/dev/ttyACM0"])
        steps = " ".join(cmd[3:]).split(" + ")
        self.assertEqual(
            steps,
            [
                "fs cp /tmp/x/config.json :config.json",
                "exec from sily import hw; hw.clear_guard()",
                "exec " + box.read_code(),
            ],
        )

    def test_rtc_set_relative_then_read_then_reset(self):
        cmd = box.clock_commands("mpremote", "/dev/ttyACM0", -1234)
        self.assertEqual(
            cmd,
            [
                "mpremote",
                "connect",
                "/dev/ttyACM0",
                "resume",
                "exec",
                "from sily import hw; hw.set_utc(hw.utc_ms() + (-1234))",
                "+",
                "exec",
                box.read_code(),
                "+",
                "reset",
            ],
        )

    def test_read_code_prints_the_readings(self):
        lines = []
        sleeps = []

        class Hw:
            t = T0

            @classmethod
            def utc_ms(cls):
                cls.t += 1
                return cls.t

        class Time:
            sleep_ms = sleeps.append

        code = box.read_code(3, 37)
        for head in ("from sily import hw\n", "import time\n"):
            self.assertTrue(code.startswith(head), code)
            code = code[len(head) :]
        out = {"hw": Hw, "time": Time, "print": lambda *a: lines.append(" ".join(map(str, a)))}
        exec(code, out)
        self.assertEqual([box.reading(line) for line in lines], [T0 + 1, T0 + 2, T0 + 3])
        self.assertEqual(sleeps, [37, 37, 37])
        self.assertIsNone(box.reading("cp /tmp/x/config.json :config.json"))
        self.assertIsNone(box.reading("RTC not a number"))


T0 = 1763460000000  # 2025-11-18 10:00:00 UTC


class Board:
    """A board's RTC, error ms off the host's clock, read through mpremote:
    each reading's line reaches the host delays[i] ms after the board read it.
    ``set`` False: the board ignores the setting."""

    def __init__(self, error, delays, set=True, status=0):
        self.error = error
        self.delays = delays
        self.set = set
        self.status = status
        self.commands = []
        self.now = T0  # the host's clock

    def readings(self):
        pairs = []
        for d in self.delays:
            pairs.append((self.now + d, self.now + self.error))
            self.now += box.READING_GAP_MS
        return pairs

    def __call__(self, cmd):
        self.commands.append(cmd)
        if self.status:
            return self.status, []
        if "resume" in cmd:
            code = cmd[cmd.index("exec") + 1]
            delta = int(code.split("+ (")[1].split(")")[0])
            if self.set:
                self.error += delta
            self.now += 1500  # the second call's start
        return 0, self.readings()


class Rtc(unittest.TestCase):
    def configure(self, board):
        self.lines = []
        return box.configure("mpremote", "/dev/x", "/tmp/c.json", board, self.lines.append)

    def test_set_to_the_host_within_the_shortest_transfer(self):
        delays = [9, 4, 12, 2, 7, 11, 3, 6]
        board = Board(946684805000 - T0, delays)  # not set since a power cut: 2000-01-01
        self.assertEqual(self.configure(board), 0)
        self.assertEqual(len(board.commands), 2)
        self.assertEqual(board.commands[1][-1], "reset")
        # Ahead of the host by the shortest transfer of the first readings.
        self.assertEqual(board.error, min(delays))
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(
            self.lines[0].startswith("rtc: read 2000-01-01 00:00:05.000 UTC"), self.lines
        )
        self.assertTrue("check: +0 ms" in self.lines[0], self.lines)

    def test_a_board_close_to_the_host(self):
        board = Board(250, [5, 3, 8])
        self.assertEqual(self.configure(board), 0)
        self.assertEqual(board.error, 3)
        self.assertTrue("(-247 ms)" in self.lines[0], self.lines)

    def test_setting_ignored_fails(self):
        board = Board(-60000, [2, 2, 2], set=False)
        self.assertEqual(self.configure(board), 1)
        self.assertTrue("check: +60002 ms" in self.lines[0], self.lines)
        self.assertTrue(self.lines[1].startswith("rtc: check beyond 10 ms"), self.lines)

    def test_no_reading_no_setting(self):
        board = Board(0, [])
        self.assertEqual(self.configure(board), 1)
        self.assertEqual(len(board.commands), 1)
        self.assertEqual(self.lines, ["rtc: no reading from the board: not set, not reset"])

    def test_failed_copy_stops(self):
        board = Board(0, [1], status=2)
        self.assertEqual(self.configure(board), 2)
        self.assertEqual(len(board.commands), 1)
