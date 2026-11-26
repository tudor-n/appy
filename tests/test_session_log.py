import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_log import SessionLogger

FIXED = datetime(2026, 11, 26, 15, 30, 12, tzinfo=timezone(timedelta(hours=2)))


def make_logger(logs_dir) -> SessionLogger:
    return SessionLogger(Path(logs_dir), now=lambda: FIXED)


def read_lines(log: SessionLogger) -> list[dict]:
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]


class SessionLoggerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_session_id_and_path(self):
        log = make_logger(self.dir)
        self.assertEqual(log.session_id, "2026-11-26_153012")
        self.assertEqual(log.path.name, "2026-11-26_153012.jsonl")

    def test_events_are_one_json_line_each(self):
        log = make_logger(self.dir)
        log.start(model="m", effort="none")
        log.user(text="hi", source="voice")
        log.tool_call(call_id="c1", name="get_weather", args="{}")
        log.tool_result(call_id="c1", name="get_weather", result='{"temp_c":3}')
        log.assistant(text="It's 3 degrees.", expression="neutral")
        log.compaction(summary="FACTS: x")
        log.end(reason="quit")

        lines = read_lines(log)
        self.assertEqual(
            [e["type"] for e in lines],
            ["session_start", "user", "tool_call", "tool_result", "assistant", "compaction", "session_end"],
        )
        self.assertEqual([e["seq"] for e in lines], list(range(1, 8)))
        self.assertTrue(all(e["v"] == 1 for e in lines))
        self.assertTrue(all(e["ts"].endswith("+02:00") for e in lines))
        self.assertEqual(lines[1]["text"], "hi")
        self.assertEqual(lines[1]["source"], "voice")

    def test_file_is_valid_without_end(self):
        log = make_logger(self.dir)
        log.start(model="m", effort="none")
        log.user(text="hello", source="text")
        # No end(): simulates a crash. Everything written so far must be on disk.
        lines = read_lines(log)
        self.assertEqual(lines[-1]["type"], "user")

    def test_tool_result_is_truncated(self):
        log = make_logger(self.dir)
        log.tool_result(call_id="c1", name="t", result="x" * 5000)
        log.tool_result(call_id="c2", name="t", result="short")
        long_line, short_line = read_lines(log)
        self.assertEqual(len(long_line["result"]), 4096)
        self.assertIs(long_line["truncated"], True)
        self.assertNotIn("truncated", short_line)

    def test_unwritable_dir_never_raises(self):
        not_a_dir = self.dir / "file"
        not_a_dir.write_text("x")
        log = make_logger(not_a_dir)
        with contextlib.redirect_stdout(io.StringIO()):
            log.start(model="m", effort="none")
            log.user(text="hi", source="voice")
            log.end(reason="quit")

    def test_non_ascii_is_kept_readable(self):
        log = make_logger(self.dir)
        log.user(text="Bună ziua 👋", source="voice")
        self.assertIn("Bună ziua 👋", log.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
