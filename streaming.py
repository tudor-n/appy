"""Pure helpers for turning a streamed LLM reply into UI text and TTS chunks.

Nothing in here does I/O, so it is all unit-testable (see tests/test_streaming.py).
"""
import re

# Speech is cut into whole sentences only. Kokoro voices each chunk as its own
# utterance, so a chunk ending at a comma got a sentence's falling tone ("That sounds
# like a plan, boss." came out as "...a plan. Boss."). Splitting the first sentence at
# a comma used to buy a head start while Kokoro ran on the CPU; on the GPU a whole
# sentence takes ~100ms, and a short opener ("Finally.") now starts on its own.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

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


def pop_ready_chunk(buf: str) -> tuple[str | None, str]:
    """Split the first complete sentence off `buf`, returning (sentence, rest).

    Returns (None, buf) when no complete sentence is available yet.
    """
    parts = SENTENCE_END.split(buf, maxsplit=1)
    if len(parts) == 1:
        return None, buf
    return parts[0].strip(), parts[1]


# Kokoro's phonemizer reads a decimal point as a pause ("5.5" -> "five five", "3.14" ->
# "three fourteen") and a thousands comma as a pause ("1,500" -> "one, five hundred").
THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# A single decimal point between digits; skips versions and IPs ("2.0.1", "192.168.1.1")
DECIMAL = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?![\d]|\.\d)")


def speakable(text: str) -> str:
    """Rewrite numbers so Kokoro says them right ("5.5" -> "5 point 5"). Only for the
    audio: the chat keeps showing the original text."""
    text = THOUSANDS_COMMA.sub("", text)
    return DECIMAL.sub(lambda m: f"{m.group(1)} point {' '.join(m.group(2))}", text)


class SpeechChunker:
    """Accumulates streamed text and hands back sentences ready for TTS."""

    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        ready = []
        while True:
            chunk, self._buf = pop_ready_chunk(self._buf)
            if chunk is None:
                return ready
            if chunk:
                ready.append(chunk)

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
