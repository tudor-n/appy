"""Single owner of the busy/speaking status shown by the UI.

Two threads used to write the status directly: the main thread ("thinking") and the
speaker's playback thread ("speaking"/"idle"). Their writes could interleave so the
mascot looked idle while a tool call and a second LLM request were still running, or
flicker to idle between sentences. Now the agent reports which phase the turn is in,
the speaker reports whether it has speech queued or playing, and the UI gets one state
derived from both.
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

    def announce(self, state: str):
        """Send a one-off state that isn't derived from phase/speech (e.g. "booting").

        The next derived state is always sent after it, even if unchanged.
        """
        with self._lock:
            self._last = state
            self._emit(state)

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


class ActivityCounter:
    """Counts outstanding work; calls on_start on 0 -> 1 and on_end on 1 -> 0.

    The speaker uses it so "speaking" covers text from the moment it is queued until
    its audio has played, or failed to synthesise. Tracking only actual playback left
    gaps: after the last sentence was queued and before its audio started, and forever
    if synthesis failed.
    """

    def __init__(self, on_start: Callable[[], None] | None = None,
                 on_end: Callable[[], None] | None = None):
        self._on_start = on_start
        self._on_end = on_end
        self._lock = threading.Lock()
        self._count = 0

    @property
    def active(self) -> bool:
        return self._count > 0

    def add(self):
        # Callbacks run under the lock so start/end can never be delivered out of order
        with self._lock:
            self._count += 1
            if self._count == 1 and self._on_start:
                self._on_start()

    def done(self):
        with self._lock:
            if self._count == 0:
                return
            self._count -= 1
            if self._count == 0 and self._on_end:
                self._on_end()
