"""AES-CCM against NIST SP 800-38C and RFC 3610 vectors (MESH_DESIGN §14)."""

import unittest
from binascii import unhexlify as h

from mesh.ccm import Ccm

# NIST SP 800-38C, Appendix C: key 40..4f; (nonce, associated data,
# plaintext, tag length, ciphertext || tag). Example 4 has 2^16 bytes of
# associated data (the 6-byte length encoding).
_NIST_KEY = "404142434445464748494a4b4c4d4e4f"
_NIST = (
    ("10111213141516", bytes(range(8)), bytes(range(0x20, 0x24)), 4, "7162015b4dac255d"),
    (
        "1011121314151617",
        bytes(range(16)),
        bytes(range(0x20, 0x30)),
        6,
        "d2a1f0e051ea5f62081a7792073d593d1fc64fbfaccd",
    ),
    (
        "101112131415161718191a1b",
        bytes(range(20)),
        bytes(range(0x20, 0x38)),
        8,
        "e3b201a9f5b71a7a9b1ceaeccd97e70b6176aad9a4428aa5484392fbc1b09951",
    ),
    (
        "101112131415161718191a1b1c",
        bytes(range(256)) * 256,
        bytes(range(0x20, 0x40)),
        14,
        "69915dad1e84c6376a68c2967e4dab615ae0fd1faec44cc484828529463ccf72"
        "b4ac6bec93e8598e7f0dadbcea5b",
    ),
)

# RFC 3610 packet vectors (13-byte nonce, L = 2): (nonce, cleartext header
# length, input packet, output packet), grouped by key and tag length M.
_RFC3610_KEY1 = "c0c1c2c3c4c5c6c7c8c9cacbcccdcecf"
_RFC3610_KEY2 = "d7828d13b2b0bdc325a76236df93cc6b"
_RFC3610_KEY1_M8 = (
    # #1
    (
        "00000003020100a0a1a2a3a4a5",
        8,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e",
        "0001020304050607588c979a61c663d2f066d0c2c0f989806d5f6b61dac38417e8d12cfdf926e0",
    ),
    # #2
    (
        "00000004030201a0a1a2a3a4a5",
        8,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
        "000102030405060772c91a36e135f8cf291ca894085c87e3cc15c439c9e43a3ba091d56e10400916",
    ),
    # #3
    (
        "00000005040302a0a1a2a3a4a5",
        8,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20",
        "000102030405060751b1e5f44a197d1da46b0f8e2d282ae871e838bb64da8596574adaa76fbd9fb0c5",
    ),
    # #4
    (
        "00000006050403a0a1a2a3a4a5",
        12,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e",
        "000102030405060708090a0ba28c6865939a9a79faaa5c4c2a9d4a91cdac8c96c861b9c9e61ef1",
    ),
    # #5
    (
        "00000007060504a0a1a2a3a4a5",
        12,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
        "000102030405060708090a0bdcf1fb7b5d9e23fb9d4e131253658ad86ebdca3e51e83f077d9c2d93",
    ),
    # #6
    (
        "00000008070605a0a1a2a3a4a5",
        12,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20",
        "000102030405060708090a0b6fc1b011f006568b5171a42d953d469b2570a4bd87405a0443ac91cb94",
    ),
)

