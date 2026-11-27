"""The vault's local git repo: history, undo and provenance for memory changes.

Every call passes its own identity, because the Mac has no global git config.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

GIT = ["git", "-c", "user.name=Appy", "-c", "user.email=appy@localhost",
       "-c", "commit.gpgsign=false"]
MEMORY_PREFIX = "memory: "
INIT_SUBJECT = "memory: init"
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{40})")


class GitError(Exception):
    pass


class GitRepo:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _git(self, *args: str) -> str:
        proc = subprocess.run([*GIT, *args], cwd=self.root, capture_output=True, text=True)
        if proc.returncode != 0:
            raise GitError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
        return proc.stdout

    def init(self) -> None:
        if not (self.root / ".git").exists():
            self._git("init", "-q", "-b", "main")

    def commit(self, subject: str, body: str = "", paths: tuple[str, ...] = (".",)) -> str | None:
        """Stage `paths` and commit. Returns the short sha, or None if nothing changed."""
        self._git("add", "-A", "--", *paths)
        if not self._git("status", "--porcelain", "--", *paths).strip():
            return None
        message = f"{subject}\n\n{body}" if body else subject
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "--short", "HEAD").strip()

    def undo_target(self) -> tuple[str, str] | None:
        """The newest memory commit that hasn't been reverted yet, as (short sha, subject)."""
        try:
            log = self._git("log", "--format=%h%x1f%H%x1f%s%x1f%b%x1e")
        except GitError:
            return None  # no commits yet
        reverted: set[str] = set()
        for record in log.split("\x1e"):
            fields = record.strip("\n").split("\x1f")
            if len(fields) < 4:
                continue
            short, full, subject, body = fields
            match = _REVERTS.search(body)
            if match:
                reverted.add(match.group(1))
                continue
            if full in reverted or not subject.startswith(MEMORY_PREFIX) or subject == INIT_SUBJECT:
                continue
            return short, subject
        return None

    def head(self) -> str:
        """The current commit's short sha ("" before the first commit)."""
        try:
            return self._git("rev-parse", "--short", "HEAD").strip()
        except GitError:
            return ""

    def log_after(self, sha: str) -> list[tuple[str, str]]:
        """Commits after `sha` up to HEAD as (short sha, subject), oldest first. Empty for
        an empty or unknown sha."""
        if not sha:
            return []
        try:
            out = self._git("log", "--reverse", "--format=%h%x1f%s", f"{sha}..HEAD")
        except GitError:
            return []
        return [tuple(line.split("\x1f", 1)) for line in out.splitlines() if "\x1f" in line]

    def commit_info(self, sha: str) -> tuple[str, list[str]]:
        """A commit's message body and the files it changed."""
        out = self._git("show", "--name-only", "--format=%b%x1e", sha)
        body, _, files = out.partition("\x1e")
        return body.strip(), [f for f in files.split() if f]

    def revert(self, sha: str) -> str:
        try:
            self._git("revert", "--no-edit", sha)
        except GitError:
            try:
                self._git("revert", "--abort")
            except GitError:
                pass
            raise
        return self._git("rev-parse", "--short", "HEAD").strip()
