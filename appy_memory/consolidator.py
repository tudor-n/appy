"""The consolidator: learns durable facts from finished sessions, as proposals.

For each window of a session's transcript:
  1. route:   which notes (if any) does this conversation add durable facts to?  (JSON)
  2. rewrite: each routed note in full, with the new facts folded in.             (markdown)
Code checks every rewrite (title and gist kept, bullets only, under the cap, actually
changed) and stores it in pending/<slug>.json for the user to accept in the overlay.
Nothing here writes memory/ itself.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .llm import LLM, BadRequest, EmptyOutput, LLMError
from .vault import _SLUG, CORE, CORE_CAP, NOTE_CAP, NoteFormatError, _gist, check_lines, estimate_tokens

MAX_ROUTED = 3
MAX_REFRESHES = 3  # redos in a row for a proposal whose note keeps changing

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "new": {"type": "boolean"},
                    "title": {"type": "string"},
                },
                # no free-text field: under the JSON grammar Gemma can loop inside one
                "required": ["slug", "new", "title"],
                "additionalProperties": False,  # LM Studio doesn't enforce "strict"
            },
        },
    },
    "required": ["notes"],
    "additionalProperties": False,
}

ROUTE_PROMPT = """\
You maintain the long-term memory of Appy, the desktop assistant of a person named Tudor. \
The memory is a set of short notes: a core note about Tudor himself, and one note per \
project or topic.

Read the conversation below (U is Tudor, A is Appy) and decide which notes it should update.

Only durable facts count:
- who Tudor is, his life, and the people and things around him
- his standing preferences and routines
- his projects: their design, parts, numbers, names, ideas, plans, decisions and status. \
This includes projects that are hypothetical, planned or not built yet.
A fact counts if Tudor said it, even in passing ("one more idea: ..."). When the \
conversation shows a memory_remember call, only the text inside that call was saved: other \
facts Tudor mentioned, even in the same message, still count.
Ignore small talk, jokes, questions, Appy's own opinions and suggestions, one-off requests, \
live data (weather, time), and anything the memory already says. Never route a fact that is \
on the forgotten list.

Most conversations change nothing, so the usual answer is {{"notes": []}}. Only list a note \
when the conversation states something new and durable for it.
Use "core" for facts about Tudor himself. Use an existing note's slug when the fact belongs \
to its topic. Only for a new project or topic, make a new note: a short slug (lowercase \
letters, digits and dashes) and a title. Set "new" to true only for a new note.
You only see each note's one-line gist, not its full text. When Tudor states a fact about a \
note's topic and you can't tell whether the note already has it, list the note anyway: the \
next step reads the full note and drops anything already there.

NOTE INDEX (slug | title | gist | updated):
{index}

CORE NOTE:
{core}

FORGOTTEN (never add these back):
{forgotten}

CONVERSATION:
{window}
"""

NOTE_FORMAT = (
    '- The first line is "# {title}" and the second line is "> " plus a one-line description '
    "of what the note is about (under 120 characters). Keep both lines."
)
CORE_FORMAT = "- This is the core note about Tudor himself: bullets only, no title or description."

REWRITE_PROMPT = """\
You are updating one note in the long-term memory of Appy, the desktop assistant of a \
person named Tudor.

Rewrite the note so it also contains the new durable facts from the conversation below \
(U is Tudor, A is Appy).{focus}

Rules:
{format_rule}
- Facts are "- " bullets: one short fact each, in the third person ("Tudor's ...", \
"The suit uses ...").
- Keep every existing fact unless the conversation changes or contradicts it; then replace \
the old fact with the new one instead of keeping both.
- Only add facts Tudor stated or confirmed. Don't add small talk, questions, Appy's \
suggestions or opinions, live data, or anything on the forgotten list.
- Don't repeat a fact the note already has. If there is nothing new, output the note \
exactly as it is.
- Keep the whole note under {words} words.
- Output only the note, then one last line: SUMMARY: <what changed, under 15 words>

FORGOTTEN (never add these back):
{forgotten}

CURRENT NOTE:
{current}

CONVERSATION:
{window}
"""

SHORTER = ("That note is too long. Write a shorter version, under {words} words: merge related facts and "
           "drop the least important ones. Same format, ending with the SUMMARY line.")


class Rejected(Exception):
    """The model's output failed a check; nothing is saved."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _listed(lines: list[str]) -> str:
    return "\n".join(lines) if lines else "(none)"


