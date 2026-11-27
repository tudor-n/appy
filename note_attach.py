"""Attach memory notes to the user's message when it names them.

Appy's system prompt holds only core facts and a one-line index of notes; the details are
in the notes, which the model is supposed to open with memory_read. Gemma often doesn't
(and then claims it checked). So the brain does it: when a message names a note ("dummy",
"lumen", "the watering schedule"), that note is read from the Mac and put in front of the
message, once per conversation (again after memory changes).
"""
import json
import re
import threading

# Words that name nothing specific (or everything: "appy" is in every greeting)
STOPWORDS = {
    "appy", "tudor", "the", "and", "for", "with", "about", "project", "projects", "note",
    "notes", "info", "information", "details", "regarding", "system", "app", "application",
    "robot", "bot", "ui", "core",
}
MIN_WORD = 3


def _keywords(slug: str, title: str) -> set[str]:
    words = set(slug.lower().split("-")) | set(re.findall(r"[a-z0-9]+", title.lower()))
    return {w for w in words if len(w) >= MIN_WORD and w not in STOPWORDS}


class NoteAttacher:
    def __init__(self, client, index: str):
        self.client = client
        self._lock = threading.Lock()
        self._notes: list[tuple[str, set[str]]] = []
        self._attached: set[str] = set()
        self._set_index(index)

    def _set_index(self, index: str) -> None:
        notes = []
        for line in index.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2 and parts[0] and parts[0] != "core":
                notes.append((parts[0], _keywords(parts[0], parts[1])))
        with self._lock:
            self._notes = notes

    def refresh(self) -> None:
        """Memory changed: take the new index, and let changed notes attach again."""
        context = self.client.context()
        if context is not None:
            self._set_index(context.get("index", ""))
        with self._lock:
            self._attached.clear()

    def matches(self, text: str) -> list[str]:
        with self._lock:
            return self._matches(text)

    def attach(self, text: str) -> tuple[str, list[str]]:
        """The block to put before the message, and the notes in it (for `release`)."""
        with self._lock:
            wanted = [s for s in self._matches(text) if s not in self._attached]
        bodies = []
        for slug in wanted:
            try:
                data = json.loads(self.client.read(slug))
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and data.get("text"):
                bodies.append((slug, str(data["text"]).strip()))
        if not bodies:
            return "", []
        slugs = [s for s, _ in bodies]
        with self._lock:
            self._attached.update(slugs)
        notes = "\n\n".join(f"note '{slug}':\n{body}" for slug, body in bodies)
        return (f"[From your memory (already read for you, no need to memory_read it):\n"
                f"{notes}]"), slugs

    def _matches(self, text: str) -> list[str]:
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        return [slug for slug, keys in self._notes if keys & words]

    def release(self, slugs: list[str]) -> None:
        """The turn failed: Appy never saw these notes."""
        with self._lock:
            self._attached.difference_update(slugs)
