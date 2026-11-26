import datetime
import json
import os
import threading
import time
import httpx

WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "rain showers",
    81: "moderate rain showers",
    82: "heavy rain showers",
    95: "thunderstorm",
}

# Icon names for the UI's weather card, (day, night); the UI draws them in its own
# monochrome style. Not shown to the LLM.
WMO_ICON = {
    0: ("clear", "clear_night"),
    1: ("partly_cloudy", "partly_cloudy_night"),
    2: ("partly_cloudy", "partly_cloudy_night"),
    3: ("cloudy", "cloudy"),
    45: ("fog", "fog"),
    48: ("fog", "fog"),
    51: ("drizzle", "drizzle"),
    53: ("drizzle", "drizzle"),
    55: ("drizzle", "drizzle"),
    61: ("rain", "rain"),
    63: ("rain", "rain"),
    65: ("rain", "rain"),
    71: ("snow", "snow"),
    73: ("snow", "snow"),
    75: ("snow", "snow"),
    80: ("rain", "rain"),
    81: ("rain", "rain"),
    82: ("rain", "rain"),
    95: ("thunder", "thunder"),
}

MAX_FORECAST_DAY = 6

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Get the local weather for one day: right now (day 0, the default) or a "
                "forecast up to 6 days ahead. Each call also shows the user a weather "
                "card, so call it every time they ask about the weather, temperature, "
                "rain or a forecast, even if you looked something up earlier."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_FORECAST_DAY,
                        "description": "0 = now/today, 1 = tomorrow, 2 = the day after, ... up to 6.",
                    },
                },
                "required": [],
            },
        },
    }
]

_weather_cache = {"data": None, "ts": 0.0}
CACHE_TTL = 900  # 15 minutes
_http = httpx.Client(timeout=5.0)


def _condition(code) -> str:
    return WMO_CODES.get(code, f"code {code}")


def _fetch_weather() -> dict:
    """Fetch (and cache) current conditions, 12 hours and 7 days from Open-Meteo."""
    lat = os.getenv("WEATHER_LAT", "45.7983")
    lon = os.getenv("WEATHER_LON", "24.1256")
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day"
        f"&hourly=temperature_2m,weather_code,precipitation_probability"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&forecast_hours=12&forecast_days={MAX_FORECAST_DAY + 1}&timezone=auto"
    )
    r = _http.get(url)
    r.raise_for_status()
    data = r.json()
    _weather_cache["data"] = data
    _weather_cache["ts"] = time.monotonic()
    return data


def _weather_data() -> dict:
    if _weather_cache["data"] and (time.monotonic() - _weather_cache["ts"] < CACHE_TTL):
        return _weather_cache["data"]
    return _fetch_weather()


def _day(daily: dict, i: int) -> dict:
    return {
        "date": daily["time"][i],
        "weekday": datetime.date.fromisoformat(daily["time"][i]).strftime("%A"),
        "condition": _condition(daily["weather_code"][i]),
        "code": daily["weather_code"][i],
        "high_c": daily["temperature_2m_max"][i],
        "low_c": daily["temperature_2m_min"][i],
        "rain_chance_pct": daily["precipitation_probability_max"][i],
    }


def get_weather(day: int = 0) -> str:
    """Only the requested day, so asking about another day means calling again (which
    is what brings up the UI's weather card)."""
    try:
        day = int(day)
    except (TypeError, ValueError):
        return json.dumps({"error": f"day must be a number from 0 to {MAX_FORECAST_DAY}"})
    if not 0 <= day <= MAX_FORECAST_DAY:
        return json.dumps({"error": f"I only have forecasts up to {MAX_FORECAST_DAY} days ahead"})
    try:
        data = _weather_data()
    except Exception as e:
        return json.dumps({"error": str(e)})

    city = os.getenv("WEATHER_CITY", "Sibiu")
    daily = data["daily"]
    if day > 0:
        return json.dumps({"city": city, "day": day, "forecast": _day(daily, day)},
                          separators=(",", ":"))

    cur, hourly = data["current"], data["hourly"]
    return json.dumps({
        "city": city,
        "day": 0,
        "current": {
            "temp_c": cur["temperature_2m"],
            "feels_like_c": cur["apparent_temperature"],
            "condition": _condition(cur["weather_code"]),
            "wind_kmh": cur["wind_speed_10m"],
            "is_day": bool(cur["is_day"]),
            "code": cur["weather_code"],
        },
        "today": _day(daily, 0),
        "next_12_hours": [
            {
                "time": hourly["time"][i].split("T")[1],
                "temp_c": hourly["temperature_2m"][i],
                "condition": _condition(hourly["weather_code"][i]),
                "rain_chance_pct": hourly["precipitation_probability"][i],
            }
            for i in range(len(hourly["time"]))
        ],
    }, separators=(",", ":"))


TOOL_MAP = {
    "get_weather": get_weather,
}

# Tools whose results go stale: the agent drops them from the history after each turn,
# so asking again calls the tool again (and shows its card) instead of reusing old data.
STALE_TOOLS = frozenset({"get_weather"})


def run_tool(name: str, arguments: str) -> str:
    """Run a tool by name. Errors come back as JSON for the model, never as exceptions."""
    fn = TOOL_MAP.get(name)
    if not fn:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        args = json.loads(arguments) if arguments and arguments.strip() else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON arguments for {name}: {e}"})
    if not isinstance(args, dict):
        return json.dumps({"error": f"Arguments for {name} must be a JSON object"})
    try:
        return fn(**args)
    except Exception as e:
        return json.dumps({"error": f"{name} failed: {type(e).__name__}: {e}"})


def weather_card(result: str) -> dict | None:
    """The UI card for a get_weather result, or None if it's an error or malformed.

    Now: the current temperature. A forecast day: its high and low, plus the weekday.
    """
    try:
        data = json.loads(result)
        if "forecast" in data:
            f = data["forecast"]
            temp, low = float(f["high_c"]), float(f["low_c"])
            code, is_day, when = f.get("code"), True, str(f["weekday"])[:3]
            condition = f.get("condition", "")
        else:
            cur = data["current"]
            temp, low, when = float(cur["temp_c"]), None, None
            code, is_day = cur.get("code"), cur.get("is_day", True)
            condition = cur.get("condition", "")
    except (ValueError, TypeError, KeyError):
        return None
    day_icon, night_icon = WMO_ICON.get(code, ("cloudy", "cloudy"))
    return {
        "widget": "weather",
        "city": str(data.get("city", "")),
        "temp_c": temp,
        "low_c": low,
        "when": when,
        "condition": str(condition),
        "icon": day_icon if is_day else night_icon,
    }


def prefetch():
    """Warm the weather cache in the background so the first ask is instant."""

    def warm():
        try:
            _fetch_weather()
        except Exception:
            pass  # offline at startup: the first real call will try again

    threading.Thread(target=warm, daemon=True).start()
