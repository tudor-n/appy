import datetime
import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable
import time
import httpx

from dj import CONTROL_ACTIONS, PLAY_KINDS

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
                "Get the local weather for one day. Day 0 (the default) has the current "
                "conditions plus an hour-by-hour forecast for the next 12 hours, so use it "
                "for questions like 'in 3 hours' or 'at 6 pm'. Days 1-6 are daily "
                "forecasts. Each call also shows the user a weather card, so call it every "
                "time they ask about the weather, temperature, rain or a forecast, even if "
                "you looked something up earlier."
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
    },
    {
        "type": "function",
        "function": {
            "name": "set_mode",
            "description": (
                "Switch how you work with Tudor. 'focus': terse, no jokes, one short "
                "sentence. 'banter': your normal self. The mode stays until you switch it "
                "again. Call it only when Tudor asks to change how you talk or work with "
                "him (e.g. 'lock in', 'focus up', 'ok we're done', 'relax'), never just "
                "because a question is serious."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["banter", "focus"]},
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": (
                "Play or queue music in Riff (Spotify). kind: track (default), artist, "
                "album, playlist (Tudor's own playlists first), liked (his liked songs; no "
                "query needed). queue=true adds one song to the queue instead of playing it "
                "now. Tell Tudor what actually started, from the result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for, e.g. 'Karma Police' or 'gym'."},
                    "kind": {"type": "string", "enum": list(PLAY_KINDS)},
                    "queue": {"type": "boolean"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_control",
            "description": (
                "Pause, resume, skip, go back, change the volume, shuffle, or check what's "
                "playing in Riff. set_volume takes value 0-100."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(CONTROL_ACTIONS)},
                    "value": {"type": "number", "minimum": 0, "maximum": 100},
                },
                "required": ["action"],
            },
        },
    },
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


# Long-term memory (memory_client.MemoryClient on the Mac's memory service). Only sent
# to the model when MEMORY_URL is set; every schema costs context on every request.
MEMORY_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": (
                "Read one long-term memory note. slug must be one listed under "
                "[Notes you can read]."
            ),
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_remember",
            "description": (
                "Save a fact to long-term memory. Only call this when the user explicitly "
                "asks you to remember something. Leave out slug for facts about the user "
                "themselves (core memory); give a slug to file it under a note (a new slug "
                "creates the note)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "One short, self-contained sentence, e.g. "
                            "'Tudor's lab printer is a Prusa MK4.'"
                        ),
                    },
                    "slug": {
                        "type": "string",
                        "description": "Note slug: lowercase letters, digits and dashes.",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_forget",
            "description": (
                "Remove a fact from long-term memory. Call this whenever the user asks you to "
                "forget something; nothing is forgotten unless you call it, so never say you "
                "forgot without calling it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "'core' or a note slug"},
                    "text": {
                        "type": "string",
                        "description": "The fact to remove, as it appears in memory.",
                    },
                },
                "required": ["slug", "text"],
            },
        },
    },
]

_memory = None  # a memory_client.MemoryClient, set by main() when memory is on
_NO_MEMORY = json.dumps({"error": "long-term memory isn't set up"})


def set_memory(client) -> None:
    global _memory
    _memory = client


def memory_read(slug: str) -> str:
    return _memory.read(slug) if _memory else _NO_MEMORY


def memory_remember(text: str, slug: str | None = None) -> str:
    return _memory.remember(text, slug) if _memory else _NO_MEMORY


def memory_forget(slug: str, text: str) -> str:
    return _memory.forget(slug, text) if _memory else _NO_MEMORY


_dj = None  # a dj.DJ, set by main()
_NO_DJ = json.dumps({"error": "Music isn't set up."})


def set_dj(dj) -> None:
    global _dj
    _dj = dj


def play_music(query: str = "", kind: str = "track", queue: bool = False) -> str:
    return _dj.play_music(query=query, kind=kind, queue=queue) if _dj else _NO_DJ


def music_control(action: str = "", value: float | None = None) -> str:
    return _dj.music_control(action=action, value=value) if _dj else _NO_DJ


# How Appy talks: "banter" (default) or "focus". Kept here rather than in the system
# prompt, which must stay byte-for-byte the same all run (KV cache); every user message
# carries the mode in its stamp instead (see turn_stamp).
VALID_MODES = ("banter", "focus")
_mode = {"value": "banter"}


def current_mode() -> str:
    return _mode["value"]


