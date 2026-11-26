"""Tests for turning session logs into consolidator windows (runs on the Mac's 3.9 too).

Don't import tests/fakes.py here: it uses `str | None` at runtime, which 3.9 rejects.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from appy_memory.transcript import load_log, render_turns, windows

TS = "2026-11-27T00:13:17.304564+02:00"


def ev(kind: str, **fields) -> dict:
    return {"v": 1, "seq": 0, "ts": TS, "type": kind, **fields}


class RenderTests(unittest.TestCase):
    def test_render_basic_turn(self):
        events = [
            ev("session_start", model="m", effort="none"),
            ev("user", text="hi", source="text"),
            ev("assistant", text="hello", expression="happy"),
            ev("tool_call", call_id="c1", name="get_weather", args='{"day":0}'),
            ev("tool_result", call_id="c1", name="get_weather", result='{"temp_c":3}'),
            ev("assistant", text="It's 3 degrees.", expression="neutral"),
            ev("user", text="thanks", source="voice"),
        ]
        self.assertEqual(render_turns(events), [
            "[00:13] U: hi\n[00:13] A: hello\n[00:13] A called get_weather({\"day\":0})\n"
            "  -> {\"temp_c\":3}\n[00:13] A: It's 3 degrees.",
            "[00:13] U: thanks",
        ])

    def test_tool_text_is_cut_to_200(self):
        events = [ev("user", text="q", source="text"),
                  ev("tool_call", call_id="c", name="t", args="a" * 300),
                  ev("tool_result", call_id="c", name="t", result="r" * 300)]
        turn = render_turns(events)[0]
        self.assertIn("A called t(" + "a" * 199 + "…)", turn)
        self.assertIn("  -> " + "r" * 199 + "…", turn)

    def test_turn_error_marks_no_reply(self):
        events = [ev("user", text="remember my bike is red", source="voice"),
                  ev("tool_call", call_id="c", name="memory_remember", args="{}"),
                  ev("turn_error", error="APIConnectionError", reply="Sorry, I couldn't reach my model.")]
        self.assertEqual(render_turns(events), [
            "[00:13] U: remember my bike is red\n(Appy failed to answer this; it never got a reply)"])

    def test_compaction_kept(self):
        events = [ev("user", text="a", source="text"), ev("assistant", text="b", expression="x"),
                  ev("compaction", summary="GOAL: build a suit")]
        self.assertEqual(render_turns(events)[-1],
                         "[00:13] (summary of earlier conversation) GOAL: build a suit")

    def test_multiline_text_stays_on_its_speaker(self):
        events = [ev("user", text="line one\nline two", source="text")]
        self.assertEqual(render_turns(events), ["[00:13] U: line one line two"])

    def test_malformed_and_unknown_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text("\n".join([
                json.dumps(ev("user", text="hi", source="text")),
                "not json",
                "[1, 2]",
                json.dumps(ev("mystery", foo=1)),
                '{"type": "assistant", "text": "cut off mid-wr',
            ]) + "\n", encoding="utf-8")
            events = load_log(path)
        self.assertEqual([e["type"] for e in events], ["user", "mystery"])
        self.assertEqual(render_turns(events), ["[00:13] U: hi"])


class WindowTests(unittest.TestCase):
    def test_windows_pack_at_turn_boundaries(self):
        turns = ["a" * 320, "b" * 320, "c" * 320]  # 100 tokens each
        self.assertEqual(windows(turns, 250), ["a" * 320 + "\n" + "b" * 320, "c" * 320])

    def test_windows_cut_oversized_turn(self):
        out = windows(["x" * 1000, "y"], 100)
        self.assertEqual(out, ["x" * 319 + "…", "y"])

    def test_windows_empty(self):
        self.assertEqual(windows([], 5000), [])


if __name__ == "__main__":
    unittest.main()
