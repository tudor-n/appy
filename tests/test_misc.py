import datetime
import json
import time
import unittest

import tools
from status import SpeechActivity, StatusTracker


class StatusTrackerTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.tracker = StatusTracker(self.sent.append)

    def test_speaking_only_once_audio_plays(self):
        # Regression: the mascot's mouth moved for ~1s before any sound, because the
        # first streamed token already counted as "speaking"
        t = self.tracker
        t.set_phase("thinking")
        t.set_phase("thinking")
        t.set_phase("replying")
        t.set_audio("queued")        # first sentence is being synthesised
        self.assertEqual(self.sent, ["thinking"])
        t.set_audio("playing")
        t.set_audio("silent")        # gap between sentences: still replying
        t.set_phase("idle")
        self.assertEqual(self.sent, ["thinking", "speaking", "idle"])

    def test_audio_outlasting_the_turn(self):
        t = self.tracker
        t.set_phase("replying")
        t.set_audio("queued")
        t.set_audio("playing")
        t.set_phase("idle")  # LLM done, audio still playing
        self.assertEqual(self.sent[-1], "speaking")
        t.set_audio("silent")
        self.assertEqual(self.sent[-1], "idle")

    def test_queued_audio_after_the_turn_keeps_thinking(self):
        t = self.tracker
        t.set_phase("replying")
        t.set_audio("queued")
        t.set_phase("idle")  # stream ended before the audio started
        self.assertEqual(self.sent, ["thinking"])
        t.set_audio("playing")
        t.set_audio("silent")
        self.assertEqual(self.sent, ["thinking", "speaking", "idle"])

    def test_trailing_speech_during_tool_round_falls_back_to_thinking(self):
        t = self.tracker
        t.set_phase("replying")
        t.set_audio("queued")
        t.set_audio("playing")
        t.set_phase("thinking")  # tool call starts while pre-tool sentence plays
        t.set_audio("silent")
        self.assertEqual(self.sent, ["thinking", "speaking", "thinking"])
        t.set_phase("replying")  # the answer streams, but isn't audible yet
        self.assertEqual(self.sent[-1], "thinking")
        t.set_audio("queued")
        t.set_audio("playing")
        self.assertEqual(self.sent, ["thinking", "speaking", "thinking", "speaking"])

    def test_rejects_unknown_audio_state(self):
        with self.assertRaises(ValueError):
            self.tracker.set_audio("loud")

    def test_force_publish_resends(self):
        self.tracker.publish(force=True)
        self.tracker.publish(force=True)
        self.tracker.publish()
        self.assertEqual(self.sent, ["idle", "idle"])

    def test_rejects_unknown_phase(self):
        with self.assertRaises(ValueError):
            self.tracker.set_phase("dancing")


    def test_message_typed_while_talking_keeps_thinking(self):
        # Regression: typing while Appy was still talking flashed "idle" when the
        # audio ended, closing the UI's "thinking" badge and opening a second one.
        t = self.tracker
        t.set_phase("replying")
        t.set_audio("playing")
        t.set_phase("idle")          # previous answer streamed, audio still playing
        t.queue_turn()               # user types the next message
        t.set_audio("silent")        # audio ends
        self.assertEqual(self.sent, ["thinking", "speaking", "thinking"])
        t.begin_turn(queued=True)    # the queued message's turn starts
        t.set_phase("replying")
        t.set_audio("playing")
        t.set_phase("idle")
        t.set_audio("silent")
        self.assertEqual(self.sent, ["thinking", "speaking", "thinking", "speaking", "idle"])

    def test_begin_turn_without_queue(self):
        self.tracker.begin_turn(queued=False)
        self.tracker.set_phase("idle")
        self.assertEqual(self.sent, ["thinking", "idle"])

    def test_late_speaking_end_does_not_hide_compacting(self):
        # The last sentence's audio can report "done" after compaction has started
        t = self.tracker
        t.set_phase("replying")
        t.set_audio("queued")
        t.set_audio("playing")
        t.set_phase("idle")
        t.set_phase("compacting")
        t.set_audio("silent")
        self.assertEqual(self.sent, ["thinking", "speaking", "compacting"])
        t.set_phase("idle")
        self.assertEqual(self.sent, ["thinking", "speaking", "compacting", "idle"])

    def test_typed_message_during_compaction_keeps_the_pill(self):
        t = self.tracker
        t.set_phase("compacting")
        t.queue_turn()
        self.assertEqual(self.sent, ["compacting"])
        t.set_phase("idle")  # compaction done: the queued message is next
        self.assertEqual(self.sent, ["compacting", "thinking"])

    def test_end_of_compaction_does_not_cancel_listening(self):
        # The user starts talking while compaction runs: the UI shows "listening"
        # (from the wake event) and must not be knocked back to idle
        t = self.tracker
        t.set_phase("compacting")
        t.heard_speech()
        t.set_phase("idle")
        self.assertEqual(self.sent, ["compacting"])
        t.begin_turn(queued=False)
        self.assertEqual(self.sent, ["compacting", "thinking"])

    def test_late_speaking_end_does_not_cancel_listening(self):
        t = self.tracker
        t.set_audio("playing")
        t.heard_speech()
        t.set_audio("silent")
        self.assertEqual(self.sent, ["speaking"])

    def test_discarded_speech_resends_the_real_state(self):
        t = self.tracker
        t.heard_speech()
        t.speech_discarded()
        self.assertEqual(self.sent, ["idle"])
        t.set_phase("thinking")
        t.set_phase("idle")
        self.assertEqual(self.sent, ["idle", "thinking", "idle"])

    def test_announce_is_followed_by_derived_state(self):
        self.tracker.announce("booting")
        self.tracker.publish()  # still idle, but must be re-sent to clear "booting"
        self.assertEqual(self.sent, ["booting", "idle"])


