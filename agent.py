"""The conversation loop: stream a reply from the LLM, speak it, run tools, repeat."""
import json
from pathlib import Path
from collections import deque

import openai

from compaction import (
    COMPACT_AT, CONTEXT_TOKENS, PRUNED_TOOL_OUTPUT, SUMMARY_MAX, TAIL_BUDGET, TURN_RESERVE,
    compact, estimate_tokens, split,
)
from session_log import NullLogger
from streaming import ExpressionTagParser, SpeechChunker
from tools import TOOL_SCHEMAS, run_tool as default_run_tool

KEEP_RECENT_TOOL_OUTPUTS = 6
# A model that keeps calling tools would otherwise loop forever. After this many tool
# rounds the next request disables tools so the model has to answer.
MAX_TOOL_ROUNDS = 4
TOOL_LOOP_FALLBACK = "I got stuck going back and forth with my tools. Could you rephrase that?"
# How many turns back a tool call still makes a time-only message ("and at 5 pm?") a
# follow-up to it.
RECENT_TURNS = 3
# Added to the last tool result when the model said something before calling tools.
# Gemma's chat template drops the text of an assistant message that has tool calls, so
# without this the model can't see it already spoke and repeats itself ("hi there.
# what's the weather" -> a greeting, then the greeting again with the weather).
ALREADY_SAID_NOTE = (
    '\n\n(You already told the user: "{said}". Continue from there with just the answer. '
    "Don't greet them or repeat that again.)"
)


def prune_old_tool_outputs(messages: list[dict], keep_recent: int = KEEP_RECENT_TOOL_OUTPUTS):
    """Blank out tool results older than the last `keep_recent` messages to save context."""
    if len(messages) <= keep_recent:
        return
    for msg in messages[:-keep_recent]:
        if msg.get("role") == "tool" and msg.get("content") != PRUNED_TOOL_OUTPUT:
            msg["content"] = PRUNED_TOOL_OUTPUT


def drop_stale_tool_exchanges(messages: list[dict], start: int, stale_tools: frozenset[str]):
    """After a turn, remove the calls to and results of tools whose data goes stale
    (e.g. weather) from messages[start:], keeping the user's question and the final
    answer. Any text said alongside a removed call is folded into the next answer.

    Measured against Gemma via LM Studio: with an old weather exchange in the history,
    follow-ups were answered from it instead of calling the tool (0-1/4), and a
    required call was skipped every time (0/3). Removing it helps but isn't enough on
    its own, hence tools.forced_call and the agent's fallback of making the call itself.
    """
    if not stale_tools:
        return
    turn = messages[start:]
    names = {
        call["id"]: call["function"]["name"] for msg in turn for call in msg.get("tool_calls") or ()
    }
    kept: list[dict] = []
    carry = ""  # text from removed assistant messages, waiting for the next answer
    for msg in turn:
        if msg.get("role") == "tool" and names.get(msg.get("tool_call_id")) in stale_tools:
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            calls = [c for c in msg["tool_calls"] if c["function"]["name"] not in stale_tools]
            if not calls:
                carry = f"{carry} {msg.get('content') or ''}".strip()
                continue
            msg = {**msg, "tool_calls": calls}
        if msg.get("role") == "assistant" and carry:
            msg = {**msg, "content": f"{carry} {msg.get('content') or ''}".strip()}
            carry = ""
        kept.append(msg)
    if carry:  # the turn ended without a final answer: keep what was said
        kept.append({"role": "assistant", "content": carry})
    messages[start:] = kept


def describe_error(exc: Exception) -> str:
    """A short, user-facing explanation for a failed turn."""
    name = type(exc).__name__
    if isinstance(exc, (openai.APIConnectionError, ConnectionError, TimeoutError)):
        return f"Sorry, I couldn't reach my model ({name}). Try again in a moment."
    if isinstance(exc, openai.APIStatusError):
        return f"Sorry, my model returned an error ({exc.status_code}). Try again in a moment."
    return f"Sorry, something went wrong on my end ({name})."


