"""Key derivation (MESH_DESIGN §14 key derivation).

HMAC-SHA256 (RFC 2104) and PBKDF2-HMAC-SHA256 (RFC 8018) in Python:
MicroPython has ``hashlib.sha256`` but no ``hmac``, and its hash objects have
no ``copy``, so each HMAC hashes the padded key blocks again (four SHA-256
compressions per PBKDF2 iteration instead of two).

Network keys: master key = PBKDF2-HMAC-SHA256(password, `SALT_PREFIX` ‖
network name, iterations, 32 B), both strings in UTF-8; from it, with one
HMAC per label: frame key (16 B, label ``frame``), key fingerprint (4 B,
``fingerprint``), wrapping key (16 B, ``key-wrap``). The iteration count is
set so that derivation takes 1–2 s on the board: 6000 iterations take
1.44 s on a T3S3 with the application loaded (docs/measurements.md).
"""

import hashlib

SALT_PREFIX = b"mesh/1"
DEFAULT_ITERATIONS = 6000  # network-wide: every node must use the same count
MASTER_SIZE = 32
FRAME_KEY_SIZE = 16
FINGERPRINT_SIZE = 4
WRAPPING_KEY_SIZE = 16

_BLOCK = 64  # SHA-256 block size
_DIGEST = 32


def _utf8(value):
    return value.encode() if isinstance(value, str) else bytes(value)


def _pads(key):
    """Return the inner and outer padded key blocks of HMAC-SHA256."""
    if len(key) > _BLOCK:
        key = hashlib.sha256(key).digest()
    key = bytes(key) + bytes(_BLOCK - len(key))
    return bytes(b ^ 0x36 for b in key), bytes(b ^ 0x5C for b in key)


def _hmac(ipad, opad, msg):
    inner = hashlib.sha256(ipad)
    inner.update(msg)
    outer = hashlib.sha256(opad)
    outer.update(inner.digest())
    return outer.digest()


def hmac_sha256(key, msg):
    """Return HMAC-SHA256(key, msg), 32 bytes."""
    ipad, opad = _pads(key)
    return _hmac(ipad, opad, msg)


def pbkdf2_hmac_sha256(password, salt, iterations, dklen=_DIGEST):
    """Return PBKDF2-HMAC-SHA256(password, salt, iterations), dklen bytes.

    As CPython's ``hashlib.pbkdf2_hmac("sha256", ...)``; password and salt
    are bytes-like.
    """
    if iterations < 1 or dklen < 1:
        raise ValueError("bad parameters")
    ipad, opad = _pads(password)
    sha256 = hashlib.sha256
    out = b""
    block = 1
    while len(out) < dklen:
        u = _hmac(ipad, opad, bytes(salt) + block.to_bytes(4, "big"))
        acc = int.from_bytes(u, "big")
        for _ in range(iterations - 1):
            inner = sha256(ipad)
            inner.update(u)
            outer = sha256(opad)
            outer.update(inner.digest())
            u = outer.digest()
            acc ^= int.from_bytes(u, "big")
        out += acc.to_bytes(_DIGEST, "big")
        block += 1
    return out[:dklen]


def master_key(password, network_name="", iterations=DEFAULT_ITERATIONS):
    """Return the 32-byte master key of password (str or bytes) and network."""
    return pbkdf2_hmac_sha256(
        _utf8(password), SALT_PREFIX + _utf8(network_name), iterations, MASTER_SIZE
    )


class Keys:
    """Keys derived from one master key.

    master: 32 B; frame_key: 16 B (AES-128-CCM of frames); fingerprint: 4 B
    (shown in hexadecimal to compare configurations, MP22); wrapping_key:
    16 B (wrapping of the next key, key distribution).
    """

    def __init__(self, master):
        if len(master) != MASTER_SIZE:
            raise ValueError("bad master key length")
        ipad, opad = _pads(master)
        self.master = bytes(master)
        self.frame_key = _hmac(ipad, opad, b"frame")[:FRAME_KEY_SIZE]
        self.fingerprint = _hmac(ipad, opad, b"fingerprint")[:FINGERPRINT_SIZE]
        self.wrapping_key = _hmac(ipad, opad, b"key-wrap")[:WRAPPING_KEY_SIZE]


def derive(password, network_name="", iterations=DEFAULT_ITERATIONS):
    """Return the `Keys` of password and network name (1–2 s on the board)."""
    return Keys(master_key(password, network_name, iterations))
