"""Frame and message codec (MESH_DESIGN §11, §14 boundary)."""

import unittest

from mesh import frame, ports, profile
from mesh.airtime import time_on_air_us
from mesh.frame import (
    BROADCAST,
    DOWNWARD,
    FIRST_HOP,
    LENGTH,
    LINK_LOCAL,
    MALFORMED,
    NODE_BROADCAST,
    P2P,
    ROOT_BROADCAST,
    TRUNCATED,
    UPWARD,
    VERSION,
    FormatError,
    Header,
    Message,
    decode,
    decode_header,
    decode_messages,
    encode,
    frame_limit,
    frame_size,
)
from sim.prng import XorShift32


def _header(**kw):
    fields = {"dest": 0x1B07, "source": 0x3F2A, "counter": 0x123456, "rank": 24}
    fields.update(kw)
    return Header(**fields)


def _encode(messages, header=None, limit=frame.MAX_FRAME):
    buf = bytearray(frame.MAX_FRAME)
    n = encode(buf, header or _header(), messages, limit)
    return bytes(buf[:n])


def _reason(fn, *args):
    # The FormatError reason raised by fn(*args); any other exception propagates.
    try:
        fn(*args)
    except FormatError as e:
        return e.reason
    return None


def _body_reason(body):
    return _reason(decode_messages, body, 0, len(body))


def _presence(origin):
    return Message(UPWARD, 0, ports.PRESENCE, bytes((0x07, 0x1B, 0x00, origin & 0xFF)), origin)


def _punch(origin, seq):
    return Message(UPWARD, 2, ports.APP_HIGH, bytes(range(10)), origin, seq=seq)


class Sizes(unittest.TestCase):
    """§11 "Sizes (SILy cases)": exact length, then decode gives them back."""

    def _round_trip(self, messages, expected):
        self.assertEqual(frame_size(messages), expected)
        data = _encode(messages)
        self.assertEqual(len(data), expected)
        header, decoded = decode(data, len(data))
        self.assertEqual(header, _header())
        self.assertEqual(decoded, messages)
        return data

    def test_presence_first_hop(self):
        self._round_trip([Message(FIRST_HOP, 0, ports.PRESENCE, b"\x00\x2b")], 17)

    def test_thirty_presence_reports_bundled(self):
        data = self._round_trip([_presence(0x100 + i) for i in range(30)], 197)
        # One shared header: control (mode 1, bundle, last), port, length 4, count 30.
        self.assertEqual(data[9:13], bytes((0x23, ports.PRESENCE, 4, 30)))

    def test_topology_six_neighbours(self):
        self._round_trip([Message(FIRST_HOP, 0, ports.TOPOLOGY, b"\x06" + bytes(24))], 40)

    def test_punch_class_2(self):
        self._round_trip([Message(FIRST_HOP, 2, ports.APP_HIGH, bytes(10), seq=0x0102)], 27)

    def test_seventeen_punches_bundled(self):
        self._round_trip([_punch(0x200 + i, 1000 + i) for i in range(17)], 255)

    def test_root_acknowledgement(self):
        self._round_trip([Message(LINK_LOCAL, 0, ports.ACK, b"\x2a\x3f\x34\x12")], 19)

    def test_class2_nack_downward_depth_3(self):
        # 13 + 2 + 2 + 5 + payload: destination, routing header of 2 hops.
        for payload in (b"\x00\x10\x05\x00", b"\x00\x10\x05\x00\x07\x00\x02"):
            msg = Message(
                DOWNWARD, 0, ports.CLASS2_CONTROL, payload, dest=0x0C01, route=(0x0A01, 0x0B01)
            )
            self._round_trip([msg], 22 + len(payload))

    def test_maximum_application_payload(self):
        # 255 - 13 - 23 (P2P, last, class 2, 7-hop routing header) = 219 B.
        route = tuple(range(0x0101, 0x0108))
        msg = Message(P2P, 2, 200, bytes(219), 0x3F2A, 0x1B07, 0xFFFF, route)
        self._round_trip([msg], 255)
        msg.payload = bytes(220)
        self.assertRaises(ValueError, _encode, [msg])


