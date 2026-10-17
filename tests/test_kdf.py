"""HMAC-SHA256, PBKDF2-HMAC-SHA256 and network keys (MESH_DESIGN §14, R13).

Expected values: RFC 4231 (HMAC), RFC 7914 §11 and CPython's
``hashlib.pbkdf2_hmac`` / ``hmac`` (PBKDF2 and the mesh derivation).
"""

import unittest
from binascii import unhexlify as h

from mesh import kdf

# RFC 4231 test cases 1-7: (key, data, HMAC-SHA256); case 5 is truncated to
# 128 bits by the RFC.
_RFC4231 = (
    (
        b"\x0b" * 20,
        b"Hi There",
        "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7",
    ),
    (
        b"Jefe",
        b"what do ya want for nothing?",
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
    ),
    (
        b"\xaa" * 20,
        b"\xdd" * 50,
        "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe",
    ),
    (
        bytes(range(1, 26)),
        b"\xcd" * 50,
        "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b",
    ),
    (b"\x0c" * 20, b"Test With Truncation", "a3b6167473100ee06e0c796c2955552b"),
    (
        b"\xaa" * 131,
        b"Test Using Larger Than Block-Size Key - Hash Key First",
        "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54",
    ),
    (
        b"\xaa" * 131,
        b"This is a test using a larger than block-size key and a larger than block-size"
        b" data. The key needs to be hashed before being used by the HMAC algorithm.",
        "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2",
    ),
)

# (password, salt, iterations, derived key): RFC 7914 §11 (dkLen 64,
# several blocks), and CPython hashlib.pbkdf2_hmac("sha256", ...).
_PBKDF2 = (
    (
        b"passwd",
        b"salt",
        1,
        "55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc"
        "49ca9cccf179b645991664b39d77ef317c71b845b1e30bd509112041d3a19783",
    ),
    (b"password", b"salt", 1, "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b"),
    (b"password", b"salt", 2, "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43"),
    (
        b"password",
        b"salt",
        4096,
        "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a",
    ),
    (
        b"passwordPASSWORDpassword",
        b"saltSALTsaltSALTsaltSALTsaltSALTsalt",
        4096,
        "348c89dbcbd32b2f32d814b8116e84cf2b17347ebc1800181c4e2a1fb8dd53e1c635518c7dac47e9",
    ),
    (b"pass\x00word", b"sa\x00lt", 4096, "89b69d0516f829893c696226650a8687"),
)

# RFC 7914 §11, second PBKDF2-HMAC-SHA256 vector: 80 000 iterations.
_PBKDF2_LONG = (
    b"Password",
    b"NaCl",
    80000,
    "4ddcd8f60b98be21830cee5ef22701f9641a4418d04c0414aeff08876b34ab56"
    "a1d425a1225833549adb841b51c9b3176a272bdebba1d078478f62b397f33c8d",
)


class Hmac(unittest.TestCase):
    def test_rfc4231(self):
        for i, (key, data, expected) in enumerate(_RFC4231):
            mac = kdf.hmac_sha256(key, data)
            self.assertEqual(mac[: len(h(expected))], h(expected), "case %d" % (i + 1))
            self.assertEqual(len(mac), 32)

    def test_key_of_one_block_not_hashed(self):
        # A 64-byte key is padded, a 65-byte one hashed first: they differ.
        self.assertNotEqual(
            kdf.hmac_sha256(b"\x01" * 64, b"m"), kdf.hmac_sha256(b"\x01" * 65, b"m")
        )
        # A key shorter than a block equals itself zero-padded (RFC 2104).
        self.assertEqual(kdf.hmac_sha256(b"k", b"m"), kdf.hmac_sha256(b"k" + bytes(63), b"m"))


class Pbkdf2(unittest.TestCase):
    def test_vectors(self):
        for password, salt, iterations, expected in _PBKDF2:
            dk = kdf.pbkdf2_hmac_sha256(password, salt, iterations, len(h(expected)))
            self.assertEqual(dk, h(expected), (password, iterations))

    def test_rfc7914_80000_iterations(self):
        password, salt, iterations, expected = _PBKDF2_LONG
        self.assertEqual(kdf.pbkdf2_hmac_sha256(password, salt, iterations, 64), h(expected))

    def test_bad_parameters(self):
        self.assertRaises(ValueError, kdf.pbkdf2_hmac_sha256, b"p", b"s", 0)
        self.assertRaises(ValueError, kdf.pbkdf2_hmac_sha256, b"p", b"s", 1, 0)


class NetworkKeys(unittest.TestCase):
    # CPython: master = pbkdf2_hmac("sha256", password.encode(), b"mesh/1" +
    # name.encode(), n, 32); labels with hmac.new(master, label, "sha256").
    def test_derivation_utf8(self):
        keys = kdf.derive("pässwörd-12chars", "Club Orient", 100)
        self.assertEqual(
            keys.master, h("a605d1b299f889727413da8e97b7e3498817841a1c7971b0bf074f71679f1c96")
        )
        self.assertEqual(keys.frame_key, h("4c7bd22267a74f819c2b14ecc586cbef"))
        self.assertEqual(keys.fingerprint, h("ad28bea0"))
        self.assertEqual(keys.wrapping_key, h("ea948d32b504f569f4c4aa4005e2a96b"))

    def test_derivation_default_iterations_empty_name(self):
        # Default iteration count (6000) and network name (empty).
        keys = kdf.derive("correct horse battery")
        self.assertEqual(
            keys.master, h("08be6dfd52152363765506e45144befdc9fb0f056cc235683b6754936e0121cd")
        )
        self.assertEqual(keys.frame_key, h("424ca433f4187a241f48084943340bc6"))
        self.assertEqual(keys.fingerprint, h("323b1cbe"))

    def test_network_name_and_password_separate_keys(self):
        a = kdf.derive("password-one", "", 2)
        self.assertNotEqual(a.master, kdf.derive("password-one", "north", 2).master)
        self.assertNotEqual(a.master, kdf.derive("password-two", "", 2).master)
        self.assertEqual(a.master, kdf.derive(b"password-one", b"", 2).master)
        self.assertEqual(len(set((a.frame_key, a.wrapping_key, a.fingerprint))), 3)

    def test_keys_from_master(self):
        # The wrapped next key carries a master key (key distribution).
        master = h("a605d1b299f889727413da8e97b7e3498817841a1c7971b0bf074f71679f1c96")
        self.assertEqual(kdf.Keys(master).frame_key, h("4c7bd22267a74f819c2b14ecc586cbef"))
        self.assertRaises(ValueError, kdf.Keys, master[:16])
