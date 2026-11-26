"""The conversation loop: stream a reply from the LLM, speak it, run tools, repeat."""
import openai

from compaction import COMPACT_AT, SUMMARY_MAX, TAIL_BUDGET, compact, estimate_tokens
from session_log import NullLogger
from streaming import ExpressionTagParser, SpeechChunker
from tools import TOOL_SCHEMAS, run_tool as default_run_tool

KEEP_RECENT_TOOL_OUTPUTS = 6
# A model that keeps calling tools would otherwise loop forever. After this many tool
# rounds the next request disables tools so the model has to answer.
MAX_TOOL_ROUNDS = 4
TOOL_LOOP_FALLBACK = "I got stuck going back and forth with my tools. Could you rephrase that?"
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
        if msg.get("role") == "tool" and msg.get("content") != "[pruned]":
            msg["content"] = "[pruned]"


def forget_stale_results(messages: list[dict], stale_tools: frozenset[str]):
    """Replace results of tools whose data goes stale (e.g. weather) with a note to call
    them again. Measured against Gemma: with the old data still in the history, a
    repeat question was answered from it 0/4 times via the tool; with it removed, 4/4."""
    if not stale_tools:
        return
    names = {
        call["id"]: call["function"]["name"]
        for msg in messages
        for call in msg.get("tool_calls") or ()
    }
    for msg in messages:
        name = names.get(msg.get("tool_call_id"))
        if msg.get("role") == "tool" and name in stale_tools:
            msg["content"] = f"[old {name} result removed: call {name} again for fresh data]"


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
    ):
        """`on_tool_result(name, result)` is called after each tool runs, e.g. so the
        UI can show a card for it. Its errors are logged, never fatal to the turn.

        Results of `stale_tools` are removed from the history once the turn is over, so
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
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        # prompt + completion tokens of the last request, as reported by the server
        self.last_usage: int | None = None

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
            return self.last_usage
        return estimate_tokens(self.messages) + estimate_tokens(self.tools)

    def run_turn(self, user_text: str, effort: str = "none", speak_aloud: bool = True):
        """Run one user turn to completion. Never raises on LLM/network errors."""
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        self.status.set_phase("thinking")
        try:
            self._run_rounds(effort, speak_aloud)
        except Exception as exc:
            # Roll the whole turn back. Keeping the unanswered user message would put two
            # user messages in a row on the next turn, which strict chat templates
            # (e.g. Gemma's) reject, breaking every later request.
            del self.messages[checkpoint:]
            print(f"\n[llm error] {type(exc).__name__}: {exc}\n")
            self.ui.response(describe_error(exc), "skeptical")
        finally:
            self.status.set_phase("idle")
            forget_stale_results(self.messages[checkpoint:], self.stale_tools)
            prune_old_tool_outputs(self.messages)

    def maybe_compact(self) -> bool:
        """Between turns: if the conversation has grown past `compact_at` tokens, fold the
        older turns into the session summary. Returns True if it compacted.

        Old tool results are already pruned at the end of every turn, so this goes
        straight to summarising."""
        if self.context_tokens() < self.compact_at:
            return False
        self.status.announce("compacting")
        print("[compacting]")
        try:
            summary = compact(
                self.client, self.model, self.messages, self.tail_budget, self.summary_max
            )
            if summary:
                self.log.compaction(summary=summary)
            self.last_usage = None  # the server's count is for the old, longer history
        finally:
            self.status.publish(force=True)
        return True

    def _run_rounds(self, effort: str, speak_aloud: bool):
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            allow_tools = round_no < MAX_TOOL_ROUNDS
            reply, expression, tool_calls = self._stream_round(effort, speak_aloud, allow_tools)

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

    def _notify_tool_result(self, name: str, result: str):
        if not self.on_tool_result:
            return
        try:
            self.on_tool_result(name, result)
        except Exception as exc:
            print(f"[tool hook error] {type(exc).__name__}: {exc}")

    def _stream_round(
        self, effort: str, speak_aloud: bool, allow_tools: bool = True
    ) -> tuple[str, str, list[dict]]:
        """Stream one completion. Returns (stripped reply, expression, tool calls)."""
        request = dict(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            temperature=self.temperature,
            presence_penalty=0.3,
            reasoning_effort=effort,
            stream=True,
            stream_options={"include_usage": True},
        )
        if not allow_tools:
            request["tool_choice"] = "none"
        self.last_usage = None
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
                print(f"Appy [{tags.expression}]: ", end="", flush=True)
                self.status.set_phase("replying")
            print(text, end="", flush=True)
            reply += text
            if speak_aloud:
                for chunk in chunker.feed(text):
                    self.speaker.speak(chunk)

        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                self.last_usage = usage.prompt_tokens + usage.completion_tokens
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
