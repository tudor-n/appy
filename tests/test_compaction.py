import contextlib
import io
import unittest

import agent as agent_mod

from compaction import SUMMARY_ACK, SUMMARY_PREFIX, compact, estimate_tokens, render, split
from tests.fakes import FakeClient, completion

SYSTEM = {"role": "system", "content": "sys"}


def turn(i: int, size: int) -> list[dict]:
    return [
        {"role": "user", "content": f"u{i} " + "x" * size},
        {"role": "assistant", "content": f"a{i} " + "x" * size},
    ]


def tool_turn(i: int) -> list[dict]:
    return [
        {"role": "user", "content": f"u{i} " + "x" * 300},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 600},
        {"role": "assistant", "content": f"a{i} " + "x" * 300},
    ]


def history(turns: int, size: int = 1200) -> list[dict]:
    msgs = [dict(SYSTEM)]
    for i in range(turns):
        msgs += turn(i, size)
    return msgs


def summary_pair(text: str) -> list[dict]:
    return [
        {"role": "user", "content": SUMMARY_PREFIX + text},
        {"role": "assistant", "content": SUMMARY_ACK},
    ]


def assert_valid(test: unittest.TestCase, msgs: list[dict]):
    test.assertEqual(msgs[0]["role"], "system")
    for prev, cur in zip(msgs, msgs[1:]):
        test.assertFalse(prev["role"] == cur["role"] == "user", "two user messages in a row")
    for i, msg in enumerate(msgs):
        if msg["role"] == "tool":
            test.assertTrue(
                any(m.get("tool_calls") for m in msgs[:i]), "tool result without its call"
            )


class EstimateTests(unittest.TestCase):
    def test_estimate_tokens(self):
        # json.dumps adds the two quotes: 33 chars / 3.2 rounds up to 11
        self.assertEqual(estimate_tokens("a" * 31), 11)


class SplitTests(unittest.TestCase):
    def test_split_returns_none_when_everything_fits(self):
        self.assertIsNone(split(history(3, size=20), tail_budget=2500))

    def test_tail_starts_at_user_and_keeps_tool_pairs(self):
        msgs = [dict(SYSTEM)]
        for i in range(10):
            msgs += tool_turn(i)
        previous, middle, tail = split(msgs, tail_budget=1000)
        self.assertIsNone(previous)
        self.assertGreater(len(middle), 0)
        self.assertEqual(tail[0]["role"], "user")
        for i, msg in enumerate(tail):
            if msg["role"] == "tool":
                self.assertTrue(any(m.get("tool_calls") for m in tail[:i]))
        self.assertEqual(middle + tail, msgs[1:])

    def test_tail_keeps_whole_last_turn_even_if_over_budget(self):
        msgs = history(4, size=200) + turn(99, 20_000)
        _, _, tail = split(msgs, tail_budget=2500)
        self.assertEqual(tail, msgs[-2:])
        self.assertEqual(tail[0]["role"], "user")

    def test_previous_summary_is_detected(self):
        msgs = [dict(SYSTEM)] + summary_pair("old facts") + history(10)[1:]
        previous, middle, _ = split(msgs, tail_budget=1000)
        self.assertEqual(previous, "old facts")
        self.assertNotIn(SUMMARY_ACK, [m["content"] for m in middle])
        self.assertFalse(any(str(m["content"]).startswith(SUMMARY_PREFIX) for m in middle))


class RenderTests(unittest.TestCase):
    def test_render(self):
        text = render([
            {"role": "user", "content": "my cat is Pixel"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "z" * 500},
            {"role": "assistant", "content": "Sunny."},
        ])
        self.assertIn("User: my cat is Pixel", text)
        self.assertIn("Appy called get_weather({})", text)
        self.assertIn("Appy: Sunny.", text)
        tool_line = next(line for line in text.splitlines() if line.startswith("Tool:"))
        self.assertLessEqual(len(tool_line), 206)


    def test_render_skips_pruned_tool_output(self):
        text = render([
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": agent_mod.PRUNED_TOOL_OUTPUT},
            {"role": "assistant", "content": "Sunny."},
        ])
        self.assertNotIn("pruned", text)
        self.assertNotIn("Tool:", text)
        self.assertIn("Appy called get_weather({})", text)

    def test_render_drops_the_already_said_note(self):
        note = agent_mod.ALREADY_SAID_NOTE.format(said="Let me check.")
        text = render([{"role": "tool", "tool_call_id": "c1", "content": '{"temp_c":3}' + note}])
        self.assertEqual(text, 'Tool: {"temp_c":3}')