class ProviderWarningTests(unittest.TestCase):
    def setUp(self):
        try:
            import voice
        except ImportError as e:  # sounddevice / kokoro not installed
            self.skipTest(str(e))
        self.warn = voice.provider_warning

    def test_warns_when_cuda_silently_fell_back_to_cpu(self):
        msg = self.warn("CUDAExecutionProvider", ["CPUExecutionProvider"])
        self.assertIn("CUDAExecutionProvider", msg)
        self.assertIn("onnxruntime-gpu", msg)

    def test_quiet_when_the_provider_is_active(self):
        self.assertIsNone(self.warn("CUDAExecutionProvider", ["CUDAExecutionProvider", "CPUExecutionProvider"]))
        self.assertIsNone(self.warn(None, ["CPUExecutionProvider"]))


class SpeechActivityTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.a = SpeechActivity(self.events.append)

    def test_playing_only_when_audio_starts(self):
        a = self.a
        a.add(); a.add()
        self.assertEqual(self.events, ["queued"])
        a.playing(); a.done()
        a.playing()  # second sentence of the same burst
        self.assertTrue(a.active)
        a.done()
        a.done()  # extra done() is ignored
        self.assertEqual(self.events, ["queued", "playing", "silent"])
        self.assertFalse(a.active)

    def test_playing_with_nothing_queued_is_ignored(self):
        self.a.playing()
        self.assertEqual(self.events, [])

    def test_failed_synthesis_goes_silent_without_playing(self):
        self.a.add(); self.a.done()
        self.assertEqual(self.events, ["queued", "silent"])

    def test_each_burst_reports_playing_again(self):
        a = self.a
        a.add(); a.playing(); a.done()
        a.add(); a.playing(); a.done()
        self.assertEqual(self.events, ["queued", "playing", "silent"] * 2)


# Shaped like Open-Meteo's response (what tools._fetch_weather caches)
FAKE_OPEN_METEO = {
    "current": {"temperature_2m": 3.7, "apparent_temperature": 1.0, "weather_code": 61,
                "wind_speed_10m": 9.0, "is_day": 1},
    "hourly": {"time": ["2026-11-26T13:00"], "temperature_2m": [4.0], "weather_code": [61],
               "precipitation_probability": [40]},
    "daily": {
        "time": [f"2026-11-{d}" for d in range(26, 33)],
        "weather_code": [61, 3, 61, 0, 2, 71, 95],
        "temperature_2m_max": [4.8, 6.0, 5.0, 7.5, 8.0, 1.0, 9.0],
        "temperature_2m_min": [1.0, 2.0, -0.4, 0.5, 1.5, -3.0, 4.0],
        "precipitation_probability_max": [80, 20, 45, 0, 10, 60, 70],
    },
}