class Example(unittest.TestCase):
    def test_presence_of_3f2a_to_1b07_byte_for_byte(self):
        # §11 example, DODAG version 3, counter 0x0A0B0C, rank 24, battery 0x5C.
        header = Header(0x1B07, 0x3F2A, 0x0A0B0C, 24, dodag_version=3)
        msg = Message(FIRST_HOP, 0, ports.PRESENCE, b"\x00\x5c")
        data = _encode([msg], header)
        expected = bytes(
            (0x40 + 2 * 3, 0x07, 0x1B, 0x2A, 0x3F, 0x0C, 0x0B, 0x0A, 24, 0xC1, 0x01, 0x00, 0x5C)
        ) + bytes(4)
        self.assertEqual(data, expected)

    def test_first_relay_restores_the_origin(self):
        data = _encode([Message(FIRST_HOP, 0, ports.PRESENCE, b"\x00\x5c")])
        header, (msg,) = decode(data, len(data))
        self.assertIs(frame.expand_first_hop(msg, header.source), msg)
        self.assertEqual(msg, Message(UPWARD, 0, ports.PRESENCE, b"\x00\x5c", 0x3F2A))
        other = Message(DOWNWARD, 0, 9, b"", dest=5)
        frame.expand_first_hop(other, 0x3F2A)
        self.assertEqual(other, Message(DOWNWARD, 0, 9, b"", dest=5))


class Versions(unittest.TestCase):
    def _with_byte0(self, value):
        data = bytearray(_encode([Message(LINK_LOCAL, 0, ports.ACK, b"\x01\x02\x03\x04")]))
        data[0] = value
        return data

    def test_only_version_1_accepted(self):
        for version in (0, 2, 3):
            for low in (0x00, 0x2A, 0x3F):
                data = self._with_byte0(version << 6 | low)
                self.assertEqual(_reason(decode_header, data, len(data)), VERSION)
                self.assertEqual(_reason(decode, data, len(data)), VERSION)
        data = self._with_byte0(0x40)
        self.assertEqual(decode_header(data, len(data)).dodag_version, 0)

    def test_escape_rejected_whatever_follows(self):
        # Escape (3): an extended version byte follows byte 0; not supported.
        data = self._with_byte0(0xC0)
        for ext in (0, 1, 4, 255):
            data[1] = ext
            self.assertEqual(_reason(decode_header, data, len(data)), VERSION)

    def test_version_byte_fields(self):
        data = self._with_byte0(0x40 | 1 << 5 | 15 << 1)
        h = decode_header(data, len(data))
        self.assertEqual((h.epoch, h.dodag_version), (1, 15))
        h = decode_header(self._with_byte0(0x40 | 7 << 1), len(data))
        self.assertEqual((h.epoch, h.dodag_version), (0, 7))

    def test_reserved_bit_sent_zero_and_ignored(self):
        for epoch in (0, 1):
            for version in (0, 9, 15):
                data = _encode([], _header(epoch=epoch, dodag_version=version))
                self.assertEqual(data[0], 0x40 | epoch << 5 | version << 1)
                flipped = bytearray(data)
                flipped[0] |= 1
                self.assertEqual(decode_header(flipped, 13), decode_header(data, 13))