class CompactTests(unittest.TestCase):
    def pixel_history(self) -> list[dict]:
        msgs = history(10)
        msgs[1] = {"role": "user", "content": "my cat is Pixel"}
        return msgs

    def test_compact_rebuilds_history(self):
        msgs = self.pixel_history()
        system = dict(msgs[0])
        client = FakeClient([completion("FACTS: cat is Pixel")])
        self.assertEqual(compact(client, "m", msgs, tail_budget=1000), "FACTS: cat is Pixel")

        self.assertEqual(msgs[0], system)
        self.assertEqual([m["role"] for m in msgs[:4]], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], SUMMARY_PREFIX + "FACTS: cat is Pixel")
        self.assertEqual(msgs[2]["content"], "Got it.")
        self.assertEqual(msgs[-1]["content"], self.pixel_history()[-1]["content"])
        assert_valid(self, msgs)

    def test_compact_request_settings(self):
        client = FakeClient([completion("FACTS: cat is Pixel")])
        compact(client, "m", self.pixel_history(), tail_budget=1000)
        call = client.calls[0]
        self.assertEqual(call["temperature"], 0.2)
        self.assertEqual(call["reasoning_effort"], "none")
        self.assertEqual(call["max_tokens"], 800)
        self.assertNotIn("stream", call)
        prompt = call["messages"][0]["content"]
        self.assertIn("User: my cat is Pixel", prompt)
        self.assertIn("Previous summary:\nnone", prompt)

    def test_second_compaction_folds_previous_summary(self):
        msgs = [dict(SYSTEM)] + summary_pair("FACTS: cat is Pixel") + history(10)[1:]
        client = FakeClient([completion("FACTS: cat is Pixel; dog is Rex")])
        compact(client, "m", msgs, tail_budget=1000)

        self.assertIn("Previous summary:\nFACTS: cat is Pixel\n", client.calls[0]["messages"][0]["content"])
        prefixed = [m for m in msgs if str(m.get("content")).startswith(SUMMARY_PREFIX)]
        self.assertEqual(len(prefixed), 1)
        self.assertEqual(msgs[1]["content"], SUMMARY_PREFIX + "FACTS: cat is Pixel; dog is Rex")

    def test_nothing_to_fold_makes_no_request(self):
        msgs = history(2, size=20)
        before = [dict(m) for m in msgs]
        client = FakeClient([])
        self.assertIsNone(compact(client, "m", msgs, tail_budget=2500))
        self.assertEqual(client.calls, [])
        self.assertEqual(msgs, before)

    def test_failed_summary_still_shrinks(self):
        msgs = self.pixel_history()
        before = len(msgs)
        with contextlib.redirect_stdout(io.StringIO()):
            result = compact(FakeClient([ConnectionError("down")]), "m", msgs, tail_budget=1000, drop_on_failure=True)
        self.assertIsNone(result)
        self.assertLess(len(msgs), before)
        self.assertEqual(msgs[1]["role"], "user")
        assert_valid(self, msgs)

    def test_failed_summary_keeps_previous_summary(self):
        msgs = [dict(SYSTEM)] + summary_pair("FACTS: cat is Pixel") + history(10)[1:]
        with contextlib.redirect_stdout(io.StringIO()):
            compact(FakeClient([ConnectionError("down")]), "m", msgs, tail_budget=1000, drop_on_failure=True)
        self.assertEqual(msgs[1]["content"], SUMMARY_PREFIX + "FACTS: cat is Pixel")
        self.assertEqual(msgs[2]["content"], SUMMARY_ACK)
        assert_valid(self, msgs)

    def test_failed_summary_keeps_history_by_default(self):
        # A blip with headroom left: keep everything and try again next turn
        msgs = self.pixel_history()
        before = [dict(m) for m in msgs]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(compact(FakeClient([ConnectionError("down")]), "m", msgs, tail_budget=1000))
        self.assertEqual(msgs, before)

    def test_blank_summary_counts_as_failure(self):
        msgs = self.pixel_history()
        before = len(msgs)
        self.assertIsNone(
            compact(FakeClient([completion("   ")]), "m", msgs, tail_budget=1000, drop_on_failure=True)
        )
        self.assertLess(len(msgs), before)
        self.assertFalse(any(str(m.get("content")).startswith(SUMMARY_PREFIX) for m in msgs))
        assert_valid(self, msgs)


if __name__ == "__main__":
    unittest.main()
