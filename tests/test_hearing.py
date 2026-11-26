import contextlib
import io
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


@unittest.skipIf(filter_transcript is None, "hearing deps unavailable")
class DiscardCallbackTests(unittest.TestCase):
    def make_ear(self, recognize):
        import queue
        from hearing import Ear

        ear = Ear.__new__(Ear)  # skip model loading
        ear.queue = queue.Queue()
        ear._utterance = 1
        self.discarded = 0
        ear.on_speech_discarded = lambda: setattr(self, "discarded", self.discarded + 1)
        ear._recognize = recognize
        return ear

    def test_reports_discard_for_latest_utterance(self):
        ear = self.make_ear(lambda audio: None)
        ear._transcribe(None, 1)
        self.assertEqual(self.discarded, 1)

    def test_ignores_discard_if_user_spoke_again(self):
        ear = self.make_ear(lambda audio: None)
        ear._transcribe(None, 0)
        self.assertEqual(self.discarded, 0)

    def test_crash_counts_as_discard(self):
        def boom(audio):
            raise RuntimeError("cuda oom")
        ear = self.make_ear(boom)
        with contextlib.redirect_stdout(io.StringIO()):
            ear._transcribe(None, 1)
        self.assertEqual(self.discarded, 1)

    def test_real_text_is_queued(self):
        ear = self.make_ear(lambda audio: "hello there")
        ear._transcribe(None, 1)
        self.assertEqual(ear.queue.get_nowait(), "hello there")
        self.assertEqual(self.discarded, 0)


if __name__ == "__main__":
    unittest.main()