def _accumulate_tool_calls(acc: dict[int, dict], deltas) -> None:
    """Merge streamed tool-call fragments into complete calls, keyed by index."""
    for tc in deltas:
        entry = acc.setdefault(
            tc.index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if tc.id:
            entry["id"] = tc.id
        if tc.function and tc.function.name:
            entry["function"]["name"] += tc.function.name
        if tc.function and tc.function.arguments:
            entry["function"]["arguments"] += tc.function.arguments


class Agent:
    def __init__(
        self,
        client,
        model: str,
        temperature: float,
        system_prompt: str,
        speaker,
        ui,
        status,
        tools: list[dict] = TOOL_SCHEMAS,
        run_tool=default_run_tool,
        on_tool_result=None,
        stale_tools: frozenset[str] = frozenset(),
        log=None,
        compact_at: int = COMPACT_AT,
        tail_budget: int = TAIL_BUDGET,
        summary_max: int = SUMMARY_MAX,
        context_limit: int = CONTEXT_TOKENS,
        compaction_dump: Path | None = None,
    ):
        """`on_tool_result(name, result)` is called after each tool runs, e.g. so the
        UI can show a card for it. Its errors are logged, never fatal to the turn.

        Calls to `stale_tools` are removed from the history once the turn is over, so
        the model calls them again next time instead of reusing old data (which also
        means their UI card shows up again)."""
        self.client = client
        self.model = model
        self.temperature = temperature
        self.speaker = speaker
        self.ui = ui
        self.status = status
        self.tools = tools
        self.run_tool = run_tool
        self.on_tool_result = on_tool_result
        self.log = log or NullLogger()
        self.stale_tools = stale_tools
        self.compact_at = compact_at
        self.tail_budget = tail_budget
        self.summary_max = summary_max
        self.context_limit = context_limit
        # If set, compaction N writes its prompt and summary to "<this>.compaction-N.txt"
        self.compaction_dump = compaction_dump
        self._compactions = 0
        # Tools called in each of the last few turns, so follow-ups ("and at 5 pm?") can
        # be recognised a couple of messages later
        self._recent_turn_tools: deque[frozenset[str]] = deque(maxlen=RECENT_TURNS)
        self._synthetic_calls = 0
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        # The server's prompt_tokens for the last request, and how many messages that
        # prompt held (completion_tokens is not used: it includes reasoning tokens that
        # never enter the history)
        self.last_usage: int | None = None
        self._usage_messages = 0

    @property
    def recent_tools(self) -> frozenset[str]:
        """Tools called in any of the last RECENT_TURNS turns."""
        return frozenset().union(*self._recent_turn_tools)

    def preheat(self):
        """Send a 1-token request so the server caches the system prompt + tool schemas."""
        try:
            self.client.chat.completions.create(
                model=self.model,
                messages=[self.messages[0], {"role": "user", "content": "."}],
                tools=self.tools,
                max_tokens=1,
                reasoning_effort="none",
            )
        except Exception:
            pass

    def context_tokens(self) -> int:
        """Size of the conversation as the model sees it: the server's own count from the
        last request when it reports one, otherwise an estimate."""
        if self.last_usage is not None:
            return self.last_usage + estimate_tokens(self.messages[self._usage_messages:])
        return estimate_tokens(self.messages) + estimate_tokens(self.tools)

    def run_turn(
        self,
        user_text: str,
        effort: str = "none",
        speak_aloud: bool = True,
        force=None,
    ):
        """Run one user turn to completion. Never raises on LLM/network errors.

        `force` (a tools.ForcedCall) makes the first round call that tool: the model
        picks the arguments, and if it skips the call anyway the agent makes it itself
        with `force.fallback_args` (see tools.forced_call for why)."""
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        self.status.set_phase("thinking")
        try:
            self._run_rounds(effort, speak_aloud, force)
        except Exception as exc:
            # Roll the whole turn back. Keeping the unanswered user message would put two
            # user messages in a row on the next turn, which strict chat templates
            # (e.g. Gemma's) reject, breaking every later request.
            del self.messages[checkpoint:]
            print(f"\n[llm error] {type(exc).__name__}: {exc}\n")
            apology = describe_error(exc)
            self.ui.response(apology, "skeptical")
            self.log.turn_error(error=f"{type(exc).__name__}: {exc}", reply=apology)
        finally:
            self.status.set_phase("idle")
            self._recent_turn_tools.append(frozenset(
                call["function"]["name"]
                for msg in self.messages[checkpoint:]
                for call in msg.get("tool_calls") or ()
            ))
            drop_stale_tool_exchanges(self.messages, checkpoint, self.stale_tools)
            prune_old_tool_outputs(self.messages)

    def maybe_compact(self) -> bool:
        """Between turns: if the conversation has grown past `compact_at` tokens, fold the
        older turns into the session summary. Returns True if it compacted.

        Old tool results are already pruned at the end of every turn, so this goes
        straight to summarising."""
        tokens = self.context_tokens()
        if tokens < self.compact_at:
            return False
        if split(self.messages, self.tail_budget) is None:
            return False  # the recent turns alone are over the threshold: nothing to fold
        self.status.set_phase("compacting")
        self._compactions += 1
        dump = None
        if self.compaction_dump:
            dump = Path(f"{self.compaction_dump}.compaction-{self._compactions}.txt")
        print(f"[compacting]{f' -> {dump}' if dump else ''}")
        try:
            summary = compact(
                self.client, self.model, self.messages, self.tail_budget, self.summary_max,
                drop_on_failure=tokens >= self.context_limit - TURN_RESERVE,
                dump_path=dump,
            )
            if summary:
                self.log.compaction(summary=summary)
            self.last_usage = None  # the server's count is for the old, longer history
        finally:
            self.status.set_phase("idle")
        return True

    def _run_rounds(self, effort: str, speak_aloud: bool, force=None):
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            allow_tools = round_no < MAX_TOOL_ROUNDS
            forced = force if round_no == 0 else None
            # A forced round is held back (not spoken or shown) until we know the model
            # actually called the tool: LM Studio's tool_choice="required" is only a
            # hint, and a skipped call means an answer made up from stale context.
            reply, expression, tool_calls = self._stream_round(
                effort, speak_aloud, allow_tools, forced and forced.name, hold=bool(forced)
            )
            if forced and not tool_calls:
                print(f"[model skipped {forced.name}; calling it directly] ", end="", flush=True)
                reply, tool_calls = "", [self._synthetic_call(forced)]
            if forced and reply:
                self._deliver(reply, expression, speak_aloud)

            if tool_calls and allow_tools:
                if reply:
                    self.ui.response(reply, expression)
                    self.log.assistant(text=reply, expression=expression)
                self.status.set_phase("thinking")
                self.messages.append(
                    {"role": "assistant", "content": reply or None, "tool_calls": tool_calls}
                )
                for call in tool_calls:
                    name = call["function"]["name"]
                    print(f"[tool: {name}] ", end="", flush=True)
                    args = call["function"]["arguments"]
                    self.log.tool_call(call_id=call["id"], name=name, args=args)
                    result = self.run_tool(name, args)
                    self.log.tool_result(call_id=call["id"], name=name, result=result)
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": result}
                    )
                    self._notify_tool_result(name, result)
                if reply:
                    self.messages[-1]["content"] += ALREADY_SAID_NOTE.format(said=reply)
                continue  # Loop back so Appy speaks the tool result

            if tool_calls:
                print("[tool limit reached, ignoring further calls] ", end="")
                if not reply:
                    reply, expression = TOOL_LOOP_FALLBACK, "skeptical"
            print("\n")
            if reply:
                self.ui.response(reply, expression)
                self.log.assistant(text=reply, expression=expression)
                self.messages.append({"role": "assistant", "content": reply})
            return

    def _synthetic_call(self, forced) -> dict:
        self._synthetic_calls += 1
        return {
            "id": f"appy-{self._synthetic_calls}",
            "type": "function",
            "function": {"name": forced.name, "arguments": json.dumps(forced.fallback_args)},
        }

    def _deliver(self, reply: str, expression: str, speak_aloud: bool):
        """Print and speak text that was held back during streaming."""
        print(f"Appy [{expression}]: {reply}", end="", flush=True)
        self.status.set_phase("replying")
        if speak_aloud:
            chunker = SpeechChunker()
            for chunk in chunker.feed(reply):
                self.speaker.speak(chunk)
            if tail := chunker.flush():
                self.speaker.speak(tail)

    def _notify_tool_result(self, name: str, result: str):
        if not self.on_tool_result:
            return
        try:
            self.on_tool_result(name, result)
        except Exception as exc:
            print(f"[tool hook error] {type(exc).__name__}: {exc}")

    def _stream_round(
        self,
        effort: str,
        speak_aloud: bool,
        allow_tools: bool = True,
        force_tool: str | None = None,
        hold: bool = False,
    ) -> tuple[str, str, list[dict]]:
        """Stream one completion. Returns (stripped reply, expression, tool calls).

        Normally text is printed and spoken as it streams; with `hold` it's only
        collected, for the caller to `_deliver` (or drop)."""
        tools = self.tools
        if force_tool:
            # LM Studio rejects tool_choice naming a function (HTTP 400), so offer only
            # that tool and require a call
            tools = [t for t in self.tools if t["function"]["name"] == force_tool] or self.tools
        request = dict(
            model=self.model,
            messages=self.messages,
            tools=tools,
            temperature=self.temperature,
            presence_penalty=0.3,
            reasoning_effort=effort,
            stream=True,
            stream_options={"include_usage": True},
        )
        if not allow_tools:
            request["tool_choice"] = "none"
        elif force_tool:
            request["tool_choice"] = "required"
        self.last_usage = None
        self._usage_messages = len(self.messages)
        stream = self.client.chat.completions.create(**request)

        tags = ExpressionTagParser()
        chunker = SpeechChunker()
        tool_calls: dict[int, dict] = {}
        reply = ""

        def on_text(text: str):
            nonlocal reply
            if not reply:
                text = text.lstrip()
                if not text:
                    return
                if hold:
                    reply += text
                    return
                print(f"Appy [{tags.expression}]: ", end="", flush=True)
                self.status.set_phase("replying")
            if hold:
                reply += text
                return
            print(text, end="", flush=True)
            reply += text
            if speak_aloud:
                for chunk in chunker.feed(text):
                    self.speaker.speak(chunk)

        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                self.last_usage = usage.prompt_tokens
            if not chunk.choices:
                continue  # the usage chunk has no choices
            delta = chunk.choices[0].delta
            if delta.tool_calls:
                _accumulate_tool_calls(tool_calls, delta.tool_calls)
            if delta.content:
                text = tags.feed(delta.content)
                if text:
                    on_text(text)

        # A very short reply may still be held back waiting for a closing "]"
        leftover = tags.flush()
        if leftover:
            on_text(leftover)

        tail = chunker.flush()
        if speak_aloud and tail:
            self.speaker.speak(tail)

        return reply.strip(), tags.expression, [tool_calls[i] for i in sorted(tool_calls)]
