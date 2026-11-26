"""Tests for the consolidator, its LLM client and its vault storage (runs on 3.9 too).

Don't import tests/fakes.py here: it uses `str | None` at runtime, which 3.9 rejects.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from appy_memory.llm import LLM, LLMError
from appy_memory.vault import Vault


class LLMTests(unittest.TestCase):
    def make(self, handler) -> LLM:
        http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://lm/v1")
        return LLM("http://lm/v1", "gemma", api_key="k", effort="low", http=http)

    def test_llm_sends_json_schema_and_effort(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "{\"notes\":[]}"}}],
                                             "usage": {"total_tokens": 42}})

        text, tokens = self.make(handler).chat([{"role": "user", "content": "x"}], temperature=0.1,
                                               max_tokens=400, schema={"type": "object"})
        self.assertEqual((text, tokens), ('{"notes":[]}', 42))
        self.assertTrue(seen["url"].endswith("/v1/chat/completions"))
        self.assertEqual(seen["auth"], "Bearer k")
        body = seen["body"]
        self.assertEqual(body["model"], "gemma")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.1, 400))
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["schema"], {"type": "object"})

    def test_plain_request_has_no_response_format(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

        self.assertEqual(self.make(handler).chat([], temperature=0.1, max_tokens=5), ("hi", 0))
        self.assertNotIn("response_format", seen["body"])

    def test_llm_errors(self):
        def boom(request):
            raise httpx.ConnectError("down")

        for handler in (lambda r: httpx.Response(500, text="oops"), boom,
                        lambda r: httpx.Response(200, json={"nope": 1}),
                        lambda r: httpx.Response(200, text="not json")):
            with self.subTest(handler=handler), self.assertRaises(LLMError):
                self.make(handler).chat([], temperature=0.1, max_tokens=5)


class VaultProposalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = Vault(self.root).open()

    def tearDown(self):
        self._tmp.cleanup()

    def test_note_text_and_hash(self):
        self.assertIsNone(self.vault.note_text("lab"))
        self.assertEqual(self.vault.note_hash("lab"), "")
        self.vault.remember("The printer is a Prusa.", "lab", "s1")
        text = self.vault.note_text("lab")
        self.assertEqual(self.vault.note_hash("lab"), hashlib.sha256(text.encode()).hexdigest())

    def test_save_proposal_merges_sessions(self):
        first = self.vault.save_proposal("lab", "Lab", "# Lab\n> g\n- a\n", "added a", "s1", 0)
        self.assertEqual(first["sessions"], ["s1"])
        second = self.vault.save_proposal("lab", "Lab", "# Lab\n> g\n- a\n- b\n", "added b", "s2", 1)
        self.assertEqual(second["sessions"], ["s1", "s2"])
        self.assertEqual(second["sources"], [["s1", 0], ["s2", 1]])
        self.assertEqual(second["created"], first["created"])
        self.assertEqual(second["new_text"], "# Lab\n> g\n- a\n- b\n")
        self.assertEqual(second["summary"], "added b")
        self.assertEqual(self.vault.proposal("lab"), second)
        self.assertEqual(json.loads((self.root / "pending" / "lab.json").read_text()), second)
        self.assertEqual([p["slug"] for p in self.vault.proposals()], ["lab"])

    def test_base_hash_tracks_current_note(self):
        self.assertEqual(self.vault.save_proposal("lab", "Lab", "x", "s", "s1", 0)["base_hash"], "")
        self.vault.remember("The printer is a Prusa.", "lab", "s1")
        self.assertEqual(self.vault.save_proposal("lab", "Lab", "y", "s", "s1", 1)["base_hash"],
                         self.vault.note_hash("lab"))

    def test_broken_proposal_file_is_ignored(self):
        (self.root / "pending" / "lab.json").write_text("{nope")
        self.assertIsNone(self.vault.proposal("lab"))
        self.assertEqual(self.vault.proposals(), [])

    def test_sessions_to_consolidate_skips_done(self):
        for sid in ("2026-11-27_001511", "2026-11-26_090000", "2026-11-27_001310"):
            self.vault.store_log(sid, b'{"type":"session_start"}\n')
        self.vault.set_session_state("2026-11-27_001310", windows=2, done_windows=2, done=True)
        self.vault.set_session_state("2026-11-27_001511", windows=3, done_windows=1, done=False)
        self.assertEqual(self.vault.sessions_to_consolidate(),
                         ["2026-11-26_090000", "2026-11-27_001511"])
        self.assertEqual(self.vault.session_state("2026-11-27_001511"),
                         {"windows": 3, "done_windows": 1, "done": False})
        self.assertEqual(self.vault.session_state("nope"),
                         {"windows": 0, "done_windows": 0, "done": False})

    def test_session_state_and_forgotten_share_state_json(self):
        self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        self.vault.forget("core", "sibiu", "s1")
        self.vault.set_session_state("s1", windows=1, done_windows=1, done=True)
        self.assertEqual(self.vault.forgotten_lines(), ["- Tudor lives in Sibiu."])
        self.vault.remember("Tudor likes tea.", None, "s1")
        self.vault.forget("core", "tea", "s1")
        self.assertTrue(self.vault.session_state("s1")["done"])

    def test_gist_cut_at_word(self):
        fact = "The suit uses " + "very " * 30 + "strong actuators."
        self.vault.remember(fact, "suit", "s1")
        gist = self.vault.note_text("suit").splitlines()[1]
        self.assertTrue(gist.startswith("> The suit uses very"))
        self.assertTrue(gist.endswith("very…"))
        self.assertLessEqual(len(gist), 122)

    def test_open_adds_missing_gitignore_entries(self):
        (self.root / ".gitignore").write_text("logs/\npending/\n")
        Vault(self.root).open()
        lines = (self.root / ".gitignore").read_text().split()
        for entry in ("logs/", "pending/", "state.json", "memory/_index.md", "dumps/"):
            self.assertIn(entry, lines)
        self.assertEqual(len(lines), 5)


if __name__ == "__main__":
    unittest.main()