_RFC3610_KEY1_M10 = (
    # #7
    (
        "00000009080706a0a1a2a3a4a5",
        8,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e",
        "00010203040506070135d1b2c95f41d5d1d4fec185d166b8094e999dfed96c048c56602c97acbb7490",
    ),
    # #8
    (
        "0000000a090807a0a1a2a3a4a5",
        8,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
        "00010203040506077b75399ac0831dd2f0bbd75879a2fd8f6cae6b6cd9b7db24c17b4433f434963f34b4",
    ),
    # #9
    (
        "0000000b0a0908a0a1a2a3a4a5",
        8,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20",
        "000102030405060782531a60cc24945a4b8279181ab5c84df21ce7f9b73f42e197ea9c07e56b5eb17e5f4e",
    ),
    # #10
    (
        "0000000c0b0a09a0a1a2a3a4a5",
        12,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e",
        "000102030405060708090a0b07342594157785152b074098330abb141b947b566aa9406b4d999988dd",
    ),
    # #11
    (
        "0000000d0c0b0aa0a1a2a3a4a5",
        12,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
        "000102030405060708090a0b676bb20380b0e301e8ab79590a396da78b834934f53aa2e9107a8b6c022c",
    ),
    # #12
    (
        "0000000e0d0c0ba0a1a2a3a4a5",
        12,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20",
        "000102030405060708090a0bc0ffa0d6f05bdb67f24d43a4338d2aa4bed7b20e43cd1aa31662e7ad65d6db",
    ),
)

_RFC3610_KEY2_M8 = (
    # #13
    (
        "00412b4ea9cdbe3c9696766cfa",
        8,
        "0be1a88bace018b108e8cf97d820ea258460e96ad9cf5289054d895ceac47c",
        "0be1a88bace018b14cb97f86a2a4689a877947ab8091ef5386a6ffbdd080f8e78cf7cb0cddd7b3",
    ),
    # #14
    (
        "0033568ef7b2633c9696766cfa",
        8,
        "63018f76dc8a1bcb9020ea6f91bdd85afa0039ba4baff9bfb79c7028949cd0ec",
        "63018f76dc8a1bcb4ccb1e7ca981befaa0726c55d378061298c85c92814abc33c52ee81d7d77c08a",
    ),
    # #15
    (
        "00103fe41336713c9696766cfa",
        8,
        "aa6cfa36cae86b40b916e0eacc1c00d7dcec68ec0b3bbb1a02de8a2d1aa346132e",
        "aa6cfa36cae86b40b1d23a2220ddc0ac900d9aa03c61fcf4a559a4417767089708a776796edb723506",
    ),
    # #16
    (
        "00764c63b8058e3c9696766cfa",
        12,
        "d0d0735c531e1becf049c24412daac5630efa5396f770ce1a66b21f7b2101c",
        "d0d0735c531e1becf049c24414d253c3967b70609b7cbb7c499160283245269a6f49975bcadeaf",
    ),
    # #17
    (
        "00f8b678094e3b3c9696766cfa",
        12,
        "77b60f011c03e1525899bcaee88b6a46c78d63e52eb8c546efb5de6f75e9cc0d",
        "77b60f011c03e1525899bcae5545ff1a085ee2efbf52b2e04bee1e2336c73e3f762c0c7744fe7e3c",
    ),
    # #18
    (
        "00d560912d3f703c9696766cfa",
        12,
        "cd9044d2b71fdb8120ea60c06435acbafb11a82e2f071d7ca4a5ebd93a803ba87f",
        "cd9044d2b71fdb8120ea60c0009769ecabdf48625594c59251e6035722675e04c847099e5ae0704551",
    ),
)

