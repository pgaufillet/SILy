"""AES-CCM (NIST SP 800-38C, RFC 3610) on top of MicroPython's cryptolib.

cryptolib offers ECB and CBC only (CTR is a build option, off by default):
the CBC-MAC is one CBC encryption of the formatted input, the counter-mode
keystream one ECB encryption of the counter blocks; the keystream is applied
with big-integer XOR (C speed, about ten times a byte loop).

Generic on the tag length M (4, 6, ... 16) and the nonce length 15 - L (7 to
13 B), so that the published vectors run; the mesh uses a 4-byte tag and a
13-byte nonce (MESH_DESIGN §14, `mesh.security`). Associated data up to
2^32 - 1 bytes.

Per call, nothing is allocated but a few memoryviews and the XOR integers:
the cipher objects and work buffers are created once per key (buffers grow
to the largest message seen; `Ccm` preallocates for a 255-byte frame). The
CBC object is reused across messages: CBC chains each call on the last
ciphertext block of the previous one, so the first block of every message is
XORed with that block beforehand, which gives the zero IV of the CBC-MAC.
"""

import cryptolib

_ECB = 1
_CBC = 2
_BLOCK = 16


def _padded(n):
    return (n + _BLOCK - 1) // _BLOCK * _BLOCK


def _aad_prefix(n):
    """Length of the encoding of an associated-data length n (0: none)."""
    if n == 0:
        return 0
    if n < 0xFF00:
        return 2
    if n < 1 << 32:
        return 6
    raise ValueError("associated data too long")


