import contextlib
import io
import unittest

from agent import Agent, prune_old_tool_outputs
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


class PruneTests(unittest.TestCase):
    def test_prunes_only_old_tool_outputs(self):
        msgs = [{"role": "tool", "content": "old"}] + [{"role": "user", "content": "x"}] * 6
        msgs.append({"role": "tool", "content": "new"})
        prune_old_tool_outputs(msgs, keep_recent=6)
        self.assertEqual(msgs[0]["content"], "[pruned]")
        self.assertEqual(msgs[-1]["content"], "new")


if __name__ == "__main__":
    unittest.main()