def _dump(path: Path | None, prompt: str, outputs: list[str]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = prompt + "".join(f"\n\n--- output {i + 1}\n{out}" for i, out in enumerate(outputs))
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        print(f"[consolidator] couldn't write dump {path}: {exc}", flush=True)


def route(llm: LLM, index: str, core: str, forgotten: list[str], window: str,
          existing: set[str], dump: Path | None = None) -> tuple[list[dict], int]:
    """The notes this window should update, as [{slug, new, title, why}], and tokens used."""
    prompt = ROUTE_PROMPT.format(index=index or "(no notes yet)", core=core or "(empty)",
                                 forgotten=_listed(forgotten), window=window)
    try:
        out, tokens = llm.chat([{"role": "user", "content": prompt}], temperature=0.1,
                               max_tokens=400, schema=ROUTE_SCHEMA)
    except (EmptyOutput, BadRequest) as exc:  # retrying the same request won't help
        _dump(dump, prompt, [""])
        raise Rejected(f"route: {exc}")
    _dump(dump, prompt, [out])
    try:
        items = json.loads(out)["notes"]
        if not isinstance(items, list):
            raise TypeError("notes is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        raise Rejected(f"route: bad JSON ({exc})")
    notes: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug", "")).strip().lower()
        if not _SLUG.match(slug) or any(n["slug"] == slug for n in notes):
            continue
        title = str(item.get("title", "")).strip()
        new = slug != CORE and slug not in existing  # the model's flag only hints at this
        if slug == CORE:
            title = "Core"
        elif not title:
            title = slug.replace("-", " ").capitalize()
        notes.append({"slug": slug, "new": new, "title": title, "why": str(item.get("why", ""))})
        if len(notes) == MAX_ROUTED:
            break
    return notes, tokens


def _split_output(out: str) -> tuple[list[str], str]:
    lines = [l.rstrip() for l in out.strip().splitlines() if not l.strip().startswith("```")]
    summary = ""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().upper().startswith("SUMMARY:"):
            summary = lines[i].strip()[len("SUMMARY:"):].strip()
            del lines[i]
            break
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines, summary


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def rewrite(llm: LLM, slug: str, current: str | None, title: str, window: str,
            forgotten: list[str], why: str = "", dump: Path | None = None) -> tuple[str, str, int]:
    """The note rewritten with this window's facts: (new_text, summary, tokens)."""
    cap = CORE_CAP if slug == CORE else NOTE_CAP
    words = cap // 2
    prompt = REWRITE_PROMPT.format(
        focus=f" The facts to add are about: {why}." if why else "",
        format_rule=CORE_FORMAT if slug == CORE else NOTE_FORMAT.format(title=title),
        words=words, forgotten=_listed(forgotten),
        current=(current or "(empty)") if slug == CORE else (current or "(this is a new note)"),
        window=window,
    )
    messages = [{"role": "user", "content": prompt}]
    outputs: list[str] = []
    tokens = 0
    try:
        for attempt in range(2):
            try:
                out, used = llm.chat(messages, temperature=0.1, max_tokens=1200)
            except (EmptyOutput, BadRequest) as exc:
                raise Rejected(str(exc))
            outputs.append(out)
            tokens += used
            lines, summary = _split_output(out)
            try:
                text = "\n".join(check_lines(slug, lines, current)) + "\n"
            except NoteFormatError as exc:
                raise Rejected(exc.reason)
            if estimate_tokens(text) <= cap:
                break
            if attempt == 1:
                raise Rejected(f"over cap ({estimate_tokens(text)} > {cap} tokens)")
            messages = messages + [{"role": "assistant", "content": out},
                                   {"role": "user", "content": SHORTER.format(words=words)}]
    finally:
        _dump(dump, prompt, outputs)
    if current is not None and _norm(text) == _norm(current):
        raise Rejected("no change")
    return text, summary or f"updated {slug}", tokens


class Consolidator:
    """Works through uploaded sessions that haven't been consolidated, oldest first.

    Vault access happens under `lock` (the server's), LLM calls outside it, so live
    remember/forget never wait on the model. Progress is saved after every window."""

    def __init__(self, vault, llm: LLM, lock, window_tokens: int = 5000,
                 idle_seconds: float = 120, last_activity=lambda: 0.0,
                 dump_dir: Path | None = None, clock=None, sleep=None):
        import time

        self.vault = vault
        self.llm = llm
        self.lock = lock
        self.window_tokens = window_tokens
        self.idle_seconds = idle_seconds
        self.last_activity = last_activity
        self.dump_dir = dump_dir
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep

    def run_pending(self) -> list[dict]:
        """Consolidate every session not done yet. LLMError propagates (try again later)."""
        with self.lock:
            sids = self.vault.sessions_to_consolidate()
        return [self.run_session(sid) for sid in sids]

    def run_session(self, sid: str) -> dict:
        from .transcript import load_log, render_turns, windows

        started = self.clock()
        stats = {"session": sid, "windows": 0, "routed": [], "proposals": 0, "rejected": [],
                 "tokens": 0, "seconds": 0.0}
        with self.lock:
            events = load_log(self.vault.root / "logs" / f"{sid}.jsonl")
            state = self.vault.session_state(sid)
        wins = windows(render_turns(events), self.window_tokens)
        stats["windows"] = len(wins)
        first = state["done_windows"] if state["windows"] == len(wins) else 0
        try:
            if not wins:
                with self.lock:
                    self.vault.set_session_state(sid, 0, 0, True)
            for k in range(first, len(wins)):
                try:
                    self._window(sid, k, wins[k], stats)
                except LLMError:
                    raise  # the model is unreachable: stop here, retry later
                except Exception as exc:
                    # A bug in this window: skip it rather than retry it (and block every
                    # later session) forever
                    import traceback

                    print(f"[consolidator] {sid} window {k} crashed:\n{traceback.format_exc()}",
                          flush=True)
                    stats["rejected"].append(f"window {k} crashed: {type(exc).__name__}: {exc}")
                with self.lock:
                    self.vault.set_session_state(sid, len(wins), k + 1, k + 1 == len(wins))
        finally:
            stats["seconds"] = round(self.clock() - started, 1)
            self._report(stats)
        return stats

    def refresh(self, slug: str) -> str:
        """Redo a stale proposal (its note changed before it was reviewed): fold its source
        windows into the note as it is now. Returns "updated", "deleted" (nothing new left)
        or "missing"."""
        from .transcript import load_log, render_turns, windows

        with self.lock:
            item = self.vault.proposal(slug)
            if item is None:
                return "missing"
            current = self.vault.note_text(slug)
            seen = self.vault.note_hash(slug)
            forgotten = self.vault.forgotten_lines()
            logs = {sid: load_log(self.vault.root / "logs" / f"{sid}.jsonl")
                    for sid in {s for s, _ in item.get("sources", [])}}
        if slug == CORE and current is None:
            current = ""
        title = item.get("title") or slug
        text, summaries = current, []
        for sid, k in item.get("sources", []):
            wins = windows(render_turns(logs[sid]), self.window_tokens)
            if k >= len(wins):
                continue
            self._wait_quiet()
            try:
                text, summary, _ = rewrite(self.llm, slug, text, title, wins[k], forgotten)
            except Rejected as exc:
                print(f"[consolidator] refresh {slug} ({sid} w{k}): {exc.reason}", flush=True)
                continue
            summaries.append(summary)
            if slug != CORE:
                title = text.splitlines()[0][2:].strip()
        with self.lock:
            now = self.vault.proposal(slug)
            if now is None:
                return "missing"  # denied (or forgotten) while it was being redone
            if now.get("updated") != item.get("updated"):
                return "changed"  # a newer version was saved meanwhile; that one stands
            if self.vault.note_hash(slug) != seen:
                return "stale"  # the note changed again meanwhile: the worker redoes it
            if not summaries:
                self.vault.delete_proposal(slug)
                print(f"[consolidator] refresh {slug}: nothing new left, proposal dropped",
                      flush=True)
                return "deleted"
            self.vault.replace_proposal(slug, title, text, "; ".join(summaries),
                                        item.get("sessions", []), item.get("sources", []))
            self._auto_commit(slug)
        print(f"[consolidator] refresh {slug}: redone against the current note", flush=True)
        return "updated"

    def _auto_commit(self, slug: str) -> None:
        """With auto-approve on, commit what was just proposed (caller holds the lock)."""
        sha = self.vault.auto_commit(slug)
        if sha:
            print(f"[consolidator] auto-saved {slug} -> {sha}", flush=True)

    def stale_slugs(self) -> list[str]:
        with self.lock:
            return [p["slug"] for p in self.vault.proposals() if p.get("stale")]

    def _report(self, s: dict) -> None:
        rejected = f"{len(s['rejected'])} rejected"
        if s["rejected"]:
            rejected += f" ({'; '.join(s['rejected'])})"
        print(f"[consolidator] {s['session']}: {s['windows']} windows, "
              f"routed [{', '.join(s['routed'])}], {s['proposals']} proposals, {rejected}, "
              f"{s['tokens']} tokens, {s['seconds']} s", flush=True)

    def _wait_quiet(self) -> None:
        """Let live chat have the model: wait until no turn has started for a while."""
        while True:
            idle = self.clock() - self.last_activity()
            if idle >= self.idle_seconds:
                return
            self.sleep(min(10.0, self.idle_seconds - idle))

    def _dump(self, name: str) -> Path | None:
        return self.dump_dir / name if self.dump_dir else None

    def _window(self, sid: str, k: int, window: str, stats: dict) -> None:
        with self.lock:
            context = self.vault.context()
            forgotten = self.vault.forgotten_lines()
            existing = set(self.vault.note_slugs())
        self._wait_quiet()
        try:
            notes, used = route(self.llm, context["index"], context["core"], forgotten, window,
                                existing, dump=self._dump(f"{sid}-w{k}-route.txt"))
        except Rejected as exc:
            stats["rejected"].append(exc.reason)
            return
        stats["tokens"] += used
        for note in notes:
            stats["routed"].append(note["slug"])
            self._note(sid, k, window, note, stats)

    def _note(self, sid: str, k: int, window: str, note: dict, stats: dict) -> None:
        slug = note["slug"]
        for attempt in range(2):
            with self.lock:
                pending = self.vault.proposal(slug)
                current = pending["new_text"] if pending else self.vault.note_text(slug)
                seen = (self.vault.note_hash(slug), (pending or {}).get("updated"))
                forgotten = self.vault.forgotten_lines()  # a forget may have just happened
            if not note["new"] and slug != CORE and current is None:
                stats["rejected"].append(f"{slug}: note was deleted during the run")
                return
            title = note["title"]
            if pending:
                title = pending.get("title") or title
            elif current and current.startswith("# "):
                title = current.splitlines()[0][2:].strip()
            if slug == CORE and current is None:
                current = ""
            self._wait_quiet()
            try:
                text, summary, used = rewrite(
                    self.llm, slug, current, title, window, forgotten, why=note["why"],
                    dump=self._dump(f"{sid}-w{k}-rewrite-{slug}.txt"))
            except Rejected as exc:
                stats["rejected"].append(f"{slug}: {exc.reason}")
                return
            stats["tokens"] += used
            if slug != CORE:
                title = text.splitlines()[0][2:].strip()  # may carry a casing fix
            with self.lock:
                now = (self.vault.note_hash(slug), (self.vault.proposal(slug) or {}).get("updated"))
                if now == seen:
                    self.vault.save_proposal(slug, title, text, summary, sid, k)
                    stats["proposals"] += 1
                    self._auto_commit(slug)
                    return
            # the note or its proposal changed while the model was writing: redo it once
        stats["rejected"].append(f"{slug}: kept changing during the rewrite")


class Worker:
    """Runs the consolidator in a background thread whenever it's kicked (a new log) and,
    after an error, again every `retry_seconds`. Never dies on an exception."""

    def __init__(self, consolidator: Consolidator, retry_seconds: float = 300):
        import threading

        self.consolidator = consolidator
        self.retry_seconds = retry_seconds
        self._event = threading.Event()
        self._stopping = False
        self._refresh: list[str] = []
        self._refresh_lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="consolidator", daemon=True)

    def start(self) -> None:
        self._thread.start()
        try:  # stale proposals whose redo was cut short (e.g. by a restart)
            for slug in self.consolidator.stale_slugs():
                self.refresh(slug)
        except Exception as exc:
            print(f"[consolidator] couldn't list stale proposals: {exc}", flush=True)

    def kick(self) -> None:
        self._event.set()

    def refresh(self, slug: str) -> None:
        """Redo a stale proposal soon (before consolidating more sessions)."""
        with self._refresh_lock:
            if slug not in self._refresh:
                self._refresh.append(slug)
        self._event.set()

    def stop(self, timeout: float = 5) -> None:
        self._stopping = True
        self._event.set()
        self._thread.join(timeout)

    def _loop(self) -> None:
        import traceback

        from .llm import LLMError

        retry = False
        tries: dict = {}
        while not self._stopping:
            self._event.wait(self.retry_seconds if retry else None)
            self._event.clear()
            if self._stopping:
                return
            try:
                while True:
                    with self._refresh_lock:
                        slug = self._refresh[0] if self._refresh else None
                    if slug is None:
                        break
                    try:
                        result = self.consolidator.refresh(slug)
                        tries[slug] = tries.get(slug, 0) + 1
                        if result == "stale" and tries[slug] < MAX_REFRESHES:
                            continue  # the note moved again while it was redone: once more
                    except LLMError:
                        raise  # keep it queued; retry when the model is back
                    except Exception:
                        print(f"[consolidator] refresh {slug} crashed:\n"
                              + traceback.format_exc(), flush=True)
                    tries.pop(slug, None)
                    with self._refresh_lock:
                        self._refresh.remove(slug)
                self.consolidator.run_pending()
                retry = False
            except LLMError as exc:
                print(f"[consolidator] LLM unavailable ({exc}); retrying in "
                      f"{int(self.retry_seconds)} s", flush=True)
                retry = True
            except Exception:
                print("[consolidator] crashed:\n" + traceback.format_exc(), flush=True)
                retry = True
