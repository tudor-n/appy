"""The DJ tools (play_music, music_control), with fake Riff and Spotify."""
import json
import unittest

from dj import DJ, Ducker
from riff import RiffError, RiffNotRunning
from spotify import SpotifyError
from spotify_auth import SpotifyAuthError


class FakeRiff:
    def __init__(self, running=True, status="Playing", volume=0.8, now=None, start_ok=True):
        self.running = running
        self._status = status
        self._volume = volume
        self.now = now
        self.start_ok = start_ok
        self.calls: list[tuple] = []
        self.launched = 0

    def _require(self):
        if not self.running:
            raise RiffNotRunning()

    def is_running(self):
        return self.running

    def ensure_running(self, timeout=10.0):
        if self.running:
            return
        self.launched += 1
        if not self.start_ok:
            raise RiffError("Riff didn't start.")
        self.running = True

    def status(self):
        self._require()
        return self._status

    def is_playing(self):
        return self.running and self._status == "Playing"

    def _record(self, name, *args):
        self._require()
        self.calls.append((name, *args))

    def play(self):
        self._record("play")
        self._status = "Playing"

    def pause(self):
        self._record("pause")
        self._status = "Paused"

    def next(self):
        self._record("next")

    def previous(self):
        self._record("previous")

    def open_uri(self, uri):
        self._record("open_uri", uri)

    def volume(self):
        self._require()
        return self._volume

    def set_volume(self, v):
        self._record("set_volume", v)
        self._volume = min(1.0, max(0.0, v))

    def shuffle(self, on):
        self._record("shuffle", on)

    def now_playing(self):
        self._require()
        return self.now


class FakeSpotify:
    def __init__(self, devices=None, search=None, playlists=None, liked=None,
                 play_error=None, device_polls_until_found=1):
        self._devices = devices if devices is not None else [
            {"id": "riff-id", "name": "Riff", "is_active": True}]
        self._search = search or {}
        self._playlists = playlists or []
        self._liked = liked or []
        self.play_error = play_error
        self.calls: list[tuple] = []
        self.device_polls = 0
        self.device_polls_until_found = device_polls_until_found

    def search(self, query, kind, limit=5):
        self.calls.append(("search", query, kind))
        return self._search.get(kind, [])

    def my_playlists(self):
        self.calls.append(("my_playlists",))
        return self._playlists

    def liked_tracks(self, limit=100):
        self.calls.append(("liked_tracks", limit))
        return self._liked[:limit]

    def user_id(self):
        return "tudor"

    def devices(self):
        self.device_polls += 1
        return self._devices if self.device_polls >= self.device_polls_until_found else []

    def play(self, device_id, context_uri=None, uris=None):
        self.calls.append(("play", device_id, context_uri, uris))
        if self.play_error:
            err, self.play_error = self.play_error, None
            raise err

    def queue(self, uri, device_id):
        self.calls.append(("queue", uri, device_id))

    def shuffle(self, on, device_id):
        self.calls.append(("shuffle", on, device_id))


HELLO = {"name": "Hello", "uri": "spotify:track:hello", "artist": "Adele"}


class DJTestCase(unittest.TestCase):
    def setUp(self):
        self.results: list[str] = []

    def dj(self, riff=None, spotify="default"):
        self.riff = riff or FakeRiff()
        self.spotify = FakeSpotify(search={"track": [HELLO]}) if spotify == "default" else spotify
        return DJ(self.riff, self.spotify, sleep=lambda s: None)

    def run_tool(self, fn, *args, **kwargs) -> dict:
        raw = fn(*args, **kwargs)
        self.results.append(raw)
        return json.loads(raw)

    def tearDown(self):
        for raw in self.results:  # tool results never carry Spotify URIs or IDs
            self.assertNotIn("spotify:", raw)
            self.assertNotIn("riff-id", raw)

    def spotify_calls(self, name):
        return [c for c in self.spotify.calls if c[0] == name]


