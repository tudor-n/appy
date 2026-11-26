import datetime
import json
import time
import unittest

import tools
from status import ActivityCounter, StatusTracker


class StatusTrackerTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.tracker = StatusTracker(self.sent.append)

    def test_dedupes_and_combines_phase_with_audio(self):
        t = self.tracker
        t.set_phase("thinking")
        t.set_phase("thinking")
        t.set_phase("replying")
        t.set_speaking(True)
        t.set_speaking(False)  # gap between sentences: still replying
        t.set_phase("idle")
        self.assertEqual(self.sent, ["thinking", "speaking", "idle"])

    def test_audio_outlasting_the_turn(self):
        t = self.tracker
        t.set_phase("replying")
        t.set_speaking(True)
        t.set_phase("idle")  # LLM done, audio still playing
        self.assertEqual(self.sent[-1], "speaking")
        t.set_speaking(False)
        self.assertEqual(self.sent[-1], "idle")

    def test_trailing_speech_during_tool_round_falls_back_to_thinking(self):
        t = self.tracker
        t.set_phase("replying")
        t.set_speaking(True)
        t.set_phase("thinking")  # tool call starts while pre-tool sentence plays
        t.set_speaking(False)
        self.assertEqual(self.sent, ["speaking", "thinking"])

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
        t.set_speaking(True)
        t.set_phase("idle")          # previous answer streamed, audio still playing
        t.queue_turn()               # user types the next message
        t.set_speaking(False)        # audio ends
        self.assertEqual(self.sent, ["speaking", "thinking"])
        t.begin_turn(queued=True)    # the queued message's turn starts
        t.set_phase("replying")
        t.set_phase("idle")
        self.assertEqual(self.sent, ["speaking", "thinking", "speaking", "idle"])

    def test_begin_turn_without_queue(self):
        self.tracker.begin_turn(queued=False)
        self.tracker.set_phase("idle")
        self.assertEqual(self.sent, ["thinking", "idle"])

    def test_announce_is_followed_by_derived_state(self):
        self.tracker.announce("booting")
        self.tracker.publish()  # still idle, but must be re-sent to clear "booting"
        self.assertEqual(self.sent, ["booting", "idle"])


class ActivityCounterTests(unittest.TestCase):
    def test_start_and_end_fire_once_per_burst(self):
        events = []
        c = ActivityCounter(lambda: events.append("start"), lambda: events.append("end"))
        c.add(); c.add(); c.done()
        self.assertTrue(c.active)
        c.done()
        c.done()  # extra done() is ignored
        c.add(); c.done()
        self.assertEqual(events, ["start", "end", "start", "end"])
        self.assertFalse(c.active)


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


if __name__ == "__main__":
    unittest.main()
