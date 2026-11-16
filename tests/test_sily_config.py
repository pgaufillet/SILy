"""Configuration of a box (sily.config): board file checks and the host merge."""

import json
import unittest

from mesh import kdf
from sily.config import NETWORK_FIELDS, board_config, check, load

KEY = "00112233445566778899aabbccddeeff"
FINGERPRINT = "a1b2c3d4"
ITERATIONS = 10  # small count: the derivation itself is tested in test_kdf


def _minimal(**fields):
    cfg = {
        "version": 1,
        "role": "node",
        "address": 3,
        "key": KEY,
        "fingerprint": FINGERPRINT,
        "sf": 7,
    }
    cfg.update(fields)
    return cfg


def _errors(**fields):
    """Return the errors of the minimal file changed by fields (None removes one)."""
    cfg = _minimal(**fields)
    for key in [k for k, v in cfg.items() if v is None and k in fields]:
        del cfg[key]
    config, errors = check(cfg)
    if errors:
        assert config is None
    return errors


class Defaults(unittest.TestCase):
    def test_minimal_file_gets_its_defaults(self):
        cfg, errors = load(json.dumps(_minimal()))
        self.assertEqual(errors, [])
        self.assertEqual(cfg["name"], "box3")
        self.assertEqual(cfg["network"], "")
        self.assertEqual(cfg["profile"], 1)
        self.assertEqual((cfg["tx_power"], cfg["antenna_gain"]), (22, 0))
        self.assertIsNone(cfg["presence_s"])
        self.assertIsNone(cfg["allow"])
        self.assertTrue(cfg["led"])
        self.assertEqual(cfg["display"], {"mode": "always", "window_min": 10})
        self.assertEqual(cfg["wifi"], {"ap": None, "known": []})
        self.assertEqual(
            cfg["journal"], {"minute_s": 60, "sync_s": 300, "max_kb": 1024, "segment_kb": 64}
        )

    def test_partial_section_keeps_the_other_defaults(self):
        cfg, errors = check(_minimal(journal={"sync_s": 30}, display={"mode": "off"}))
        self.assertEqual(errors, [])
        self.assertEqual(cfg["journal"]["sync_s"], 30)
        self.assertEqual(cfg["journal"]["minute_s"], 60)
        self.assertEqual(cfg["display"], {"mode": "off", "window_min": 10})

    def test_full_gateway_file(self):
        cfg, errors = check(
            _minimal(
                role="gateway",
                address=0,
                name="arena",
                presence_s=30,
                allow=[1, 2],
                led=False,
                wifi={
                    "ap": {"prefix": "SILy", "password": "12345678"},
                    "known": [{"ssid": "club", "password": ""}],
                },
            )
        )
        self.assertEqual(errors, [])
        self.assertEqual(cfg["presence_s"], 30)
        self.assertEqual(cfg["allow"], [1, 2])
        self.assertEqual(cfg["wifi"]["ap"]["prefix"], "SILy")


class Errors(unittest.TestCase):
    """One case per error class: the message names the field."""

    def assertOne(self, errors, prefix):
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(prefix), errors)

    def test_missing_key(self):
        self.assertEqual(_errors(key=None), ["key: missing"])

    def test_unknown_key(self):
        self.assertEqual(_errors(tx_pwr=10), ["tx_pwr: unknown key"])

    def test_sf_as_a_string(self):
        self.assertEqual(_errors(sf="8"), ["sf: an integer expected"])

    def test_sf_out_of_range(self):
        self.assertEqual(_errors(sf=13), ["sf: out of range 7 to 12"])

    def test_boolean_is_not_an_integer(self):
        self.assertOne(_errors(sf=True), "sf: an integer expected")

    def test_broadcast_address(self):
        self.assertEqual(_errors(address=65535), ["address: out of range 0 to 65534"])

    def test_tx_power_above_the_radio(self):
        self.assertEqual(_errors(tx_power=23), ["tx_power: out of range -9 to 22"])

    def test_power_limit_with_antenna_gain(self):
        # 22 dBm + 8 dBi - 2.15 dB = 27.85 dBm ERP, above 26.99.
        self.assertOne(_errors(antenna_gain=8), "tx_power: 22 dBm with antenna_gain 8 dBi above")
        self.assertEqual(_errors(antenna_gain=7, tx_power=22), [])

    def test_short_key(self):
        self.assertEqual(_errors(key=KEY[:31]), ["key: 32 hexadecimal characters expected"])

    def test_key_not_hexadecimal(self):
        self.assertOne(_errors(fingerprint="a1b2c3dz"), "fingerprint: 8 hexadecimal")

    def test_node_at_the_gateway_address(self):
        self.assertOne(_errors(address=0), "address: 0 is the gateway's")

    def test_gateway_elsewhere_than_0(self):
        self.assertOne(_errors(role="gateway", address=3), "role: a gateway has address 0")

    def test_unknown_role(self):
        self.assertOne(_errors(role="relay"), "role: one of gateway, node expected")

    def test_presence_period_on_a_node(self):
        self.assertOne(_errors(presence_s=30), "presence_s: gateway only")

    def test_allow_as_a_string(self):
        self.assertEqual(_errors(allow="1,2"), ["allow: a list expected"])

    def test_allow_item(self):
        self.assertEqual(_errors(allow=[1, 70000]), ["allow[1]: out of range 0 to 65534"])

    def test_display_mode(self):
        self.assertOne(_errors(display={"mode": "blink"}), "display.mode: one of off")

    def test_short_access_point_password(self):
        errors = _errors(wifi={"ap": {"prefix": "SILy", "password": "1234567"}})
        self.assertEqual(errors, ["wifi.ap.password: 8 to 63 characters expected"])

    def test_known_network_without_ssid(self):
        errors = _errors(wifi={"known": [{"password": ""}]})
        self.assertEqual(errors, ["wifi.known[0].ssid: missing"])

    def test_ssid_too_long(self):
        errors = _errors(name="a" * 16, wifi={"ap": {"prefix": "p" * 16, "password": "x" * 8}})
        self.assertOne(errors, "wifi.ap.prefix: SSID")

    def test_name_too_long_in_bytes(self):
        self.assertOne(_errors(name="é" * 9), "name: 1 to 16 bytes expected")

    def test_unknown_section_key(self):
        self.assertEqual(_errors(journal={"minute": 60}), ["journal.minute: unknown key"])

    def test_segment_above_the_bound(self):
        self.assertOne(_errors(journal={"max_kb": 32, "segment_kb": 64}), "journal.segment_kb")

    def test_unknown_profile(self):
        self.assertOne(_errors(profile=0xFF), "profile: unknown")

    def test_wrong_version(self):
        self.assertEqual(_errors(version=2), ["version: 1 expected"])

    def test_bad_json(self):
        cfg, errors = load('{"role": "node",')
        self.assertIsNone(cfg)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("file: not valid JSON"), errors)

    def test_not_an_object(self):
        self.assertEqual(load("[1]"), (None, ["file: a JSON object expected"]))

    def test_two_faults_two_messages(self):
        errors = _errors(sf=13, tx_pwr=1)
        self.assertEqual(errors, ["sf: out of range 7 to 12", "tx_pwr: unknown key"])