class Limits(unittest.TestCase):
    def test_frame_length_bounds_on_decode(self):
        buf = bytearray(_encode([Message(LINK_LOCAL, 0, 9, bytes(240))]))
        self.assertEqual(len(buf), 255)
        self.assertEqual(decode_header(buf, 255).source, 0x3F2A)
        self.assertEqual(decode_header(buf, 13).source, 0x3F2A)
        long_buf = buf + b"\x00"
        for length in (0, 1, 12):
            self.assertEqual(_reason(decode_header, buf, length), LENGTH)
        self.assertEqual(_reason(decode_header, long_buf, 256), LENGTH)
        self.assertEqual(_reason(decode_header, buf[:20], 21), LENGTH)

    def test_255_bytes_enforced_on_encode(self):
        self.assertEqual(len(_encode([Message(LINK_LOCAL, 0, 9, bytes(240))])), 255)
        self.assertRaises(ValueError, _encode, [Message(LINK_LOCAL, 0, 9, bytes(241))])
        big = bytearray(300)
        self.assertRaises(ValueError, encode, big, _header(), [Message(0, 0, 9, bytes(241))])
        small = bytearray(20)
        self.assertRaises(ValueError, encode, small, _header(), [Message(0, 0, 9, bytes(8))])
        self.assertEqual(encode(small, _header(), [Message(0, 0, 9, bytes(5))]), 20)

    def test_no_cap_without_maximum_transmission_time(self):
        self.assertEqual(frame_limit(), 255)
        eu = profile.get(profile.EU_869)
        for sf in range(7, 13):
            self.assertEqual(frame_limit(sf, eu), 255)

    def _capped(self, max_tx_ms):
        return profile.Profile(
            2, "capped", 920000000, 125000, 5, 1300, profile.EIRP, 10000, max_tx_ms=max_tx_ms
        )

    def test_maximum_time_on_air_caps_the_frame(self):
        # 255 B last 399.616 ms at SF7 (§16): a 400 ms cap keeps 255 B.
        self.assertEqual(frame_limit(7, self._capped(400)), 255)
        for sf, cap in ((7, 399), (7, 100), (9, 400), (12, 4000), (10, 300)):
            p = self._capped(cap)
            n = frame_limit(sf, p)
            self.assertTrue(13 <= n < 255)
            self.assertTrue(time_on_air_us(n, sf) <= cap * 1000)
            self.assertTrue(time_on_air_us(n + 1, sf) > cap * 1000)

    def test_cap_enforced_on_encode(self):
        limit = frame_limit(9, self._capped(400))
        fits = [Message(LINK_LOCAL, 0, 9, bytes(limit - 15))]
        self.assertEqual(len(_encode(fits, limit=limit)), limit)
        too_long = [Message(LINK_LOCAL, 0, 9, bytes(limit - 14))]
        self.assertRaises(ValueError, _encode, too_long, None, limit)
        self.assertEqual(len(_encode(too_long)), limit + 1)

    def test_cap_below_an_empty_frame(self):
        self.assertRaises(ValueError, frame_limit, 12, self._capped(100))


class Boundary(unittest.TestCase):
    """§14 boundary: AAD, encrypted body, tag; nonce fields in the header."""

    def test_regions(self):
        msg = Message(LINK_LOCAL, 0, ports.ACK, b"\x2a\x3f\x34\x12")
        buf = bytearray(255)
        n = encode(buf, _header(), [msg])
        self.assertEqual(bytes(frame.aad(buf)), bytes(buf[:9]))
        self.assertEqual(bytes(frame.body(buf, n)), bytes((0x01, 0x03)) + msg.payload)
        self.assertEqual(bytes(frame.tag(buf, n)), bytes(4))
        self.assertEqual(n, 9 + 6 + 4)
        # The nonce starts with link source then counter, as in the header.
        self.assertEqual(bytes(buf[frame.SOURCE_OFFSET : frame.COUNTER_OFFSET + 3]), b"*?V4\x12")

    def test_tag_zeroed_over_old_content(self):
        buf = bytearray(b"\xff" * 255)
        n = encode(buf, _header(), [Message(FIRST_HOP, 0, 1, b"\x00\x01")])
        self.assertEqual(bytes(buf[n - 4 : n]), bytes(4))
        self.assertEqual(buf[n], 0xFF)


