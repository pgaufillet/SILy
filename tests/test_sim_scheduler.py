"""Discrete-event scheduler of the simulator."""

import unittest

from sim.scheduler import Scheduler


class Ordering(unittest.TestCase):
    def test_time_order_then_scheduling_order(self):
        s = Scheduler()
        log = []
        s.at(30, log.append, "c")
        s.at(10, log.append, "a1")
        s.at(20, log.append, "b")
        s.at(10, log.append, "a2")
        s.at(10, log.append, "a3")
        s.run(100)
        self.assertEqual(log, ["a1", "a2", "a3", "b", "c"])

    def test_now_is_event_time_during_run(self):
        s = Scheduler()
        seen = []
        s.at(7, lambda: seen.append(s.now))
        s.at(12, lambda: seen.append(s.now))
        s.run(50)
        self.assertEqual(seen, [7, 12])
        self.assertEqual(s.now, 50)

    def test_call_scheduled_now_runs_after_queued_ones(self):
        s = Scheduler()
        log = []

        def first():
            log.append("first")
            s.at(s.now, log.append, "added")

        s.at(5, first)
        s.at(5, log.append, "second")
        s.run(5)
        self.assertEqual(log, ["first", "second", "added"])

    def test_run_stops_at_until(self):
        s = Scheduler()
        log = []
        s.at(10, log.append, 10)
        s.at(11, log.append, 11)
        s.run(10)
        self.assertEqual(log, [10])
        self.assertEqual(s.now, 10)
        s.run(20)
        self.assertEqual(log, [10, 11])

    def test_after_is_relative(self):
        s = Scheduler(start=100)
        log = []
        s.after(5, lambda: log.append(s.now))
        s.run(200)
        self.assertEqual(log, [105])


class Control(unittest.TestCase):
    def test_cancel(self):
        s = Scheduler()
        log = []
        handle = s.at(10, log.append, "x")
        s.at(10, log.append, "y")
        s.cancel(handle)
        s.run(20)
        self.assertEqual(log, ["y"])

    def test_past_rejected(self):
        s = Scheduler()
        s.run(10)
        self.assertRaises(ValueError, s.at, 9, print)

    def test_limit_guards_against_zero_time_loops(self):
        s = Scheduler()

        def again():
            s.at(s.now, again)

        s.at(0, again)
        self.assertRaises(RuntimeError, s.run, 10, 1000)
