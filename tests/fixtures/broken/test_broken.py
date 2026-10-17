import unittest

import module_that_does_not_exist  # noqa: F401


class NeverLoaded(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(1 + 1, 2)
