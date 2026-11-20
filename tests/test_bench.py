"""Bench driver (tools/bench.py): a hard reset leaves the board startable."""

import unittest

from tools import bench


class FakeTime:
    """The bench's ``time``: sleeps and reads advance a counter, no wait."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        self.now += 0.01
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Recorder(bench.Board):
    """A board whose serial port, raw REPL and interrupts are a log."""

    def __init__(self):
        self.name = "A"
        self.port = "fake"
        self.echo = False
        self.lines = []
        self._partial = b""
        self.log = []
        self.conn = self

    def open(self, retries=50):
        self.log.append("open")
        self.conn = self

    def close(self):
        self.log.append("close")
        self.conn = None

    def interrupt(self):
        self.log.append("interrupt")

    def exec(self, code, timeout=60):
        self.log.append(("exec", code))
        return ""

    def write(self, data):  # the serial port's
        self.log.append(("write", data))


class HardReset(unittest.TestCase):
    def setUp(self):
        self.saved = bench.time
        bench.time = FakeTime()

    def tearDown(self):
        bench.time = self.saved

    def test_skip_set_before_the_reset(self):
        board = Recorder()
        board.hard_reset()
        reset = board.log.index(("write", bench.RESET_LINE))
        self.assertTrue(board.log.index(("exec", bench.SKIP_CODE)) < reset)

    def test_guard_cleared_once_back_at_the_repl(self):
        # The interrupt after the reconnection may stop the boot before its
        # start decision consumed the skip flag: cleared last, after it.
        board = Recorder()
        board.hard_reset()
        reset = board.log.index(("write", bench.RESET_LINE))
        after = board.log[reset:]
        self.assertIn("open", after)
        self.assertEqual(after[-2:], ["interrupt", ("exec", bench.CLEAR_CODE)])


if __name__ == "__main__":
    unittest.main()
