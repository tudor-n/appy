"""Session logs (session_log.py's JSONL) as plain text for the consolidator.

One block per turn ("[HH:MM] U: ...", "[HH:MM] A: ...", tool calls and results cut to
200 characters, compaction summaries kept), packed into windows of about N tokens that
never split a turn.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

TOOL_CHARS = 200
NO_REPLY = "(Appy failed to answer this; it never got a reply)"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 3.2)


def load_log(path: Path) -> list[dict]:
    events = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue  # blank, or the last line cut off by a crash
        if isinstance(event, dict):
            events.append(event)
    return events


def _clock(event: dict) -> str:
    try:
        return datetime.fromisoformat(str(event.get("ts", ""))).strftime("%H:%M")
    except ValueError:
        return "--:--"


def _flat(text, limit: int = 0) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def render_turns(events: list[dict]) -> list[str]:
    turns: list[list[str]] = []
    for event in events:
        kind = event.get("type")
        at = f"[{_clock(event)}]"
        if kind == "user":
            turns.append([f"{at} U: {_flat(event.get('text'))}"])
        elif kind == "compaction":
            turns.append([f"{at} (summary of earlier conversation) {_flat(event.get('summary'))}"])
        elif not turns:
            continue  # session_start, or anything before the first user turn
        elif kind == "assistant":
            turns[-1].append(f"{at} A: {_flat(event.get('text'))}")
        elif kind == "tool_call":
            turns[-1].append(f"{at} A called {event.get('name')}({_flat(event.get('args'), TOOL_CHARS)})")
        elif kind == "tool_result":
            turns[-1].append(f"  -> {_flat(event.get('result'), TOOL_CHARS)}")
        elif kind == "turn_error":
            # The turn was rolled back: the user said it, but Appy never answered
            turns[-1] = [turns[-1][0], NO_REPLY]
    return ["\n".join(lines) for lines in turns]


def windows(turns: list[str], budget: int) -> list[str]:
    """Pack whole turns into windows of at most `budget` tokens; a turn that is bigger
    on its own is cut to fit."""
    limit = int(budget * 3.2)
    out: list[str] = []
    current: list[str] = []
    for turn in turns:
        if len(turn) > limit:
            turn = turn[: limit - 1] + "…"
        if current and estimate_tokens("\n".join(current + [turn])) > budget:
            out.append("\n".join(current))
            current = []
        current.append(turn)
    if current:
        out.append("\n".join(current))
    return out
