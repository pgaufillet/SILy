"""Frame sealing and reception steps 1-3 (MESH_DESIGN §14, §11)."""

import unittest
from binascii import unhexlify as h

from mesh import frame
from mesh.counter import COUNTER_LIMIT, RECORD_KEY, FrameCounter
from mesh.frame import Header, Message
from mesh.replay import ReplayTable
from mesh.security import AUTHENTICATION, REPLAY, KeyRing, Security
from sim.platform import FakePlatform
from sim.prng import XorShift32

_KEY = h("4c7bd22267a74f819c2b14ecc586cbef")  # frame key of test_kdf's network
_OTHER_KEY = bytes(range(16))

# Reference frame, sealed with CPython's cryptography AESCCM(_KEY, 4):
# header epoch 1, DODAG version 3, dest 0x1B07, source 0x3F2A, counter
# 0x012345, rank 16; nonce = header[3:8] + 8 zero bytes; aad = header.
_PLAIN = b"\x21\x01\x2a\x3f\x07\x1bpresence report"
_SEALED = h("66071b2a3f45230110d9411092895c8458ac8537f5de97fb9c2f43895892611a603d")


def _board(seed, key=_KEY, epoch=0, start=None):
    """Return (Security, platform) of a board; start: its recorded counter."""
    platform = FakePlatform(XorShift32(seed))
    if start is not None:
        platform.storage.set(RECORD_KEY, start.to_bytes(4, "little"))
        platform.storage.commit()
    security = Security(
        KeyRing(key, epoch),
        FrameCounter(platform.storage, platform.random),
        ReplayTable(platform.retained),
    )
    return security, platform


def _frame(source=0x3F2A, payload=b"punch", dest=0x0000, rank=16):
    """Return (buf, length) of an encoded frame, body in clear, tag zero."""
    buf = bytearray(frame.MAX_FRAME)
    header = Header(dest, source, 0, rank, dodag_version=3)
    msg = Message(frame.UPWARD, 1, 16, payload, origin=source, seq=7)
    return buf, frame.encode(buf, header, [msg])


class Sealing(unittest.TestCase):
    def test_reference_frame(self):
        sender, _ = _board(1, epoch=1, start=0x012345)
        buf = bytearray(len(_SEALED))
        frame.encode_header(buf, Header(0x1B07, 0x3F2A, 0, 16, dodag_version=3))
        buf[frame.HEADER_SIZE : len(buf) - frame.TAG_SIZE] = _PLAIN
        self.assertEqual(sender.seal(buf, len(buf)), 0x012345)
        self.assertEqual(bytes(buf), _SEALED)

    def test_counter_and_epoch_written_body_encrypted(self):
        sender, _ = _board(1, epoch=1, start=512)
        buf, n = _frame()
        clear = bytes(buf[:n])
        self.assertEqual(sender.seal(buf, n), 512)
        header = frame.decode_header(buf, n)
        self.assertEqual((header.counter, header.epoch, header.source), (512, 1, 0x3F2A))
        self.assertEqual((header.rank, header.dodag_version, header.dest), (16, 3, 0))
        start, end = frame.HEADER_SIZE, n - frame.TAG_SIZE
        self.assertNotEqual(bytes(buf[start:end]), clear[start:end])
        self.assertEqual(sender.seal(buf, n), 513)  # every seal takes a new counter

    def test_exhausted_counter_stops_sealing(self):
        sender, _ = _board(1, start=COUNTER_LIMIT - 1)
        buf, n = _frame()
        self.assertEqual(sender.seal(buf, n), 0xFFFFFE)
        buf, n = _frame()
        before = bytes(buf)
        self.assertEqual(sender.seal(buf, n), None)
        self.assertEqual(bytes(buf), before)


