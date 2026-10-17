# Not a test_*.py module: run.py must not collect this test case.
import unittest


class NotCollected(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(1 + 1, 2)
