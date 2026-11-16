"""Assembly of a board's mesh node (sily.stack) on fake hardware.

`FakeHw` stands for ``sily.hw``: the scripted adapter of
``test_sily_radio`` with the rest of the ``board.sx1262.Sx1262`` API, the
simulator's platform fakes, an LED and the board's control functions. The
application's tests (``test_sily_app``) use it too.
"""

import asyncio
import unittest
from time import ticks_diff, ticks_ms

from test_sily_radio import FakeAdapter

import sily
from mesh import profile as profiles
from mesh.link import destination, open_link
from mesh.radio import CHANNEL, RX, SENSE, TX_DONE, Radio
from mesh.reports import decode_node_info
from sily.radio import lora_config
from sily.stack import Stack, firmware_version
from sim.platform import FakePlatform
from sim.prng import XorShift32

KEY = bytes(range(16))
MAC = b"\x02\x00\x00\xab\xcd\xef"
TX_MS = 40  # duration reported for every transmission
PORT = 20


class Modem:
    def __init__(self):
        self.standbys = 0

    def standby(self):
        self.standbys += 1


class Adapter(FakeAdapter):
    """Scripted adapter: transmissions end TX_MS later, run may raise.

    fail: (after_ms, exception) raised by `run` once after_ms have passed.
    """

    def __init__(self, lora_cfg, fail=None):
        super().__init__()
        self.lora_cfg = lora_cfg
        self.modem = Modem()
        self.started = 0
        self.fail = fail
        self._pending = []

    def start(self):
        self.started += 1

    def stats(self):
        return {"rx": 0, "crc": 0, "hdr_err": 0, "tx": 0, "noise": -110.5}

    def transmit(self, frame):
        super().transmit(bytes(frame))
        self._pending.append(ticks_ms())

    async def run(self):
        t0 = ticks_ms()
        while True:
            await asyncio.sleep_ms(2)
            now = ticks_ms()
            while self._pending and ticks_diff(now, self._pending[0]) >= TX_MS:
                self._pending.pop(0)
                self.on_transmitted(TX_MS)
            if self.fail is not None and ticks_diff(now, t0) >= self.fail[0]:
                raise self.fail[1]


class Led:
    def __init__(self):
        self.values = []

    def value(self, v):
        self.values.append(v)


class FakeHw:
    """The ``sily.hw`` bundle on fakes; records what the application does.

    fail: as `Adapter`; adapter_error: raised by `adapter` (a silent radio);
    interrupt_sleep: `sleep_ms` raises KeyboardInterrupt (Ctrl-C);
    battery: the reading of `battery_mv` (mV).
    """

    def __init__(self, power_on=False, fail=None, adapter_error=None, seed=1):
        self.platform = FakePlatform(XorShift32(seed))
        self.on = power_on
        self.fail = fail
        self.adapter_error = adapter_error
        self.interrupt_sleep = False
        self.adapters = []
        self.led_pin = Led()
        self.resets = 0
        self.sleeps = []
        self.kbd = []
        self.blinks = []
        self.battery = 3900

    def adapter(self, lora_cfg):
        if self.adapter_error is not None:
            raise self.adapter_error
        a = Adapter(lora_cfg, self.fail)
        self.adapters.append(a)
        return a

    def storage(self, namespace="mesh"):
        if namespace == "mesh":
            return self.platform.storage
        return self.platform.flash.namespace(namespace)

    def retained(self):
        return self.platform.retained

    def random(self):
        return self.platform.random

    def mac(self):
        return MAC

    def led(self):
        return self.led_pin

    def power_on(self):
        return self.on

    def reset_cause(self):
        return "power" if self.on else "hard"

    def battery_mv(self):
        return self.battery

    def reset(self):
        self.resets += 1
        self.platform.reset()

    def sleep_ms(self, ms):
        self.sleeps.append(ms)
        if self.interrupt_sleep:
            raise KeyboardInterrupt

    def kbd_intr(self, char):
        self.kbd.append(char)

    def blink(self, period_ms):
        self.blinks.append(period_ms)


class Recorder(Radio):
    """Radio wrapper recording the kinds of the events it passes on."""

    def __init__(self, radio):
        self.radio = radio
        self.kinds = []
        radio.sink = self._on_event

    def transmit(self, frame):
        self.radio.transmit(frame)

    def sense(self):
        self.radio.sense()

    def _on_event(self, event):
        self.kinds.append(event[0])
        self.sink(event)


