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

# Emoji for the UI's weather card: (day, night). Not shown to the LLM, so it can't end
# up in a spoken reply.
WMO_EMOJI = {
    0: ("☀️", "🌙"),
    1: ("🌤️", "🌙"),
    2: ("⛅", "☁️"),
    3: ("☁️", "☁️"),
    45: ("🌫️", "🌫️"),
    48: ("🌫️", "🌫️"),
    51: ("🌦️", "🌧️"),
    53: ("🌦️", "🌧️"),
    55: ("🌧️", "🌧️"),
    61: ("🌦️", "🌧️"),
    63: ("🌧️", "🌧️"),
    65: ("🌧️", "🌧️"),
    71: ("🌨️", "🌨️"),
    73: ("🌨️", "🌨️"),
    75: ("❄️", "❄️"),
    80: ("🌦️", "🌧️"),
    81: ("🌧️", "🌧️"),
    82: ("⛈️", "⛈️"),
    95: ("⛈️", "⛈️"),
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current local weather, 12-hour hourly forecast, and 3-day daily forecast. Only call this when the user asks about weather, temperature, rain, or forecast.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }
]

_weather_cache = {"data": None, "ts": 0.0}
CACHE_TTL = 900  # 15 minutes
_http = httpx.Client(timeout=5.0)


def _fetch_weather() -> str:
    city = os.getenv("WEATHER_CITY", "Sibiu")
    lat = os.getenv("WEATHER_LAT", "45.7983")
    lon = os.getenv("WEATHER_LON", "24.1256")

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day"
        f"&hourly=temperature_2m,weather_code,precipitation_probability"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&forecast_hours=12&forecast_days=3&timezone=auto"
    )
    try:
        r = _http.get(url)
        r.raise_for_status()
        data = r.json()
        cur = data["current"]
        hourly = data["hourly"]
        daily = data["daily"]

        result = json.dumps({
            "city": city,
            "current": {
                "temp_c": cur["temperature_2m"],
                "feels_like_c": cur["apparent_temperature"],
                "condition": WMO_CODES.get(cur["weather_code"], f"code {cur['weather_code']}"),
                "wind_kmh": cur["wind_speed_10m"],
                "is_day": bool(cur["is_day"]),
                "code": cur["weather_code"],
            },
            "next_12_hours": [
                {
                    "time": hourly["time"][i].split("T")[1],
                    "temp_c": hourly["temperature_2m"][i],
                    "condition": WMO_CODES.get(hourly["weather_code"][i], f"code {hourly['weather_code'][i]}"),
                    "rain_chance_pct": hourly["precipitation_probability"][i],
                }
                for i in range(len(hourly["time"]))
            ],
            "forecast": [
                {
                    "date": daily["time"][i],
                    "condition": WMO_CODES.get(daily["weather_code"][i], f"code {daily['weather_code'][i]}"),
                    "high_c": daily["temperature_2m_max"][i],
                    "low_c": daily["temperature_2m_min"][i],
                    "rain_chance_pct": daily["precipitation_probability_max"][i],
                }
                for i in range(len(daily["time"]))
            ],
        }, separators=(",", ":"))

        _weather_cache["data"] = result
        _weather_cache["ts"] = time.monotonic()
        return result
    except Exception as e:
        return json.dumps({"error": str(e)})


def get_weather() -> str:
    if _weather_cache["data"] and (time.monotonic() - _weather_cache["ts"] < CACHE_TTL):
        return _weather_cache["data"]
    return _fetch_weather()


TOOL_MAP = {
    "get_weather": get_weather,
}


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
    """The UI card for a get_weather result, or None if it's an error or malformed."""
    try:
        data = json.loads(result)
        cur = data["current"]
        temp = float(cur["temp_c"])
    except (ValueError, TypeError, KeyError):
        return None
    day, night = WMO_EMOJI.get(cur.get("code"), ("🌡️", "🌡️"))
    return {
        "widget": "weather",
        "city": str(data.get("city", "")),
        "temp_c": temp,
        "condition": str(cur.get("condition", "")),
        "emoji": day if cur.get("is_day", True) else night,
    }


def prefetch():
    """Warm the weather cache in the background so the first ask is instant."""
    threading.Thread(target=_fetch_weather, daemon=True).start()
