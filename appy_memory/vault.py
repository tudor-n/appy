"""The memory vault on the Mac: plain markdown notes in a local git repo.

Layout under MEMORY_DIR:
  memory/core.md       bullets about the user; always in the system prompt (≤400 tokens)
  memory/<slug>.md     "# Title", "> gist", then "- fact" bullets (≤800 tokens)
  memory/_index.md     generated: "slug | title | gist | updated", newest first
  logs/                uploaded session logs
  pending/             consolidator proposals (phase 4)
  state.json           forgotten lines (and, later, consolidation progress)

Only code writes here, never the model. Every change to memory/ is a commit whose body
carries the session id; undo is `git revert`. The server serialises calls.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
from pathlib import Path

from .gitrepo import GitError, GitRepo

CORE_CAP = 400
NOTE_CAP = 800
INDEX_CAP = 600
MAX_FORGOTTEN = 20
MAX_FACT_CHARS = 300
GIST_CHARS = 120
CORE = "core"
GITIGNORE = "logs/\npending/\nstate.json\nmemory/_index.md\ndumps/\n"
_SLUG = re.compile(r"^[a-z0-9-]{1,64}$")
_SESSION_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
MAX_LOG_BYTES = 20 * 1024 * 1024


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 3.2)


class VaultError(Exception):
    """A request the vault refuses; `message` is meant for the model to read."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def _one_line(text: str) -> str:
    """Collapse whitespace (so a fact can't add a heading) and drop a leading bullet."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    return re.sub(r"^[-*•]\s+", "", text).strip()  # only a marker + space: "-5 C" stays


def _match_key(text: str) -> str:
    return _one_line(text).casefold().rstrip(".! ")


class NoteFormatError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_lines(slug: str, lines: list[str], current: str | None) -> list[str]:
    """`lines` as a well-formed note (normalised), or NoteFormatError: "# title" (the
    current one, ignoring case) and "> gist" for a note, then only "- " bullets."""
    body = lines
    if slug == CORE:
        header: list[str] = []
    else:
        if len(lines) < 2 or not lines[0].startswith("# ") or not lines[0][2:].strip():
            raise NoteFormatError("lost the title line")
        if current is not None:
            old_title = (current.splitlines() or [""])[0].strip()
            # a casing fix ("Mark i suit" -> "Mark I suit") is fine; a new name is not
            if old_title.startswith("# ") and lines[0].strip().casefold() != old_title.casefold():
                raise NoteFormatError(f"renamed the title to {lines[0].strip()!r}")
        if not lines[1].startswith("> ") or not lines[1][2:].strip():
            raise NoteFormatError("lost the gist line")
        header = [lines[0].strip(), "> " + _gist(lines[1][2:].strip())]
        body = lines[2:]
    facts = [l.strip() for l in body if l.strip()]
    if not facts:
        raise NoteFormatError("no facts left")
    stray = [l for l in facts if not l.startswith("- ")]
    if stray:
        raise NoteFormatError(f"non-bullet line {stray[0][:60]!r}")
    return header + facts


def check_note(slug: str, text: str, current: str | None) -> str:
    """A whole note (e.g. Tudor's edit) checked like a rewrite, including the size cap."""
    lines = [l.rstrip() for l in str(text).strip().splitlines()]
    out = "\n".join(check_lines(slug, lines, current)) + "\n"
    cap = CORE_CAP if slug == CORE else NOTE_CAP
    if estimate_tokens(out) > cap:
        raise NoteFormatError(f"too long: over the {cap}-token cap")
    return out


def _gist(text: str) -> str:
    """At most GIST_CHARS, cut at a word boundary."""
    if len(text) <= GIST_CHARS:
        return text
    cut = text[: GIST_CHARS - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:") + "…"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.memory = self.root / "memory"
        self.git = GitRepo(self.root)

    # -- setup -----------------------------------------------------------------------

    def open(self) -> Vault:
        for d in (self.memory, self.root / "logs", self.root / "pending"):
            d.mkdir(parents=True, exist_ok=True)
        self.git.init()
        ignore = self.root / ".gitignore"
        have = ignore.read_text(encoding="utf-8").split() if ignore.exists() else []
        missing = [entry for entry in GITIGNORE.split() if entry not in have]
        if missing:  # an older vault gets the entries added since
            text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
            if text and not text.endswith("\n"):
                text += "\n"
            ignore.write_text(text + "".join(e + "\n" for e in missing), encoding="utf-8")
        if not (self.memory / "core.md").exists():
            (self.memory / "core.md").write_text("", encoding="utf-8")
        self.git.commit("memory: init")
        self._rebuild_index()
        return self

    # -- reading ---------------------------------------------------------------------

    def _path(self, slug: str) -> Path:
        if not isinstance(slug, str) or not _SLUG.match(slug):
            raise VaultError(400, f"bad note slug {slug!r}: use lowercase letters, digits and dashes")
        return self.memory / f"{slug}.md"

    def read(self, slug: str) -> str:
        path = self._path(slug)
        if not path.exists():
            raise VaultError(404, f"there is no memory note called '{slug}'")
        return path.read_text(encoding="utf-8")

    def context(self) -> dict:
        """core.md plus the index, capped at INDEX_CAP tokens (newest notes first)."""
        kept: list[str] = []
        for line in self._index_lines():
            if estimate_tokens("\n".join(kept + [line])) > INDEX_CAP:
                break
            kept.append(line)
        return {"core": self.read(CORE).strip(), "index": "\n".join(kept)}

    def forgotten(self) -> list[dict]:
        return self._state().get("forgotten", [])

    def forgotten_lines(self) -> list[str]:
        return [str(f.get("line", "")) for f in self.forgotten()]

    def note_slugs(self) -> list[str]:
        """Every note except core, newest first (the index order)."""
        return [line.split(" | ", 1)[0] for line in self._index_lines()]

    def note_text(self, slug: str) -> str | None:
        path = self._path(slug)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def note_hash(self, slug: str) -> str:
        text = self.note_text(slug)
        return "" if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()

    # -- consolidator: proposals and progress ------------------------------------------

    def proposal(self, slug: str) -> dict | None:
        path = self.root / "pending" / f"{self._path(slug).stem}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("slug") == slug else None

    def proposals(self) -> list[dict]:
        found = []
        for path in (self.root / "pending").glob("*.json"):
            if _SLUG.match(path.stem):
                item = self.proposal(path.stem)
                if item:
                    # for review: the note as it stands now, and whether it moved on since
                    found.append({**item, "before": self.note_text(item["slug"]) or "",
                                  "stale": bool(item.get("stale"))})
        return sorted(found, key=lambda p: (str(p.get("updated", "")), p["slug"]))

    def save_proposal(self, slug: str, title: str, new_text: str, summary: str, session: str,
                      window: int) -> dict:
        """Write (or build on) the one pending proposal for this note."""
        self._path(slug)
        old = self.proposal(slug) or {}
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        sessions = list(old.get("sessions", []))
        if session not in sessions:
            sessions.append(session)
        sources = [list(s) for s in old.get("sources", [])]
        if [session, window] not in sources:
            sources.append([session, window])
        # base_hash is the note the proposal was first built on: building on the pending
        # text later keeps it, so a note that changed in between still reads as stale
        base = old["base_hash"] if "base_hash" in old else self.note_hash(slug)
        item = {
            "slug": slug, "title": title, "base_hash": base,
            "new_text": new_text, "summary": summary, "sessions": sessions, "sources": sources,
            "created": old.get("created", now), "updated": now,
        }
        _write_atomic(self.root / "pending" / f"{slug}.json",
                      json.dumps(item, ensure_ascii=False, indent=1))
        return item

    def apply_note(self, slug: str, text: str, subject: str, body: str) -> str | None:
        """Write a reviewed note (accepted or edited), commit it, drop its proposal."""
        path = self._path(slug)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        sha = self._commit(path, old, text, subject, body) if text != old else None
        self.delete_proposal(slug)
        return sha

    def delete_proposal(self, slug: str) -> bool:
        path = self.root / "pending" / f"{self._path(slug).stem}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    def mark_stale(self, slug: str) -> None:
        item = self.proposal(slug)
        if item is not None:
            item["stale"] = True
            _write_atomic(self.root / "pending" / f"{slug}.json",
                          json.dumps(item, ensure_ascii=False, indent=1))

    def replace_proposal(self, slug: str, title: str, new_text: str, summary: str,
                         sessions: list, sources: list) -> dict:
        """A proposal redone against the note as it is now (not stale any more)."""
        old = self.proposal(slug) or {}
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        item = {
            "slug": slug, "title": title, "base_hash": self.note_hash(slug),
            "new_text": new_text, "summary": summary, "sessions": list(sessions),
            "sources": [list(s) for s in sources], "created": old.get("created", now),
            "updated": now,
        }
        _write_atomic(self.root / "pending" / f"{slug}.json",
                      json.dumps(item, ensure_ascii=False, indent=1))
        return item

    def session_state(self, sid: str) -> dict:
        found = self._state().get("sessions", {}).get(sid) or {}
        return {"windows": int(found.get("windows", 0)),
                "done_windows": int(found.get("done_windows", 0)),
                "done": bool(found.get("done", False))}

    def set_session_state(self, sid: str, windows: int, done_windows: int, done: bool) -> None:
        state = self._state()
        state.setdefault("sessions", {})[sid] = {
            "windows": windows, "done_windows": done_windows, "done": done}
        self._save_state(state)

    def sessions_to_consolidate(self) -> list[str]:
        sids = sorted(p.stem for p in (self.root / "logs").glob("*.jsonl"))
        return [sid for sid in sids if not self.session_state(sid)["done"]]

    # -- writing ---------------------------------------------------------------------

    def remember(self, text: str, slug: str | None, session: str) -> dict:
        slug = slug or CORE
        path = self._path(slug)
        fact = _one_line(text)
        if not fact:
            raise VaultError(400, "there's nothing to remember: the text is empty")
        if len(fact) > MAX_FACT_CHARS:
            raise VaultError(400, f"that's too long for one fact (max {MAX_FACT_CHARS} characters)")
        line = f"- {fact}"
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old is not None and any(_match_key(l) == _match_key(fact) for l in self._bullets(old)):
            return {"slug": slug, "line": line, "commit": None}
        base = old if old is not None else f"# {slug.replace('-', ' ').capitalize()}\n> {_gist(fact)}\n"
        if base and not base.endswith("\n"):
            base += "\n"
        new = base + line + "\n"
        cap = CORE_CAP if slug == CORE else NOTE_CAP
        if estimate_tokens(new) > cap:
            where = "core memory" if slug == CORE else f"the note '{slug}'"
            raise VaultError(409, f"{where} is full ({cap} tokens); nothing was saved")
        sha = self._commit(path, old, new, f"memory: remember {slug}: {fact[:60]}",
                           f"session={session}")
        return {"slug": slug, "line": line, "commit": sha}

    def forget(self, slug: str, text: str, session: str) -> dict:
        path = self._path(slug)
        old = self.read(slug)
        needle = _match_key(text)
        if not needle:
            raise VaultError(400, "say which fact to forget: the text is empty")
        bullets = self._bullets(old)
        matches = [b for b in bullets if needle in _match_key(b)]
        if not matches:
            raise VaultError(404, f"no line in '{slug}' matches that", lines=bullets)
        if len(matches) > 1:
            raise VaultError(409, f"more than one line in '{slug}' matches; be more specific",
                             lines=matches)
        line = matches[0]
        lines = old.splitlines(keepends=True)
        lines.remove(next(l for l in lines if l.rstrip("\n") == line))
        rest = self._bullets("".join(lines))
        new: str | None = "".join(lines)
        if slug != CORE and not rest:
            new = None  # nothing left in the note: drop it (and its index line)
        elif slug != CORE and len(lines) > 1 and lines[1].startswith("> "):
            # The gist is in every system prompt (via the index). A new note's gist is its
            # first fact, so forgetting that fact has to take it out of the gist too.
            gist = _match_key(lines[1][2:])
            if gist and (gist in _match_key(line) or needle in gist):
                lines[1] = f"> {rest[0][2:][:GIST_CHARS]}\n"
                new = "".join(lines)
        sha = self._commit(path, old, new, f"memory: forget {slug}: {line[2:62]}",
                           f"session={session}")
        (self.root / "pending" / f"{slug}.json").unlink(missing_ok=True)
        state = self._state()
        forgotten = state.get("forgotten", [])
        forgotten.append({"slug": slug, "line": line,
                          "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
        state["forgotten"] = forgotten[-MAX_FORGOTTEN:]
        self._save_state(state)
        return {"slug": slug, "line": line, "commit": sha}

    def undo(self) -> dict:
        target = self.git.undo_target()
        if target is None:
            return {"undone": None, "commit": None}
        sha, subject = target
        try:
            new = self.git.revert(sha)
        except GitError as exc:
            raise VaultError(500, f"undo failed: {exc}")
        finally:
            self._rebuild_index()
        return {"undone": subject, "commit": new}

    def store_log(self, session_id: str, data: bytes) -> bool:
        """Keep an uploaded session log. Returns True if new; the same bytes again are a
        no-op (an upload retried after a crash), different bytes are refused."""
        if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
            raise VaultError(400, f"bad session id {session_id!r}")
        if len(data) > MAX_LOG_BYTES:
            raise VaultError(413, f"log is too big ({len(data)} bytes)")
        try:
            text = data.decode("utf-8")
            for line in text.split("\n"):  # not splitlines: U+2028 etc. are valid in JSON
                if line.strip() and not isinstance(json.loads(line), dict):
                    raise ValueError("not a JSON object")
        except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError included
            raise VaultError(400, f"not a JSONL session log: {exc}")
        path = self.root / "logs" / f"{session_id}.jsonl"
        if path.exists():
            if path.read_bytes() == data:
                return False
            raise VaultError(409, f"a different log for session {session_id} is already stored")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return True

    # -- internals -------------------------------------------------------------------

    @staticmethod
    def _bullets(text: str) -> list[str]:
        return [l for l in text.splitlines() if l.startswith("- ")]

    def _commit(self, path: Path, old: str | None, new: str | None, subject: str,
                body: str) -> str:
        """Write `new` to `path` (None deletes it) and commit; on failure put `old` back."""
        if new is None:
            path.unlink()
        else:
            _write_atomic(path, new)
        try:
            sha = self.git.commit(subject, body, paths=("memory",))
        except GitError as exc:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                _write_atomic(path, old)
            raise VaultError(500, f"saving to memory failed: {exc}")
        finally:
            self._rebuild_index()
        return sha

    def _index_lines(self) -> list[str]:
        notes = []
        for path in self.memory.glob("*.md"):
            slug = path.stem
            if slug == CORE or not _SLUG.match(slug):
                continue
            head = path.read_text(encoding="utf-8").splitlines()[:2] + ["", ""]
            title = head[0][2:].strip() if head[0].startswith("# ") else slug
            gist = head[1][2:].strip() if head[1].startswith("> ") else ""
            mtime = path.stat().st_mtime
            day = datetime.date.fromtimestamp(mtime).isoformat()
            notes.append((-mtime, slug, f"{slug} | {title} | {gist} | {day}"))
        return [line for _, _, line in sorted(notes)]

    def _rebuild_index(self) -> None:
        lines = self._index_lines()
        _write_atomic(self.memory / "_index.md", "".join(l + "\n" for l in lines))

    def _save_state(self, state: dict) -> None:
        _write_atomic(self.root / "state.json", json.dumps(state, ensure_ascii=False, indent=1))

    def _state(self) -> dict:
        try:
            return json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
