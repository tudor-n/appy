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

from appy_memory.gitrepo import GitRepo


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


if __name__ == "__main__":
    unittest.main()