class Fields(unittest.TestCase):
    def test_header_extremes_round_trip(self):
        for h in (
            Header(0, 0, 0, 0, 0, 0),
            Header(0xFFFF, 0xFFFF, 0xFFFFFF, 255, 15, 1),
            Header(BROADCAST, 0x0001, 0x000100, 1, 8, 0),
        ):
            data = _encode([], h)
            self.assertEqual(len(data), 13)
            self.assertEqual(decode(data, 13), (h, []))

    def test_header_out_of_range(self):
        for kw in (
            {"dest": 0x10000},
            {"dest": -1},
            {"source": 0x10000},
            {"counter": 0x1000000},
            {"counter": -1},
            {"rank": 256},
            {"dodag_version": 16},
            {"epoch": 2},
            {"rank": None},
        ):
            self.assertRaises(ValueError, _encode, [], _header(**kw))

    def test_message_extremes_round_trip(self):
        messages = [
            Message(UPWARD, 1, 0, b"\x01", 0, seq=0),
            Message(DOWNWARD, 2, 255, b"", dest=0xFFFF, seq=0xFFFF, route=()),
            Message(P2P, 0, 128, b"\x02\x03", 0xFFFF, 0, route=(0, 0xFFFF)),
            Message(NODE_BROADCAST, 0, 16, b"\x04", 0x1234, seq=0x8000),
            Message(ROOT_BROADCAST, 0, 127, b"\x05\x06\x07", seq=1),
            Message(FIRST_HOP, 1, ports.ERROR, bytes(12), seq=7),
            Message(LINK_LOCAL, 0, ports.DODAG_INFO, b""),
        ]
        data = _encode(messages)
        self.assertEqual(decode(data, len(data))[1], messages)

    def test_message_out_of_range(self):
        bad = (
            Message(7, 0, 1, b""),
            Message(-1, 0, 1, b""),
            Message(LINK_LOCAL, 3, 1, b""),
            Message(LINK_LOCAL, 0, 256, b""),
            Message(LINK_LOCAL, 0, -1, b""),
            Message(UPWARD, 0, 1, b""),  # origin missing
            Message(UPWARD, 0, 1, b"", 0x10000),
            Message(DOWNWARD, 0, 1, b"", dest=-1),
            Message(P2P, 0, 1, b"", 1),  # destination missing
            Message(LINK_LOCAL, 0, 1, b"", origin=1),  # not carried
            Message(FIRST_HOP, 0, 1, b"", origin=1),
            Message(UPWARD, 0, 1, b"", 1, dest=2),
            Message(UPWARD, 1, 1, b"", 1),  # class 1 without sequence
            Message(ROOT_BROADCAST, 0, 1, b""),  # broadcast without sequence
            Message(UPWARD, 2, 1, b"", 1, seq=0x10000),
            Message(UPWARD, 0, 1, b"", 1, seq=5),  # class 0: no sequence
            Message(UPWARD, 0, 1, b"", 1, route=()),  # not a source route
            Message(FIRST_HOP, 0, 1, b"", route=(1,)),
            Message(DOWNWARD, 0, 1, b"", dest=1, route=tuple(range(8))),
            Message(DOWNWARD, 0, 1, b"", dest=1, route=(0x10000,)),
        )
        for msg in bad:
            self.assertRaises(ValueError, frame_size, [msg])
            self.assertRaises(ValueError, _encode, [msg])
        long_payload = Message(LINK_LOCAL, 0, 1, bytes(256))
        self.assertRaises(ValueError, frame_size, [long_payload])

    def test_routes_up_to_seven_hops(self):
        for hops in range(8):
            route = tuple(0x0100 + i for i in range(hops))
            msg = Message(DOWNWARD, 1, 20, b"x", dest=0x0C01, seq=3, route=route)
            data = _encode([msg])
            self.assertEqual(len(data), 13 + 2 + 2 + 2 + 1 + 2 * hops + 1)
            self.assertEqual(decode(data, len(data))[1], [msg])


