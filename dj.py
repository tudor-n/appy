"""Appy's DJ: the play_music and music_control tools, and the now-playing card.

Finding and starting music by name goes through the Spotify Web API (spotify.py), aimed
at Appy's own background player (spotifyd, player.py), the Spotify Connect device "Appy";
Riff can't be told to play a specific song. Transport, volume and "what's playing" go
over MPRIS (riff.py) to whichever player is playing, his or Tudor's Riff (ActivePlayer).
Tool results are JSON for the LLM and never contain Spotify URIs or device IDs.
"""
import json
import queue
import threading
import time
from typing import Callable

from player import DEVICE_NAME
from riff import Riff, RiffError, RiffNotRunning
from spotify import Spotify, SpotifyError
from spotify_auth import NOT_LOGGED_IN, SpotifyAuthError

PLAY_KINDS = ("track", "artist", "album", "playlist", "liked")
CONTROL_ACTIONS = ("pause", "resume", "next", "previous", "volume_up", "volume_down",
                   "set_volume", "shuffle_on", "shuffle_off", "now_playing")
# Appy's background player, among Spotify's devices (exactly; "Happy Speaker" isn't it)
PLAYER_DEVICE_NAME = DEVICE_NAME
VOLUME_STEP = 0.15
DEVICE_POLLS = 5  # spotifyd may still be connecting: look this many times, 1 s apart
NOW_PLAYING_WAIT_S = 4.0  # a playlist can take >2 s to load on spotifyd (seen live)
LIKED_FALLBACK = 100
CARD_STATES = ("playing", "paused")  # the `state` values of the music card
# How far the music goes down while someone talks. Riff and spotifyd map MPRIS volume
# logarithmically over ~60 dB, so a step (not a fraction) is a fixed loudness drop:
# 0.3 ~ 18 dB, background but audible. (A quarter of the volume was ~45 dB: silent.)
DUCK_STEP = 0.3
# While Tudor talks the laptop mic needs the music almost gone (~45 dB down); it's only
# for the few seconds of his sentence, then Appy's reply uses DUCK_STEP.
LISTEN_DUCK_STEP = 0.75


def _error(message: str) -> str:
    return json.dumps({"error": message})


