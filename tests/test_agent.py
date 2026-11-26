import contextlib
import io
import unittest

import agent as agent_mod
from agent import Agent, compact_history, prune_old_tool_outputs
from status import ActivityCounter, StatusTracker
from tools import ForcedCall
from tests.fakes import FakeClient, FakeSpeaker, FakeUi, text_chunk, tool_chunk


def make_agent(scripts, run_tool=None):
    client = FakeClient(scripts)
    speaker = FakeSpeaker()
    ui = FakeUi()
    agent = Agent(
        client,
        model="test-model",
        temperature=0.0,
        system_prompt="sys",
        speaker=speaker,
        ui=ui,
        status=StatusTracker(lambda state: ui.events.append(("status", state))),
        run_tool=run_tool or (lambda name, args: '{"ok":true}'),
    )
    return agent, client, speaker, ui


def run_quietly(agent: Agent, text: str, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        agent.run_turn(text, **kwargs)


class AgentTurnTests(unittest.TestCase):
    def test_plain_reply_is_spoken_shown_and_recorded(self):
        agent, _, speaker, ui = make_agent([[
            text_chunk("[happy] Hello"),
            text_chunk(" there, my good friend. How are"),
            text_chunk(" you?"),
        ]])
        run_quietly(agent, "hi")

        self.assertEqual(speaker.spoken, ["Hello there, my good friend.", "How are you?"])
        self.assertIn(("response", "Hello there, my good friend. How are you?", "happy"), ui.events)
        self.assertEqual(agent.messages[-1], {
            "role": "assistant",
            "content": "Hello there, my good friend. How are you?",
        })

    def test_tool_call_round_trip(self):
        seen = []

        def run_tool(name, args):
            seen.append((name, args))
            return '{"temp_c":21}'

        agent, client, speaker, ui = make_agent([
            [
                tool_chunk(0, "call_1", "get_", None),
                tool_chunk(0, None, "weather", "{}"),
            ],
            [text_chunk("[neutral] It's 21 degrees.")],
        ], run_tool=run_tool)
        run_quietly(agent, "weather?")

        self.assertEqual(seen, [("get_weather", "{}")])
        roles = [m["role"] for m in agent.messages]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(agent.messages[2]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(agent.messages[3]["tool_call_id"], "call_1")
        self.assertEqual(speaker.spoken, ["It's 21 degrees."])
        self.assertEqual(len(client.calls), 2)

    def test_empty_reply_does_not_record_assistant_message(self):
        agent, _, _, ui = make_agent([[text_chunk("   ")]])
        run_quietly(agent, "hi")
        self.assertEqual([m["role"] for m in agent.messages], ["system", "user"])
        self.assertNotIn("response", [e[0] for e in ui.events])

    def test_usage_only_chunks_are_ignored(self):
        from types import SimpleNamespace

        agent, _, _, ui = make_agent([[SimpleNamespace(choices=[]), text_chunk("Okay then.")]])
        run_quietly(agent, "hi")
        self.assertIn(("response", "Okay then.", "neutral"), ui.events)


    def test_status_sequence_for_tool_turn(self):
        agent, _, _, ui = make_agent([
            [text_chunk("Let me check, one moment please. "), tool_chunk(0, "c1", "get_weather", "{}")],
            [text_chunk("Sunny and warm.")],
        ])
        run_quietly(agent, "weather?")
        statuses = [e[1] for e in ui.events if e[0] == "status"]
        # thinking -> talking -> back to thinking while the tool runs -> talking -> idle
        self.assertEqual(statuses, ["thinking", "speaking", "thinking", "speaking", "idle"])

    def test_llm_error_rolls_back_and_reports(self):
        agent, _, _, ui = make_agent([ConnectionError("server down")])
        run_quietly(agent, "hi")  # must not raise

        self.assertEqual([m["role"] for m in agent.messages], ["system"])
        responses = [e for e in ui.events if e[0] == "response"]
        self.assertEqual(len(responses), 1)
        self.assertIn("ConnectionError", responses[0][1])
        self.assertEqual(ui.events[-1], ("status", "idle"))

    def test_error_mid_tool_loop_rolls_back_whole_turn(self):
        agent, _, _, _ = make_agent([
            [text_chunk("[happy] Hello friend, nice to see you.")],
            [tool_chunk(0, "c1", "get_weather", "{}")],
            TimeoutError("slow"),
        ])
        run_quietly(agent, "first")
        before = [dict(m) for m in agent.messages]
        run_quietly(agent, "second")
        self.assertEqual(agent.messages, before)

    def test_tool_rounds_are_capped(self):
        looping = [[tool_chunk(0, f"c{i}", "get_weather", "{}")] for i in range(agent_mod.MAX_TOOL_ROUNDS)]
        final = [tool_chunk(0, "cx", "get_weather", "{}"), text_chunk("Fine, it's sunny.")]
        calls = []
        agent, client, _, ui = make_agent(
            looping + [final], run_tool=lambda n, a: calls.append(n) or "{}"
        )
        run_quietly(agent, "weather?")

        self.assertEqual(len(calls), agent_mod.MAX_TOOL_ROUNDS)
        self.assertEqual(len(client.calls), agent_mod.MAX_TOOL_ROUNDS + 1)
        self.assertNotIn("tool_choice", client.calls[0])
        self.assertEqual(client.calls[-1]["tool_choice"], "none")
        self.assertEqual(agent.messages[-1], {"role": "assistant", "content": "Fine, it's sunny."})

    def test_compaction_happens_before_request(self):
        agent, client, _, ui = make_agent([[text_chunk("Okay.")]])
        for i in range(agent_mod.HISTORY_MAX_MESSAGES // 2 + 1):
            agent.messages.append({"role": "user", "content": f"u{i}"})
            agent.messages.append({"role": "assistant", "content": f"a{i}"})
        run_quietly(agent, "latest")

        sent = client.calls[0]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sent[1]["role"], "user")
        self.assertLessEqual(len(sent) - 1, agent_mod.HISTORY_KEEP_MESSAGES + 1)
        self.assertEqual(sent[-1]["content"], "latest")
        self.assertEqual(ui.events[0], ("status", "thinking"))  # no "compacted for 0s" pill


    def test_no_idle_while_last_sentence_is_still_queued(self):
        # Regression: the last sentence is only queued when the stream ends, so the
        # turn finishes before its audio starts. That must not flash "idle".
        agent, _, speaker, ui = make_agent([[text_chunk("Sure, it's 14 degrees in Sibiu.")]])
        activity = ActivityCounter(
            lambda: agent.status.set_speaking(True), lambda: agent.status.set_speaking(False)
        )
        speaker.on_speak = activity.add
        run_quietly(agent, "temp?")

        statuses = lambda: [e[1] for e in ui.events if e[0] == "status"]
        self.assertEqual(statuses(), ["thinking", "speaking"])
        activity.done()  # audio finished
        self.assertEqual(statuses(), ["thinking", "speaking", "idle"])

    def test_capped_round_with_only_tool_calls_still_answers(self):
        looping = [[tool_chunk(0, f"c{i}", "get_weather", "{}")] for i in range(agent_mod.MAX_TOOL_ROUNDS + 1)]
        agent, _, _, ui = make_agent(looping)
        run_quietly(agent, "weather?")
        self.assertIn(("response", agent_mod.TOOL_LOOP_FALLBACK, "skeptical"), ui.events)
        self.assertEqual(agent.messages[-1]["content"], agent_mod.TOOL_LOOP_FALLBACK)

    def test_preamble_is_noted_in_the_tool_result(self):
        # Regression: "hi there. what's the weather" produced the greeting twice,
        # because the model can't see text it wrote alongside a tool call.
        agent, client, _, _ = make_agent([
            [text_chunk("[happy] Good afternoon, sir."), tool_chunk(0, "c1", "get_weather", "{}")],
            [text_chunk("It's 3 degrees.")],
        ], run_tool=lambda n, a: '{"temp_c":3}')
        seen = []
        agent.on_tool_result = lambda name, result: seen.append(result)
        run_quietly(agent, "hi there. what's the weather btw")

        tool_msg = client.calls[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertTrue(tool_msg["content"].startswith('{"temp_c":3}'))
        self.assertIn('You already told the user: "Good afternoon, sir."', tool_msg["content"])
        self.assertEqual(seen, ['{"temp_c":3}'])  # the UI hook still gets the raw result

    def test_no_note_without_a_preamble(self):
        agent, client, _, _ = make_agent([
            [tool_chunk(0, "c1", "get_weather", "{}")],
            [text_chunk("It's 3 degrees.")],
        ], run_tool=lambda n, a: '{"temp_c":3}')
        run_quietly(agent, "weather?")
        self.assertEqual(client.calls[1]["messages"][-1]["content"], '{"temp_c":3}')

    def test_forced_tool_round(self):
        agent, client, _, _ = make_agent([
            [tool_chunk(0, "c1", "get_weather", '{"day":0}')],
            [text_chunk("It'll be 5 degrees.")],
        ])
        agent.tools = [
            {"type": "function", "function": {"name": "get_weather"}},
            {"type": "function", "function": {"name": "other"}},
        ]
        run_quietly(agent, "and in 3 hours?", force=ForcedCall("get_weather", {"day": 0}))

        first, second = client.calls
        self.assertEqual(first["tool_choice"], "required")
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["get_weather"])
        self.assertNotIn("tool_choice", second)  # only the first round is forced
        self.assertEqual(len(second["tools"]), 2)
        self.assertEqual(agent.recent_tools, frozenset({"get_weather"}))

    def test_recent_tools_cover_the_last_few_turns(self):
        agent, _, _, _ = make_agent(
            [[tool_chunk(0, "c1", "get_weather", "{}")], [text_chunk("Rain.")]]
            + [[text_chunk("Sure.")]] * agent_mod.RECENT_TURNS
        )
        run_quietly(agent, "weather?")
        for _ in range(agent_mod.RECENT_TURNS - 1):
            run_quietly(agent, "thanks!")
            self.assertEqual(agent.recent_tools, frozenset({"get_weather"}))
        run_quietly(agent, "ok")
        self.assertEqual(agent.recent_tools, frozenset())

    def test_skipped_forced_call_is_made_by_the_agent(self):
        # Regression: LM Studio's tool_choice="required" is only a hint; the model's
        # made-up answer must never be spoken or shown, and the call happens anyway.
        ran = []
        agent, client, speaker, ui = make_agent([
            [text_chunk("It's still raining, sir.")],   # ignored the requirement
            [text_chunk("It'll clear up by five.")],    # answers from the real data
        ], run_tool=lambda n, a: ran.append((n, a)) or '{"temp_c":5}')
        run_quietly(agent, "and saturday?", force=ForcedCall("get_weather", {"day": 2}))

        self.assertEqual(ran, [("get_weather", '{"day": 2}')])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["tool_choice"], "required")
        self.assertNotIn("It's still raining, sir.", " ".join(speaker.spoken))
        self.assertNotIn(("response", "It's still raining, sir.", "neutral"), ui.events)
        self.assertEqual(speaker.spoken, ["It'll clear up by five."])
        # The second round saw a well-formed call/result pair
        sent = client.calls[1]["messages"]
        self.assertEqual(sent[-2]["tool_calls"][0]["id"], sent[-1]["tool_call_id"])

    def test_forced_round_preamble_is_spoken_once_the_call_is_confirmed(self):
        agent, _, speaker, ui = make_agent([
            [text_chunk("Let me check, sir."), tool_chunk(0, "c1", "get_weather", "{}")],
            [text_chunk("It's 3 degrees.")],
        ])
        run_quietly(agent, "weather?", force=ForcedCall("get_weather", {"day": 0}))
        self.assertEqual(speaker.spoken, ["Let me check, sir.", "It's 3 degrees."])
        self.assertIn(("response", "Let me check, sir.", "neutral"), ui.events)

    def test_stale_tool_exchanges_are_dropped_after_the_turn(self):
        # Regression: with an old weather exchange in the history, Gemma answered weather
        # follow-ups from it (no tool call, no card) and LM Studio ignored
        # tool_choice="required".
        agent, client, _, _ = make_agent([
            [text_chunk("Let me check, sir."), tool_chunk(0, "c1", "get_weather", "{}"),
             tool_chunk(1, "c2", "other_tool", "{}")],
            [text_chunk("It's 3 degrees.")],
        ], run_tool=lambda n, a: f'{{"from":"{n}"}}')
        agent.stale_tools = frozenset({"get_weather"})
        run_quietly(agent, "weather?")

        # The follow-up round within the turn still saw the real result...
        self.assertIn('{"from":"get_weather"}', client.calls[1]["messages"][-2]["content"])
        # ...but afterwards only the other tool's exchange remains
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["c2"])
        calls = agent.messages[2]["tool_calls"]
        self.assertEqual([c["function"]["name"] for c in calls], ["other_tool"])

    def test_dropping_an_exchange_folds_the_preamble_into_the_answer(self):
        agent, _, _, _ = make_agent([
            [text_chunk("Good afternoon, sir."), tool_chunk(0, "c1", "get_weather", "{}")],
            [text_chunk("It's 3 degrees.")],
        ])
        agent.stale_tools = frozenset({"get_weather"})
        run_quietly(agent, "hi, what's the weather")
        self.assertEqual(agent.messages[1:], [
            {"role": "user", "content": "hi, what's the weather"},
            {"role": "assistant", "content": "Good afternoon, sir. It's 3 degrees."},
        ])

    def test_tool_results_reach_the_hook(self):
        seen = []
        agent, _, _, _ = make_agent([
            [tool_chunk(0, "c1", "get_weather", "{}")],
            [text_chunk("Sunny.")],
        ], run_tool=lambda n, a: '{"temp_c":21}')
        agent.on_tool_result = lambda name, result: seen.append((name, result))
        run_quietly(agent, "weather?")
        self.assertEqual(seen, [("get_weather", '{"temp_c":21}')])

    def test_broken_hook_does_not_break_the_turn(self):
        agent, _, _, ui = make_agent([
            [tool_chunk(0, "c1", "get_weather", "{}")],
            [text_chunk("Sunny.")],
        ])
        agent.on_tool_result = lambda name, result: 1 / 0
        run_quietly(agent, "weather?")
        self.assertIn(("response", "Sunny.", "neutral"), ui.events)

    def test_error_messages(self):
        import httpx
        import openai

        req = httpx.Request("POST", "http://x")
        conn = openai.APIConnectionError(request=req)
        status = openai.APIStatusError("bad", response=httpx.Response(500, request=req), body=None)
        self.assertIn("couldn't reach", agent_mod.describe_error(conn))
        self.assertIn("500", agent_mod.describe_error(status))
        self.assertIn("ValueError", agent_mod.describe_error(ValueError("x")))


class CompactHistoryTests(unittest.TestCase):
    def test_noop_under_limit(self):
        msgs = [{"role": "system"}] + [{"role": "user"}] * 5
        self.assertFalse(compact_history(msgs, max_messages=10, keep=4))
        self.assertEqual(len(msgs), 6)

    def test_never_starts_with_orphaned_tool_message(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(10):
            msgs += [
                {"role": "user", "content": f"u{i}"},
                {"role": "assistant", "content": None, "tool_calls": []},
                {"role": "tool", "content": "{}"},
                {"role": "assistant", "content": f"a{i}"},
            ]
        self.assertTrue(compact_history(msgs, max_messages=20, keep=6))
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], "a9")


class PruneTests(unittest.TestCase):
    def test_prunes_only_old_tool_outputs(self):
        msgs = [{"role": "tool", "content": "old"}] + [{"role": "user", "content": "x"}] * 6
        msgs.append({"role": "tool", "content": "new"})
        prune_old_tool_outputs(msgs, keep_recent=6)
        self.assertEqual(msgs[0]["content"], "[pruned]")
        self.assertEqual(msgs[-1]["content"], "new")


if __name__ == "__main__":
    unittest.main()