_RFC3610_KEY2_M10 = (
    # #19
    (
        "0042fff8f1951c3c9696766cfa",
        8,
        "d85bc7e69f944fb88a19b950bcf71a018e5e6701c91787659809d67dbedd18",
        "d85bc7e69f944fb8bc218daa947427b6db386a99ac1aef23ade0b52939cb6a637cf9bec2408897c6ba",
    ),
    # #20
    (
        "00920f40e56cdc3c9696766cfa",
        8,
        "74a0ebc9069f5b371761433c37c5a35fc1f39f406302eb907c6163be38c98437",
        "74a0ebc9069f5b375810e6fd25874022e80361a478e3e9cf484ab04f447efff6f0a477cc2fc9bf548944",
    ),
    # #21
    (
        "0027ca0c7120bc3c9696766cfa",
        8,
        "44a3aa3aae6475caa434a8e58500c6e41530538862d686ea9e81301b5ae4226bfa",
        "44a3aa3aae6475caf2beed7bc5098e83feb5b31608f8e29c38819a89c8e776f1544d4151a4ed3a8b87b9ce",
    ),
    # #22
    (
        "005b8ccbcd9af83c9696766cfa",
        12,
        "ec46bb63b02520c33c49fd70b96b49e21d621741632875db7f6c9243d2d7c2",
        "ec46bb63b02520c33c49fd7031d750a09da3ed7fddd49a2032aabf17ec8ebf7d22c8088c666be5c197",
    ),
    # #23
    (
        "003ebe94044b9a3c9696766cfa",
        12,
        "47a65ac78b3d594227e85e71e2fcfbb880442c731bf95167c8ffd7895e337076",
        "47a65ac78b3d594227e85e71e882f1dbd38ce3eda7c23f04dd65071eb41342acdf7e00dccec7ae52987d",
    ),
    # #24
    (
        "008d493b30ae8b3c9696766cfa",
        12,
        "6e37a6ef546d955d34ab6059abf21c0b02feb88f856df4a37381bce3cc128517d4",
        "6e37a6ef546d955d34ab6059f32905b88a641b04b9c9ffb58cc390900f3da12ab16dce9e82efa16da62059",
    ),
)

_RFC3610 = (
    (_RFC3610_KEY1, 8, _RFC3610_KEY1_M8),
    (_RFC3610_KEY1, 10, _RFC3610_KEY1_M10),
    (_RFC3610_KEY2, 8, _RFC3610_KEY2_M8),
    (_RFC3610_KEY2, 10, _RFC3610_KEY2_M10),
)


def _seal(ccm, nonce, aad, plaintext):
    """Return ciphertext || tag through Ccm.encrypt."""
    data = bytearray(plaintext)
    tag = bytearray(ccm.tag_len)
    ccm.encrypt(nonce, aad, data, tag)
    return bytes(data) + bytes(tag)


def _open(ccm, nonce, aad, sealed):
    """Return (ok, data after decrypt) through Ccm.decrypt."""
    n = len(sealed) - ccm.tag_len
    data = bytearray(sealed[:n])
    ok = ccm.decrypt(nonce, aad, data, sealed[n:])
    return ok, bytes(data)


class NistExamples(unittest.TestCase):
    def test_encrypt_and_decrypt(self):
        for i, (nonce, aad, plaintext, tag_len, expected) in enumerate(_NIST):
            ccm = Ccm(h(_NIST_KEY), tag_len, len(h(nonce)))
            sealed = _seal(ccm, h(nonce), aad, plaintext)
            self.assertEqual(sealed, h(expected), "example %d" % (i + 1))
            self.assertEqual(_open(ccm, h(nonce), aad, sealed), (True, plaintext))


class Rfc3610Vectors(unittest.TestCase):
    def test_packet_vectors(self):
        # One Ccm per key and M: consecutive messages reuse its cipher objects
        # and buffers (CBC chaining cancelled between messages).
        for key, tag_len, vectors in _RFC3610:
            ccm = Ccm(h(key), tag_len, 13)
            for nonce, header_len, packet, expected in vectors:
                packet = h(packet)
                aad = packet[:header_len]
                sealed = _seal(ccm, h(nonce), aad, packet[header_len:])
                self.assertEqual(aad + sealed, h(expected), nonce)
                ok, plaintext = _open(ccm, h(nonce), aad, sealed)
                self.assertTrue(ok, nonce)
                self.assertEqual(plaintext, packet[header_len:])

    def test_in_place_on_memoryviews(self):
        # The mesh seals regions of one frame buffer: aad, data and tag are
        # memoryviews into the same bytearray.
        nonce, header_len, packet, expected = _RFC3610_KEY1_M8[0]
        buf = bytearray(h(packet) + bytes(8))
        mv = memoryview(buf)
        ccm = Ccm(h(_RFC3610_KEY1), 8, 13)
        n = len(buf) - 8
        ccm.encrypt(h(nonce), mv[:header_len], mv[header_len:n], mv[n:])
        self.assertEqual(bytes(buf), h(expected))
        self.assertTrue(ccm.decrypt(h(nonce), mv[:header_len], mv[header_len:n], mv[n:]))
        self.assertEqual(bytes(buf[:n]), h(packet))