class Reception(unittest.TestCase):
    def setUp(self):
        self.sender, _ = _board(1, start=1000)
        self.receiver, self.platform = _board(2)
        self.header = Header(0, 0, 0, 0)

    def _sealed(self, payload=b"punch"):
        buf, n = _frame(payload=payload)
        self.sender.seal(buf, n)
        return buf, n

    def receive(self, buf, n):
        return self.receiver.receive(buf, n, self.header)

    def test_accepted_and_decrypted(self):
        buf, n = self._sealed(b"punch 31 at 10:42")
        self.assertEqual(self.receive(buf, n), None)
        self.assertEqual(self.header.source, 0x3F2A)
        self.assertEqual(self.header.counter, 1000)
        msgs = frame.decode_messages(buf, frame.HEADER_SIZE, n - frame.TAG_SIZE)
        self.assertEqual(bytes(msgs[0].payload), b"punch 31 at 10:42")
        self.assertEqual(self.receiver.replay.highest(0x3F2A), 1000)

    def test_reference_frame_opened(self):
        receiver, _ = _board(3, epoch=1)
        buf = bytearray(_SEALED)
        self.assertEqual(receiver.receive(buf, len(buf), self.header), None)
        self.assertEqual(bytes(buf[frame.HEADER_SIZE : len(buf) - frame.TAG_SIZE]), _PLAIN)

    def test_tampered_header_rejected(self):
        buf, n = self._sealed()
        # Every header byte is authenticated (bit 0 of byte 0 is reserved and
        # ignored by decoding, still authenticated); the version bits would
        # fail at step 1, so byte 0 is flipped on bits 0-5 only.
        for i in range(frame.HEADER_SIZE):
            for bit in (0, 5) if i == 0 else (0, 7):
                bad = bytearray(buf)
                bad[i] ^= 1 << bit
                received = bytes(bad)
                self.assertEqual(self.receive(bad, n), AUTHENTICATION, (i, bit))
                self.assertEqual(bytes(bad), received)  # left as received
        self.assertEqual(self.receiver.replay.highest(0x3F2A), None)
        self.assertEqual(self.receive(buf, n), None)  # the genuine frame still passes

    def test_tampered_tag_or_body_rejected(self):
        buf, n = self._sealed()
        for i in range(frame.HEADER_SIZE, n):
            bad = bytearray(buf)
            bad[i] ^= 0x40
            self.assertEqual(self.receive(bad, n), AUTHENTICATION, i)
        self.assertEqual(self.receive(buf, n), None)

    def test_truncated_frame_rejected(self):
        buf, n = self._sealed()
        self.assertEqual(self.receive(buf, n - 1), AUTHENTICATION)

    def test_wrong_key_rejected(self):
        intruder, _ = _board(4, key=_OTHER_KEY, start=5000)
        buf, n = _frame()
        intruder.seal(buf, n)
        self.assertEqual(self.receive(buf, n), AUTHENTICATION)
        self.assertEqual(self.header.source, 0x3F2A)  # header decoded for counting
        self.assertEqual(len(self.receiver.replay), 0)

    def test_epoch_selects_key(self):
        # A sender switched to epoch 1 under the next key.
        switched, _ = _board(5, key=_OTHER_KEY, epoch=1, start=2000)
        buf, n = _frame(source=0x1111)
        switched.seal(buf, n)
        received = bytes(buf)
        self.assertEqual(self.receive(buf, n), AUTHENTICATION)  # no key for epoch 1
        self.receiver.keys.set(1, _OTHER_KEY)
        buf = bytearray(received)
        self.assertEqual(self.receive(buf, n), None)
        self.assertEqual(self.header.epoch, 1)
        # Epoch 0 is still opened with the current key.
        self.assertEqual(self.receive(*self._sealed()), None)
        # Erased: epoch 1 fails authentication again (before the replay check).
        self.receiver.keys.set(1, None)
        self.assertEqual(self.receive(bytearray(received), n), AUTHENTICATION)
        self.assertRaises(ValueError, self.receiver.keys.set, 0, None)

    def test_replay_rejected(self):
        first = self._sealed()
        second = self._sealed()
        copies = [bytes(first[0]), bytes(second[0])]
        self.assertEqual(self.receive(*second), None)
        # The same frame again, and an older one (lower counter).
        for copy in copies:
            buf = bytearray(copy)
            self.assertEqual(self.receive(buf, second[1]), REPLAY)
            self.assertEqual(self.header.source, 0x3F2A)
        self.assertEqual(self.receiver.replay.highest(0x3F2A), 1001)
        self.assertEqual(self.receive(*self._sealed()), None)

    def test_authentication_checked_before_replay(self):
        buf, n = self._sealed()
        copy = bytes(buf)
        self.assertEqual(self.receive(buf, n), None)
        bad = bytearray(copy)
        bad[n - 1] ^= 1
        # A stale counter with a bad tag is an authentication failure.
        self.assertEqual(self.receive(bad, n), AUTHENTICATION)

    def test_replay_after_receiver_reset_and_power_loss(self):
        frames = [bytes(self._sealed()[0]) for _ in range(3)]
        n = self._sealed()[1]
        for f in frames[1:]:
            self.assertEqual(self.receive(bytearray(f), n), None)
        self.platform.reset()
        self.receiver.replay = ReplayTable(self.platform.retained)
        self.assertEqual(self.receive(bytearray(frames[2]), n), REPLAY)
        self.platform.power_loss()
        self.receiver.replay = ReplayTable(self.platform.retained)
        # Residual risk of §14: once each, then the baseline is raised.
        self.assertEqual(self.receive(bytearray(frames[0]), n), None)
        self.assertEqual(self.receive(bytearray(frames[0]), n), REPLAY)

    def test_step_1_errors(self):
        buf, n = self._sealed()
        self.assertEqual(self.receive(buf, frame.OVERHEAD - 1), frame.LENGTH)
        bad = bytearray(buf)
        bad[0] &= 0x3F  # protocol version 0
        self.assertEqual(self.receive(bad, n), frame.VERSION)
        self.assertEqual(len(self.receiver.replay), 0)

    def test_sender_power_loss_does_not_trigger_replay(self):
        sender, platform = _board(6, start=0)
        for _ in range(200):
            buf, n = _frame()
            sender.seal(buf, n)
            self.assertEqual(self.receive(buf, n), None)
        platform.power_loss()
        sender = Security(
            sender.keys,
            FrameCounter(platform.storage, platform.random),
            ReplayTable(platform.retained),
        )
        buf, n = _frame()
        self.assertTrue(sender.seal(buf, n) > 199)
        self.assertEqual(self.receive(buf, n), None)