class Ccm:
    """AES-CCM under one key.

    key: 16, 24 or 32 bytes (AES-128 for the mesh); tag_len: M, an even
    number of bytes from 4 to 16; nonce_len: 7 to 13 bytes (L = 15 -
    nonce_len bytes of message length). max_aad, max_data: sizes the work
    buffers are preallocated for (default: a mesh frame); larger inputs make
    them grow once.
    """

    def __init__(self, key, tag_len=4, nonce_len=13, max_aad=9, max_data=242):
        if len(key) not in (16, 24, 32):
            raise ValueError("bad key length")
        if tag_len < 4 or tag_len > 16 or tag_len & 1:
            raise ValueError("bad tag length")
        if nonce_len < 7 or nonce_len > 13:
            raise ValueError("bad nonce length")
        self.tag_len = tag_len
        self.nonce_len = nonce_len
        self._q = 15 - nonce_len  # L: bytes of the message length
        self._max_data = (1 << (8 * self._q)) - 1
        self._ecb = cryptolib.aes(bytes(key), _ECB)
        self._cbc = cryptolib.aes(bytes(key), _CBC, bytes(_BLOCK))
        self._chain = bytearray(_BLOCK)  # last ciphertext block of _cbc
        self._mac_in = bytearray(0)
        self._mac_out = bytearray(0)
        self._ctr = bytearray(0)
        self._stream = bytearray(0)
        self._reserve(max_aad, max_data)

    def _reserve(self, aad_len, data_len):
        """Grow the work buffers for aad_len and data_len bytes if needed."""
        n = _BLOCK + _padded(_aad_prefix(aad_len) + aad_len) + _padded(data_len)
        if n > len(self._mac_in):
            self._mac_in = bytearray(n)
            self._mac_out = bytearray(n)
            self._mac_in_mv = memoryview(self._mac_in)
            self._mac_out_mv = memoryview(self._mac_out)
        n = _BLOCK + _padded(data_len)
        if n > len(self._ctr):
            self._ctr = bytearray(n)
            self._stream = bytearray(n)
            self._ctr_mv = memoryview(self._ctr)
            self._stream_mv = memoryview(self._stream)

    def _check(self, nonce, aad, data, tag):
        if len(nonce) != self.nonce_len:
            raise ValueError("bad nonce length")
        if len(tag) != self.tag_len:
            raise ValueError("bad tag length")
        if len(data) > self._max_data:
            raise ValueError("message too long")
        self._reserve(len(aad), len(data))

    def _mac(self, nonce, aad, data):
        """Return the CBC-MAC of (nonce, aad, data): a 16-byte memoryview."""
        q = self._q
        a = len(aad)
        n = len(data)
        buf = self._mac_in
        mv = self._mac_in_mv
        # B0: flags, nonce, message length (big-endian on q bytes).
        buf[0] = (0x40 if a else 0) | ((self.tag_len - 2) // 2) << 3 | (q - 1)
        mv[1 : 1 + self.nonce_len] = nonce
        length = n
        i = 15
        while i > 15 - q:
            buf[i] = length & 0xFF
            length >>= 8
            i -= 1
        p = _BLOCK
        if a:
            prefix = _aad_prefix(a)
            if prefix == 2:
                buf[p] = a >> 8
                buf[p + 1] = a & 0xFF
            else:
                buf[p] = 0xFF
                buf[p + 1] = 0xFE
                buf[p + 2] = a >> 24
                buf[p + 3] = (a >> 16) & 0xFF
                buf[p + 4] = (a >> 8) & 0xFF
                buf[p + 5] = a & 0xFF
            p += prefix
            mv[p : p + a] = aad
            p += a
            end = _padded(p)
            while p < end:
                buf[p] = 0
                p += 1
        mv[p : p + n] = data
        p += n
        end = _padded(p)
        while p < end:
            buf[p] = 0
            p += 1
        # Cancel the chaining left by the previous message: zero IV.
        chain = self._chain
        for i in range(_BLOCK):
            buf[i] ^= chain[i]
        out = self._mac_out_mv
        self._cbc.encrypt(mv[:end], out[:end])
        last = out[end - _BLOCK : end]
        chain[:] = last
        return last

    def _keystream(self, nonce, n):
        """Encrypt the counter blocks A0... for n message bytes into _stream."""
        q = self._q
        ctr = self._ctr
        mv = self._ctr_mv
        end = _BLOCK + _padded(n)
        ctr[0] = q - 1
        mv[1 : 1 + self.nonce_len] = nonce
        for i in range(16 - q, 16):
            ctr[i] = 0
        block = 1
        p = _BLOCK
        while p < end:
            mv[p : p + _BLOCK] = mv[:_BLOCK]
            value = block
            i = p + 15
            while value:
                ctr[i] = value & 0xFF
                value >>= 8
                i -= 1
            block += 1
            p += _BLOCK
        self._ecb.encrypt(mv[:end], self._stream_mv[:end])

    def _xor_stream(self, data):
        n = len(data)
        if n:
            x = int.from_bytes(data, "little") ^ int.from_bytes(
                self._stream_mv[_BLOCK : _BLOCK + n], "little"
            )
            data[:] = x.to_bytes(n, "little")

    def encrypt(self, nonce, aad, data, tag):
        """Encrypt data in place and write the tag.

        nonce: nonce_len bytes; aad: associated data (bytes-like, may be
        empty); data: writable bytes-like (bytearray or memoryview), plaintext
        in, ciphertext out; tag: writable bytes-like of tag_len bytes.
        """
        self._check(nonce, aad, data, tag)
        mac = self._mac(nonce, aad, data)
        self._keystream(nonce, len(data))
        stream = self._stream
        for i in range(self.tag_len):
            tag[i] = mac[i] ^ stream[i]
        self._xor_stream(data)

    def decrypt(self, nonce, aad, data, tag):
        """Decrypt data in place if the tag is valid; return True if it is.

        Arguments as for `encrypt`, tag read only. On failure, data is
        restored to the ciphertext: unauthenticated plaintext is never left.
        """
        self._check(nonce, aad, data, tag)
        self._keystream(nonce, len(data))
        self._xor_stream(data)
        mac = self._mac(nonce, aad, data)
        stream = self._stream
        diff = 0
        for i in range(self.tag_len):
            diff |= mac[i] ^ stream[i] ^ tag[i]
        if diff:
            self._xor_stream(data)
            return False
        return True
