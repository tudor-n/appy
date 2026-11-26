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

from appy_memory.consolidator import Rejected, rewrite, route
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


class FakeLLM:
    """Stands in for LLM: replays scripted outputs and records every call."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, temperature, max_tokens, schema=None):
        self.calls.append({"messages": [dict(m) for m in messages], "temperature": temperature,
                           "max_tokens": max_tokens, "schema": schema})
        if not self.responses:
            raise AssertionError("FakeLLM ran out of responses")
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out, 100

    def prompt(self, i=-1) -> str:
        return "\n".join(m["content"] for m in self.calls[i]["messages"])


def routed(*notes) -> str:
    return json.dumps({"notes": [dict(zip(("slug", "new", "title", "why"), n)) for n in notes]})


NOTE = "# Mark I suit\n> Iron Man style arm exoskeleton\n- The suit uses BLDC motors.\n"


class RouteRewriteTests(unittest.TestCase):
    def test_route_parses_and_validates(self):
        llm = FakeLLM([routed(("Bad Slug", True, "X", "w"), ("core", True, "Core", "name"),
                              ("mark-i-suit", True, "Mark I", "hud"), ("lumen", False, "", "kpi"),
                              ("mark-i-suit", False, "", "dup"), ("extra-one", True, "E", "e"),
                              ("extra-two", True, "F", "f"))])
        notes, tokens = route(llm, "idx", "", [], "U: hi", existing={"mark-i-suit"})
        self.assertEqual(tokens, 100)
        self.assertEqual(notes, [
            {"slug": "core", "new": False, "title": "Core", "why": "name"},
            {"slug": "mark-i-suit", "new": False, "title": "Mark I", "why": "hud"},
            {"slug": "lumen", "new": True, "title": "Lumen", "why": "kpi"},
        ])

    def test_route_empty_and_bad_json(self):
        self.assertEqual(route(FakeLLM([routed()]), "", "", [], "w", existing=set())[0], [])
        with self.assertRaises(Rejected):
            route(FakeLLM(["not json"]), "", "", [], "w", existing=set())

    def test_route_sends_schema(self):
        llm = FakeLLM([routed()])
        route(llm, "idx-line", "- core fact", [], "U: window text", existing=set())
        call = llm.calls[0]
        self.assertEqual(call["schema"]["required"], ["notes"])
        self.assertEqual((call["temperature"], call["max_tokens"]), (0.1, 400))
        for text in ("idx-line", "- core fact", "U: window text"):
            self.assertIn(text, llm.prompt())

    def test_prompts_carry_forgotten_lines(self):
        forgotten = ["- Tudor lives in Sibiu.", "- The PSU is a Riden."]
        r = FakeLLM([routed()])
        route(r, "", "", forgotten, "w", existing=set())
        w = FakeLLM(["- Tudor likes tea.\nSUMMARY: tea"])
        rewrite(w, "core", "", "Core", "w", forgotten)
        for llm in (r, w):
            for line in forgotten:
                self.assertIn(line, llm.prompt())

    def test_rewrite_new_note_ok(self):
        llm = FakeLLM(["# Lumen\n> Tudor's BI app\n- Lumen uses DuckDB.\n\nSUMMARY: new note for Lumen"])
        text, summary, _ = rewrite(llm, "lumen", None, "Lumen", "w", [], why="BI app")
        self.assertEqual(text, "# Lumen\n> Tudor's BI app\n- Lumen uses DuckDB.\n")
        self.assertEqual(summary, "new note for Lumen")
        self.assertIn("BI app", llm.prompt())
        self.assertEqual((llm.calls[0]["temperature"], llm.calls[0]["max_tokens"]), (0.1, 1200))
        self.assertIsNone(llm.calls[0]["schema"])

    def test_rewrite_strips_fences_and_long_gist(self):
        llm = FakeLLM(["```markdown\n# Mark I suit\n> " + "g " * 80 + "\n- The suit uses BLDC motors.\n"
                       "- The HUD runs on a Pi Zero 2 W.\n```\nSUMMARY: added HUD"])
        text, summary, _ = rewrite(llm, "mark-i-suit", NOTE, "Mark I suit", "w", [])
        lines = text.splitlines()
        self.assertEqual(lines[0], "# Mark I suit")
        self.assertLessEqual(len(lines[1]), 122)
        self.assertEqual(lines[-1], "- The HUD runs on a Pi Zero 2 W.")
        self.assertEqual(summary, "added HUD")

    def test_rewrite_rejects_lost_title(self):
        llm = FakeLLM(["- The suit uses BLDC motors.\n- New fact.\nSUMMARY: x"])
        with self.assertRaises(Rejected) as ctx:
            rewrite(llm, "mark-i-suit", NOTE, "Mark I suit", "w", [])
        self.assertIn("title", ctx.exception.reason)

    def test_rewrite_rejects_renamed_title(self):
        llm = FakeLLM(["# Iron suit\n> g\n- The suit uses BLDC motors.\n- New.\nSUMMARY: x"])
        with self.assertRaises(Rejected):
            rewrite(llm, "mark-i-suit", NOTE, "Mark I suit", "w", [])

    def test_rewrite_rejects_non_bullet_lines(self):
        llm = FakeLLM(["# Mark I suit\n> g\nThe suit has a new HUD now.\nSUMMARY: x"])
        with self.assertRaises(Rejected) as ctx:
            rewrite(llm, "mark-i-suit", NOTE, "Mark I suit", "w", [])
        self.assertIn("bullet", ctx.exception.reason)

    def test_rewrite_rejects_unchanged(self):
        llm = FakeLLM([NOTE.replace("motors.", "motors. ") + "SUMMARY: nothing new"])
        with self.assertRaises(Rejected) as ctx:
            rewrite(llm, "mark-i-suit", NOTE, "Mark I suit", "w", [])
        self.assertIn("no change", ctx.exception.reason)

    def test_rewrite_retries_once_when_too_long(self):
        long_note = "# Mark I suit\n> g\n" + "- A fact that takes room in the note.\n" * 80
        llm = FakeLLM([long_note + "SUMMARY: x", "# Mark I suit\n> g\n- Short.\nSUMMARY: y"])
        text, summary, tokens = rewrite(llm, "mark-i-suit", NOTE, "Mark I suit", "w", [])
        self.assertEqual((text, summary, tokens), ("# Mark I suit\n> g\n- Short.\n", "y", 200))
        self.assertIn("shorter", llm.calls[1]["messages"][-1]["content"])
        llm = FakeLLM([long_note + "SUMMARY: x", long_note + "SUMMARY: x"])
        with self.assertRaises(Rejected) as ctx:
            rewrite(llm, "mark-i-suit", NOTE, "Mark I suit", "w", [])
        self.assertIn("over cap", ctx.exception.reason)

    def test_rewrite_core_has_no_header(self):
        llm = FakeLLM(["# Core\n- Tudor lives in Sibiu.\nSUMMARY: x"])
        with self.assertRaises(Rejected):
            rewrite(llm, "core", "", "Core", "w", [])
        llm = FakeLLM(["- Tudor lives in Sibiu.\n"])  # no SUMMARY line
        self.assertEqual(rewrite(llm, "core", "", "Core", "w", [])[:2],
                         ("- Tudor lives in Sibiu.\n", "updated core"))

    def test_dump_files_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, w = Path(tmp) / "r.txt", Path(tmp) / "w.txt"
            route(FakeLLM([routed()]), "", "", [], "WINDOW", existing=set(), dump=r)
            rewrite(FakeLLM(["- x\nSUMMARY: s"]), "core", "", "Core", "WINDOW", [], dump=w)
            for path in (r, w):
                text = path.read_text()
                self.assertIn("WINDOW", text)
                self.assertIn("--- output", text)


if __name__ == "__main__":
    unittest.main()
