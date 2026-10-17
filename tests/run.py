"""Host test runner for the MicroPython unix port.

Usage: micropython tests/run.py [DIRECTORY]

Imports every test_*.py module of DIRECTORY (default: the directory of this
file), runs its unittest.TestCase classes with micropython-lib's unittest and
exits with status 1 if a test fails, a module cannot be imported, or no test
ran at all (a misconfigured path must not pass silently).
"""

import os
import sys
import unittest


def _dirname(path):
    head = path.rsplit("/", 1)
    return head[0] if len(head) == 2 and head[0] else "."


def find_test_modules(directory):
    """Return the sorted names of the test_*.py modules of directory."""
    return sorted(
        name[:-3]
        for name in os.listdir(directory)
        if name.startswith("test_") and name.endswith(".py")
    )


def run(directory):
    """Run the tests of directory; return True if they all passed."""
    sys.path.insert(0, directory)
    suite = unittest.TestSuite(directory)
    import_errors = []
    for name in find_test_modules(directory):
        try:
            module = __import__(name)
        except Exception as e:  # reported below, counted as a failure
            import_errors.append((name, e))
            continue
        for attr in dir(module):
            obj = getattr(module, attr)
            if (
                isinstance(obj, type)
                and issubclass(obj, unittest.TestCase)
                and obj is not unittest.TestCase
            ):
                suite.addTest(obj)

    result = unittest.TextTestRunner().run(suite)
    for name, e in import_errors:
        print("IMPORT ERROR: %s: %s: %s" % (name, type(e).__name__, e))
    if result.testsRun == 0:
        print("NO TEST RAN in", directory)
    return result.wasSuccessful() and not import_errors and result.testsRun > 0


if __name__ == "__main__":
    directory = sys.argv[1] if len(sys.argv) > 1 else _dirname(__file__)
    sys.exit(0 if run(directory) else 1)