def reset_mode() -> None:
    _mode["value"] = "banter"


def set_mode(mode: str) -> str:
    mode = str(mode).strip().lower()
    if mode not in VALID_MODES:
        return json.dumps({"error": f"Unknown mode: {mode}. Use banter or focus."})
    if mode == _mode["value"]:
        return json.dumps({"mode": mode, "note": f"Already in {mode.upper()} mode."})
    _mode["value"] = mode
    print(f"[mode -> {mode}] ", end="", flush=True)
    return json.dumps({"mode": mode, "note": f"Mode is now {mode.upper()}. Reply in {mode} mode."})


def pinned_expression() -> str | None:
    """The face every reply gets in the current mode, or None to let the model pick.
    The focused face is how Tudor can tell focus mode is on."""
    return "focused" if _mode["value"] == "focus" else None


def turn_stamp(now: str) -> str:
    """The tag that starts every user message: `[10:32 PM · FOCUS]`."""
    return f"[{now} \u00b7 {current_mode().upper()}]"


TOOL_MAP = {
    "get_weather": get_weather,
    "memory_read": memory_read,
    "memory_remember": memory_remember,
    "memory_forget": memory_forget,
    "set_mode": set_mode,
    "play_music": play_music,
    "music_control": music_control,
}

# Words that make a message unmistakably about the weather. Deliberately excludes
# ambiguous ones like "cold"/"hot"/"warm" ("my coffee's cold").
_WEATHER_WORDS = re.compile(
    r"\b(weather|forecast|rain\w*|snow\w*|sunny|sunshine|cloud(s|y)?|overcast|"
    r"temperature|degrees|umbrella|storm\w*|fog\w*|humid\w*|drizzl\w*|thunder\w*|"
    r"wind|windy|hail)\b",
    re.IGNORECASE,
)
# Time phrases that, shortly after a weather answer, mean "and the weather then?"
_TIME_FOLLOW_UP = re.compile(
    r"\b(now|again|later|tonight|today|tomorrow|morning|afternoon|evening|night|noon|"
    r"weekend|week|then|hourly|hours?|days?|in \d+|at \d+|\d+\s*(h|hrs?|mins?|minutes?)|"
    r"(mon|tues|wednes|thurs|fri|satur|sun)day)\b",
    re.IGNORECASE,
)


# A clear request to forget something. Gemma answers these with "I've wiped that" and no
# tool call (live check: 0/9 runs; forced: 9/9), so they force memory_forget. A forced
# turn has to delete *something*, so this only matches a request addressed to Appy at the
# start of the message ("forget that ...", "Appy, can you forget my ..."), never a
# sentence that merely mentions forgetting ("I always forget my keys", "I don't remember
# the name"), and not "forget it" / "forget about it" / a bare "forget that". The fallback
# args are only used if the model still skips the call.
_FORGET = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|so|and|also|hey|well)[,.]?\s+)*(?:appy[,.:]?\s+)?"
    r"(?:please\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"i\s+(?:want|need)\s+you\s+to\s+)?"
    r"(?:forget|stop\s+remembering)\s+(?:that\s+|about\s+)?"
    r"(?P<what>(?!(?:about\s+)?(?:it|this|that)\b)\w.*)",
    re.IGNORECASE,
)
_FORGET_TRAILER = re.compile(r"(\s+(anymore|any more|please))?[\s.!?]*$", re.IGNORECASE)

