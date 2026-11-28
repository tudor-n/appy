"""Appy's DJ: the play_music and music_control tools, and the now-playing card.

Transport goes to Riff over MPRIS (riff.py); finding and starting music by name goes
through the Spotify Web API (spotify.py), aimed at Riff's Spotify Connect device. If Riff
doesn't show up as a device, tracks and playlists are handed to Riff's MPRIS OpenUri.
Tool results are JSON for the LLM and never contain Spotify URIs or device IDs.
"""
import json
import queue
import threading
import time
from typing import Callable

from riff import Riff, RiffError, RiffNotRunning
from spotify import Spotify, SpotifyError
from spotify_auth import NOT_LOGGED_IN, SpotifyAuthError

PLAY_KINDS = ("track", "artist", "album", "playlist", "liked")
CONTROL_ACTIONS = ("pause", "resume", "next", "previous", "volume_up", "volume_down",
                   "set_volume", "shuffle_on", "shuffle_off", "now_playing")
# Riff's Spotify Connect device is found by this (case-insensitive) part of its name
RIFF_DEVICE_HINT = "riff"
VOLUME_STEP = 0.15
DEVICE_POLLS = 5  # after launching Riff, check for its device this many times, 1 s apart
NOW_PLAYING_WAIT_S = 2.0
LIKED_FALLBACK = 100
CARD_STATES = ("playing", "paused")  # the `state` values of the music card


def _error(message: str) -> str:
    return json.dumps({"error": message})