class PlayMusicTests(DJTestCase):
    def test_play_track_uses_riff_device_and_names_real_track(self):
        dj = self.dj(riff=FakeRiff(now={"title": "Hello", "artist": "Adele", "album": "25"}))
        result = self.run_tool(dj.play_music, "hello", "track")
        self.assertEqual(self.spotify_calls("play"), [("play", "riff-id", None, ["spotify:track:hello"])])
        self.assertEqual(result["playing"], "Hello by Adele")
        self.assertFalse(result["queued"])
        self.assertEqual((result["title"], result["artist"]), ("Hello", "Adele"))

    def test_no_results_is_an_error(self):
        dj = self.dj(spotify=FakeSpotify())
        result = self.run_tool(dj.play_music, "zzqx nonsense", "track")
        self.assertIn("zzqx nonsense", result["error"])
        self.assertEqual(self.spotify_calls("play"), [])

    def test_empty_query_is_an_error(self):
        result = self.run_tool(self.dj().play_music, "  ", "track")
        self.assertIn("error", result)

    def test_unknown_kind_is_an_error(self):
        result = self.run_tool(self.dj().play_music, "x", "podcast")
        self.assertIn("error", result)

    def test_artist_plays_its_context(self):
        sp = FakeSpotify(search={"artist": [{"name": "Radiohead", "uri": "spotify:artist:r"}]})
        result = self.run_tool(self.dj(spotify=sp).play_music, "radiohead", "artist")
        self.assertEqual(self.spotify_calls("play"), [("play", "riff-id", "spotify:artist:r", None)])
        self.assertEqual(result["playing"], "Radiohead")

    def test_playlist_prefers_own_playlists(self):
        sp = FakeSpotify(playlists=[{"name": "Gym Mix", "uri": "spotify:playlist:mine2"},
                                    {"name": "gym", "uri": "spotify:playlist:mine"}],
                         search={"playlist": [{"name": "Gym", "uri": "spotify:playlist:public"}]})
        result = self.run_tool(self.dj(spotify=sp).play_music, "Gym", "playlist")
        self.assertEqual(self.spotify_calls("play"), [("play", "riff-id", "spotify:playlist:mine", None)])
        self.assertEqual(self.spotify_calls("search"), [])
        self.assertEqual(result["playing"], "gym")

    def test_playlist_substring_match_before_search(self):
        sp = FakeSpotify(playlists=[{"name": "Late Night Coding", "uri": "spotify:playlist:c"}])
        self.run_tool(self.dj(spotify=sp).play_music, "coding", "playlist")
        self.assertEqual(self.spotify_calls("play"), [("play", "riff-id", "spotify:playlist:c", None)])

    def test_playlist_falls_back_to_search(self):
        sp = FakeSpotify(search={"playlist": [{"name": "Lofi Beats", "uri": "spotify:playlist:l"}]})
        result = self.run_tool(self.dj(spotify=sp).play_music, "lofi", "playlist")
        self.assertEqual(self.spotify_calls("play"), [("play", "riff-id", "spotify:playlist:l", None)])
        self.assertEqual(result["playing"], "Lofi Beats")

    def test_liked_plays_collection(self):
        result = self.run_tool(self.dj().play_music, "", "liked")
        self.assertEqual(self.spotify_calls("play"),
                         [("play", "riff-id", "spotify:user:tudor:collection", None)])
        self.assertEqual(result["playing"], "your liked songs")

    def test_liked_falls_back_to_newest_100_shuffled(self):
        liked = [f"spotify:track:{i}" for i in range(150)]
        sp = FakeSpotify(liked=liked, play_error=SpotifyError("Spotify couldn't find that."))
        result = self.run_tool(self.dj(spotify=sp).play_music, "", "liked")
        plays = self.spotify_calls("play")
        self.assertEqual(len(plays), 2)
        self.assertEqual(plays[1], ("play", "riff-id", None, liked[:100]))
        self.assertEqual(self.spotify_calls("shuffle"), [("shuffle", True, "riff-id")])
        self.assertEqual(result["playing"], "your liked songs")

    def test_queue_track(self):
        result = self.run_tool(self.dj().play_music, "hello", "track", queue=True)
        self.assertEqual(self.spotify_calls("queue"), [("queue", "spotify:track:hello", "riff-id")])
        self.assertEqual(self.spotify_calls("play"), [])
        self.assertTrue(result["queued"])
        self.assertEqual(result["playing"], "Hello by Adele")

    def test_queue_non_track_is_error(self):
        result = self.run_tool(self.dj().play_music, "radiohead", "artist", queue=True)
        self.assertEqual(result["error"], "I can only queue single songs.")

    def test_launches_riff_and_waits_for_device(self):
        sp = FakeSpotify(search={"track": [HELLO]}, device_polls_until_found=3)
        self.run_tool(self.dj(riff=FakeRiff(running=False), spotify=sp).play_music, "hello", "track")
        self.assertEqual(self.riff.launched, 1)
        self.assertEqual(sp.device_polls, 3)
        self.assertEqual(self.spotify_calls("play"), [("play", "riff-id", None, ["spotify:track:hello"])])

    def test_riff_wont_start(self):
        result = self.run_tool(self.dj(riff=FakeRiff(running=False, start_ok=False)).play_music,
                               "hello", "track")
        self.assertEqual(result["error"], "Riff didn't start.")

    def test_falls_back_to_open_uri_without_device(self):
        sp = FakeSpotify(devices=[{"id": "phone", "name": "Pixel", "is_active": False}],
                         search={"track": [HELLO]})
        result = self.run_tool(self.dj(spotify=sp).play_music, "hello", "track")
        self.assertEqual(self.spotify_calls("play"), [])
        self.assertEqual(self.riff.calls, [("open_uri", "spotify:track:hello")])
        self.assertEqual(result["playing"], "Hello by Adele")

    def test_queue_without_device_uses_active_device(self):
        sp = FakeSpotify(devices=[], search={"track": [HELLO]})
        self.run_tool(self.dj(spotify=sp).play_music, "hello", "track", queue=True)
        self.assertEqual(self.spotify_calls("queue"), [("queue", "spotify:track:hello", None)])

    def test_spotify_errors_become_tool_errors(self):
        sp = FakeSpotify(search={"track": [HELLO]}, play_error=SpotifyError("Spotify needs Premium for that."))
        result = self.run_tool(self.dj(spotify=sp).play_music, "hello", "track")
        self.assertEqual(result["error"], "Spotify needs Premium for that.")

    def test_auth_errors_become_tool_errors(self):
        sp = FakeSpotify(search={"track": [HELLO]},
                         play_error=SpotifyAuthError("Spotify logged me out. Run appy spotify-login again."))
        result = self.run_tool(self.dj(spotify=sp).play_music, "hello", "track")
        self.assertIn("spotify-login", result["error"])

    def test_not_logged_in_play_errors_but_control_works(self):
        dj = self.dj(spotify=None)
        self.assertIn("spotify-login", self.run_tool(dj.play_music, "hello", "track")["error"])
        self.assertTrue(self.run_tool(dj.music_control, "pause")["ok"])
        self.assertEqual(self.riff.calls, [("pause",)])


