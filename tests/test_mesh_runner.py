"""asyncio runner of a mesh core (mesh.runner), on the unix port's asyncio."""

import asyncio
import unittest

from mesh.core import Core
from mesh.radio import CHANNEL, RX, SENSE_ACTION, TX, TX_DONE
from mesh.runner import Clock, Runner

_PERIOD = 1 << 30  # ticks_ms period on the boards (the unix port's is larger)


def _diff(a, b):
    """ticks_diff with the boards' period."""
    half = _PERIOD // 2
    return ((a - b + half) & (_PERIOD - 1)) - half


class Scripted:
    """Ticks function returning the values of a list, in order."""

    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class Clocks(unittest.TestCase):
    def test_starts_at_the_first_ticks(self):
        self.assertEqual(Clock(Scripted([500, 520])).now(), 520)

    def test_monotonic_across_the_wrap(self):
        clock = Clock(Scripted([_PERIOD - 10, _PERIOD - 1, 5, 100]), _diff)
        self.assertEqual(
            [clock.now(), clock.now(), clock.now()],
            [_PERIOD - 1, _PERIOD + 5, _PERIOD + 100],
        )

    def test_same_ticks_same_time(self):
        clock = Clock(Scripted([7, 7, 7]))
        self.assertEqual(clock.now(), 7)
        self.assertEqual(clock.now(), 7)


class Recorder(Core):
    """Core recording its steps; scripted actions and deadlines."""

    def __init__(self):
        super().__init__()
        self.steps = []  # (now, event)
        self.inside = False
        self.reentered = False
        self.on_event = {}  # event kind -> actions to emit
        self.next_deadline = None

    def handle(self, now, event):
        if self.inside:
            self.reentered = True
        self.inside = True
        self.steps.append((now, event))
        kind = None if event is None else event[0]
        for action in self.on_event.get(kind, ()):
            self.emit(action)
        self.set_deadline(self.next_deadline)
        self.next_deadline = None
        self.inside = False


class Radio:
    """Radio answering a sense at once (as sily.radio.MeshRadio does)."""

    def __init__(self):
        self.sink = None
        self.sent = []

    def transmit(self, frame):
        self.sent.append(frame)

    def sense(self):
        self.sink((CHANNEL, False))


def _run(scenario):
    """Run scenario(runner, core, radio) next to a runner; return them."""
    core = Recorder()
    radio = Radio()
    runner = Runner(core, radio)

    async def main():
        task = asyncio.create_task(runner.run())
        await asyncio.sleep_ms(0)
        await scenario(runner, core, radio)
        runner.stop()
        await task

    asyncio.run(main())
    return runner, core, radio


def _events(core):
    return [e for _, e in core.steps]


class Steps(unittest.TestCase):
    def test_first_step_without_event(self):
        async def scenario(runner, core, radio):
            pass

        _, core, _ = _run(scenario)
        self.assertEqual(_events(core)[0], None)

    def test_events_in_order(self):
        async def scenario(runner, core, radio):
            radio.sink((RX, b"a", -50, 5))
            radio.sink((RX, b"b", -51, 6))
            radio.sink((TX_DONE, 40))
            await asyncio.sleep_ms(5)

        _, core, _ = _run(scenario)
        self.assertEqual(
            _events(core)[1:4],
            [(RX, b"a", -50, 5), (RX, b"b", -51, 6), (TX_DONE, 40)],
        )

    def test_time_never_decreases(self):
        async def scenario(runner, core, radio):
            for _ in range(5):
                radio.sink((TX_DONE, 1))
                await asyncio.sleep_ms(2)

        _, core, _ = _run(scenario)
        times = [t for t, _ in core.steps]
        self.assertEqual(times, sorted(times))

    def test_deadline_wakes_the_core(self):
        async def scenario(runner, core, radio):
            core.wanted = core.next_deadline = runner.clock.now() + 30
            radio.sink((TX_DONE, 1))  # a step that sets the deadline
            await asyncio.sleep_ms(80)

        _, core, _ = _run(scenario)
        deadline_step = [(t, e) for t, e in core.steps[2:] if e is None]
        self.assertEqual(len(deadline_step), 1)
        self.assertGreaterEqual(deadline_step[0][0], core.wanted)

    def test_no_step_before_the_deadline(self):
        async def scenario(runner, core, radio):
            core.next_deadline = runner.clock.now() + 200
            radio.sink((TX_DONE, 1))
            await asyncio.sleep_ms(50)

        _, core, _ = _run(scenario)
        self.assertEqual(len(core.steps), 2)

    def test_wake_steps_without_deadline(self):
        async def scenario(runner, core, radio):
            await asyncio.sleep_ms(5)
            runner.wake()
            await asyncio.sleep_ms(5)

        _, core, _ = _run(scenario)
        self.assertEqual(_events(core), [None, None])

    def test_actions_executed(self):
        async def scenario(runner, core, radio):
            core.on_event[RX] = [(TX, b"reply")]
            radio.sink((RX, b"x", 0, 0))
            await asyncio.sleep_ms(5)

        _, _, radio = _run(scenario)
        self.assertEqual(radio.sent, [b"reply"])

    def test_sense_result_comes_in_a_later_step(self):
        async def scenario(runner, core, radio):
            core.on_event[TX_DONE] = [SENSE_ACTION]
            radio.sink((TX_DONE, 1))
            await asyncio.sleep_ms(5)

        _, core, _ = _run(scenario)
        self.assertEqual(_events(core)[1:], [(TX_DONE, 1), (CHANNEL, False)])
        self.assertFalse(core.reentered)

    def test_unknown_action_raises(self):
        core = Recorder()
        core.on_event[None] = [(99,)]
        runner = Runner(core, Radio())
        with self.assertRaises(ValueError):
            asyncio.run(runner.run())

    def test_cpu_meters_per_event_kind(self):
        async def scenario(runner, core, radio):
            radio.sink((RX, b"a", 0, 0))
            radio.sink((RX, b"b", 0, 0))
            await asyncio.sleep_ms(5)

        runner, _, _ = _run(scenario)
        self.assertEqual(runner.cpu[RX][0], 2)
        self.assertEqual(runner.cpu[None][0], 1)
        self.assertGreaterEqual(runner.cpu[RX][1], runner.cpu[RX][2])
        self.assertEqual(runner.steps, 3)
