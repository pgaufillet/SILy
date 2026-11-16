"""Host configuration of the boxes (tools/box.py): network checks and templates."""

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
    def test_copy_then_guard_cleared_then_reset(self):
        cmd = box.push_commands("mpremote", "/dev/ttyACM0", "/tmp/x/config.json")
        self.assertEqual(cmd[:3], ["mpremote", "connect", "/dev/ttyACM0"])
        steps = " ".join(cmd[3:]).split(" + ")
        self.assertEqual(
            steps,
            [
                "fs cp /tmp/x/config.json :config.json",
                "exec from sily import hw; hw.clear_guard()",
                "reset",
            ],
        )
