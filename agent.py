"""The conversation loop: stream a reply from the LLM, speak it, run tools, repeat."""
import openai

from streaming import ExpressionTagParser, SpeechChunker
from tools import TOOL_SCHEMAS, run_tool as default_run_tool

KEEP_RECENT_TOOL_OUTPUTS = 6
# A model that keeps calling tools would otherwise loop forever. After this many tool
# rounds the next request disables tools so the model has to answer.
MAX_TOOL_ROUNDS = 4
TOOL_LOOP_FALLBACK = "I got stuck going back and forth with my tools. Could you rephrase that?"
# Once the transcript (excluding the system prompt) grows past HISTORY_MAX_MESSAGES,
# drop the oldest messages down to about HISTORY_KEEP_MESSAGES. Trimming in one big step
# instead of a message at a time keeps the server's prompt cache warm between turns.
HISTORY_MAX_MESSAGES = 60
HISTORY_KEEP_MESSAGES = 30


def prune_old_tool_outputs(messages: list[dict], keep_recent: int = KEEP_RECENT_TOOL_OUTPUTS):
    """Blank out tool results older than the last `keep_recent` messages to save context."""
    if len(messages) <= keep_recent:
        return
    for msg in messages[:-keep_recent]:
        if msg.get("role") == "tool" and msg.get("content") != "[pruned]":
            msg["content"] = "[pruned]"


def compact_history(
    messages: list[dict],
    max_messages: int = HISTORY_MAX_MESSAGES,
    keep: int = HISTORY_KEEP_MESSAGES,
) -> bool:
    """Drop the oldest messages, keeping the system prompt. Returns True if it trimmed.

    The kept part always starts at a user message so an assistant tool call is never
    separated from its tool results.
    """
    body = messages[1:]
    if len(body) <= max_messages:
        return False

    start = max(len(body) - keep, 0)
    cut = next((i for i in range(start, len(body)) if body[i].get("role") == "user"), None)
    if cut is None:
        cut = next((i for i in range(start - 1, -1, -1) if body[i].get("role") == "user"), None)
    if not cut:
        return False
    del messages[1 : 1 + cut]
    return True


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
    ):
        """`on_tool_result(name, result)` is called after each tool runs, e.g. so the
        UI can show a card for it. Its errors are logged, never fatal to the turn."""
        self.client = client
        self.model = model
        self.temperature = temperature
        self.speaker = speaker
        self.ui = ui
        self.status = status
        self.tools = tools
        self.run_tool = run_tool
        self.on_tool_result = on_tool_result
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]

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

    def run_turn(self, user_text: str, effort: str = "none", speak_aloud: bool = True):
        """Run one user turn to completion. Never raises on LLM/network errors."""
        if compact_history(self.messages):
            print(f"[history compacted to {len(self.messages) - 1} messages]")

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
            prune_old_tool_outputs(self.messages)

    def _run_rounds(self, effort: str, speak_aloud: bool):
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            allow_tools = round_no < MAX_TOOL_ROUNDS
            reply, expression, tool_calls = self._stream_round(effort, speak_aloud, allow_tools)

            if tool_calls and allow_tools:
                if reply:
                    self.ui.response(reply, expression)
                self.status.set_phase("thinking")
                self.messages.append(
                    {"role": "assistant", "content": reply or None, "tool_calls": tool_calls}
                )
                for call in tool_calls:
                    name = call["function"]["name"]
                    print(f"[tool: {name}] ", end="", flush=True)
                    result = self.run_tool(name, call["function"]["arguments"])
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": result}
                    )
                    self._notify_tool_result(name, result)
                continue  # Loop back so Appy speaks the tool result

            if tool_calls:
                print("[tool limit reached, ignoring further calls] ", end="")
                if not reply:
                    reply, expression = TOOL_LOOP_FALLBACK, "skeptical"
            print("\n")
            if reply:
                self.ui.response(reply, expression)
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
        )
        if not allow_tools:
            request["tool_choice"] = "none"
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
            if not chunk.choices:
                continue  # some servers send usage-only chunks
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
