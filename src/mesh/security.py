"""Frame security (MESH_DESIGN §14): sealing and reception steps 1–3.

AES-128-CCM (`mesh.ccm`), tag 4 B, nonce 13 B: link source (2 B) and frame
counter (3 B) exactly as in the header (bytes 3–7, `mesh.frame`), then 8 zero
bytes. Associated data: the clear header (bytes 0–8, epoch bit included);
plaintext: the messages, encrypted in place in the frame buffer.

Keys: a `KeyRing` holds the frame key of the current key epoch (the header's
epoch bit) and at most one other (next or previous key, key change). A frame
is sealed under the current key and opened under the key its epoch bit
selects; no key for that epoch is an authentication failure.

Reception order (`Security.receive`):

1. length and protocol version (`mesh.frame.decode_header`; the LoRa CRC is
   the radio's);
2. key selected by the epoch bit, CCM verification and decryption;
3. replay check (counter strictly above the sender's highest authenticated
   one), then the replay table is updated: only authenticated, fresh frames
   change it.

Each failure drops the frame and returns its reason for the caller's
counters (per sender for `AUTHENTICATION` and `REPLAY`, the header being
decoded by then). Step 4 (neighbours, duplicate address, key switch,
messages) belongs to the link engine.
"""

from mesh import frame
from mesh.ccm import Ccm
from mesh.frame import COUNTER_OFFSET, SOURCE_OFFSET, FormatError

KEY_SIZE = 16
NONCE_SIZE = 13

# Reasons returned by Security.receive, besides frame.LENGTH and frame.VERSION.
AUTHENTICATION = "authentication"  # bad tag, wrong key, or no key for the epoch
REPLAY = "replay"  # counter not above the sender's highest authenticated one

_EPOCH_BIT = 0x20


def frame_cipher(key):
    """Return the `Ccm` of a 16-byte frame key with the mesh parameters."""
    if len(key) != KEY_SIZE:
        raise ValueError("bad frame key length")
    return Ccm(key, frame.TAG_SIZE, NONCE_SIZE)


class KeyRing:
    """Frame keys by key epoch: the current one and at most one other.

    key: frame key (16 B, `mesh.kdf.Keys.frame_key`) of the current epoch
    epoch (0 or 1). The key change (which epoch is current, when the other
    key is loaded or erased) is driven by the link engine.
    """

    def __init__(self, key, epoch=0):
        if epoch != 0 and epoch != 1:
            raise ValueError("bad epoch")
        self.epoch = epoch
        self._ciphers = [None, None]
        self._ciphers[epoch] = frame_cipher(key)

    def set(self, epoch, key):
        """Load key (None: erase) for epoch; the current key cannot be erased."""
        if epoch != 0 and epoch != 1:
            raise ValueError("bad epoch")
        if key is None and epoch == self.epoch:
            raise ValueError("current key required")
        self._ciphers[epoch] = None if key is None else frame_cipher(key)

    def cipher(self, epoch):
        """Return the `Ccm` of epoch, or None if no key is loaded for it."""
        return self._ciphers[epoch]


class Security:
    """Seals outgoing frames and runs reception steps 1–3 of one board.

    keys: a `KeyRing`; counter: the board's `mesh.counter.FrameCounter`;
    replay: its `mesh.replay.ReplayTable`.
    """

    def __init__(self, keys, counter, replay):
        self.keys = keys
        self.counter = counter
        self.replay = replay
        self._nonce = bytearray(NONCE_SIZE)  # bytes 5-12 stay zero

    def _set_nonce(self, buf):
        nonce = self._nonce
        for i in range(5):
            nonce[i] = buf[SOURCE_OFFSET + i]

    def seal(self, buf, length):
        """Seal the frame of length bytes in buf, in place; return its counter.

        buf holds an encoded frame (`mesh.frame.encode`: header, messages in
        clear, room for the tag). Seal writes the next frame counter (bytes
        5–7) and the current key epoch (bit 5 of byte 0) into the header,
        encrypts the messages and writes the tag. Every transmission is
        sealed anew, retransmissions included. Returns None, leaving buf
        unchanged, once the counter is exhausted: the board must stop
        transmitting.
        """
        if length < frame.OVERHEAD or length > len(buf):
            raise ValueError("bad frame length")
        c = self.counter.next()
        if c is None:
            return None
        keys = self.keys
        buf[COUNTER_OFFSET] = c & 0xFF
        buf[COUNTER_OFFSET + 1] = (c >> 8) & 0xFF
        buf[COUNTER_OFFSET + 2] = c >> 16
        buf[0] = (buf[0] & ~_EPOCH_BIT) | keys.epoch << 5
        self._set_nonce(buf)
        keys.cipher(keys.epoch).encrypt(
            self._nonce, frame.aad(buf), frame.body(buf, length), frame.tag(buf, length)
        )
        return c

    def receive(self, buf, length, header):
        """Run reception steps 1–3 on a received frame; return None if accepted.

        header: a `mesh.frame.Header` filled in place (valid unless the
        reason is `frame.LENGTH` or `frame.VERSION`). Accepted: the messages
        are decrypted in place (`mesh.frame.decode_messages` next) and the
        sender's counter recorded. Otherwise returns the reason:
        `frame.LENGTH`, `frame.VERSION`, `AUTHENTICATION` (buf left as
        received) or `REPLAY`.
        """
        try:
            frame.decode_header(buf, length, header)
        except FormatError as e:
            return e.reason
        cipher = self.keys.cipher(header.epoch)
        if cipher is None:
            return AUTHENTICATION
        self._set_nonce(buf)
        if not cipher.decrypt(
            self._nonce, frame.aad(buf), frame.body(buf, length), frame.tag(buf, length)
        ):
            return AUTHENTICATION
        replay = self.replay
        if not replay.check(header.source, header.counter):
            return REPLAY
        replay.update(header.source, header.counter)
        return None