class Messages(unittest.TestCase):
    def test_control_bytes_and_length_of_non_last(self):
        a = Message(FIRST_HOP, 0, ports.PRESENCE, b"\x00\x01")
        b = Message(FIRST_HOP, 0, ports.TOPOLOGY, b"\x00")
        data = _encode([a, b])
        # a: mode 6, not last, length 2; b: mode 6, last, no length.
        self.assertEqual(data[9:18], b"\xc0\x01\x02\x00\x01\xc1\x02\x00" + b"\x00")
        self.assertEqual(len(data), 9 + 5 + 3 + 4)
        self.assertEqual(decode(data, len(data))[1], [a, b])

    def test_class_and_route_bits(self):
        msg = Message(P2P, 2, 20, b"", 1, 2, 3, (4,))
        data = _encode([msg])
        self.assertEqual(data[9], 3 << 5 | 2 << 3 | 0x04 | 0x01)

    def test_bundles_group_consecutive_compatible_messages(self):
        p = [_presence(i) for i in range(1, 6)]
        odd = Message(UPWARD, 0, ports.PRESENCE, b"\x00\x00\x00", 9)  # other length
        messages = [p[0], p[1], odd, p[2], p[3], p[4]]
        data = _encode(messages)
        # Bundle of 2 (4 + 12), single non-last (3 + 5), bundle of 3, last (4 + 18).
        self.assertEqual(len(data), 13 + 16 + 8 + 22)
        self.assertEqual(data[9:13], bytes((0x22, 1, 4, 2)))
        self.assertEqual(data[25:28], bytes((0x20, 1, 3)))
        self.assertEqual(data[33:37], bytes((0x23, 1, 4, 3)))
        self.assertEqual(decode(data, len(data))[1], messages)

    def test_routed_and_unrouted_not_bundled(self):
        a = Message(DOWNWARD, 0, 20, b"ab", dest=1, route=())
        b = Message(DOWNWARD, 0, 20, b"cd", dest=2)
        data = _encode([a, b, a])
        self.assertEqual(len(data), 13 + (3 + 5) + (3 + 4) + (2 + 5))
        self.assertEqual(decode(data, len(data))[1], [a, b, a])

    def test_bundle_count_split_at_255(self):
        messages = [Message(LINK_LOCAL, 0, 20, b"") for _ in range(300)]
        data = _encode(messages)
        self.assertEqual(data[9:17], bytes((0x02, 20, 0, 255, 0x03, 20, 0, 45)))
        self.assertEqual(len(decode(data, len(data))[1]), 300)

    def test_canonical_reencoding(self):
        messages = [_punch(0x200 + i, i) for i in range(5)] + [_presence(3)]
        data = _encode(messages)
        header, decoded = decode(data, len(data))
        self.assertEqual(_encode(decoded, header), data)

    def test_empty_body(self):
        data = _encode([])
        self.assertEqual(decode(data, 13)[1], [])

    def test_payload_is_a_view_of_the_buffer(self):
        buf = bytearray(_encode([Message(FIRST_HOP, 0, 1, b"\x00\x01")]))
        (msg,) = decode_messages(buf, 9, len(buf) - 4)
        buf[11] = 0x7F
        self.assertEqual(bytes(msg.payload), b"\x7f\x01")

    def test_final_message_without_last_flag_accepted(self):
        body = bytes((0x00, 3, 2, 0xAA, 0xBB))
        self.assertEqual(decode_messages(body, 0, 5), [Message(LINK_LOCAL, 0, 3, b"\xaa\xbb")])


