"""Host configuration of the boxes (tools/box.py): network checks and templates."""

import unittest

from tools import box

NETWORK = {"password": "secret words", "network": "club", "iterations": 10, "sf": 7}


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
        self.assertEqual(errors, ["net.json: password: still the template's"])

    def test_templates_merge_once_the_password_is_set(self):
        network = self._load("network.example.json")
        network["password"] = "secret words"
        network["iterations"] = 10
        cfg, errors = box.merge(network, self._load("box.example.json"))
        self.assertEqual(errors, [])
        self.assertEqual(cfg["role"], "node")


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
