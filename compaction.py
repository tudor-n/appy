"""Keep a long session inside the model's context by folding older turns into a summary.

After compaction the history is:
    [system, user: SUMMARY_PREFIX + summary, assistant: SUMMARY_ACK, ...recent turns]
The system prompt is never touched (its KV cache stays valid), the recent tail always
starts at a user message (so a tool call is never split from its results), and the next
compaction edits the previous summary instead of summarising a summary.
"""
import json
import math
from pathlib import Path

COMPACT_AT = 9500
CONTEXT_TOKENS = 16384
# Room a single turn needs. If a failed summary leaves the session closer than this to
# the context limit, the older turns are dropped without a summary instead of retried.
TURN_RESERVE = 3500
TAIL_BUDGET = 2500
SUMMARY_MAX = 700
TOOL_RESULT_CHARS = 200
# What prune_old_tool_outputs leaves in place of an old tool result
PRUNED_TOOL_OUTPUT = "[pruned]"
# Start of agent.ALREADY_SAID_NOTE, an instruction appended to tool results; the
# summariser doesn't need it (tests keep the two in sync)
ALREADY_SAID_START = "\n\n(You already told the user"

SUMMARY_PREFIX = "[Earlier in this session — summary]\n"
SUMMARY_ACK = "Got it."
SUMMARY_TEMPLATE = """\
Update the session summary. Keep it under 250 words. Use exactly these sections; omit empty ones.
GOAL: what the user is trying to do right now
DECISIONS: choices already made
FACTS: things the user stated that matter later (names, numbers, preferences). Leave out live data that goes stale, like weather or the time.
OPEN: unresolved questions / pending tasks
PROMISES: anything Appy said it would do
STATE: where we left off
Previous summary:
{previous}
New conversation to fold in:
{conversation}"""


def estimate_tokens(obj) -> int:
    """Rough, conservative token count for any JSON-serialisable value."""
    return math.ceil(len(json.dumps(obj, ensure_ascii=False)) / 3.2)


def _summary_pair(summary: str) -> list[dict]:
    return [
        {"role": "user", "content": SUMMARY_PREFIX + summary},
        {"role": "assistant", "content": SUMMARY_ACK},
    ]


def split(
    messages: list[dict], tail_budget: int
) -> tuple[str | None, list[dict], list[dict]] | None:
    """Split the history into (previous summary, middle to fold, tail to keep).

    Returns None when there is nothing to fold.
    """
    body = 1
    previous = None
    if (
        len(messages) >= 3
        and messages[1].get("role") == "user"
        and str(messages[1].get("content") or "").startswith(SUMMARY_PREFIX)
        and messages[2].get("role") == "assistant"
        and messages[2].get("content") == SUMMARY_ACK
    ):
        previous = messages[1]["content"][len(SUMMARY_PREFIX):]
        body = 3

    # Earliest message that still fits the budget, counting back from the end
    start, used = len(messages), 0
    for i in range(len(messages) - 1, body - 1, -1):
        used += estimate_tokens(messages[i])
        if used > tail_budget:
            break
        start = i

    # The tail starts at a user message, and always holds at least the whole last turn
    start = min(start, len(messages) - 1)
    while start >= body and messages[start].get("role") != "user":
        start -= 1
    if start <= body:
        return None
    return previous, messages[body:start], messages[start:]


def render(messages: list[dict]) -> str:
    """Plain-text transcript for the summariser, with tool output cut short and
    placeholders for pruned tool output left out."""
    lines = []
    for msg in messages:
        role, content = msg.get("role"), msg.get("content")
        if role == "user" and content:
            lines.append(f"User: {content}")
        elif role == "assistant":
            if content:
                lines.append(f"Appy: {content}")
            for call in msg.get("tool_calls") or ():
                fn = call["function"]
                lines.append(f"Appy called {fn['name']}({fn['arguments']})")
        elif role == "tool" and content and content != PRUNED_TOOL_OUTPUT:
            result = content.split(ALREADY_SAID_START, 1)[0]
            lines.append("Tool: " + " ".join(result.split())[:TOOL_RESULT_CHARS])
    return "\n".join(lines)


def _dump(path: Path, prompt: str, outcome: str, folded: int, kept: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {folded} messages folded, {kept} kept verbatim\n\n"
            f"=== PROMPT SENT TO THE MODEL ===\n{prompt}\n\n"
            f"=== SUMMARY RETURNED ===\n{outcome}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[compaction dump failed] {type(exc).__name__}: {exc}")


def compact(
    client,
    model: str,
    messages: list[dict],
    tail_budget: int = TAIL_BUDGET,
    summary_max: int = SUMMARY_MAX,
    drop_on_failure: bool = False,
    dump_path: Path | None = None,
) -> str | None:
    """Fold older turns into the session summary, rebuilding `messages` in place.

    Returns the new summary, or None if nothing was folded or summarising failed. On
    failure the history is left as it was so the next turn can retry, unless
    `drop_on_failure`: then the older turns are dropped anyway (keeping any previous
    summary), so the conversation still shrinks and stays valid.

    `dump_path`, if given, gets the exact prompt sent and what came back, for checking
    what the summariser actually sees.
    """
    parts = split(messages, tail_budget)
    if parts is None:
        return None
    previous, middle, tail = parts

    prompt = SUMMARY_TEMPLATE.format(previous=previous or "none", conversation=render(middle))
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            reasoning_effort="none",
            max_tokens=summary_max + 100,
        )
        summary = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"[compaction failed] {type(exc).__name__}: {exc}")
        summary = ""
        outcome = f"(failed: {type(exc).__name__}: {exc})"
    else:
        outcome = summary or "(blank summary)"
    if dump_path:
        _dump(dump_path, prompt, outcome, len(middle), len(tail))

    if summary:
        messages[1:] = _summary_pair(summary) + tail
        return summary
    if drop_on_failure:
        messages[1:] = (_summary_pair(previous) if previous else []) + tail
    return None