def _network(**fields):
    net = {"password": "correct horse", "network": "club", "iterations": ITERATIONS, "sf": 9}
    net.update(fields)
    return net


class Merge(unittest.TestCase):
    def test_key_and_fingerprint_derived_without_the_password(self):
        cfg, errors = board_config(_network(), {"role": "node", "address": 4})
        self.assertEqual(errors, [])
        keys = kdf.derive("correct horse", "club", ITERATIONS)
        self.assertEqual(cfg["key"], keys.frame_key.hex())
        self.assertEqual(cfg["fingerprint"], keys.fingerprint.hex())
        self.assertEqual((cfg["sf"], cfg["network"], cfg["version"]), (9, "club", 1))
        text = json.dumps(cfg)
        self.assertFalse("password" in text)
        self.assertFalse("correct horse" in text)
        self.assertFalse("iterations" in text)
        self.assertEqual(load(text), (cfg, []))

    def test_network_name_changes_the_key(self):
        a, _ = board_config(_network(), {"role": "node", "address": 4})
        b, _ = board_config(_network(network="other"), {"role": "node", "address": 4})
        self.assertNotEqual(a["key"], b["key"])

    def test_presence_period_for_the_gateway_only(self):
        net = _network(presence_s=20)
        gateway, errors = board_config(net, {"role": "gateway", "address": 0})
        self.assertEqual(errors, [])
        self.assertEqual(gateway["presence_s"], 20)
        node, errors = board_config(net, {"role": "node", "address": 1})
        self.assertEqual(errors, [])
        self.assertIsNone(node["presence_s"])

    def test_box_overriding_a_network_field_refused(self):
        cfg, errors = board_config(_network(), {"role": "node", "address": 4, "sf": 7})
        self.assertIsNone(cfg)
        self.assertEqual(errors, ["sf: set by the network file, not by a box"])

    def test_box_cannot_bring_a_key(self):
        _, errors = board_config(_network(), {"role": "node", "address": 4, "key": KEY})
        self.assertEqual(errors, ["key: set by the network file, not by a box"])

    def test_every_network_field_refused_in_a_box(self):
        for field in NETWORK_FIELDS:
            _, errors = board_config(_network(), {"role": "node", "address": 4, field: 1})
            self.assertEqual(len(errors), 1, field)

    def test_network_file_errors(self):
        net = _network(iterations="many")
        del net["password"]
        _, errors = board_config(net, {"role": "node", "address": 4})
        self.assertEqual(
            errors,
            [
                "network file: password: missing",
                "network file: iterations: an integer expected",
            ],
        )

    def test_box_errors_reported_after_the_merge(self):
        _, errors = board_config(_network(), {"role": "node", "address": 4, "tx_power": 30})
        self.assertEqual(errors, ["tx_power: out of range -9 to 22"])

    def test_default_iteration_count(self):
        seen = []

        def derive(password, network, iterations):
            seen.append(iterations)
            return kdf.derive(password, network, ITERATIONS)

        net = _network()
        del net["iterations"]
        board_config(net, {"role": "node", "address": 4}, derive)
        self.assertEqual(seen, [kdf.DEFAULT_ITERATIONS])