class DJ:
    def __init__(self, riff, spotify: Spotify | None,
                 sleep: Callable[[float], None] = time.sleep,
                 player_running: Callable[[], bool] = lambda: True,
                 pause_others: Callable[[], None] = lambda: None,
                 own_player=None,
                 start_player: Callable[[], bool] = lambda: False):
        """`riff`: the MPRIS side (an ActivePlayer in main); `player_running()`: whether
        the background player process is up (no point waiting for its device if not)."""
        self.riff = riff
        self._player_running = player_running
        self._start_player = start_player  # brings spotifyd back if it died mid-session
        # Spotify doesn't stop Riff when Appy's player starts (seen live), so Appy does
        self._pause_others = pause_others
        # What "did the song change?" is judged on: Appy's own player, not whichever
        # player was active before (Riff's track differs from his stale one, too)
        self._own = own_player
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
        if not self._player_running() and not self._start_player():
            return _error("My music player isn't set up.")
        try:
            device = self._player_device()
            if device is None:
                return _error("My music player isn't running.")
            if kind == "liked":
                return self._play_liked(device)
            target = self._find(query, kind)
            if target is None:
                return _error(f"I couldn't find {query} on Spotify.")
            label, uri, hit, name = target
            # what the card shows under the song: the playlist or album it's from
            context = name if kind in ("playlist", "album") else None
            if queue:
                self.spotify.queue(uri, device)
                return json.dumps({"playing": label, "queued": True})
            before = self._now()
            if kind == "track" and hit.get("album_uri"):
                self.spotify.play(device, context_uri=hit["album_uri"], offset_uri=uri)
            elif kind == "track":
                self.spotify.play(device, uris=[uri])
            else:
                self.spotify.play(device, context_uri=uri)
            self._pause_others()
            return self._started(label, before, hit, context)
        except (SpotifyError, SpotifyAuthError, RiffError) as e:
            return _error(str(e))

    def _player_device(self) -> str | None:
        for attempt in range(DEVICE_POLLS):
            if attempt:
                self._sleep(1.0)
            for d in self.spotify.devices():
                if d["name"].strip().lower() == PLAYER_DEVICE_NAME.lower() and d.get("id"):
                    return d["id"]
        return None

    def _find(self, query: str, kind: str) -> tuple[str, str, dict | None, str] | None:
        """(what to call it, its URI, the track hit for a track, its bare name) or None."""
        if kind == "playlist":
            mine = self.spotify.my_playlists()
            q = query.lower()
            match = next((p for p in mine if p["name"].lower() == q), None) or \
                next((p for p in mine if q in p["name"].lower()), None)
            if match:
                return match["name"], match["uri"], None, match["name"]
        hits = self.spotify.search(query, kind)
        if not hits:
            return None
        hit = hits[0]
        if kind in ("track", "album") and hit.get("artist"):
            label = f"{hit['name']} by {hit['artist']}"
        else:
            label = hit["name"]
        return label, hit["uri"], hit if kind == "track" else None, hit["name"]

    def _play_liked(self, device: str) -> str:
        before = self._now()
        try:
            self.spotify.play(device, context_uri=f"spotify:user:{self.spotify.user_id()}:collection")
        except SpotifyError:
            uris = self.spotify.liked_tracks(LIKED_FALLBACK)
            if not uris:
                return _error("You don't have any liked songs yet.")
            self.spotify.play(device, uris=uris)
            self.spotify.shuffle(True, device)
        self._pause_others()
        return self._started("your liked songs", before, None, "Liked Songs")

    def _now(self) -> dict | None:
        try:
            return (self._own or self.riff).now_playing()
        except RiffError:
            return None

    def _started(self, label: str, before: dict | None, hit: dict | None,
                 context: str | None = None) -> str:
        """The play_music result, naming the track the player actually started. A track
        that never changed from `before` is the old one still showing: left out, never
        reported as the new one (seen live: a playlist reported the previous song)."""
        now = None
        waited = 0.0
        while waited < NOW_PLAYING_WAIT_S:
            now = self._now()
            if now and now != before:
                break
            self._sleep(0.25)
            waited += 0.25
        if not (now and now != before):
            now = {"title": hit["name"], "artist": hit.get("artist", "")} if hit else None
        result = {"playing": label, "queued": False}
        if now:
            result.update(title=now["title"], artist=now.get("artist", ""))
        if context:
            result["context"] = context
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
            # The player reports a pause/resume a moment later; say what was just done
            state = {"pause": "paused", "resume": "playing"}.get(action) or self.riff.status().lower()
            return json.dumps({"ok": True, "state": state})
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
        """The `music` widget for a tool result, or None if that result gets no card.
        `context` is the playlist/album it plays from; with no track known yet, that name
        is the card's title instead."""
        if tool_name not in ("play_music", "music_control"):
            return None
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or "error" in data:
            return None
        title, context = data.get("title"), data.get("context")
        if not title and not context:
            return None
        if tool_name == "play_music":
            if data.get("queued"):
                return None
            state = "playing"
        else:
            state = data.get("state", "playing")
        return {"widget": "music", "title": title or context,
                "artist": data.get("artist", "") if title else "",
                "state": state if state in CARD_STATES else "paused",
                "context": context if title else None}


