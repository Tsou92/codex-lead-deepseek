import unittest
from math_utils import clamp
class ClampTests(unittest.TestCase):
    def test_middle(self): self.assertEqual(clamp(5, 0, 10), 5)
    def test_low(self): self.assertEqual(clamp(-1, 0, 10), 0)
    def test_high(self): self.assertEqual(clamp(11, 0, 10), 10)
    def test_equal(self): self.assertEqual(clamp(9, 3, 3), 3)
    def test_reversed(self):
        with self.assertRaises(ValueError): clamp(1, 3, 2)
