"""Platform fakes: storage power-loss semantics, retained memory."""

import unittest

from sim.platform import FakeFlash, FakePlatform, FakeRetained
from sim.prng import XorShift32


class StorageSemantics(unittest.TestCase):
    def test_uncommitted_write_visible_then_lost_on_power_loss(self):
        flash = FakeFlash()
        nvs = flash.namespace("mesh")
        nvs.set("fc", b"\x01")
        nvs.commit()
        nvs.set("fc", b"\x02")
        nvs.set("seq", b"\x09")
        self.assertEqual(nvs.get("fc"), b"\x02")
        flash.drop_uncommitted()
        self.assertEqual(nvs.get("fc"), b"\x01")
        self.assertEqual(nvs.get("seq"), None)

    def test_commit_survives(self):
        flash = FakeFlash()
        nvs = flash.namespace("mesh")
        nvs.set("fc", bytearray(b"\x05\x06"))
        nvs.commit()
        flash.drop_uncommitted()
        self.assertEqual(nvs.get("fc"), b"\x05\x06")
        self.assertEqual(flash.commits, 1)

    def test_erase_needs_commit(self):
        flash = FakeFlash()
        nvs = flash.namespace("mesh")
        nvs.set("k", b"v")
        nvs.commit()
        nvs.erase("k")
        self.assertEqual(nvs.get("k"), None)
        flash.drop_uncommitted()
        self.assertEqual(nvs.get("k"), b"v")
        nvs.erase("k")
        nvs.commit()
        flash.drop_uncommitted()
        self.assertEqual(nvs.get("k"), None)

    def test_namespaces_isolated(self):
        flash = FakeFlash()
        a = flash.namespace("a")
        b = flash.namespace("b")
        a.set("k", b"1")
        a.commit()
        self.assertEqual(b.get("k"), None)
        b.set("k", b"2")
        b.commit()
        self.assertEqual(a.get("k"), b"1")

    def test_value_copied(self):
        nvs = FakeFlash().namespace("mesh")
        value = bytearray(b"ab")
        nvs.set("k", value)
        value[0] = 0
        self.assertEqual(nvs.get("k"), b"ab")

    def test_key_rules(self):
        nvs = FakeFlash().namespace("mesh")
        self.assertRaises(ValueError, nvs.set, "x" * 16, b"")
        self.assertRaises(ValueError, nvs.get, "")

    def test_full_erase(self):
        flash = FakeFlash()
        nvs = flash.namespace("mesh")
        nvs.set("k", b"v")
        nvs.commit()
        flash.full_erase()
        self.assertEqual(nvs.get("k"), None)


class RetainedSemantics(unittest.TestCase):
    def test_reset_restores_saved_content(self):
        mem = FakeRetained(16)
        mem.data[0] = 7
        mem.save()
        mem.data[0] = 8
        mem.data[1] = 9
        mem.reset()
        self.assertEqual(mem.data[0], 7)
        self.assertEqual(mem.data[1], 0)
        self.assertEqual(len(mem.data), 16)

    def test_power_loss_clears(self):
        mem = FakeRetained(16)
        mem.data[3] = 1
        mem.save()
        mem.power_loss()
        self.assertEqual(mem.data, bytearray(16))
        mem.reset()
        self.assertEqual(mem.data, bytearray(16))

    def test_data_is_the_same_object_after_reset(self):
        mem = FakeRetained(4)
        data = mem.data
        mem.reset()
        mem.power_loss()
        self.assertTrue(mem.data is data)


class Platform(unittest.TestCase):
    def test_reset_versus_power_loss(self):
        p = FakePlatform(XorShift32(1), retained_size=8)
        p.retained.data[0] = 1
        p.retained.save()
        p.storage.set("fc", b"\x10")
        p.storage.commit()
        p.storage.set("fc", b"\x20")
        p.reset()
        self.assertEqual(p.retained.data[0], 1)
        self.assertEqual(p.storage.get("fc"), b"\x10")
        p.power_loss()
        self.assertEqual(p.retained.data[0], 0)
        self.assertEqual(p.storage.get("fc"), b"\x10")
        self.assertEqual(len(p.random.randbytes(3)), 3)