class MusicControlTests(DJTestCase):
    def test_transport_actions(self):
        dj = self.dj()
        for action, call in (("pause", "pause"), ("resume", "play"), ("next", "next"),
                             ("previous", "previous")):
            with self.subTest(action=action):
                self.assertTrue(self.run_tool(dj.music_control, action)["ok"])
                self.assertEqual(self.riff.calls[-1], (call,))
        self.assertEqual(self.spotify.calls, [])  # transport never touches the Web API

    def test_control_without_riff_is_friendly_and_does_not_launch(self):
        dj = self.dj(riff=FakeRiff(running=False))
        for action in ("pause", "next", "now_playing", "volume_up"):
            with self.subTest(action=action):
                self.assertEqual(self.run_tool(dj.music_control, action)["error"], "Riff isn't open.")
        self.assertEqual(self.riff.launched, 0)

    def test_volume_steps_and_set_volume_percent(self):
        dj = self.dj(riff=FakeRiff(volume=0.5))
        self.run_tool(dj.music_control, "volume_up")
        self.assertAlmostEqual(self.riff._volume, 0.65)
        self.run_tool(dj.music_control, "volume_down")
        self.run_tool(dj.music_control, "volume_down")
        self.assertAlmostEqual(self.riff._volume, 0.35)
        self.run_tool(dj.music_control, "set_volume", 40)
        self.assertAlmostEqual(self.riff._volume, 0.40)

    def test_set_volume_needs_a_value(self):
        self.assertIn("error", self.run_tool(self.dj().music_control, "set_volume"))

    def test_volume_hook_takes_over_when_it_handles(self):
        dj = self.dj(riff=FakeRiff(volume=0.5))
        seen = []
        dj.set_volume_hook(lambda v: seen.append(v) or True)
        self.run_tool(dj.music_control, "set_volume", 70)
        self.assertEqual(seen, [0.7])
        self.assertEqual(self.riff.calls, [])

    def test_shuffle(self):
        dj = self.dj()
        self.run_tool(dj.music_control, "shuffle_on")
        self.run_tool(dj.music_control, "shuffle_off")
        self.assertEqual(self.riff.calls, [("shuffle", True), ("shuffle", False)])

    def test_now_playing(self):
        dj = self.dj(riff=FakeRiff(now={"title": "Karma Police", "artist": "Radiohead",
                                        "album": "OK Computer"}))
        self.assertEqual(self.run_tool(dj.music_control, "now_playing"),
                         {"title": "Karma Police", "artist": "Radiohead",
                          "album": "OK Computer", "state": "playing"})

    def test_now_playing_nothing(self):
        result = self.run_tool(self.dj(riff=FakeRiff(status="Stopped")).music_control, "now_playing")
        self.assertEqual(result["error"], "Nothing's playing in Riff.")

    def test_unknown_action(self):
        self.assertIn("error", self.run_tool(self.dj().music_control, "rewind"))


