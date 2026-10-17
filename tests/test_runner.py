"""The test harness reports failures through its exit status.

Runs run.py in a subprocess on the directories of fixtures/ (not collected by
`make test`, as run.py does not recurse) and checks the exit status.
"""

import os
import sys
import unittest

import run

_HERE = run._dirname(__file__)
_FIXTURES = _HERE + "/fixtures/"


def _exit_status(fixture):
    command = '"%s" "%s/run.py" "%s" > /dev/null 2>&1' % (
        sys.executable,
        _HERE,
        _FIXTURES + fixture,
    )
    return os.system(command)


class ExitStatus(unittest.TestCase):
    def test_passing_tests_exit_zero(self):
        self.assertEqual(_exit_status("passing"), 0)

    def test_failing_test_exits_non_zero(self):
        self.assertNotEqual(_exit_status("failing"), 0)

    def test_import_error_exits_non_zero(self):
        self.assertNotEqual(_exit_status("broken"), 0)

    def test_no_test_module_exits_non_zero(self):
        self.assertNotEqual(_exit_status("no_tests"), 0)


class Discovery(unittest.TestCase):
    def test_collects_test_modules_only(self):
        self.assertEqual(run.find_test_modules(_FIXTURES + "failing"), ["test_one_fails"])
        self.assertEqual(run.find_test_modules(_FIXTURES + "no_tests"), [])