# Clear music commands, addressed to Appy at the start of the message ("skip", "Appy,
# play my gym playlist", "can you turn it up"). Forced like weather, because Gemma tends
# to answer "skipped!" without calling anything. Matched against the whole message (after
# the same filler/"appy"/"can you" prefix as _FORGET), so a sentence that merely mentions
# music ("I was listening to music") or a game ("let's play chess") isn't forced. Controls
# are only forced while Riff is open, and the words that also mean something else in a
# conversation ("stop", "next", "go back", "what is this") only while music is playing.
_MUSIC_PREFIX = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|so|and|also|hey|well|yo)[,.!]?\s+)*(?:appy[,.:!]?\s+)?"
    r"(?:please\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?)?",
    re.IGNORECASE,
)
_PUNCT_TRAILER = re.compile(r"[\s.!?]*$")
# After a control, "please" may just follow ("skip please"); otherwise only a comma'd
# "please"/"thanks"/"appy" is dropped, so titles like "Happy", "Thanks" or "Please Please
# Please" keep their words
_POLITE_TRAILER = re.compile(r"\s+please$", re.IGNORECASE)
_COMMA_TRAILER = re.compile(r",\s*(?:please|thanks|appy)$", re.IGNORECASE)
_SONG = r"(?:the\s+)?(?:song|track|one|music|tune)"
# (pattern, action, when): "running" = Riff open, "playing" = music playing right now
_MUSIC_CONTROLS = [
    (re.compile(rf"pause(?:\s+(?:it|this|that|{_SONG}|the\s+music))?"), "pause", "running"),
    (re.compile(rf"stop\s+(?:{_SONG}|the\s+music)"), "pause", "running"),
    (re.compile(r"stop(?:\s+(?:it|this|that))?"), "pause", "playing"),
    (re.compile(rf"(?:resume|unpause)(?:\s+(?:it|{_SONG}|playing))?"), "resume", "running"),
    (re.compile(r"play|play\s+(?:it|the\s+music)\s+again|keep\s+playing"), "resume", "running"),
    (re.compile(rf"(?:skip|next)\s+(?:{_SONG}|this\s+(?:song|track|one))|skip(?:\s+(?:it|this|that))?|"
                rf"play\s+the\s+next\s+(?:song|track|one)"), "next", "running"),
    (re.compile(r"next"), "next", "playing"),
    (re.compile(r"previous(?:\s+(?:song|track|one))?|go\s+back\s+(?:a|one)(?:\s+(?:song|track))?|"
                r"last\s+(?:song|track)|play\s+the\s+(?:previous|last)\s+(?:song|track|one)"),
     "previous", "running"),
    (re.compile(r"go\s+back"), "previous", "playing"),
    (re.compile(r"turn\s+(?:it|this|the\s+music|the\s+volume)\s+up|turn\s+up\s+the\s+(?:music|volume)|"
                r"louder|volume\s+up"), "volume_up", "running"),
    (re.compile(r"turn\s+(?:it|this|the\s+music|the\s+volume)\s+down|turn\s+down\s+the\s+(?:music|volume)|"
                r"quieter|softer|volume\s+down"), "volume_down", "running"),
    (re.compile(r"what(?:'s|\s+is)\s+(?:this\s+song|playing|the\s+song(?:\s+playing)?)|"
                r"what\s+song\s+is\s+(?:this|playing)|who(?:'s|\s+is)\s+singing"), "now_playing", "running"),
    (re.compile(r"what(?:'s|\s+is)\s+this|who(?:'s|\s+is)\s+this"), "now_playing", "playing"),
]
_PLAY_LIKED = re.compile(r"play\s+(?:my\s+)?(?:liked\s+songs|likes|favou?rites)|"
                         r"play\s+(?:some\s+)?music|play\s+something|play\s+me\s+(?:a\s+song|something)")
_PLAY_PLAYLIST = re.compile(r"play\s+(?:my|the)\s+(?P<x>.+?)\s+playlist")
_PLAY_ARTIST = re.compile(r"play\s+(?:some|something\s+by|some\s+music\s+by|songs\s+by)\s+(?P<x>.+)")
_QUEUE = re.compile(r"(?:queue(?:\s+up)?\s+(?P<x>.+?)(?:\s+next)?|add\s+(?P<y>.+?)\s+to\s+(?:the|my)\s+queue)")
_PLAY_TRACK = re.compile(r"play\s+(?P<x>.+)")
# "play" that's about a game or a part, not a song
_NOT_MUSIC = re.compile(
    r"\bgames?\b|\bwith\s+me\b|\bwith\s+you\b|\brole\b|\bpretend\b|devil'?s\s+advocate|\bchess\b|"
    r"\bcheckers\b|\bhangman\b|rock\s*,?\s*paper\s*,?\s*scissors|\btrivia\b|\b(?:twenty|20)\s+questions\b|"
    r"\briddles?\b|\bquiz\b|\bi\s+spy\b|\btic\s*-?\s*tac\s*-?\s*toe\b|\bcards\b|\bdumb\b|"
    r"^play\s+(?:a\s+|an\s+)?(?:round|around|along|it\s+cool|it\s+safe|nice|fair)\b"
)
# HEARING-WORKAROUND(respeaker): while the mic hears music, only short commands count
STRICT_MUSIC_MAX_WORDS = 6