class WeatherToolTests(unittest.TestCase):
    def setUp(self):
        self._cache = dict(tools._weather_cache)
        tools._weather_cache.update(data=FAKE_OPEN_METEO, ts=time.monotonic())

    def tearDown(self):
        tools._weather_cache.clear()
        tools._weather_cache.update(self._cache)

    def test_today_has_current_conditions_but_no_other_days(self):
        data = json.loads(tools.get_weather())
        self.assertEqual(data["current"]["temp_c"], 3.7)
        self.assertEqual(data["today"]["weekday"], "Thursday")
        # Other days are left out, so asking about them means calling again (and
        # that call is what shows the weather card)
        self.assertNotIn("forecast", data)
        self.assertNotIn("6.0", json.dumps(data["today"]))

    def test_forecast_day(self):
        data = json.loads(tools.run_tool("get_weather", '{"day": 2}'))
        self.assertEqual(data["forecast"]["weekday"], "Saturday")
        self.assertEqual((data["forecast"]["high_c"], data["forecast"]["low_c"]), (5.0, -0.4))

    def test_bad_days(self):
        self.assertIn("up to 6 days", tools.run_tool("get_weather", '{"day": 7}'))
        self.assertIn("up to 6 days", tools.run_tool("get_weather", '{"day": -1}'))
        self.assertIn("must be a number", tools.run_tool("get_weather", '{"day": "soon"}'))
        self.assertIn('"day":1', tools.run_tool("get_weather", '{"day": "1"}'))


class ForcedCallTests(unittest.TestCase):
    def test_weather_words_force_the_tool(self):
        for text in ["what's the weather", "will it rain later", "hi there. what's the weather btw",
                     "do I need an umbrella", "how many degrees outside", "is it snowing?"]:
            self.assertEqual(tools.forced_call(text, frozenset()).name, "get_weather", text)

    def test_time_follow_ups_only_after_a_recent_weather_turn(self):
        recent = frozenset({"get_weather"})
        for text in ["and 3 hours from now?", "and right now again?", "wb in 2 days",
                     "tomorrow?", "how about at 5 pm today", "and saturday?"]:
            self.assertEqual(tools.forced_call(text, recent).name, "get_weather", text)
            self.assertIsNone(tools.forced_call(text, frozenset()), text)

    def test_ordinary_messages_are_left_alone(self):
        recent = frozenset({"get_weather"})
        for text in ["thanks!", "ok cool", "my coffee is cold", "tell me a joke", "yo"]:
            self.assertIsNone(tools.forced_call(text, recent), text)

    def test_fallback_day(self):
        thursday = datetime.date(2026, 11, 26)
        cases = {
            "what's the weather": 0, "and 3 hours from now?": 0, "at 5 pm today": 0,
            "tomorrow?": 1, "the day after tomorrow": 2, "wb in 2 days": 2,
            "in three days": 3, "saturday?": 2, "on thursday": 0, "next thursday": 7,
            "monday": 4,
        }
        for text, day in cases.items():
            self.assertEqual(tools.weather_day(text, thursday), day, text)