class DJ:
    def __init__(self, riff: Riff, spotify: Spotify | None,
                 sleep: Callable[[float], None] = time.sleep):
        self.riff = riff
        self.spotify = spotify
        self._sleep = sleep
        self._volume_hook: Callable[[float], bool] | None = None
        self._volume_source: Callable[[], float | None] | None = None

    def set_volume_hook(self, fn: Callable[[float], bool],
                        current: Callable[[], float | None] | None = None) -> None:
        """`fn(level)` gets every volume change first and returns True if it handled it;
        `current()` is the real volume to step from, or None to read Riff's (the ducker,
        while the music is turned down for speech: Riff's own volume is the ducked one)."""
        self._volume_hook = fn
        self._volume_source = current

    def riff_state(self) -> str:
        """"closed", "paused" (open, not playing) or "playing"; for tools.forced_call."""
        if not self.riff.is_running():
            return "closed"
        return "playing" if self.riff.is_playing() else "paused"

    # play_music

    def play_music(self, query: str = "", kind: str = "track", queue: bool = False) -> str:
        query, kind = str(query or "").strip(), str(kind or "track").strip().lower()
        if kind not in PLAY_KINDS:
            return _error(f"Unknown kind {kind}; use one of {', '.join(PLAY_KINDS)}.")
        if kind != "liked" and not query:
            return _error("What should I play?")
        if queue and kind != "track":
            return _error("I can only queue single songs.")
        if self.spotify is None:
            return _error(NOT_LOGGED_IN)
        try:
            launched = not self.riff.is_running()
            self.riff.ensure_running()
            device = self._riff_device(launched)
            if kind == "liked":
                return self._play_liked(device)
            target = self._find(query, kind)
            if target is None:
                return _error(f"I couldn't find {query} on Spotify.")
            label, uri, hit = target
            if queue:
                self.spotify.queue(uri, device)
                return json.dumps({"playing": label, "queued": True})
            before = self._now()
            if device:
                if kind == "track":
                    self.spotify.play(device, uris=[uri])
                else:
                    self.spotify.play(device, context_uri=uri)
            else:
                self.riff.open_uri(uri)
            return self._started(label, before, hit)
        except (SpotifyError, SpotifyAuthError, RiffError) as e:
            return _error(str(e))

    def _riff_device(self, launched: bool) -> str | None:
        for attempt in range(DEVICE_POLLS if launched else 1):
            if attempt:
                self._sleep(1.0)
            for d in self.spotify.devices():
                if RIFF_DEVICE_HINT in d["name"].lower() and d.get("id"):
                    return d["id"]
        return None

    def _find(self, query: str, kind: str) -> tuple[str, str, dict | None] | None:
        """(what to call it, its URI, the track hit for a track) or None."""
        if kind == "playlist":
            mine = self.spotify.my_playlists()
            q = query.lower()
            match = next((p for p in mine if p["name"].lower() == q), None) or \
                next((p for p in mine if q in p["name"].lower()), None)
            if match:
                return match["name"], match["uri"], None
        hits = self.spotify.search(query, kind)
        if not hits:
            return None
        hit = hits[0]
        if kind in ("track", "album") and hit.get("artist"):
            label = f"{hit['name']} by {hit['artist']}"
        else:
            label = hit["name"]
        return label, hit["uri"], hit if kind == "track" else None

    def _play_liked(self, device: str | None) -> str:
        before = self._now()
        try:
            self.spotify.play(device, context_uri=f"spotify:user:{self.spotify.user_id()}:collection")
        except SpotifyError:
            uris = self.spotify.liked_tracks(LIKED_FALLBACK)
            if not uris:
                return _error("You don't have any liked songs yet.")
            self.spotify.play(device, uris=uris)
            self.spotify.shuffle(True, device)
        return self._started("your liked songs", before, None)

    def _now(self) -> dict | None:
        try:
            return self.riff.now_playing()
        except RiffError:
            return None

    def _started(self, label: str, before: dict | None, hit: dict | None) -> str:
        """The play_music result, naming the track Riff actually started if it says."""
        now = None
        waited = 0.0
        while waited < NOW_PLAYING_WAIT_S:
            now = self._now()
            if now and now != before:
                break
            self._sleep(0.25)
            waited += 0.25
        if hit and not (now and now != before):
            now = {"title": hit["name"], "artist": hit.get("artist", "")}
        result = {"playing": label, "queued": False}
        if now:
            result.update(title=now["title"], artist=now.get("artist", ""))
        return json.dumps(result)

    # music_control

    def music_control(self, action: str = "", value: float | None = None) -> str:
        action = str(action or "").strip().lower()
        if action not in CONTROL_ACTIONS:
            return _error(f"Unknown action {action}; use one of {', '.join(CONTROL_ACTIONS)}.")
        try:
            if action == "now_playing":
                return self._now_playing()
            if action == "pause":
                self.riff.pause()
            elif action == "resume":
                self.riff.play()
            elif action == "next":
                self.riff.next()
            elif action == "previous":
                self.riff.previous()
            elif action in ("volume_up", "volume_down"):
                step = VOLUME_STEP if action == "volume_up" else -VOLUME_STEP
                base = self._volume_source() if self._volume_source else None
                self._set_volume((self.riff.volume() if base is None else base) + step)
            elif action == "set_volume":
                if value is None:
                    return _error("set_volume needs a value from 0 to 100.")
                self._set_volume(float(value) / 100.0)
            elif action in ("shuffle_on", "shuffle_off"):
                self.riff.shuffle(action == "shuffle_on")
            return json.dumps({"ok": True, "state": self.riff.status().lower()})
        except (RiffError, ValueError, TypeError) as e:
            return _error(str(e) if isinstance(e, RiffError) else f"Bad value for {action}.")

    def _set_volume(self, level: float) -> None:
        level = min(1.0, max(0.0, level))
        if self._volume_hook and self._volume_hook(level):
            return
        self.riff.set_volume(level)

    def _now_playing(self) -> str:
        state = self.riff.status().lower()
        now = self.riff.now_playing()
        if not now or state == "stopped":
            return _error("Nothing's playing in Riff.")
        return json.dumps({**now, "state": state})

    # the overlay's card

    def music_card(self, tool_name: str, result: str) -> dict | None:
        """The `music` widget for a tool result, or None if that result gets no card."""
        if tool_name not in ("play_music", "music_control"):
            return None
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or "error" in data or not data.get("title"):
            return None
        if tool_name == "play_music":
            if data.get("queued"):
                return None
            state = "playing"
        else:
            state = data.get("state", "playing")
        return {"widget": "music", "title": data["title"], "artist": data.get("artist", ""),
                "state": state if state in CARD_STATES else "paused"}


