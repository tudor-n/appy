"""Tests for the consolidator, its LLM client and its vault storage (runs on 3.9 too).

Don't import tests/fakes.py here: it uses `str | None` at runtime, which 3.9 rejects.
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
from contextlib import redirect_stdout
import tempfile
import unittest
from pathlib import Path

import httpx

from appy_memory.consolidator import ROUTE_SCHEMA, Consolidator, Rejected, Worker, rewrite, route
from appy_memory.llm import LLM, LLMError
from appy_memory.vault import Vault


class LLMTests(unittest.TestCase):
    def make(self, handler) -> LLM:
        http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://lm/v1")
        return LLM("http://lm/v1", "gemma", api_key="k", effort="low", http=http)  # reasons

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
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.1, 3400))
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["schema"], {"type": "object"})

    def test_plain_request_has_no_response_format(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

        self.assertEqual(self.make(handler).chat([], temperature=0.1, max_tokens=5), ("hi", 0))
        self.assertNotIn("response_format", seen["body"])

    def test_reasoning_gets_headroom_and_empty_output_fails(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": ""},
                                                          "finish_reason": "length"}]})

        llm = self.make(handler)
        with self.assertRaises(LLMError):
            llm.chat([], temperature=0.1, max_tokens=400)
        self.assertEqual(seen[0]["max_tokens"], 400 + 3000)  # effort "low" reasons first
        llm.effort = "none"
        with self.assertRaises(LLMError):
            llm.chat([], temperature=0.1, max_tokens=400)
        self.assertEqual(seen[1]["max_tokens"], 400)

    def test_client_errors_are_bad_requests(self):
        from appy_memory.llm import BadRequest
        for status in (400, 404, 422):
            with self.subTest(status=status), self.assertRaises(BadRequest):
                self.make(lambda r, s=status: httpx.Response(s, text="context too long")).chat(
                    [], temperature=0.1, max_tokens=5)
        for status in (408, 429, 500, 503):  # worth retrying later: plain LLMError
            with self.subTest(status=status), self.assertRaises(LLMError) as ctx:
                self.make(lambda r, s=status: httpx.Response(s)).chat([], temperature=0.1, max_tokens=5)
            self.assertNotIsInstance(ctx.exception, BadRequest)

    def test_route_and_rewrite_skip_bad_requests(self):
        from appy_memory.llm import BadRequest
        with self.assertRaises(Rejected):
            route(FakeLLM([BadRequest("400 context too long")]), "", "", [], "w", existing=set())
        with self.assertRaises(Rejected):
            rewrite(FakeLLM([BadRequest("400")]), "core", "", "Core", "w", [])

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

    def test_base_hash_is_the_note_the_proposal_was_first_built_on(self):
        self.assertEqual(self.vault.save_proposal("lab", "Lab", "x", "s", "s1", 0)["base_hash"], "")
        self.vault.remember("The printer is a Prusa.", "lab", "s1")
        # building on it later keeps the old base, so the change since reads as stale
        self.assertEqual(self.vault.save_proposal("lab", "Lab", "y", "s", "s1", 1)["base_hash"], "")
        (self.root / "pending" / "lab.json").unlink()
        self.assertEqual(self.vault.save_proposal("lab", "Lab", "z", "s", "s1", 2)["base_hash"],
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

    def test_route_schema_has_no_free_text(self):
        # a free-text field let constrained decoding loop ("wayte-1234567890: ...")
        props = ROUTE_SCHEMA["properties"]["notes"]["items"]["properties"]
        self.assertEqual(sorted(props), ["new", "slug", "title"])
        # LM Studio doesn't enforce "strict": without this Gemma adds fields of its own
        self.assertIs(ROUTE_SCHEMA["additionalProperties"], False)
        self.assertIs(ROUTE_SCHEMA["properties"]["notes"]["items"]["additionalProperties"], False)

    def test_route_prompt_counts_hypothetical_project_ideas(self):
        llm = FakeLLM([routed()])
        route(llm, "", "", [], "w", existing=set())
        self.assertIn("hypothetical", llm.prompt())
        self.assertIn("list the note anyway", llm.prompt())

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

    def test_rewrite_accepts_title_case_fix(self):
        note = "# Mark i suit\n> g\n- The suit uses BLDC motors.\n"
        llm = FakeLLM(["# Mark I Suit\n> g\n- The suit uses BLDC motors.\n- New.\nSUMMARY: x"])
        text, _, _ = rewrite(llm, "mark-i-suit", note, "Mark i suit", "w", [])
        self.assertTrue(text.startswith("# Mark I Suit\n"))

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


def log_bytes(*turns, crashed=False) -> bytes:
    """A session log: each turn is (user text, assistant reply)."""
    events = [{"v": 1, "seq": 1, "ts": "2026-11-27T00:13:10+02:00", "type": "session_start"}]
    for user, reply in turns:
        events.append({"ts": "2026-11-27T00:14:00+02:00", "type": "user", "text": user})
        events.append({"ts": "2026-11-27T00:14:05+02:00", "type": "assistant", "text": reply})
    if not crashed:
        events.append({"ts": "2026-11-27T00:20:00+02:00", "type": "session_end", "reason": "quit"})
    return "".join(json.dumps(e) + "\n" for e in events).encode()


S1, S2 = "2026-11-27_001310", "2026-11-27_101500"
HUD = "# Mark i suit\n> The suit uses BLDC motors.\n- The suit uses BLDC motors.\n- The HUD runs on a Pi Zero 2 W.\n"


class RunBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = Vault(self.root).open()
        self.vault.remember("The suit uses BLDC motors.", "mark-i-suit", "s0")
        self.llm = FakeLLM()
        self.now = [1000.0]
        self.slept = []
        self.activity = [0.0]

    def tearDown(self):
        self._tmp.cleanup()

    def consolidator(self, **kw) -> Consolidator:
        def sleep(seconds):
            self.slept.append(seconds)
            self.now[0] += seconds

        kw.setdefault("window_tokens", 5000)
        return Consolidator(self.vault, self.llm, threading.Lock(), clock=lambda: self.now[0],
                            sleep=sleep, last_activity=lambda: self.activity[0], **kw)

    def run_quietly(self, c=None):
        with redirect_stdout(io.StringIO()) as out:
            stats = (c or self.consolidator()).run_pending()
        return stats, out.getvalue()


class RunTests(RunBase):
    def test_small_talk_marks_done_without_proposals(self):
        self.vault.store_log(S1, log_bytes(("hey, how's it going?", "Fine, sir.")))
        self.llm.responses = [routed()]
        self.run_quietly()
        self.assertEqual(self.vault.proposals(), [])
        self.assertEqual(self.vault.session_state(S1), {"windows": 1, "done_windows": 1, "done": True})
        self.assertEqual(self.vault.sessions_to_consolidate(), [])

    def test_project_update_creates_one_proposal(self):
        before = self.vault.note_text("mark-i-suit")
        self.vault.store_log(S1, log_bytes(("the helmet HUD will run on a Pi Zero 2 W", "Nice.")))
        self.llm.responses = [routed(("mark-i-suit", False, "", "HUD hardware")), HUD + "SUMMARY: added the HUD"]
        stats, out = self.run_quietly()
        [item] = self.vault.proposals()
        self.assertEqual(item["slug"], "mark-i-suit")
        self.assertEqual(item["new_text"], HUD)
        self.assertEqual(item["summary"], "added the HUD")
        self.assertEqual(item["sessions"], [S1])
        self.assertEqual(item["sources"], [[S1, 0]])
        self.assertEqual(item["base_hash"], self.vault.note_hash("mark-i-suit"))
        self.assertEqual(item["title"], "Mark i suit")
        self.assertEqual(self.vault.note_text("mark-i-suit"), before)  # memory/ untouched
        self.assertIn("Pi Zero 2 W", self.llm.prompt(1))
        self.assertIn(before, self.llm.prompt(1))
        self.assertEqual(stats[0]["proposals"], 1)

    def test_second_session_builds_on_pending(self):
        self.vault.store_log(S1, log_bytes(("the helmet HUD will run on a Pi Zero 2 W", "ok")))
        self.vault.store_log(S2, log_bytes(("the OLED is 1.3 inches", "ok")))
        both = HUD + "- The HUD OLED is 1.3 inches.\n"
        self.llm.responses = [routed(("mark-i-suit", False, "", "HUD")), HUD + "SUMMARY: a",
                              routed(("mark-i-suit", False, "", "OLED size")), both + "SUMMARY: b"]
        self.run_quietly()
        self.assertIn(HUD, self.llm.prompt(3))  # the second rewrite starts from the pending text
        [item] = self.vault.proposals()
        self.assertEqual(item["new_text"], both)
        self.assertEqual(item["sessions"], [S1, S2])

    def test_new_note_proposal(self):
        self.vault.store_log(S1, log_bytes(("my BI app Lumen uses DuckDB", "ok")))
        self.llm.responses = [routed(("lumen", True, "Lumen BI app", "stack")),
                              "# Lumen BI app\n> Tudor's BI app\n- Lumen uses DuckDB.\nSUMMARY: new"]
        self.run_quietly()
        [item] = self.vault.proposals()
        self.assertEqual((item["slug"], item["title"], item["base_hash"]), ("lumen", "Lumen BI app", ""))
        self.assertIn("(this is a new note)", self.llm.prompt(1))

    def test_long_session_one_proposal_over_windows(self):
        turns = [(f"suit detail number {i}: " + "x" * 300, "ok") for i in range(6)]
        self.vault.store_log(S1, log_bytes(*turns))
        texts = [HUD.replace("Pi Zero 2 W", f"Pi Zero rev {i}") for i in range(3)]
        self.llm.responses = []
        for t in texts:
            self.llm.responses += [routed(("mark-i-suit", False, "", "detail")), t + "SUMMARY: s"]
        stats, _ = self.run_quietly(self.consolidator(window_tokens=250))
        self.assertEqual(stats[0]["windows"], 3)
        [item] = self.vault.proposals()
        self.assertEqual(item["sources"], [[S1, 0], [S1, 1], [S1, 2]])
        self.assertEqual(item["new_text"], texts[-1])

    def test_crashed_session_processed(self):
        self.vault.store_log(S1, log_bytes(("hello", "hi"), crashed=True))
        self.llm.responses = [routed()]
        self.run_quietly()
        self.assertTrue(self.vault.session_state(S1)["done"])

    def test_empty_session_is_done_without_llm(self):
        self.vault.store_log(S1, b'{"type":"session_start"}\n{"type":"session_end"}\n')
        self.run_quietly()
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.vault.session_state(S1), {"windows": 0, "done_windows": 0, "done": True})

    def test_activity_pauses_before_llm_calls(self):
        self.vault.store_log(S1, log_bytes(("hello", "hi")))
        self.activity[0] = self.now[0] - 30  # a turn started 30 s ago
        self.llm.responses = [routed()]
        self.run_quietly(self.consolidator(idle_seconds=120))
        self.assertGreaterEqual(sum(self.slept), 90)
        self.assertTrue(all(s <= 10 for s in self.slept))
        self.assertEqual(len(self.llm.calls), 1)

    def test_llm_error_resumes_at_same_window(self):
        turns = [(f"suit detail {i}: " + "x" * 300, "ok") for i in range(4)]
        self.vault.store_log(S1, log_bytes(*turns))
        self.llm.responses = [routed(), LLMError("connection refused")]
        with self.assertRaises(LLMError), redirect_stdout(io.StringIO()):
            self.consolidator(window_tokens=250).run_pending()
        self.assertEqual(self.vault.session_state(S1), {"windows": 2, "done_windows": 1, "done": False})
        self.llm.responses = [routed()]
        self.run_quietly(self.consolidator(window_tokens=250))
        self.assertEqual(len(self.llm.calls), 3)  # window 0 isn't routed again
        self.assertTrue(self.vault.session_state(S1)["done"])

    def test_rejected_rewrite_does_not_block_session(self):
        self.vault.store_log(S1, log_bytes(("the HUD uses a Pi Zero", "ok")))
        self.llm.responses = [routed(("mark-i-suit", False, "", "HUD")), "not a note at all"]
        stats, out = self.run_quietly()
        self.assertEqual(self.vault.proposals(), [])
        self.assertTrue(self.vault.session_state(S1)["done"])
        self.assertIn("rejected", out)
        self.assertEqual(len(stats[0]["rejected"]), 1)

    def test_note_changed_during_call_retries_rewrite(self):
        self.vault.store_log(S1, log_bytes(("the HUD uses a Pi Zero", "ok")))
        vault = self.vault

        class Racing(FakeLLM):
            def chat(inner, messages, **kw):
                if len(inner.calls) == 1:  # while the first rewrite runs, the user remembers a fact
                    vault.remember("The suit has a 6S LiPo.", "mark-i-suit", "live")
                return FakeLLM.chat(inner, messages, **kw)

        self.llm = Racing([routed(("mark-i-suit", False, "", "HUD")), HUD + "SUMMARY: a",
                           HUD + "- The suit has a 6S LiPo.\nSUMMARY: b"])
        self.run_quietly()
        self.assertIn("6S LiPo", self.llm.prompt(2))  # redone against the changed note
        [item] = self.vault.proposals()
        self.assertEqual(item["summary"], "b")
        self.assertEqual(item["base_hash"], self.vault.note_hash("mark-i-suit"))

    def test_building_on_pending_keeps_its_base_hash(self):
        # review #1: a remember between two sessions must leave the proposal stale
        self.vault.store_log(S1, log_bytes(("the helmet HUD will run on a Pi Zero 2 W", "ok")))
        self.llm.responses = [routed(("mark-i-suit", False, "", "")), HUD + "SUMMARY: a"]
        self.run_quietly()
        first_base = self.vault.proposal("mark-i-suit")["base_hash"]
        self.vault.remember("The DMM is a Fluke 87V.", "mark-i-suit", "live")
        self.vault.store_log(S2, log_bytes(("the OLED is 1.3 inches", "ok")))
        self.llm.responses = [routed(("mark-i-suit", False, "", "")),
                              HUD + "- The HUD OLED is 1.3 inches.\nSUMMARY: b"]
        self.run_quietly()
        item = self.vault.proposal("mark-i-suit")
        self.assertEqual(item["base_hash"], first_base)
        self.assertNotEqual(item["base_hash"], self.vault.note_hash("mark-i-suit"))

    def test_forget_during_run_reaches_the_rewrite(self):
        # review #2: forgotten lines are re-read right before each rewrite
        self.vault.remember("The PSU is a Riden.", "mark-i-suit", "s0")
        self.vault.store_log(S1, log_bytes(("the HUD uses a Pi Zero", "ok")))
        vault = self.vault

        class ForgetsDuringRoute(FakeLLM):
            def chat(inner, messages, **kw):
                out = FakeLLM.chat(inner, messages, **kw)
                if len(inner.calls) == 1:
                    vault.forget("mark-i-suit", "riden", "live")
                return out

        self.llm = ForgetsDuringRoute([routed(("mark-i-suit", False, "", "")), HUD + "SUMMARY: a"])
        self.run_quietly()
        self.assertIn("- The PSU is a Riden.", self.llm.prompt(1).split("CURRENT NOTE")[0])

    def test_note_deleted_during_run_is_not_recreated(self):
        self.vault.store_log(S1, log_bytes(("the HUD uses a Pi Zero", "ok")))
        vault = self.vault

        class DeletesDuringRoute(FakeLLM):
            def chat(inner, messages, **kw):
                out = FakeLLM.chat(inner, messages, **kw)
                if len(inner.calls) == 1:  # forgetting the only fact deletes the note
                    vault.forget("mark-i-suit", "bldc", "live")
                return out

        self.llm = DeletesDuringRoute([routed(("mark-i-suit", False, "", ""))])
        stats, _ = self.run_quietly()
        self.assertEqual(self.vault.proposals(), [])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("deleted", stats[0]["rejected"][0])

    def test_window_that_keeps_crashing_is_skipped(self):
        # review #3: a bug (not an LLM outage) in one window must not block every session
        self.vault.store_log(S1, log_bytes(("hello", "hi")))
        self.vault.store_log(S2, log_bytes(("hello again", "hi")))
        c = self.consolidator()
        real = c._window
        calls = []

        def broken(sid, k, window, stats):
            calls.append(sid)
            if sid == S1:
                raise RuntimeError("a bug")
            return real(sid, k, window, stats)

        c._window = broken
        self.llm.responses = [routed()]
        stats, out = self.run_quietly(c)
        self.assertTrue(self.vault.session_state(S1)["done"])
        self.assertTrue(self.vault.session_state(S2)["done"])
        self.assertIn("RuntimeError", out)

    def test_stats_line_printed(self):
        self.vault.store_log(S1, log_bytes(("the HUD uses a Pi Zero", "ok")))
        self.llm.responses = [routed(("mark-i-suit", False, "", "HUD")), HUD + "SUMMARY: a"]
        _, out = self.run_quietly()
        self.assertRegex(out, r"\[consolidator\] 2026-11-27_001310: 1 windows?, routed \[mark-i-suit\], "
                              r"1 proposals?, 0 rejected, 200 tokens, [0-9.]+ s")

    def test_dumps_written_per_window(self):
        self.vault.store_log(S1, log_bytes(("the HUD uses a Pi Zero", "ok")))
        self.llm.responses = [routed(("mark-i-suit", False, "", "HUD")), HUD + "SUMMARY: a"]
        self.run_quietly(self.consolidator(dump_dir=self.root / "dumps"))
        names = sorted(p.name for p in (self.root / "dumps").iterdir())
        self.assertEqual(names, [f"{S1}-w0-rewrite-mark-i-suit.txt", f"{S1}-w0-route.txt"])


class WorkerTests(unittest.TestCase):
    def test_runs_on_kick_and_survives_errors(self):
        runs = []
        done = threading.Event()

        class Stub:
            def run_pending(self):
                runs.append(1)
                if len(runs) == 1:
                    raise LLMError("LM Studio is down")
                if len(runs) == 2:
                    raise RuntimeError("a bug")
                done.set()
                return []

        worker = Worker(Stub(), retry_seconds=0.05)
        with redirect_stdout(io.StringIO()) as out:
            worker.start()
            worker.kick()
            self.assertTrue(done.wait(3))
            worker.stop()
        self.assertEqual(len(runs), 3)  # retried after each failure without a new kick
        self.assertIn("LLM unavailable", out.getvalue())
        self.assertIn("crashed", out.getvalue())


class RefreshTests(RunBase):
    """A stale proposal (its note changed before review) is redone from its sources."""

    def stale_proposal(self):
        self.vault.store_log(S1, log_bytes(("the helmet HUD will run on a Pi Zero 2 W", "ok")))
        self.llm.responses = [routed(("mark-i-suit", False, "", "")), HUD + "SUMMARY: a"]
        self.run_quietly()
        self.vault.remember("The suit has a 6S LiPo.", "mark-i-suit", "live")
        self.vault.mark_stale("mark-i-suit")

    def test_refresh_rewrites_sources_against_current(self):
        self.stale_proposal()
        current = self.vault.note_text("mark-i-suit")
        redone = current + "- The HUD runs on a Pi Zero 2 W.\n"
        self.llm.responses = [redone + "SUMMARY: added the HUD again"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.consolidator().refresh("mark-i-suit"), "updated")
        self.assertIn("6S LiPo", self.llm.prompt(-1))  # rewritten against the current note
        self.assertIn("Pi Zero 2 W", self.llm.prompt(-1))  # with the source window
        item = self.vault.proposal("mark-i-suit")
        self.assertEqual(item["new_text"], redone)
        self.assertFalse(item.get("stale"))
        self.assertEqual(item["base_hash"], self.vault.note_hash("mark-i-suit"))
        self.assertEqual(item["sessions"], [S1])

    def test_refresh_deletes_when_nothing_new(self):
        self.stale_proposal()
        self.llm.responses = [self.vault.note_text("mark-i-suit") + "SUMMARY: nothing"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.consolidator().refresh("mark-i-suit"), "deleted")
        self.assertIsNone(self.vault.proposal("mark-i-suit"))

    def test_refresh_does_not_bring_back_a_denied_proposal(self):
        self.stale_proposal()
        vault = self.vault

        class DeniesDuringRewrite(FakeLLM):
            def chat(inner, messages, **kw):
                vault.delete_proposal("mark-i-suit")  # Tudor denies it meanwhile
                return FakeLLM.chat(inner, messages, **kw)

        self.llm = DeniesDuringRewrite([self.vault.note_text("mark-i-suit") + "- new\nSUMMARY: x"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.consolidator().refresh("mark-i-suit"), "missing")
        self.assertIsNone(self.vault.proposal("mark-i-suit"))

    def test_stale_slugs_lists_stale_proposals(self):
        self.stale_proposal()
        self.assertEqual(self.consolidator().stale_slugs(), ["mark-i-suit"])

    def test_refresh_missing(self):
        self.assertEqual(self.consolidator().refresh("mark-i-suit"), "missing")

    def test_worker_runs_queued_refreshes(self):
        done = threading.Event()
        seen = []

        class Stub:
            def refresh(self, slug):
                seen.append(slug)
                return "updated"

            def run_pending(self):
                done.set()
                return []

        worker = Worker(Stub(), retry_seconds=0.05)
        worker.start()
        worker.refresh("bench")
        self.assertTrue(done.wait(3))
        worker.stop()
        self.assertEqual(seen, ["bench"])

    def test_still_stale_refresh_is_requeued_and_startup_queues_stale(self):
        results = ["stale", "stale", "updated"]
        seen = []
        done = threading.Event()

        class Stub:
            def stale_slugs(self):
                return ["bench"]

            def refresh(self, slug):
                seen.append(slug)
                return results.pop(0)

            def run_pending(self):
                if not results:
                    done.set()
                return []

        worker = Worker(Stub(), retry_seconds=0.05)
        with redirect_stdout(io.StringIO()):
            worker.start()  # queues the stale proposal left by an earlier run
            self.assertTrue(done.wait(3))
            worker.stop()
        self.assertEqual(seen, ["bench", "bench", "bench"])

    def test_crashing_refresh_is_dropped(self):
        done = threading.Event()

        class Stub:
            def refresh(self, slug):
                raise RuntimeError("a bug")

            def run_pending(self):
                done.set()
                return []

        worker = Worker(Stub(), retry_seconds=60)
        with redirect_stdout(io.StringIO()) as out:
            worker.start()
            worker.refresh("bench")
            self.assertTrue(done.wait(3))  # the queue moves on
            worker.stop()
        self.assertIn("refresh bench crashed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
