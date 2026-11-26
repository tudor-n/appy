"""Tests for the memory service's vault (runs on the Mac's Python 3.9 too).

Don't import tests/fakes.py here: it uses `str | None` at runtime, which 3.9 rejects.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import datetime
import json

from appy_memory.gitrepo import GitRepo
from appy_memory.vault import CORE_CAP, MAX_LOG_BYTES, Vault, VaultError, estimate_tokens


def git_out(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout


class GitRepoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = GitRepo(self.root)
        self.repo.init()

    def tearDown(self):
        self._tmp.cleanup()

    def write_and_commit(self, text: str, subject: str) -> str:
        (self.root / "core.md").write_text(text, encoding="utf-8")
        sha = self.repo.commit(subject)
        self.assertIsNotNone(sha)
        return sha

    def test_init_is_idempotent(self):
        self.repo.init()
        self.assertTrue((self.root / ".git").is_dir())

    def test_commit_returns_none_when_nothing_changed(self):
        self.write_and_commit("a\n", "memory: init")
        self.assertIsNone(self.repo.commit("memory: remember core: nothing"))

    def test_commit_works_without_global_identity(self):
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with mock.patch.dict(os.environ, env):
            self.write_and_commit("a\n", "memory: init")
        self.assertEqual(git_out(self.root, "log", "-1", "--format=%an <%ae>").strip(),
                         "Appy <appy@localhost>")

    def test_undo_target_skips_init_and_reverted(self):
        self.write_and_commit("", "memory: init")
        c1 = self.write_and_commit("- a\n", "memory: remember core: a")
        c2 = self.write_and_commit("- a\n- b\n", "memory: remember core: b")
        self.assertEqual(self.repo.undo_target(), (c2, "memory: remember core: b"))
        self.repo.revert(c2)
        self.assertEqual(self.repo.undo_target(), (c1, "memory: remember core: a"))
        self.repo.revert(c1)
        self.assertIsNone(self.repo.undo_target())
        self.assertEqual((self.root / "core.md").read_text(), "")

    def test_undo_target_after_new_commit(self):
        self.write_and_commit("", "memory: init")
        c1 = self.write_and_commit("- a\n", "memory: remember core: a")
        self.repo.revert(c1)
        c3 = self.write_and_commit("- c\n", "memory: remember core: c")
        self.assertEqual(self.repo.undo_target(), (c3, "memory: remember core: c"))
        self.repo.revert(c3)
        self.assertIsNone(self.repo.undo_target())

    def test_undo_target_on_empty_repo(self):
        self.assertIsNone(self.repo.undo_target())


def commit_count(root: Path) -> int:
    return int(git_out(root, "rev-list", "--count", "HEAD").strip())


class VaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "appy-data"
        self.vault = Vault(self.root).open()
        self.mem = self.root / "memory"

    def tearDown(self):
        self._tmp.cleanup()

    def assertStatus(self, status: int, fn, *args):
        with self.assertRaises(VaultError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.status, status)
        return ctx.exception

    def test_open_creates_layout_and_is_idempotent(self):
        Vault(self.root).open()
        self.assertEqual((self.mem / "core.md").read_text(), "")
        self.assertEqual((self.root / ".gitignore").read_text().split(),
                         ["logs/", "pending/", "state.json", "memory/_index.md"])
        for d in ("logs", "pending"):
            self.assertTrue((self.root / d).is_dir())
        self.assertEqual(commit_count(self.root), 1)
        self.assertEqual(git_out(self.root, "log", "-1", "--format=%s").strip(), "memory: init")

    def test_bad_slugs_rejected(self):
        for slug in ("../x", "Core", "a/b", "_index", "", "a" * 65):
            with self.subTest(slug=slug):
                self.assertStatus(400, self.vault.read, slug)
                if slug:  # remember treats "" as "no slug" (core); see test below
                    self.assertStatus(400, self.vault.remember, "fact", slug, "s1")
                self.assertStatus(400, self.vault.forget, slug, "fact", "s1")

    def test_remember_empty_slug_means_core(self):
        # models often send "" for an optional argument they mean to leave out
        self.assertEqual(self.vault.remember("Tudor lives in Sibiu.", "", "s1")["slug"], "core")

    def test_read_unknown_note(self):
        self.assertStatus(404, self.vault.read, "nope")

    def test_remember_core_appends_bullet_and_commits(self):
        result = self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        self.assertEqual((self.mem / "core.md").read_text(), "- Tudor lives in Sibiu.\n")
        self.assertEqual(result["slug"], "core")
        self.assertEqual(result["line"], "- Tudor lives in Sibiu.")
        self.assertIsNotNone(result["commit"])
        subject, body = git_out(self.root, "log", "-1", "--format=%s%x1f%b").split("\x1f")
        self.assertTrue(subject.startswith("memory: remember core:"))
        self.assertIn("session=s1", body)

    def test_remember_new_note_gets_title_and_gist(self):
        self.vault.remember("Tudor's printer is a Prusa MK4.", "lab-printer", "s1")
        self.assertEqual(
            (self.mem / "lab-printer.md").read_text(),
            "# Lab printer\n> Tudor's printer is a Prusa MK4.\n- Tudor's printer is a Prusa MK4.\n",
        )
        self.vault.remember("x" * 200, "long-one", "s1")
        gist = (self.mem / "long-one.md").read_text().splitlines()[1]
        self.assertEqual(gist, "> " + "x" * 120)

    def test_remember_duplicate_is_noop(self):
        self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        before = commit_count(self.root)
        result = self.vault.remember("tudor lives in  Sibiu", None, "s2")
        self.assertIsNone(result["commit"])
        self.assertEqual((self.mem / "core.md").read_text().count("- "), 1)
        self.assertEqual(commit_count(self.root), before)

    def test_remember_text_is_one_line(self):
        result = self.vault.remember("  - line one\n# Evil\nline two ", None, "s1")
        self.assertEqual(result["line"], "- line one # Evil line two")
        self.assertEqual((self.mem / "core.md").read_text(), "- line one # Evil line two\n")
        self.assertStatus(400, self.vault.remember, "  \n ", None, "s1")
        self.assertStatus(400, self.vault.remember, "y" * 301, None, "s1")

    def test_remember_over_cap_writes_nothing(self):
        while estimate_tokens((self.mem / "core.md").read_text()) < CORE_CAP - 30:
            n = len((self.mem / "core.md").read_text())
            self.vault.remember(f"Filler fact number {n} that takes up some room.", None, "s1")
        before_text = (self.mem / "core.md").read_text()
        before = commit_count(self.root)
        err = self.assertStatus(409, self.vault.remember, "z" * 200, None, "s1")
        self.assertIn("full", err.message)
        self.assertEqual((self.mem / "core.md").read_text(), before_text)
        self.assertEqual(commit_count(self.root), before)

    def test_index_regenerates(self):
        self.vault.remember("Tudor's printer is a Prusa MK4.", "lab-printer", "s1")
        self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        day = datetime.date.fromtimestamp((self.mem / "lab-printer.md").stat().st_mtime)
        index = (self.mem / "_index.md").read_text()
        self.assertEqual(
            index,
            f"lab-printer | Lab printer | Tudor's printer is a Prusa MK4. | {day.isoformat()}\n",
        )
        self.vault.undo()  # the core remember
        self.vault.undo()  # the lab-printer remember
        self.assertEqual((self.mem / "_index.md").read_text(), "")
        self.assertFalse((self.mem / "lab-printer.md").exists())

    def test_context_index_is_capped_newest_first(self):
        for i in range(40):
            self.vault.remember(f"Fact number {i} about a project with a fairly long gist.",
                                f"note-{i:02d}", "s1")
        for i in range(40):
            os.utime(self.mem / f"note-{i:02d}.md", (1_700_000_000 + i * 60,) * 2)
        context = self.vault.context()
        self.assertEqual(context["core"], "")
        lines = context["index"].splitlines()
        self.assertLessEqual(estimate_tokens(context["index"]), 600)
        self.assertGreater(len(lines), 5)
        self.assertLess(len(lines), 40)
        self.assertTrue(lines[0].startswith("note-39 |"))
        self.assertTrue(lines[1].startswith("note-38 |"))

    def test_forget_removes_single_match(self):
        self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        self.vault.remember("Tudor likes coffee.", None, "s1")
        result = self.vault.forget("core", "LIVES in   sibiu", "s2")
        self.assertEqual(result["line"], "- Tudor lives in Sibiu.")
        self.assertEqual((self.mem / "core.md").read_text(), "- Tudor likes coffee.\n")
        subject, body = git_out(self.root, "log", "-1", "--format=%s%x1f%b").split("\x1f")
        self.assertTrue(subject.startswith("memory: forget core:"))
        self.assertIn("session=s2", body)

    def test_forget_no_match_lists_lines(self):
        self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        err = self.assertStatus(404, self.vault.forget, "core", "likes tea", "s1")
        self.assertEqual(err.extra["lines"], ["- Tudor lives in Sibiu."])
        self.assertEqual((self.mem / "core.md").read_text(), "- Tudor lives in Sibiu.\n")

    def test_forget_ambiguous_lists_candidates(self):
        self.vault.remember("The printer is a Prusa.", "lab", "s1")
        self.vault.remember("The printer sits by the window.", "lab", "s1")
        self.vault.remember("The bench PSU is a Riden.", "lab", "s1")
        before = (self.mem / "lab.md").read_text()
        err = self.assertStatus(409, self.vault.forget, "lab", "printer", "s1")
        self.assertEqual(err.extra["lines"],
                         ["- The printer is a Prusa.", "- The printer sits by the window."])
        self.assertEqual((self.mem / "lab.md").read_text(), before)

    def test_forget_never_touches_title_or_gist(self):
        self.vault.remember("Tudor's printer is a Prusa MK4.", "lab-printer", "s1")
        self.assertStatus(404, self.vault.forget, "lab-printer", "Lab printer", "s1")
        self.assertStatus(400, self.vault.forget, "lab-printer", "  ", "s1")
        self.assertStatus(404, self.vault.forget, "missing-note", "x", "s1")

    def test_forget_records_line_and_discards_pending(self):
        self.vault.remember("Tudor's printer is a Prusa MK4.", "lab-printer", "s1")
        pending = self.root / "pending" / "lab-printer.json"
        pending.write_text("{}")
        self.vault.forget("lab-printer", "prusa", "s1")
        self.assertFalse(pending.exists())
        last = self.vault.forgotten()[-1]
        self.assertEqual(last["slug"], "lab-printer")
        self.assertEqual(last["line"], "- Tudor's printer is a Prusa MK4.")
        self.assertIn("ts", last)
        self.assertEqual(json.loads((self.root / "state.json").read_text())["forgotten"][-1], last)
        for i in range(25):
            self.vault.remember(f"Fact {i}.", None, "s1")
            self.vault.forget("core", f"Fact {i}.", "s1")
        self.assertEqual(len(self.vault.forgotten()), 20)
        self.assertEqual(self.vault.forgotten()[-1]["line"], "- Fact 24.")

    def test_forget_also_clears_a_gist_built_from_that_fact(self):
        # a new note's gist is its first fact; forgetting that fact must not leave it in
        # the gist, which goes into every system prompt through the index
        self.vault.remember("Tudor's PSU is a Riden RD6024.", "bench", "s1")
        self.vault.remember("The scope is a Rigol DS1054Z.", "bench", "s1")
        self.vault.forget("bench", "riden", "s1")
        self.assertEqual((self.mem / "bench.md").read_text(),
                         "# Bench\n> The scope is a Rigol DS1054Z.\n- The scope is a Rigol DS1054Z.\n")
        self.assertNotIn("Riden", self.vault.context()["index"])

    def test_forget_last_fact_removes_the_note(self):
        self.vault.remember("Tudor's PSU is a Riden RD6024.", "bench", "s1")
        self.vault.forget("bench", "riden", "s1")
        self.assertFalse((self.mem / "bench.md").exists())
        self.assertEqual(self.vault.context()["index"], "")
        self.assertEqual(git_out(self.root, "status", "--porcelain"), "")
        self.vault.undo()
        self.assertIn("Riden", (self.mem / "bench.md").read_text())
        self.assertIn("bench |", self.vault.context()["index"])

    def test_forget_last_core_fact_keeps_core(self):
        self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        self.vault.forget("core", "sibiu", "s1")
        self.assertEqual((self.mem / "core.md").read_text(), "")

    def test_failed_commit_restores_the_note(self):
        from appy_memory.gitrepo import GitError
        self.vault.remember("Tudor lives in Sibiu.", None, "s1")
        with mock.patch.object(self.vault.git, "commit", side_effect=GitError("disk full")):
            self.assertStatus(500, self.vault.remember, "Another fact.", None, "s1")
            self.assertStatus(500, self.vault.remember, "New note fact.", "new-note", "s1")
        self.assertEqual((self.mem / "core.md").read_text(), "- Tudor lives in Sibiu.\n")
        self.assertFalse((self.mem / "new-note.md").exists())

    def test_undo_sequence(self):
        core = self.mem / "core.md"
        self.vault.remember("a", None, "s1")
        self.vault.forget("core", "a", "s1")
        self.assertEqual(core.read_text(), "")
        self.assertTrue(self.vault.undo()["undone"].startswith("memory: forget core:"))
        self.assertEqual(core.read_text(), "- a\n")
        self.vault.remember("b", None, "s1")
        self.vault.undo()
        self.assertEqual(core.read_text(), "- a\n")
        self.assertTrue(self.vault.undo()["undone"].startswith("memory: remember core: a"))
        self.assertEqual(core.read_text(), "")
        self.assertEqual(self.vault.undo(), {"undone": None, "commit": None})


SID = "2026-11-26_153012"
LOG = ('{"v":1,"seq":1,"type":"session_start"}\n\n'
       '{"v":1,"seq":2,"type":"user","text":"Bună ziua"}\n').encode()


class LogStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = Vault(self.root).open()

    def tearDown(self):
        self._tmp.cleanup()

    def assertStatus(self, status: int, sid: str, data: bytes):
        with self.assertRaises(VaultError) as ctx:
            self.vault.store_log(sid, data)
        self.assertEqual(ctx.exception.status, status)

    def test_store_log_new(self):
        self.assertTrue(self.vault.store_log(SID, LOG))
        self.assertEqual((self.root / "logs" / f"{SID}.jsonl").read_bytes(), LOG)

    def test_store_log_same_bytes_is_idempotent(self):
        self.vault.store_log(SID, LOG)
        self.assertFalse(self.vault.store_log(SID, LOG))
        self.assertEqual((self.root / "logs" / f"{SID}.jsonl").read_bytes(), LOG)

    def test_store_log_different_bytes_conflicts(self):
        self.vault.store_log(SID, LOG)
        self.assertStatus(409, SID, LOG + b'{"type":"session_end"}\n')
        self.assertEqual((self.root / "logs" / f"{SID}.jsonl").read_bytes(), LOG)

    def test_store_log_rejects_bad_input(self):
        self.assertStatus(400, "../x", LOG)
        self.assertStatus(400, "2026-11-26", LOG)
        self.assertStatus(400, SID, b"\xff\xfe")
        self.assertStatus(400, SID, b'{"ok":1}\nnot json\n')
        self.assertStatus(400, SID, b"[1, 2]\n")
        self.assertStatus(413, SID, b" " * (MAX_LOG_BYTES + 1))
        self.assertEqual(list((self.root / "logs").iterdir()), [])

    def test_store_log_allows_unicode_line_separators_inside_strings(self):
        # session_log writes ensure_ascii=False, so U+2028/U+2029/U+0085 stay raw in
        # JSON strings; they aren't line breaks in JSONL
        data = ('{"type":"user","text":"a\u2028b\u2029c\u0085d"}\n').encode()
        self.assertTrue(self.vault.store_log(SID, data))

    def test_store_log_is_not_committed(self):
        before = commit_count(self.root)
        self.vault.store_log(SID, LOG)
        self.assertEqual(git_out(self.root, "status", "--porcelain"), "")
        self.assertEqual(commit_count(self.root), before)


if __name__ == "__main__":
    unittest.main()