class Ducker:
    """Turns Riff down while someone talks: Tudor (from the moment the mic hears him
    until his turn is over) or Appy (while his voice is queued or playing).

    The first reason to duck saves Riff's volume and sets `level` of it; once the last
    reason is gone, the saved volume comes back `restore_delay` seconds later, unless
    Riff's volume was changed by hand meanwhile. MPRIS only.

    hold/release are called from the mic and speaker threads, so their D-Bus work runs
    on a worker thread (`run`), and no error ever escapes: a hiccup talking to Riff must
    never make Appy deaf or mute.
    """

    TOLERANCE = 0.02  # MPRIS volumes come back rounded

    def __init__(self, riff: Riff, level: float = 0.25, restore_delay: float = 0.5,
                 timer=threading.Timer, run: Callable[[Callable[[], None]], None] | None = None):
        self.riff = riff
        self.level = level
        self.restore_delay = restore_delay
        self._timer_cls = timer
        self._lock = threading.RLock()
        self._reasons: set[str] = set()
        self._saved: float | None = None  # the volume to restore; None = not ducked
        self._ducked_to: float | None = None
        self._pending = None
        if run is None:
            self._jobs: queue.Queue = queue.Queue()
            threading.Thread(target=self._worker, daemon=True, name="ducker").start()
            run = self._jobs.put
        self._run = run

    def _worker(self):
        while True:
            job = self._jobs.get()
            try:
                job()
            finally:
                self._jobs.task_done()

    def flush(self) -> None:
        """Wait for queued hold/release work (tests, shutdown)."""
        if hasattr(self, "_jobs"):
            self._jobs.join()

    def hold(self, reason: str) -> None:
        self._run(lambda: self._safely(self._hold, reason))

    def release(self, reason: str) -> None:
        self._run(lambda: self._safely(self._release, reason))

    @staticmethod
    def _safely(fn, *args):
        try:
            return fn(*args)
        except RiffNotRunning:
            return None  # Riff was closed meanwhile; nothing to duck or restore
        except Exception as e:  # never kill the caller's thread over the music
            print(f"[music] ducking failed: {type(e).__name__}: {e}", flush=True)
            return None

    def _hold(self, reason: str) -> None:
        with self._lock:
            self._cancel_pending()
            self._reasons.add(reason)
            if self._saved is not None or not self.riff.is_playing():
                return
            saved = self.riff.volume()
            self.riff.set_volume(saved * self.level)
            self._saved, self._ducked_to = saved, saved * self.level

    def _release(self, reason: str) -> None:
        with self._lock:
            self._reasons.discard(reason)
            if self._reasons or self._saved is None or self._pending is not None:
                return
            self._pending = self._timer_cls(self.restore_delay, self._timer_fired)
            self._pending.daemon = True
            self._pending.start()

    def saved_volume(self) -> float | None:
        """The volume Riff goes back to, while ducked; None when it isn't ducked."""
        with self._lock:
            return self._saved

    def on_volume_change(self, level: float) -> bool:
        """A volume command while ducked: remember it for the restore. True = handled."""
        with self._lock:
            if self._saved is None:
                return False
            try:
                self.riff.set_volume(level * self.level)
            except Exception:  # Riff went away: nothing ducked any more
                self._saved = self._ducked_to = None
                return False
            self._saved, self._ducked_to = level, level * self.level
            return True

    def restore_now(self) -> None:
        with self._lock:
            self._cancel_pending()
            self._reasons.clear()
            self._safely(self._restore)

    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _timer_fired(self) -> None:
        self._safely(self._restore)

    def _restore(self) -> None:
        with self._lock:
            self._pending = None
            if self._reasons or self._saved is None:
                return
            saved, ducked_to = self._saved, self._ducked_to
            self._saved = self._ducked_to = None
            if abs(self.riff.volume() - ducked_to) <= self.TOLERANCE:
                self.riff.set_volume(saved)
