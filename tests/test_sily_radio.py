"""Binding of the SX1262 adapter to the mesh radio contract (sily.radio)."""

import unittest

from mesh import profile as profiles
from mesh.frame import HEADER_SIZE, Header, encode_header
from mesh.radio import CHANNEL, RX, RX_ERROR, SENSE_ACTION, TX, TX_DONE, execute
from sily.radio import PREAMBLE, SYNC_WORD, MeshRadio, link_source, lora_config


class FakeAdapter:
    """The callback API of board.sx1262.Sx1262, scripted."""

    def __init__(self):
        self.on_receive = None
        self.on_crc_error = None
        self.on_transmitted = None
        self.sent = []
        self.channel_busy = False

    def transmit(self, frame):
        self.sent.append(frame)

    def busy(self):
        return self.channel_busy


class Events(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter()
        self.radio = MeshRadio(self.adapter)
        self.events = []
        self.radio.sink = self.events.append

    def test_reception_becomes_rx(self):
        self.adapter.on_receive(b"frame", -50, 9.25)
        self.assertEqual(self.events, [(RX, b"frame", -50, 9.25)])

    def test_crc_error_becomes_rx_error(self):
        self.adapter.on_crc_error(-120, -11.5)
        self.assertEqual(self.events, [(RX_ERROR, -120, -11.5)])

    def test_transmission_end_becomes_tx_done(self):
        self.adapter.on_transmitted(78)
        self.assertEqual(self.events, [(TX_DONE, 78)])

    def test_tx_action_reaches_the_adapter(self):
        self.assertTrue(execute(self.radio, (TX, b"abc")))
        self.assertEqual(self.adapter.sent, [b"abc"])
        self.assertEqual(self.events, [])

    def test_sense_reports_the_channel(self):
        execute(self.radio, SENSE_ACTION)
        self.adapter.channel_busy = True
        execute(self.radio, SENSE_ACTION)
        self.assertEqual(self.events, [(CHANNEL, False), (CHANNEL, True)])


class Config(unittest.TestCase):
    def test_eu_profile(self):
        cfg = lora_config(profiles.get(profiles.EU_869), 9, 14)
        self.assertEqual(
            cfg,
            {
                "freq_khz": 869525,
                "sf": 9,
                "bw": "125",
                "coding_rate": 5,
                "preamble_len": 8,
                "syncword": 0x1424,
                "output_power": 14,
            },
        )
        self.assertEqual(type(cfg["freq_khz"]), int)

    def test_parameters_of_the_design(self):
        self.assertEqual(PREAMBLE, 8)
        self.assertEqual(SYNC_WORD, 0x1424)

    def test_custom_bandwidth_and_fractional_khz(self):
        p = profiles.custom(868100500, 62500, 6, 100)
        cfg = lora_config(p, 7, 0)
        self.assertEqual(cfg["bw"], "62.5")
        self.assertEqual(cfg["coding_rate"], 6)
        self.assertAlmostEqual(cfg["freq_khz"], 868100.5, delta=0.1)

    def test_unknown_bandwidth_refused(self):
        p = profiles.custom(868100000, 100000, 5, 100)
        with self.assertRaises(ValueError):
            lora_config(p, 7, 0)


class LinkSource(unittest.TestCase):
    def test_source_of_an_encoded_header(self):
        buf = bytearray(HEADER_SIZE)
        encode_header(buf, Header(0xFFFF, 0x1234, 0xABCDEF, 7))
        self.assertEqual(link_source(buf), 0x1234)

    def test_short_frame(self):
        self.assertIsNone(link_source(b"\x40\xff\xff\x34"))
        self.assertEqual(link_source(b"\x40\xff\xff\x34\x12"), 0x1234)
