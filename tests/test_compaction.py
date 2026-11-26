import unittest

from compaction import estimate_tokens


class EstimateTests(unittest.TestCase):
    def test_estimate_tokens(self):
        # json.dumps adds the two quotes: 33 chars / 3.2 rounds up to 11
        self.assertEqual(estimate_tokens("a" * 31), 11)


if __name__ == "__main__":
    unittest.main()
