import unittest
import context_transfer


class ImportTest(unittest.TestCase):
    def test_import(self):
        self.assertIsNotNone(context_transfer)
