"""Single owner of the busy/speaking status shown by the UI.

Two threads used to write the status directly: the main thread ("thinking") and the
speaker's playback thread ("speaking"/"idle"). Their writes could interleave so the
mascot looked idle while a tool call and a second LLM request were still running, or
flicker to idle between sentences. Now the agent reports which phase the turn is in,
the speaker reports whether audio is playing, and the UI gets one state derived from
both.
"""
import threading
from typing import Callable

PHASES = ("idle", "thinking", "replying")


class StatusTracker:
    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self._lock = threading.Lock()
        self._phase = "idle"
        self._speaking = False
        self._last: str | None = None

    @property
    def state(self) -> str:
        if self._speaking or self._phase == "replying":
            return "speaking"
        if self._phase == "thinking":
            return "thinking"
        return "idle"

    def set_phase(self, phase: str):
        if phase not in PHASES:
            raise ValueError(f"unknown phase: {phase}")
        with self._lock:
            self._phase = phase
            self._publish_locked(force=False)

    def set_speaking(self, speaking: bool):
        with self._lock:
            self._speaking = speaking
            self._publish_locked(force=False)

    def publish(self, force: bool = False):
        """Re-send the current state; `force` sends it even if unchanged.

        Use force after something else has changed the UI's state behind the
        tracker's back (e.g. a "wake" that turned out to be noise).
        """
        with self._lock:
            self._publish_locked(force)

    def _publish_locked(self, force: bool):
        state = self.state
        if force or state != self._last:
            self._last = state
            self._emit(state)