class RiffStateTests(DJTestCase):
    def test_riff_state(self):
        self.assertEqual(self.dj(riff=FakeRiff(running=False)).riff_state(), "closed")
        self.assertEqual(self.dj(riff=FakeRiff(status="Paused")).riff_state(), "paused")
        self.assertEqual(self.dj(riff=FakeRiff(status="Playing")).riff_state(), "playing")


class MusicCardTests(DJTestCase):
    def test_music_card_for_play_and_now_playing_only(self):
        dj = self.dj(riff=FakeRiff(now={"title": "Hello", "artist": "Adele", "album": "25"}))
        played = dj.play_music("hello", "track")
        self.assertEqual(dj.music_card("play_music", played),
                         {"widget": "music", "title": "Hello", "artist": "Adele", "state": "playing"})
        now = dj.music_card("music_control", dj.music_control("now_playing"))
        self.assertEqual(now["title"], "Hello")
        self.assertIsNone(dj.music_card("music_control", dj.music_control("pause")))
        self.assertIsNone(dj.music_card("play_music", dj.play_music("hello", "track", queue=True)))
        self.assertIsNone(dj.music_card("play_music", json.dumps({"error": "x"})))
        self.assertIsNone(dj.music_card("get_weather", played))

    def test_paused_now_playing_card(self):
        dj = self.dj(riff=FakeRiff(status="Paused", now={"title": "A", "artist": "B", "album": ""}))
        self.assertEqual(dj.music_card("music_control", dj.music_control("now_playing"))["state"], "paused")


class FakeTimer:
    """threading.Timer stand-in: fire() runs it by hand."""
    made: list["FakeTimer"] = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.started, self.cancelled = delay, fn, False, False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if self.started and not self.cancelled:
            self.fn()