def peer_frame(source, dest, payload, key=KEY, port=PORT, cls=0):
    """Return a frame sealed by another board's engine: payload on port to dest."""
    platform = FakePlatform(XorShift32(7))
    peer = open_link(
        source,
        key,
        platform.storage,
        platform.retained,
        platform.random,
        profiles.get(profiles.EU_869),
        7,
        destination,
    )
    peer.step(0, None)
    peer.submit(port, cls, dest, payload)
    actions = list(peer.step(0, None))
    while actions[0][0] == SENSE:
        actions = list(peer.step(0, (CHANNEL, False)))
    frame = actions[0][1]
    peer.step(100, (TX_DONE, 100))
    return bytes(frame)


def run_for(stack, ms, during=None):
    """Run the stack's runner (and adapter) for ms; during(stack) once started."""

    async def main():
        tasks = [asyncio.create_task(stack.runner.run()), asyncio.create_task(stack.adapter.run())]
        await asyncio.sleep_ms(0)
        if during is not None:
            during(stack)
        await asyncio.sleep_ms(ms)
        stack.runner.stop()
        await tasks[0]
        tasks[1].cancel()

    asyncio.new_event_loop()
    asyncio.run(main())
    asyncio.new_event_loop()


class Inbox:
    def __init__(self):
        self.messages = []

    def __call__(self, origin, msg, header):
        self.messages.append((origin, bytes(msg.payload)))


class Assembly(unittest.TestCase):
    def test_radio_and_node_from_the_settings(self):
        hw = FakeHw()
        s = Stack(hw, 3, KEY, 9, name="box3", power=14)
        eu = profiles.get(profiles.EU_869)
        self.assertEqual(len(hw.adapters), 1)
        self.assertEqual(s.adapter.lora_cfg, lora_config(eu, 9, 14))
        self.assertEqual(s.adapter.started, 1)
        self.assertEqual((s.engine.address, s.engine.sf), (3, 9))
        self.assertFalse(s.engine.is_root)
        info = decode_node_info(s.routing.reporter.node_info)
        self.assertEqual((info.mac, info.name, info.firmware), (MAC, "box3", firmware_version()))
        self.assertTrue(s.heap_radio > 0 and s.heap_node > 0)

    def test_root_presence_period(self):
        s = Stack(FakeHw(), 0, KEY, 7, presence_s=10)
        self.assertTrue(s.engine.is_root)
        info = s.routing.dodag.info
        self.assertEqual((info.presence_s, info.topology_s), (10, 60))

    def test_profile_period_without_override(self):
        default = Stack(FakeHw(), 0, KEY, 7).routing.dodag.info.presence_s
        self.assertNotEqual(default, 10)

    def test_settings_outside_the_profile_refused_before_the_radio(self):
        hw = FakeHw()
        with self.assertRaises(ValueError):
            Stack(hw, 3, KEY, 7, power=22, gain=8)
        with self.assertRaises(ValueError):
            Stack(hw, 3, KEY, 13)
        self.assertEqual(hw.adapters, [])

    def test_static_routes(self):
        s = Stack(FakeHw(), 3, KEY, 7, routing=False)
        self.assertIsNone(s.routing)
        self.assertIs(s.engine.next_hop, destination)

    def test_firmware_version_from_the_package(self):
        major, minor = (int(v) for v in sily.__version__.split(".")[:2])
        self.assertEqual(firmware_version(), major << 8 | minor)

    def test_close_stops_the_radio(self):
        s = Stack(FakeHw(), 3, KEY, 7)
        s.close()
        self.assertEqual(s.adapter.modem.standbys, 1)


class Reception(unittest.TestCase):
    def _received(self, allow=None, wrap=None):
        s = Stack(FakeHw(), 3, KEY, 7, routing=False, allow=allow, wrap=wrap)
        inbox = Inbox()
        s.engine.bind(PORT, inbox)
        frame = peer_frame(1, 3, b"punch")
        run_for(s, 30, lambda s: s.adapter.on_receive(frame, -70, 6.5))
        return s, inbox

    def test_peer_frame_reaches_the_engine(self):
        s, inbox = self._received()
        self.assertEqual(inbox.messages, [(1, b"punch")])
        self.assertEqual(s.engine.counters.rx_frames, 1)
        self.assertIsNotNone(s.engine.neighbours.get(1))  # the neighbour table heard it

    def test_neighbour_filter(self):
        s, inbox = self._received(allow=(2,))
        self.assertEqual(inbox.messages, [])
        self.assertEqual(s.filter.filtered, 1)
        self.assertEqual(s.engine.counters.rx_frames, 0)
        self.assertIsNone(s.engine.neighbours.get(1))

    def test_wrapper_between_filter_and_runner(self):
        made = []

        def wrap(stack, radio):
            made.append(Recorder(radio))
            return made[0]

        s, inbox = self._received(wrap=wrap)
        self.assertIs(s.runner.radio, made[0])
        self.assertEqual(made[0].kinds, [RX])
        self.assertEqual(inbox.messages, [(1, b"punch")])