class MusicForcedCallTests(unittest.TestCase):
    def setUp(self):
        tools.set_dj(object())  # any DJ: forcing only checks that music is set up

    def tearDown(self):
        tools.set_dj(None)

    def forced(self, text, **kw):
        call = tools.forced_call(text, frozenset(), **kw)
        return (call.name, call.fallback_args) if call else None

    def test_music_commands(self):
        control = lambda action: ("music_control", {"action": action})  # noqa: E731
        play = lambda **args: ("play_music", args)  # noqa: E731
        cases = {
            "Pause.": control("pause"),
            "pause the music": control("pause"),
            "Appy, stop the music please": control("pause"),
            "resume": control("resume"),
            "unpause it": control("resume"),
            "Play.": control("resume"),
            "Skip.": control("next"),
            "skip this song": control("next"),
            "next song": control("next"),
            "next track please": control("next"),
            "previous song": control("previous"),
            "go back a song": control("previous"),
            "turn it up": control("volume_up"),
            "louder!": control("volume_up"),
            "turn it down": control("volume_down"),
            "quieter please": control("volume_down"),
            "what's playing?": control("now_playing"),
            "What song is this?": control("now_playing"),
            "what's this song": control("now_playing"),
            "play my liked songs": play(kind="liked"),
            "play some music": play(kind="liked"),
            "Play my gym playlist.": play(query="gym", kind="playlist"),
            "play the late night coding playlist": play(query="late night coding", kind="playlist"),
            "play some Radiohead": play(query="Radiohead", kind="artist"),
            "play something by Daft Punk": play(query="Daft Punk", kind="artist"),
            "queue Paranoid Android": play(query="Paranoid Android", kind="track", queue=True),
            "add Hello by Adele to the queue": play(query="Hello by Adele", kind="track", queue=True),
            "Play Karma Police.": play(query="Karma Police", kind="track"),
            "hey appy, can you play Bohemian Rhapsody": play(query="Bohemian Rhapsody", kind="track"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.forced(text), expected)

    def test_music_mentions_do_not_force(self):
        for text in ["I was listening to music earlier", "my sister plays guitar",
                     "what should I play tonight", "let's play a game", "can we play twenty questions",
                     "the next step is hard", "don't stop believing in yourself", "skip the small talk"]:
            with self.subTest(text=text):
                self.assertIsNone(self.forced(text))

    # HEARING-WORKAROUND(respeaker): delete with the workaround
    def test_strict_music_rejects_long_lyrics(self):
        lyric = "don't stop, play it again, play it all night long baby"
        self.assertIsNone(self.forced(lyric, strict_music=True))
        self.assertIsNone(self.forced("play that funky music white boy, play that funky music",
                                      strict_music=True))
        self.assertEqual(self.forced("skip", strict_music=True), ("music_control", {"action": "next"}))
        self.assertEqual(self.forced("play my gym playlist", strict_music=True),
                         ("play_music", {"query": "gym", "kind": "playlist"}))

    def test_long_requests_are_fine_when_not_strict(self):
        self.assertEqual(self.forced("play that funky music white boy, play that funky music"),
                         ("play_music", {"query": "that funky music white boy, play that funky music",
                                         "kind": "track"}))

    def test_no_dj_no_music_forcing(self):
        tools.set_dj(None)
        self.assertIsNone(self.forced("skip"))
        self.assertIsNone(self.forced("play Karma Police"))

    def test_weather_still_forced_with_dj(self):
        self.assertEqual(self.forced("what's the weather")[0], "get_weather")


class WeatherCardTests(unittest.TestCase):
    def result(self, **current):
        cur = {"temp_c": 21.4, "condition": "clear sky", "code": 0, "is_day": True, **current}
        return json.dumps({"city": "Sibiu", "current": cur})

    def test_card_for_right_now(self):
        self.assertEqual(tools.weather_card(self.result()), {
            "widget": "weather",
            "city": "Sibiu",
            "temp_c": 21.4,
            "low_c": None,
            "when": None,
            "condition": "clear sky",
            "icon": "clear",
        })

    def test_card_for_a_forecast_day(self):
        result = json.dumps({"city": "Sibiu", "day": 2, "forecast": {
            "weekday": "Saturday", "high_c": 5.0, "low_c": -0.4,
            "condition": "light rain", "code": 61}})
        card = tools.weather_card(result)
        self.assertEqual((card["temp_c"], card["low_c"], card["when"]), (5.0, -0.4, "Sat"))
        self.assertEqual(card["icon"], "rain")  # forecasts use the daytime icon

    def test_night_and_unknown_codes(self):
        self.assertEqual(tools.weather_card(self.result(is_day=False))["icon"], "clear_night")
        self.assertEqual(tools.weather_card(self.result(code=999))["icon"], "cloudy")

    def test_errors_and_garbage_give_no_card(self):
        self.assertIsNone(tools.weather_card(json.dumps({"error": "timeout"})))
        self.assertIsNone(tools.weather_card("not json"))
        self.assertIsNone(tools.weather_card(json.dumps({"current": {"temp_c": None}})))


class RunToolTests(unittest.TestCase):
    def setUp(self):
        self._orig = dict(tools.TOOL_MAP)
        tools.TOOL_MAP["echo"] = lambda **kw: json.dumps(kw)
        tools.TOOL_MAP["boom"] = lambda: 1 / 0

    def tearDown(self):
        tools.TOOL_MAP.clear()
        tools.TOOL_MAP.update(self._orig)

    def error(self, name, args):
        return json.loads(tools.run_tool(name, args))["error"]

    def test_passes_arguments(self):
        self.assertEqual(json.loads(tools.run_tool("echo", '{"a": 1}')), {"a": 1})

    def test_empty_arguments(self):
        self.assertEqual(json.loads(tools.run_tool("echo", "  ")), {})

    def test_unknown_tool(self):
        self.assertIn("Unknown tool", self.error("nope", "{}"))

    def test_bad_json(self):
        self.assertIn("Invalid JSON", self.error("echo", "{not json"))

    def test_non_object_arguments(self):
        self.assertIn("JSON object", self.error("echo", "[1, 2]"))

    def test_tool_exception(self):
        self.assertIn("ZeroDivisionError", self.error("boom", "{}"))

    def test_unexpected_argument(self):
        self.assertIn("TypeError", self.error("boom", '{"x": 1}'))


class DJToolTests(unittest.TestCase):
    def tearDown(self):
        tools.set_dj(None)

    def test_dj_tools_without_dj_return_error(self):
        tools.set_dj(None)
        for name in ("play_music", "music_control"):
            with self.subTest(name=name):
                self.assertEqual(json.loads(tools.run_tool(name, "{}")),
                                 {"error": "Music isn't set up."})

    def test_dj_tools_route_to_the_dj(self):
        calls = []

        class FakeDJ:
            def play_music(self, query="", kind="track", queue=False):
                calls.append(("play", query, kind, queue))
                return "{}"

            def music_control(self, action="", value=None):
                calls.append(("control", action, value))
                return "{}"

        tools.set_dj(FakeDJ())
        tools.run_tool("play_music", json.dumps({"query": "gym", "kind": "playlist"}))
        tools.run_tool("music_control", json.dumps({"action": "set_volume", "value": 40}))
        self.assertEqual(calls, [("play", "gym", "playlist", False), ("control", "set_volume", 40)])

    def test_music_schemas_are_registered(self):
        names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
        self.assertIn("play_music", names)
        self.assertIn("music_control", names)
        play = next(s for s in tools.TOOL_SCHEMAS if s["function"]["name"] == "play_music")
        self.assertEqual(play["function"]["parameters"]["properties"]["kind"]["enum"],
                         ["track", "artist", "album", "playlist", "liked"])
        control = next(s for s in tools.TOOL_SCHEMAS if s["function"]["name"] == "music_control")
        self.assertEqual(control["function"]["parameters"]["properties"]["action"]["enum"],
                         ["pause", "resume", "next", "previous", "volume_up", "volume_down",
                          "set_volume", "shuffle_on", "shuffle_off", "now_playing"])

    def test_music_control_results_go_stale(self):
        self.assertIn("music_control", tools.STALE_TOOLS)


class ModeTests(unittest.TestCase):
    def setUp(self):
        tools.reset_mode()

    def tearDown(self):
        tools.reset_mode()

    def test_starts_in_banter(self):
        self.assertEqual(tools.current_mode(), "banter")

    def test_set_mode_persists(self):
        json.loads(tools.run_tool("set_mode", '{"mode": "focus"}'))
        self.assertEqual(tools.current_mode(), "focus")
        self.assertEqual(tools.turn_stamp("10:32 PM"), "[10:32 PM \u00b7 FOCUS]")

    def test_case_insensitive(self):
        self.assertEqual(json.loads(tools.set_mode("FOCUS"))["mode"], "focus")

    def test_unknown_mode_is_an_error_and_keeps_mode(self):
        tools.set_mode("focus")
        self.assertIn("Unknown mode", json.loads(tools.set_mode("normal"))["error"])
        self.assertEqual(tools.current_mode(), "focus")

    def test_missing_mode_is_an_error(self):
        self.assertIn("error", json.loads(tools.run_tool("set_mode", "{}")))

    def test_already_in_mode(self):
        self.assertIn("Already", json.loads(tools.set_mode("banter"))["note"])

    def test_schema_registered_and_not_stale(self):
        names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
        self.assertIn("set_mode", names)
        self.assertNotIn("set_mode", tools.STALE_TOOLS)

    def test_pinned_expression(self):
        self.assertIsNone(tools.pinned_expression())
        tools.set_mode("focus")
        self.assertEqual(tools.pinned_expression(), "focused")

    def test_banter_stamp(self):
        self.assertEqual(tools.turn_stamp("09:00 AM"), "[09:00 AM \u00b7 BANTER]")


if __name__ == "__main__":
    unittest.main()