class Rejection(unittest.TestCase):
    def setUp(self):
        self.nonce, header_len, packet, expected = _RFC3610_KEY2_M8[2]
        self.nonce = h(self.nonce)
        out = h(expected)
        self.aad = out[:header_len]
        self.sealed = out[header_len:]
        self.ccm = Ccm(h(_RFC3610_KEY2), 8, 13)

    def assertRejected(self, nonce, aad, sealed):
        ok, data = _open(self.ccm, nonce, aad, sealed)
        self.assertFalse(ok)
        # Unauthenticated plaintext is not left behind: ciphertext restored.
        self.assertEqual(data, sealed[: len(sealed) - 8])

    def test_genuine_accepted(self):
        self.assertTrue(_open(self.ccm, self.nonce, self.aad, self.sealed)[0])

    def test_any_flipped_bit_rejected(self):
        for i in range(len(self.sealed)):
            bad = bytearray(self.sealed)
            bad[i] ^= 0x01 << (i % 8)
            self.assertRejected(self.nonce, self.aad, bytes(bad))
        for i in range(len(self.aad)):
            bad = bytearray(self.aad)
            bad[i] ^= 0x80
            self.assertRejected(self.nonce, bytes(bad), self.sealed)
        for i in range(len(self.nonce)):
            bad = bytearray(self.nonce)
            bad[i] ^= 0x10
            self.assertRejected(bytes(bad), self.aad, self.sealed)

    def test_truncated_associated_data_rejected(self):
        self.assertRejected(self.nonce, self.aad[:-1], self.sealed)
        self.assertRejected(self.nonce, b"", self.sealed)

    def test_wrong_key_rejected(self):
        self.ccm = Ccm(h(_RFC3610_KEY1), 8, 13)
        self.assertRejected(self.nonce, self.aad, self.sealed)

    def test_rejection_does_not_disturb_next_message(self):
        bad = bytearray(self.sealed)
        bad[0] ^= 1
        self.assertRejected(self.nonce, self.aad, bytes(bad))
        self.assertTrue(_open(self.ccm, self.nonce, self.aad, self.sealed)[0])


class Parameters(unittest.TestCase):
    def test_lengths_checked(self):
        key = bytes(16)
        self.assertRaises(ValueError, Ccm, bytes(15))
        for tag_len in (2, 5, 18):
            self.assertRaises(ValueError, Ccm, key, tag_len)
        for nonce_len in (6, 14):
            self.assertRaises(ValueError, Ccm, key, 4, nonce_len)
        ccm = Ccm(key, 4, 13)
        self.assertRaises(ValueError, ccm.encrypt, bytes(12), b"", bytearray(4), bytearray(4))
        self.assertRaises(ValueError, ccm.encrypt, bytes(13), b"", bytearray(4), bytearray(8))
        # L = 2: messages up to 65535 bytes.
        self.assertRaises(
            ValueError, Ccm(key, 4, 13).encrypt, bytes(13), b"", bytearray(65536), bytearray(4)
        )

    def test_empty_message_and_aad(self):
        # Authentication only (empty plaintext), and no associated data.
        ccm = Ccm(bytes(range(16)), 4, 13)
        tag = bytearray(4)
        ccm.encrypt(bytes(13), b"header", bytearray(0), tag)
        self.assertTrue(ccm.decrypt(bytes(13), b"header", bytearray(0), tag))
        self.assertFalse(ccm.decrypt(bytes(13), b"Header", bytearray(0), tag))
        sealed = _seal(ccm, bytes(13), b"", b"payload")
        self.assertEqual(_open(ccm, bytes(13), b"", sealed), (True, b"payload"))
        self.assertFalse(_open(ccm, bytes(13), b"\x00", sealed)[0])
