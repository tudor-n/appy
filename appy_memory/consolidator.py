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

from .llm import LLM
from .vault import _SLUG, CORE, CORE_CAP, NOTE_CAP, _gist, estimate_tokens

MAX_ROUTED = 3

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
                    "why": {"type": "string"},
                },
                "required": ["slug", "new", "title", "why"],
            },
        },
    },
    "required": ["notes"],
}

ROUTE_PROMPT = """\
You maintain the long-term memory of Appy, the desktop assistant of a person named Tudor. \
The memory is a set of short notes: a core note about Tudor himself, and one note per \
project or topic.

Read the conversation below (U is Tudor, A is Appy) and decide which notes it should update.

Only durable facts count:
- who Tudor is, his life, and the people and things around him
- his standing preferences and routines
- his projects: their design, parts, numbers, names, decisions and status
Ignore small talk, jokes, questions, Appy's own opinions and suggestions, one-off requests, \
live data (weather, time), and anything the memory already says. Never route a fact that is \
on the forgotten list.

Most conversations change nothing, so the usual answer is {{"notes": []}}. Only list a note \
when the conversation states something new and durable for it.
Use "core" for facts about Tudor himself. Use an existing note's slug when the fact belongs \
to its topic. Only for a new project or topic, make a new note: a short slug (lowercase \
letters, digits and dashes) and a title. Set "new" to true only for a new note. "why" names \
the new facts in a few words.

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
    out, tokens = llm.chat([{"role": "user", "content": prompt}], temperature=0.1,
                           max_tokens=400, schema=ROUTE_SCHEMA)
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


def _check(slug: str, lines: list[str], current: str | None) -> list[str]:
    """Raises Rejected unless `lines` is a well-formed note; returns it normalised."""
    body = lines
    if slug == CORE:
        header: list[str] = []
    else:
        if len(lines) < 2 or not lines[0].startswith("# ") or not lines[0][2:].strip():
            raise Rejected("lost the title line")
        if current is not None:
            old_title = (current.splitlines() or [""])[0].strip()
            if old_title.startswith("# ") and lines[0].strip() != old_title:
                raise Rejected(f"renamed the title to {lines[0].strip()!r}")
        if not lines[1].startswith("> ") or not lines[1][2:].strip():
            raise Rejected("lost the gist line")
        header = [lines[0].strip(), "> " + _gist(lines[1][2:].strip())]
        body = lines[2:]
    facts = [l.strip() for l in body if l.strip()]
    if not facts:
        raise Rejected("no facts left")
    stray = [l for l in facts if not l.startswith("- ")]
    if stray:
        raise Rejected(f"non-bullet line {stray[0][:60]!r}")
    return header + facts


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
            out, used = llm.chat(messages, temperature=0.1, max_tokens=1200)
            outputs.append(out)
            tokens += used
            lines, summary = _split_output(out)
            text = "\n".join(_check(slug, lines, current)) + "\n"
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
