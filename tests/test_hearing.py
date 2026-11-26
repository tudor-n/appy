import unittest

try:
    from hearing import filter_transcript
except ImportError as e:  # heavy audio deps (torch, faster-whisper) not installed
    filter_transcript = None
    _skip_reason = str(e)


@unittest.skipIf(filter_transcript is None, "hearing deps unavailable")
class FilterTranscriptTests(unittest.TestCase):
    def test_keeps_normal_speech(self):
        self.assertEqual(filter_transcript("  What's the weather?  "), "What's the weather?")

    def test_drops_too_short(self):
        self.assertIsNone(filter_transcript("a"))

    def test_drops_junk_phrases(self):
        self.assertIsNone(filter_transcript("Thank you."))
        self.assertIsNone(filter_transcript(" you "))

    def test_drops_repetition(self):
        self.assertIsNone(filter_transcript("go go go go go go go go"))


if __name__ == "__main__":
    unittest.main()
