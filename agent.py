"""The conversation loop: stream a reply from the LLM, speak it, run tools, repeat."""
from streaming import ExpressionTagParser, SpeechChunker
from tools import TOOL_SCHEMAS, run_tool as default_run_tool

KEEP_RECENT_TOOL_OUTPUTS = 6


def prune_old_tool_outputs(messages: list[dict], keep_recent: int = KEEP_RECENT_TOOL_OUTPUTS):
    """Blank out tool results older than the last `keep_recent` messages to save context."""
    if len(messages) <= keep_recent:
        return
    for msg in messages[:-keep_recent]:
        if msg.get("role") == "tool" and msg.get("content") != "[pruned]":
            msg["content"] = "[pruned]"


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
        tools: list[dict] = TOOL_SCHEMAS,
        run_tool=default_run_tool,
    ):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.speaker = speaker
        self.ui = ui
        self.tools = tools
        self.run_tool = run_tool
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
        self.messages.append({"role": "user", "content": user_text})

        while True:
            reply, expression, tool_calls = self._stream_round(effort, speak_aloud)

            if tool_calls:
                if reply:
                    self.ui.response(reply, expression)
                    self.ui.status("thinking")
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
                continue  # Loop back so Appy speaks the tool result

            print("\n")
            if reply:
                self.ui.response(reply, expression)
                self.messages.append({"role": "assistant", "content": reply})
            else:
                self.ui.status("idle")
            prune_old_tool_outputs(self.messages)
            return

    def _stream_round(self, effort: str, speak_aloud: bool) -> tuple[str, str, list[dict]]:
        """Stream one completion. Returns (stripped reply, expression, tool calls)."""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            temperature=self.temperature,
            presence_penalty=0.3,
            reasoning_effort=effort,
            stream=True,
        )

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
