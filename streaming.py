"""Pure helpers for turning a streamed LLM reply into UI text and TTS chunks.

Nothing in here does I/O, so it is all unit-testable (see tests/test_streaming.py).
"""
import re

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
CLAUSE_END = re.compile(r"(?<=[.!?,;:—])\s+")
# The first chunk may end at a clause boundary so speech starts sooner, but only once
# it's long enough to sound natural ("It's going fine," yes; "Let me check," no).
MIN_FIRST_CLAUSE = 16

TAG_RE = re.compile(r"^\s*\[([a-zA-Z]+)\]\s*(.*)$", re.DOTALL)
VALID_EXPRESSIONS = frozenset({
    "neutral",
    "happy",
    "excited",
    "curious",
    "surprised",
    "focused",
    "skeptical",
})


def pop_ready_chunk(buf: str, first_chunk: bool) -> tuple[str | None, str]:
    """Split off the first speakable chunk of `buf`, returning (chunk, rest).

    Returns (None, buf) when no complete chunk is available yet.
    """
    pattern = CLAUSE_END if first_chunk else SENTENCE_END
    parts = pattern.split(buf)
    if len(parts) <= 1:
        return None, buf

    acc = ""
    for idx, part in enumerate(parts[:-1]):
        acc = f"{acc} {part}".strip() if acc else part.strip()
        if not first_chunk or len(acc) >= MIN_FIRST_CLAUSE:
            rest = " ".join(parts[idx + 1 :])
            return acc, rest
    return None, buf


class SpeechChunker:
    """Accumulates streamed text and hands back chunks ready for TTS."""

    def __init__(self):
        self._buf = ""
        self._sent_first = False

    def feed(self, text: str) -> list[str]:
        self._buf += text
        ready = []
        while True:
            chunk, self._buf = pop_ready_chunk(self._buf, not self._sent_first)
            if not chunk:
                return ready
            ready.append(chunk)
            self._sent_first = True

    def flush(self) -> str:
        """Return whatever is left over (possibly empty) and reset the buffer."""
        rest, self._buf = self._buf.strip(), ""
        return rest


class ExpressionTagParser:
    """Peels an optional leading `[expression]` tag off a streamed reply.

    Feed it the raw content deltas in order; it returns the text that should be shown
    and spoken (holding back anything that might still turn out to be the tag).
    Call `flush()` once the stream ends to release text held back from a very short
    reply. `expression` holds the parsed expression, defaulting to "neutral".
    """

    # Longest valid tag is "[surprised]" / "[skeptical]" plus a little whitespace slack
    MAX_TAG_LEN = 14

    def __init__(self):
        self.expression = "neutral"
        self._resolved = False
        self._buf = ""

    def feed(self, piece: str) -> str:
        if self._resolved:
            return piece

        self._buf += piece
        stripped = self._buf.lstrip()
        if not stripped:
            return ""
        if not stripped.startswith("["):
            return self._release(self._buf)
        if "]" in self._buf:
            m = TAG_RE.match(self._buf)
            if m and m.group(1).lower() in VALID_EXPRESSIONS:
                self.expression = m.group(1).lower()
                return self._release(m.group(2))
            return self._release(self._buf)
        if len(self._buf) > self.MAX_TAG_LEN:
            return self._release(self._buf)
        return ""

    def flush(self) -> str:
        if self._resolved:
            return ""
        return self._release(self._buf)

    def _release(self, text: str) -> str:
        self._resolved = True
        self._buf = ""
        return text