class Ducker:
    """Turns the music down while someone talks: Tudor (from the moment the mic hears
    him until his turn is over) or Appy (while his voice is queued or playing).

    Every player that's playing gets its volume saved and lowered by a step (deeper
    while Tudor talks); once the last reason is gone, each saved volume comes back
    `restore_delay` seconds later, unless it was changed by hand meanwhile. A player
    that stops playing while ducked (playback moved to the other one) gets its volume
    back right away. MPRIS only.

    hold/release are called from the mic and speaker threads, so their D-Bus work runs
    on a worker thread (`run`), and no error ever escapes: a hiccup talking to a player
    must never make Appy deaf or mute. The saved volumes are also written to
    `state_file`, so a crash can't leave the music turned down (see `recover`).
    """

    TOLERANCE = 0.02  # MPRIS volumes come back rounded
    RESTORE_RETRIES = 3

    def __init__(self, riff: Riff, step: float = DUCK_STEP,
                 listen_step: float = LISTEN_DUCK_STEP, restore_delay: float = 0.5,
                 timer=threading.Timer, run: Callable[[Callable[[], None]], None] | None = None,
                 state_file=None):
        self.riff = riff
        self.step = step  # while Appy talks
        self.listen_step = listen_step  # while Tudor talks
        self.restore_delay = restore_delay
        self._timer_cls = timer
        self._state_file = state_file
        self._lock = threading.RLock()
        self._reasons: set[str] = set()
        self._ducked: dict = {}  # player -> [saved volume, volume we set]
        self._closed = False
        self._restore_retries = 0
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
            return None  # the player was closed meanwhile; nothing to duck or restore
        except Exception as e:  # never kill the caller's thread over the music
            print(f"[music] ducking failed: {type(e).__name__}: {e}", flush=True)
            return None

    def _players(self) -> list:
        return list(getattr(self.riff, "players", [self.riff]))

    def _is_playing(self, player) -> bool:
        try:
            return player.is_playing()
        except Exception:
            return False

    def _hold(self, reason: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._cancel_pending()
            self._reasons.add(reason)
            for player in self._players():
                playing = self._is_playing(player)
                if player in self._ducked:
                    if playing:
                        self._relevel(player)
                    else:
                        # Playback moved off it (Appy started a song over Riff): give it
                        # its volume back now
                        self._restore_player(player, forget_on_error=True)
                elif playing:
                    saved = player.volume()
                    ducked = self._depth(saved)
                    player.set_volume(ducked)
                    self._ducked[player] = [saved, ducked]
            self._save_state()

    def _release(self, reason: str) -> None:
        with self._lock:
            self._reasons.discard(reason)
            if self._ducked and self._reasons:
                for player in list(self._ducked):
                    self._relevel(player)  # e.g. Tudor done, Appy still talking: softer
                return
            if self._reasons or not self._ducked or self._pending is not None:
                return
            self._schedule_restore()

    def _depth(self, volume: float) -> float:
        step = self.listen_step if "user" in self._reasons else self.step
        return max(0.0, volume - step)

    def _relevel(self, player) -> None:
        """Move a ducked player to the depth the current reasons call for."""
        saved, ducked_to = self._ducked[player]
        ducked = self._depth(saved)
        if abs(ducked - ducked_to) > 1e-9:
            player.set_volume(ducked)
            self._ducked[player][1] = ducked

    def _volume_target(self):
        """The ducked player a volume command means: the one playing, else any."""
        playing = [p for p in self._ducked if self._is_playing(p)]
        return (playing or list(self._ducked) or [None])[0]

    def saved_volume(self) -> float | None:
        """The volume the playing player goes back to, while ducked; else None."""
        with self._lock:
            target = self._volume_target()
            return self._ducked[target][0] if target is not None else None

    def on_volume_change(self, level: float) -> bool:
        """A volume command while ducked: remember it for the restore. True = handled."""
        with self._lock:
            target = self._volume_target()
            if target is None:
                return False
            try:
                target.set_volume(self._depth(level))
            except Exception:  # it went away: nothing ducked there any more
                self._ducked.pop(target, None)
                self._save_state()
                return False
            self._ducked[target] = [level, self._depth(level)]
            self._save_state()
            return True

    def restore_now(self) -> None:
        with self._lock:
            self._cancel_pending()
            self._reasons.clear()
            self._safely(self._restore)

    def close(self) -> None:
        """Appy is quitting: put the music back and never duck again (a sentence still
        queued in the speaker could otherwise turn it down after this)."""
        with self._lock:
            self._closed = True
            self.restore_now()

    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _schedule_restore(self) -> None:
        self._pending = self._timer_cls(self.restore_delay, self._timer_fired)
        self._pending.daemon = True
        self._pending.start()

    def _timer_fired(self) -> None:
        self._safely(self._restore)

    def _restore(self) -> None:
        with self._lock:
            self._pending = None
            if self._reasons or not self._ducked:
                return
            for player in list(self._ducked):
                self._restore_player(player)
            self._save_state()
            if not self._ducked:
                self._restore_retries = 0
            elif self._restore_retries < self.RESTORE_RETRIES and not self._closed:
                # A player didn't answer: keep its saved volume and try again shortly,
                # so a hiccup never makes the ducked level the new normal
                self._restore_retries += 1
                self._schedule_restore()

    def _restore_player(self, player, forget_on_error: bool = False) -> None:
        """Give a player its volume back, unless it was changed by hand. On no answer
        its record is kept (to retry), unless `forget_on_error`."""
        saved, ducked_to = self._ducked[player]
        try:
            if abs(player.volume() - ducked_to) <= self.TOLERANCE:
                player.set_volume(saved)
        except RiffNotRunning:
            pass  # closed meanwhile: nothing to give back
        except RiffError:
            if not forget_on_error:
                return
        del self._ducked[player]

    # crash safety

    def _save_state(self) -> None:
        if self._state_file is None:
            return
        try:
            if self._ducked:
                state = {type(p).__name__: saved for p, (saved, _) in self._ducked.items()}
                self._state_file.parent.mkdir(parents=True, exist_ok=True)
                self._state_file.write_text(json.dumps(state))
            else:
                self._state_file.unlink(missing_ok=True)
        except OSError:
            pass

    def recover(self) -> None:
        """At startup: an earlier run died with the music ducked; put it back."""
        if self._state_file is None:
            return
        try:
            state = json.loads(self._state_file.read_text())
        except (OSError, ValueError):
            return
        for player in self._players():
            saved = state.get(type(player).__name__)
            if saved is None:
                continue
            try:
                if player.volume() < saved:
                    player.set_volume(saved)
                    print(f"[music] put {type(player).__name__}'s volume back after a crash")
            except Exception:
                pass
        self._state_file.unlink(missing_ok=True)
