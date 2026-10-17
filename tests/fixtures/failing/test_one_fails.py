import unittest


class OneFails(unittest.TestCase):
    def test_passes(self):
        self.assertEqual(1 + 1, 2)

    def test_fails(self):
        self.assertEqual(1 + 1, 3)
