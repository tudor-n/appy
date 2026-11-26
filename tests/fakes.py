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

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        if not self.scripts:
            raise AssertionError("FakeClient ran out of scripted responses")
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
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

    def status(self, state: str, label: str = ""):
        self.events.append(("status", state))

    def response(self, text: str, expression: str = "neutral"):
        self.events.append(("response", text, expression))


class RecordingLogger:
    """Stands in for SessionLogger; records (method, kwargs) for every call."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        if name in ("start", "user", "assistant", "tool_call", "tool_result", "compaction", "end"):
            return lambda **kwargs: self.events.append((name, kwargs))
        raise AttributeError(name)
