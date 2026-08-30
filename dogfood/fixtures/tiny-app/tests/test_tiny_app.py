import unittest

from tiny_app import greeting


class GreetingTest(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(greeting("Ada"), "Hello, Ada!")

    def test_surrounding_whitespace(self):
        self.assertEqual(greeting(" Ada "), "Hello, Ada!")


if __name__ == "__main__":
    unittest.main()
