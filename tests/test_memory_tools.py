import json
import unittest

import tools
from agent import Agent
from memory_client import system_prompt
from tests.fakes import FakeClient, FakeSpeaker, FakeUi, completion


class StubMemory:
    def __init__(self):
        self.calls = []

    def read(self, slug):
        self.calls.append(("read", slug))
        return json.dumps({"slug": slug, "text": "- a"})

    def remember(self, text, slug=None):
        self.calls.append(("remember", text, slug))
        return json.dumps({"slug": slug or "core", "line": f"- {text}", "commit": "abc"})

    def forget(self, slug, text):
        self.calls.append(("forget", slug, text))
        return json.dumps({"slug": slug, "line": f"- {text}", "commit": "def"})


class MemoryToolTests(unittest.TestCase):
    def tearDown(self):
        tools.set_memory(None)

    def test_four_tools_with_memory(self):
        names = [t["function"]["name"] for t in tools.TOOL_SCHEMAS + tools.MEMORY_TOOL_SCHEMAS]
        self.assertEqual(names, ["get_weather", "memory_read", "memory_remember", "memory_forget"])
        required = {t["function"]["name"]: t["function"]["parameters"]["required"]
                    for t in tools.MEMORY_TOOL_SCHEMAS}
        self.assertEqual(required, {"memory_read": ["slug"], "memory_remember": ["text"],
                                    "memory_forget": ["slug", "text"]})

    def test_run_tool_routes_to_memory(self):
        stub = StubMemory()
        tools.set_memory(stub)
        tools.run_tool("memory_remember", '{"text":"x"}')
        tools.run_tool("memory_remember", '{"text":"y","slug":"lab"}')
        tools.run_tool("memory_read", '{"slug":"lab"}')
        tools.run_tool("memory_forget", '{"slug":"core","text":"x"}')
        self.assertEqual(stub.calls, [("remember", "x", None), ("remember", "y", "lab"),
                                      ("read", "lab"), ("forget", "core", "x")])

    def test_memory_tools_without_client(self):
        tools.set_memory(None)
        for name, args in (("memory_read", '{"slug":"a"}'), ("memory_remember", '{"text":"a"}'),
                           ("memory_forget", '{"slug":"a","text":"b"}')):
            self.assertEqual(json.loads(tools.run_tool(name, args)),
                             {"error": "long-term memory isn't set up"})

    def test_memory_tools_are_not_stale(self):
        self.assertFalse(tools.STALE_TOOLS & {"memory_read", "memory_remember", "memory_forget"})

    def test_preheat_uses_final_prompt(self):
        prompt = system_prompt("BASE", {"core": "- a", "index": "x | X | g | 2026-11-26"})
        client = FakeClient([completion("")])
        agent = Agent(client, model="m", temperature=0.4, system_prompt=prompt,
                      speaker=FakeSpeaker(), ui=FakeUi(), status=None,
                      tools=tools.TOOL_SCHEMAS + tools.MEMORY_TOOL_SCHEMAS)
        agent.preheat()
        self.assertEqual(client.calls[0]["messages"][0]["content"], prompt)
        self.assertIn("[Long-term memory]\n- a", prompt)
        self.assertEqual(len(client.calls[0]["tools"]), 4)


class ForcedForgetTests(unittest.TestCase):
    """Gemma says "I've wiped that" without calling memory_forget (live check: 0/9 runs;
    forced 9/9), so a clear request to forget forces the call, like weather does."""

    def setUp(self):
        tools.set_memory(StubMemory())

    def tearDown(self):
        tools.set_memory(None)

    def test_forget_requests_force_memory_forget(self):
        cases = {
            "forget that I live in Sibiu": "I live in Sibiu",
            "Please forget my PSU is a Riden.": "my PSU is a Riden",
            "stop remembering my address": "my address",
            "Appy, forget that I live in Sibiu": "I live in Sibiu",
            "okay, can you forget my old address please": "my old address",
            "I want you to forget my phone number": "my phone number",
            "forget about my old printer": "my old printer",
            "forget that I hate rain": "I hate rain",  # beats the weather word
        }
        for text, what in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tools.forced_call(text, frozenset()),
                                 tools.ForcedCall("memory_forget", {"slug": "core", "text": what}))

    def test_other_uses_of_forget_are_left_alone(self):
        for text in ("don't forget to buy milk", "Don't forget that I'm out tomorrow",
                     "forget it", "never mind, forget about it", "forget that, tell me a joke",
                     "forget that", "I forgot my keys", "I keep forgetting things",
                     # review: everyday sentences that only mention forgetting
                     "I don't remember the name of that film", "I always forget my keys",
                     "I tend to forget names", "Did I forget anything?",
                     "never forget the dentist on friday", "Let's not forget the meeting",
                     "you can't forget the milk", "I'll never forget that trip to Rome",
                     "I no longer remember my wifi password", "Don’t forget to buy milk",
                     "don't  forget the milk", "I do not remember that at all"):
            with self.subTest(text=text):
                self.assertIsNone(tools.forced_call(text, frozenset()))

    def test_no_forced_forget_without_memory(self):
        tools.set_memory(None)  # memory_forget isn't offered then: forcing would pick weather
        self.assertIsNone(tools.forced_call("forget that I live in Sibiu", frozenset()))

    def test_weather_is_still_forced(self):
        self.assertEqual(tools.forced_call("what's the weather?", frozenset()).name, "get_weather")


if __name__ == "__main__":
    unittest.main()