def music_call(user_text: str, strict: bool = False,
               riff_state: Callable[[], str] = lambda: "playing") -> "ForcedCall | None":
    """The music tool a clear command must call, or None. `riff_state()` ("closed",
    "paused", "playing") is only asked when a control matches. `strict` (the mic hears
    the music) also requires a short message, so a sung lyric doesn't skip the song."""
    text = user_text.replace("’", "'").strip()
    if strict and len(text.split()) > STRICT_MUSIC_MAX_WORDS:  # HEARING-WORKAROUND(respeaker)
        return None
    rest = _PUNCT_TRAILER.sub("", _MUSIC_PREFIX.sub("", text, count=1))
    command = _PUNCT_TRAILER.sub("", _COMMA_TRAILER.sub("", rest))  # "play X" requests
    control = _POLITE_TRAILER.sub("", command).lower()  # "skip please"
    low = command.lower()
    state = None
    for pattern, action, when in _MUSIC_CONTROLS:
        if pattern.fullmatch(control):
            state = state or riff_state()
            if state == "playing" or (when == "running" and state != "closed"):
                return ForcedCall("music_control", {"action": action})
            return None
    if _NOT_MUSIC.search(low):
        return None
    if _PLAY_LIKED.fullmatch(low):
        return ForcedCall("play_music", {"kind": "liked"})
    for pattern, kind in ((_PLAY_PLAYLIST, "playlist"), (_PLAY_ARTIST, "artist")):
        if m := pattern.fullmatch(low):
            return ForcedCall("play_music", {"query": command[m.start("x"):m.end("x")], "kind": kind})
    if m := _QUEUE.fullmatch(low):
        group = "x" if m.group("x") else "y"
        query = command[m.start(group):m.end(group)]
        return ForcedCall("play_music", {"query": query, "kind": "track", "queue": True})
    if m := _PLAY_TRACK.fullmatch(low):
        return ForcedCall("play_music", {"query": command[m.start("x"):m.end("x")], "kind": "track"})
    return None


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a": 1}


def weather_day(user_text: str, today: datetime.date | None = None) -> int:
    """Best-effort `day` for get_weather from the user's words; 0 (today) if unsure."""
    text = user_text.lower()
    today = today or datetime.date.today()
    if "day after tomorrow" in text:
        return 2
    if "tomorrow" in text:
        return 1
    if m := re.search(r"\bin (\d+|one|two|three|four|five|six|a) days?\b", text):
        n = m.group(1)
        return int(n) if n.isdigit() else _NUMBER_WORDS[n]
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", text):
            offset = (i - today.weekday()) % 7
            return offset + 7 if offset == 0 and "next" in text else offset
    return 0


@dataclass(frozen=True)
class ForcedCall:
    """A tool the next turn must call. The model still gets to pick the arguments;
    `fallback_args` are used only if it skips the call anyway."""

    name: str
    fallback_args: dict


def forced_call(user_text: str, recent_tools: frozenset[str],
                strict_music: bool = False) -> ForcedCall | None:
    """The call the next turn must make: memory_forget for a clear request to forget
    something (only while memory is on), a music tool for a clear music command (only
    while the DJ is set up; `strict_music`: see music_call), get_weather for a clear
    weather question.

    Gemma often answers weather follow-ups from its earlier reply instead of calling the
    tool (no card, and for "in 3 hours" made-up or refused answers), whatever the
    prompt says: measured 0-1/4 calls. LM Studio's tool_choice="required" is only a
    hint too (ignored in ~40% of forced turns), so the agent falls back to making the
    call itself. `recent_tools` is Agent.recent_tools (the last few turns), so a
    "thanks!" in between doesn't break the follow-up.
    """
    if _memory is not None and (forget := _FORGET.match(user_text.replace("\u2019", "'"))):
        what = _FORGET_TRAILER.sub("", forget.group("what")).strip()
        return ForcedCall("memory_forget", {"slug": "core", "text": what})
    if _dj is not None and (music := music_call(user_text, strict=strict_music,
                                                riff_state=_dj.riff_state)):
        return music
    weather = bool(_WEATHER_WORDS.search(user_text)) or (
        "get_weather" in recent_tools and bool(_TIME_FOLLOW_UP.search(user_text))
    )
    if not weather:
        return None
    return ForcedCall("get_weather", {"day": weather_day(user_text)})


# Tools whose results go stale: the agent drops them from the history after each turn,
# so asking again calls the tool again (and shows its card) instead of reusing old data.
# "what's playing" answers go stale as fast as the weather
STALE_TOOLS = frozenset({"get_weather", "music_control"})


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
