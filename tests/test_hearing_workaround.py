"""HEARING-WORKAROUND(respeaker): the temporary rules for while the mic hears the music.
Delete this file with the workaround (see docs/hearing-workaround.md)."""
import os
import unittest
from unittest import mock

import hearing_workaround


class FakeRiff:
    def __init__(self, playing):
        self.playing = playing

    def is_playing(self):
        return self.playing


class HearingWorkaroundTests(unittest.TestCase):
    def env(self, value):
        env = {k: v for k, v in os.environ.items() if k != "APPY_MIC_HEARS_MUSIC"}
        if value is not None:
            env["APPY_MIC_HEARS_MUSIC"] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def test_flag_off_means_no_cap_and_not_strict(self):
        for value in (None, "", "0"):
            with self.subTest(value=value), self.env(value):
                self.assertFalse(hearing_workaround.enabled())
                self.assertFalse(hearing_workaround.strict_music("voice", FakeRiff(True)))
                self.assertIsNone(hearing_workaround.utterance_cap(FakeRiff(True)))

    def test_flag_on_only_while_playing(self):
        with self.env("1"):
            self.assertTrue(hearing_workaround.enabled())
            self.assertTrue(hearing_workaround.strict_music("voice", FakeRiff(True)))
            self.assertEqual(hearing_workaround.utterance_cap(FakeRiff(True)), 15.0)
            self.assertFalse(hearing_workaround.strict_music("voice", FakeRiff(False)))
            self.assertIsNone(hearing_workaround.utterance_cap(FakeRiff(False)))

    def test_strict_only_for_voice(self):
        with self.env("1"):
            self.assertFalse(hearing_workaround.strict_music("text", FakeRiff(True)))


if __name__ == "__main__":
    unittest.main()