class Malformed(unittest.TestCase):
    def test_reserved_mode_and_class(self):
        self.assertEqual(_body_reason(bytes((0xE1, 1))), MALFORMED)  # mode 7
        self.assertEqual(_body_reason(bytes((0x19, 1))), MALFORMED)  # class 3

    def test_bundle_count_below_two(self):
        for count in (0, 1):
            self.assertEqual(_body_reason(bytes((0x03, 1, 1, count, 0xAA))), MALFORMED)
        self.assertEqual(len(decode_messages(bytes((0x03, 1, 1, 2, 0xAA, 0xBB)), 0, 6)), 2)

    def test_routing_header_outside_source_routes(self):
        for mode in (0, 1, 4, 5, 6):
            body = bytes((mode << 5 | 0x05, 1, 0, 0, 0, 0, 0, 0, 0))
            self.assertEqual(_body_reason(body), MALFORMED)

    def test_route_beyond_seven_hops(self):
        body = bytes((0x45, 1, 0x01, 0x00, 8)) + bytes(16)
        self.assertEqual(_body_reason(body), MALFORMED)
        body = bytes((0x45, 1, 0x01, 0x00, 7)) + bytes(14)
        self.assertEqual(len(decode_messages(body, 0, len(body))[0].route), 7)

    def test_last_bundle_must_end_the_body(self):
        body = bytes((0x03, 1, 1, 2, 0xAA, 0xBB, 0xCC))
        self.assertEqual(_body_reason(body), MALFORMED)

    def test_truncations(self):
        cases = (
            bytes((0x00,)),  # port missing
            bytes((0x00, 1)),  # length missing (not last)
            bytes((0x00, 1, 3, 0xAA, 0xBB)),  # payload short
            bytes((0x02, 1, 2)),  # count missing
            bytes((0x03, 1, 2, 3, 0, 0, 0, 0, 0)),  # bundle short
            bytes((0x21, 1, 0x2A)),  # origin short
            bytes((0x61, 1, 1, 0, 2)),  # P2P destination short
            bytes((0x09, 1, 0x05)),  # sequence short
            bytes((0x45, 1, 1, 0)),  # routing count missing
            bytes((0x45, 1, 1, 0, 2, 1, 0, 2)),  # route short
        )
        for body in cases:
            self.assertEqual(_body_reason(body), TRUNCATED, body)

    def test_every_truncation_of_valid_frames(self):
        samples = (
            [_punch(0x200 + i, i) for i in range(4)] + [_presence(3)],
            [
                Message(P2P, 2, 20, b"abc", 1, 2, 3, (4, 5)),
                Message(NODE_BROADCAST, 0, 21, b"d", 6, seq=7),
                Message(FIRST_HOP, 0, 1, b"\x00\x01"),
            ],
        )
        for messages in samples:
            data = _encode(messages)
            body = data[9:-4]
            for cut in range(len(body)):
                # Only FormatError, or a shorter valid body.
                reason = _body_reason(body[:cut])
                self.assertTrue(reason in (None, TRUNCATED, MALFORMED))
            for cut in range(len(data)):
                self.assertTrue(
                    _reason(decode, data[:cut], cut) in (None, LENGTH, TRUNCATED, MALFORMED)
                )

    def test_random_and_corrupted_input_raises_only_format_error(self):
        rng = XorShift32(11)
        valid = _encode([_punch(0x200 + i, i) for i in range(3)] + [_presence(9)])
        reasons = {}
        for _ in range(3000):
            if rng.chance(0.5):
                data = bytearray(rng.randbytes(rng.getrandbits(8)))
                if data:
                    data[0] = 0x40 | data[0] & 0x3F
            else:
                data = bytearray(valid)
                for _ in range(1 + rng.getrandbits(2)):
                    data[9 + rng.getrandbits(8) % (len(data) - 13)] = rng.getrandbits(8)
            reason = _reason(decode, data, len(data))
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason in (LENGTH, TRUNCATED, MALFORMED, None):
            self.assertTrue(reasons.get(reason, 0) > 0, reason)