class DuckerTests(unittest.TestCase):
    def setUp(self):
        FakeTimer.made = []
        self.riff = FakeRiff(volume=0.8)
        self.ducker = Ducker(self.riff, timer=FakeTimer, run=lambda fn: fn())

    def fire_last(self):
        FakeTimer.made[-1].fire()

    def test_ducks_to_quarter_and_restores_after_both_release(self):
        self.ducker.hold("user")
        self.assertAlmostEqual(self.riff._volume, 0.2)
        self.ducker.hold("appy")
        self.ducker.release("appy")
        self.assertEqual(FakeTimer.made, [])  # Tudor's turn is still open
        self.assertAlmostEqual(self.riff._volume, 0.2)
        self.ducker.release("user")
        self.assertEqual(FakeTimer.made[-1].delay, 0.5)
        self.fire_last()
        self.assertAlmostEqual(self.riff._volume, 0.8)

    def test_no_duck_when_not_playing(self):
        self.riff._status = "Paused"
        self.ducker.hold("appy")
        self.ducker.release("appy")
        self.assertEqual(self.riff.calls, [])

    def test_new_hold_cancels_pending_restore(self):
        self.ducker.hold("appy")
        self.ducker.release("appy")
        pending = FakeTimer.made[-1]
        self.ducker.hold("appy")
        self.assertTrue(pending.cancelled)
        pending.fire()
        self.assertAlmostEqual(self.riff._volume, 0.2)

    def test_manual_change_while_ducked_is_kept(self):
        self.ducker.hold("appy")
        self.riff._volume = 0.5  # Tudor drags Riff's slider
        self.ducker.release("appy")
        self.fire_last()
        self.assertAlmostEqual(self.riff._volume, 0.5)

    def test_volume_command_while_ducked_updates_saved(self):
        self.ducker.hold("appy")
        self.assertTrue(self.ducker.on_volume_change(0.6))
        self.assertAlmostEqual(self.riff._volume, 0.15)
        self.ducker.release("appy")
        self.fire_last()
        self.assertAlmostEqual(self.riff._volume, 0.6)

    def test_volume_command_when_not_ducked_is_not_handled(self):
        self.assertFalse(self.ducker.on_volume_change(0.6))
        self.assertEqual(self.riff.calls, [])

    def test_restore_now(self):
        self.ducker.hold("user")
        self.ducker.hold("appy")
        self.ducker.restore_now()
        self.assertAlmostEqual(self.riff._volume, 0.8)
        self.ducker.hold("appy")  # and it ducks again afterwards
        self.assertAlmostEqual(self.riff._volume, 0.2)

    def test_release_without_hold_is_harmless(self):
        self.ducker.release("user")
        self.assertEqual(FakeTimer.made, [])

    def test_volume_up_while_ducked_steps_from_the_real_level(self):
        dj = DJ(self.riff, None, sleep=lambda s: None)
        dj.set_volume_hook(self.ducker.on_volume_change, self.ducker.saved_volume)
        self.ducker.hold("user")  # Tudor says "turn it up" over the music
        self.assertTrue(json.loads(dj.music_control("volume_up"))["ok"])
        self.assertAlmostEqual(self.riff._volume, 0.95 * 0.25)
        self.ducker.release("user")
        self.fire_last()
        self.assertAlmostEqual(self.riff._volume, 0.95)

    def test_saved_volume_only_while_ducked(self):
        self.assertIsNone(self.ducker.saved_volume())
        self.ducker.hold("appy")
        self.assertAlmostEqual(self.ducker.saved_volume(), 0.8)

    def test_unexpected_errors_never_escape(self):
        class Exploding(FakeRiff):
            def volume(self):
                raise RuntimeError("bus exploded")

        ducker = Ducker(Exploding(), timer=FakeTimer, run=lambda fn: fn())
        ducker.hold("user")  # must not raise on the mic thread
        ducker.release("user")
        ducker.restore_now()
        self.assertFalse(ducker.on_volume_change(0.5))

    def test_hold_and_release_run_on_the_worker(self):
        queued = []
        ducker = Ducker(self.riff, timer=FakeTimer, run=queued.append)
        ducker.hold("appy")  # returns at once: no D-Bus on the caller's thread
        self.assertEqual(self.riff.calls, [])
        for fn in queued:
            fn()
        self.assertAlmostEqual(self.riff._volume, 0.2)

    def test_default_worker_processes_in_order(self):
        ducker = Ducker(self.riff, timer=FakeTimer)
        ducker.hold("appy")
        ducker.flush()
        self.assertAlmostEqual(self.riff._volume, 0.2)

    def test_riff_errors_are_swallowed(self):
        self.ducker.hold("appy")
        self.riff.running = False  # Riff quit while ducked
        self.ducker.release("appy")
        self.fire_last()
        self.ducker.restore_now()
        self.ducker.hold("user")
        self.assertFalse(self.ducker.on_volume_change(0.3))


if __name__ == "__main__":
    unittest.main()
