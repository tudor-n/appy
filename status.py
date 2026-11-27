"""Single owner of the busy/speaking status shown by the UI.

Two threads used to write the status directly: the main thread ("thinking") and the
speaker's playback thread ("speaking"/"idle"). Their writes could interleave so the
mascot looked idle while a tool call and a second LLM request were still running, or
flicker to idle between sentences. Now the agent reports which phase the turn is in,
the speaker reports whether it has speech queued or playing, and the UI gets one state
derived from both.

"speaking" means audible: a reply that is streaming or being synthesised but not yet
coming out of the speakers shows as "thinking", so the mascot's mouth doesn't move ahead
of the voice.
"""
import threading
from typing import Callable

PHASES = ("idle", "thinking", "replying", "compacting")
AUDIO_STATES = ("silent", "queued", "playing")


class StatusTracker:
    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self._lock = threading.Lock()
        self._phase = "idle"
        self._audio = "silent"
        # Audio has played during the current "replying" phase. Keeps the mascot
        # talking through a gap between sentences, when the LLM is slower than speech.
        self._heard_reply = False
        # Typed messages waiting for their turn (sent while Appy was still talking)
        self._queued = 0
        # The mic heard the user start talking and the UI shows "listening" (sent as a
        # "wake" event, not through here). Until their turn starts, don't knock it back
        # to idle: a late end-of-audio or end-of-compaction would otherwise do that.
        self._listening = False
        self._last: str | None = None

    @property
    def state(self) -> str:
        if self._audio == "playing" or (self._phase == "replying" and self._heard_reply):
            return "speaking"
        if self._phase == "compacting":
            return "compacting"
        if self._phase in ("thinking", "replying") or self._audio == "queued" or self._queued:
            return "thinking"
        return "idle"

    def set_phase(self, phase: str):
        if phase not in PHASES:
            raise ValueError(f"unknown phase: {phase}")
        with self._lock:
            if phase != "replying":
                self._heard_reply = False
            self._phase = phase
            self._publish_locked(force=False)

    def queue_turn(self):
        """A typed message is waiting for its turn. The UI already shows "thinking" for
        it, so when the current answer finishes we stay "thinking" instead of
        flashing "idle" (which would close that badge and open a second one)."""
        with self._lock:
            self._queued += 1
            self._publish_locked(force=False)

    def begin_turn(self, queued: bool):
        """A turn starts now; `queued` if its message went through `queue_turn`."""
        with self._lock:
            if queued:
                self._queued = max(0, self._queued - 1)
            self._listening = False
            self._phase = "thinking"
            self._heard_reply = False
            self._publish_locked(force=False)

    def heard_speech(self):
        """The mic heard speech start (the UI was sent "wake" and shows "listening")."""
        with self._lock:
            self._listening = True

    def speech_discarded(self):
        """That speech turned out to be noise: re-send the real state."""
        with self._lock:
            self._listening = False
            self._publish_locked(force=True)

    def set_audio(self, state: str):
        """The speaker's state: one of AUDIO_STATES (see SpeechActivity)."""
        if state not in AUDIO_STATES:
            raise ValueError(f"unknown audio state: {state}")
        with self._lock:
            self._audio = state
            if state == "playing" and self._phase == "replying":
                self._heard_reply = True
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
        if self._listening and state == "idle":
            return  # keep showing "listening" until the user's turn starts
        if force or state != self._last:
            self._last = state
            self._emit(state)


class SpeechActivity:
    """Tracks the speaker's work and reports it as one of AUDIO_STATES via on_change.

    "queued" from the moment text is queued with nothing else pending, "playing" once
    its audio actually starts (and through the gaps between sentences of the same
    burst), "silent" once everything queued has played or failed to synthesise.
    Covering queued-but-not-audible text keeps the status from flashing "idle" before
    the last sentence starts, or staying stuck if synthesis fails.
    """

    def __init__(self, on_change: Callable[[str], None] | None = None):
        self._on_change = on_change
        self._lock = threading.Lock()
        self._count = 0
        self._state = "silent"

    @property
    def active(self) -> bool:
        return self._count > 0

    def add(self):
        with self._lock:
            self._count += 1
            if self._count == 1:
                self._set_locked("queued")

    def playing(self):
        """Audio for queued text is about to come out of the speakers."""
        with self._lock:
            if self._count > 0:
                self._set_locked("playing")

    def done(self):
        with self._lock:
            if self._count == 0:
                return
            self._count -= 1
            if self._count == 0:
                self._set_locked("silent")

    def _set_locked(self, state: str):
        # Callbacks run under the lock so changes can never be delivered out of order
        if state != self._state:
            self._state = state
            if self._on_change:
                self._on_change(state)
