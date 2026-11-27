"""Test doubles for the OpenAI streaming client, the speaker and the UI bridge."""
from types import SimpleNamespace


def text_chunk(content: str):
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def tool_chunk(index: int, call_id: str | None, name: str | None, arguments: str | None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=index, id=call_id, function=fn)
    delta = SimpleNamespace(content=None, tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class FakeClient:
    """Replays one scripted stream per create() call.

    Each script entry is either a list of chunks or an Exception to raise.
    """

    def __init__(self, scripts=(), responder=None):
        """`responder(kwargs)`, if given, answers every request instead of the scripts."""
        self.scripts = list(scripts)
        self.responder = responder
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        if self.responder:
            return self.responder(kwargs)
        if not self.scripts:
            raise AssertionError("FakeClient ran out of scripted responses")
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        if not isinstance(script, list):
            return script  # a non-streamed response, e.g. completion(...)
        return iter(script)


class FakeSpeaker:
    def __init__(self):
        self.spoken: list[str] = []
        self.on_speak = None

    def speak(self, text: str):
        self.spoken.append(text)
        if self.on_speak:
            self.on_speak()


class FakeUi:
    def __init__(self):
        self.events: list[tuple] = []
        self.contexts: list[tuple[int, int]] = []  # kept apart: tests check event order

    def status(self, state: str, label: str = ""):
        self.events.append(("status", state))

    def response(self, text: str, expression: str = "neutral"):
        self.events.append(("response", text, expression))

    def context(self, tokens: int, limit: int):
        self.contexts.append((tokens, limit))


class RecordingLogger:
    """Stands in for SessionLogger; records (method, kwargs) for every call."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        if name in ("start", "user", "assistant", "tool_call", "tool_result", "compaction", "turn_error",
                    "end"):
            return lambda **kwargs: self.events.append((name, kwargs))
        raise AttributeError(name)


def usage_chunk(prompt_tokens: int, completion_tokens: int):
    """The final usage-only chunk a server sends when stream_options.include_usage is set."""
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[], usage=usage)


def completion(text: str):
    """A non-streamed chat completion response."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
