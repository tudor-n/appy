import json
import unittest

import tools
from status import StatusTracker


class StatusTrackerTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.tracker = StatusTracker(self.sent.append)

    def test_dedupes_and_combines_phase_with_audio(self):
        t = self.tracker
        t.set_phase("thinking")
        t.set_phase("thinking")
        t.set_phase("replying")
        t.set_speaking(True)
        t.set_speaking(False)  # gap between sentences: still replying
        t.set_phase("idle")
        self.assertEqual(self.sent, ["thinking", "speaking", "idle"])

    def test_audio_outlasting_the_turn(self):
        t = self.tracker
        t.set_phase("replying")
        t.set_speaking(True)
        t.set_phase("idle")  # LLM done, audio still playing
        self.assertEqual(self.sent[-1], "speaking")
        t.set_speaking(False)
        self.assertEqual(self.sent[-1], "idle")

    def test_trailing_speech_during_tool_round_falls_back_to_thinking(self):
        t = self.tracker
        t.set_phase("replying")
        t.set_speaking(True)
        t.set_phase("thinking")  # tool call starts while pre-tool sentence plays
        t.set_speaking(False)
        self.assertEqual(self.sent, ["speaking", "thinking"])

    def test_force_publish_resends(self):
        self.tracker.publish(force=True)
        self.tracker.publish(force=True)
        self.tracker.publish()
        self.assertEqual(self.sent, ["idle", "idle"])

    def test_rejects_unknown_phase(self):
        with self.assertRaises(ValueError):
            self.tracker.set_phase("dancing")


class RunToolTests(unittest.TestCase):
    def setUp(self):
        self._orig = dict(tools.TOOL_MAP)
        tools.TOOL_MAP["echo"] = lambda **kw: json.dumps(kw)
        tools.TOOL_MAP["boom"] = lambda: 1 / 0

    def tearDown(self):
        tools.TOOL_MAP.clear()
        tools.TOOL_MAP.update(self._orig)

    def error(self, name, args):
        return json.loads(tools.run_tool(name, args))["error"]

    def test_passes_arguments(self):
        self.assertEqual(json.loads(tools.run_tool("echo", '{"a": 1}')), {"a": 1})

    def test_empty_arguments(self):
        self.assertEqual(json.loads(tools.run_tool("echo", "  ")), {})

    def test_unknown_tool(self):
        self.assertIn("Unknown tool", self.error("nope", "{}"))

    def test_bad_json(self):
        self.assertIn("Invalid JSON", self.error("echo", "{not json"))

    def test_non_object_arguments(self):
        self.assertIn("JSON object", self.error("echo", "[1, 2]"))

    def test_tool_exception(self):
        self.assertIn("ZeroDivisionError", self.error("boom", "{}"))

    def test_unexpected_argument(self):
        self.assertIn("TypeError", self.error("boom", '{"x": 1}'))


if __name__ == "__main__":
    unittest.main()
