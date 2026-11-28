"""Standalone mock backend for UI work: no audio stack or LLM needed.

Serves the same WebSocket on :8001 as main.py. Replies come from APPY_LLM_URL (any
OpenAI-compatible chat endpoint) if set, otherwise from canned keyword responses.
Run: .venv/bin/python dev/mock_backend.py [--memory] [--busy]
`--memory` also sends three sample memory proposals and the auto-approve setting, and
answers the review panel's buttons and the pill's auto-approve toggle.
`--busy` holds the "thinking" status, which keeps the chat (and the memory panel) open.
`--demo` replays a short made-up conversation on connect (bubbles, status pills, a
weather card, a now-playing card, a memory pill and a part-filled compact ring), for
looking at the UI. MOCK_MUSIC=paused shows the now-playing card paused.
MOCK_PORT picks the port (default 8001); pair it with the overlay's APPY_WS_URL.
"""
import asyncio
import json
import os
import sys
import urllib.request
from typing import List, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()

EXPRESSIONS = [
    "neutral",
    "happy",
    "excited",
    "curious",
    "surprised",
    "focused",
    "skeptical",
]

SYSTEM_PROMPT = f"""You are Appy, a minimalist, witty desktop companion living on the user's screen.
Keep replies concise, conversational, and natural (1-3 short sentences unless asked for detail).

You MUST respond with valid JSON ONLY in this exact format:
{{"text": "your reply here", "expression": "one_of_the_choices"}}

Choose the single best expression for your reply from:
- "neutral": calm, matter-of-fact, everyday answers
- "happy": warm, friendly, helpful, reassuring
- "excited": enthusiastic, celebratory, great news
- "curious": asking a question, wondering, exploring an idea
- "surprised": unexpected info, sudden realization, playful shock
- "focused": technical, analytical, debugging, serious work
- "skeptical": teasing doubt, questioning a claim, dry humor
"""

LLM_URL = os.getenv("APPY_LLM_URL", "")  # e.g. http://localhost:11434/v1/chat/completions
LLM_MODEL = os.getenv("APPY_LLM_MODEL", "llama3.2")
LLM_API_KEY = os.getenv("APPY_LLM_API_KEY", "")


def call_llm_sync(history: List[Dict[str, str]]) -> Dict[str, str]:
    """Calls an OpenAI-compatible endpoint (Ollama, vLLM, OpenAI) if configured."""
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history],
        "response_format": {"type": "json_object"},
        "temperature": 0.7,
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    content = raw["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    expr = parsed.get("expression", "neutral")
    if expr not in EXPRESSIONS:
        expr = "neutral"
    return {"text": str(parsed.get("text", "")).strip(), "expression": expr}


def fallback_reply(user_text: str) -> Dict[str, str]:
    """Built-in local responder when no external LLM URL is configured."""
    low = user_text.lower()
    if any(w in low for w in ["awesome", "woohoo", "yay", "excited", "win", "finally"]):
        return {"text": "That's huge! Ready whenever you want to keep rolling.", "expression": "excited"}
    if any(w in low for w in ["what", "whoa", "really", "wait", "no way", "!"]):
        return {"text": "Wait, seriously? Tell me more about that.", "expression": "surprised"}
    if "?" in low or any(w in low for w in ["why", "how", "wonder"]):
        return {"text": "Good question — want me to dig into it with you?", "expression": "curious"}
    if any(w in low for w in ["bug", "code", "rust", "fix", "error", "compile"]):
        return {"text": "Locked in. Drop the error or file and we'll trace it.", "expression": "focused"}
    if any(w in low for w in ["sure", "trust me", "maybe", "doubt", "hmm"]):
        return {"text": "Hmm... I'm gonna need to see that to believe it.", "expression": "skeptical"}
    if any(w in low for w in ["hi", "hello", "hey", "thanks", "nice", "good"]):
        return {"text": "Hey! Good to see you — what are we working on?", "expression": "happy"}
    return {"text": f"Got it: \"{user_text}\". Backend is live and listening.", "expression": "neutral"}


MEMORY_DEMO = "--memory" in sys.argv
SAMPLE_PROPOSALS = [
    {"slug": "mark-i-suit", "title": "Mark I suit", "summary": "Added the helmet HUD hardware.",
     "before": "# Mark I suit\n> Iron Man style arm exoskeleton\n- BLDC motors with 50:1 harmonic drives.\n"
               "- An STM32 runs the joint loops at 1 kHz.\n",
     "after": "# Mark I suit\n> Iron Man style arm exoskeleton\n- BLDC motors with 50:1 harmonic drives.\n"
              "- An STM32 runs the joint loops at 1 kHz.\n- The helmet HUD runs on a Raspberry Pi Zero 2 W.\n"},
    {"slug": "bench", "title": "Bench", "summary": "Replaced the Riden PSU with a Korad.",
     "before": "# Bench\n> Lab bench gear\n- The PSU is a Riden RD6024.\n- The scope is a Rigol DS1054Z.\n",
     "after": "# Bench\n> Lab bench gear\n- The PSU is a Korad KA3005P.\n- The scope is a Rigol DS1054Z.\n"},
    {"slug": "watering-bot", "title": "Watering bot",
     "summary": "New note: Tudor's plant watering robot.",
     "before": "",
     "after": "# Watering bot\n> Tudor's automatic plant watering robot\n"
              "- An ESP32 runs the watering bot.\n"
              "- Capacitive soil moisture sensors read each pot.\n"
              "- A 12 V peristaltic pump waters a pot when its moisture drops below 30%.\n"
              "- Readings are logged over MQTT to the Mac mini.\n"
              "- The pump is driven through a logic-level MOSFET with a flyback diode.\n"
              "- Each pot has its own solenoid valve on a shared manifold.\n"
              "- The tank holds 5 litres and has a float switch for low water.\n"
              "- Firmware updates go over the air.\n"
              "- A small OLED shows the last watering time per pot.\n"},
]
AUTO_APPROVE = [False]


def facts(item: dict) -> dict:
    """What a change added and removed, as the real backend sends it."""
    old = [l[2:] for l in item["before"].splitlines() if l.startswith("- ")]
    new = [l[2:] for l in item["after"].splitlines() if l.startswith("- ")]
    return {"facts": [l for l in new if l not in old], "dropped": [l for l in old if l not in new]}


async def memory_demo(ws: WebSocket, data: dict, items: list) -> None:
    """Answer the review card like the real backend would (without a memory service)."""
    if data.get("type") == "memory_auto_approve":
        AUTO_APPROVE[0] = bool(data.get("on"))
        await ws.send_json({"type": "memory_settings", "auto_approve": AUTO_APPROVE[0]})
        if AUTO_APPROVE[0]:  # like the Mac: what's waiting is approved too
            for item in list(items):
                items.remove(item)
                await ws.send_json({"type": "memory_committed", "kind": "auto",
                                    "summary": item["summary"], **facts(item)})
            await ws.send_json({"type": "memory_proposals", "items": items})
        return
    if data.get("type") == "memory_undo":
        await ws.send_json({"type": "memory_committed", "kind": "undo", "summary": "memory: the last change"})
        return
    slug, decision = data.get("slug"), data.get("decision")
    item = next((i for i in items if i["slug"] == slug), None)
    if not item:
        return
    if decision == "edit" and not str(data.get("text", "")).startswith("# "):
        item["error"] = "edit rejected: lost the title line"
    else:
        items.remove(item)
        if decision in ("accept", "edit"):
            kind = "approved" if decision == "accept" else "edited"
            await ws.send_json({"type": "memory_committed", "kind": kind,
                                "summary": item["summary"], **facts(item)})
    await ws.send_json({"type": "memory_proposals", "items": items})


# The compact ring: a made-up context size that grows with the history, against the
# real backend's default auto-compact point
MOCK_COMPACT_AT = 9500


def context_event(history: List[Dict[str, str]]) -> dict:
    tokens = 3200 + sum(450 + len(m["content"]) for m in history)
    return {"type": "context", "tokens": tokens, "limit": MOCK_COMPACT_AT}


# --demo: (user text, events Appy answers with, reply text, expression)
DEMO_TURNS = [
    ("hey appy, what's the weather like in Sibiu tomorrow?",
     [{"type": "widget", "widget": "weather", "city": "Sibiu", "temp_c": 9.0, "low_c": 2.0,
       "when": "Sat", "condition": "light rain", "icon": "rain"}],
     "Rainy and cold tomorrow: 9 degrees at best, 2 overnight. Bring the good jacket.",
     "focused"),
    ("play Karma Police",
     [{"type": "widget", "widget": "music", "title": os.getenv("MOCK_TITLE", "Karma Police"), "artist": "Radiohead",
       "state": os.getenv("MOCK_MUSIC", "playing"), "context": os.getenv("MOCK_CONTEXT") or None}],
     "Karma Police, coming up.", "happy"),
    ("ugh. ok, remind me what PSU I have on the bench?", [],
     "A Riden RD6024: 60 volts, 24 amps, the one you control over USB.", "happy"),
    ("I swapped it for a Korad KA3005P last week",
     [{"type": "memory_committed", "kind": "remember",
       "summary": "- The bench PSU is a Korad KA3005P.",
       "facts": ["The bench PSU is a Korad KA3005P."],
       "dropped": ["The PSU is a Riden RD6024."]}],
     "Nice upgrade! Noted: the bench PSU is now the Korad KA3005P.", "excited"),
    ("also, do you think the watering bot pump can run straight off the ESP32 if I use a "
     "small MOSFET, or should I just put a proper motor driver in there and stop worrying?", [],
     "A logic-level MOSFET with a flyback diode is plenty for a 12 V peristaltic pump, "
     "and you already have that in the design. A motor driver only earns its place if you "
     "want to reverse the pump. Do you?", "curious"),
]
DEMO_STEP = 0.25  # seconds between events, so every entrance animation plays


async def play_demo(ws: WebSocket, history: List[Dict[str, str]]) -> None:
    async def send(event: dict, pause: float = DEMO_STEP) -> None:
        await ws.send_json(event)
        await asyncio.sleep(pause)

    await asyncio.sleep(0.6)  # let the overlay finish popping out
    for user, extras, reply, expression in DEMO_TURNS:
        await send({"type": "transcript", "text": user, "is_final": True})
        await send({"type": "status", "state": "thinking", "label": "thinking"}, 1.2)
        for extra in extras:
            await send(extra)
        await send({"type": "response", "text": reply, "expression": expression})
        await send({"type": "status", "state": "idle", "label": "idle"})
        history += [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
        await send(context_event(history))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    # The overlay starts out showing "booting"; tell it the (instant) mock is ready
    await ws.send_json({"type": "status", "state": "idle", "label": "idle"})
    history: List[Dict[str, str]] = []
    await ws.send_json(context_event(history))
    if "--demo" in sys.argv:
        await play_demo(ws, history)
    proposals = [dict(p) for p in SAMPLE_PROPOSALS] if MEMORY_DEMO else []
    if "--busy" in sys.argv:
        await ws.send_json({"type": "status", "state": "thinking", "label": "thinking"})
    if MEMORY_DEMO:
        await ws.send_json({"type": "memory_settings", "auto_approve": AUTO_APPROVE[0]})
        await ws.send_json({"type": "memory_proposals", "items": proposals})
        await ws.send_json({"type": "memory_committed", "kind": "remember",
                            "summary": "- Tudor's bench PSU is a Riden RD6024.",
                            "facts": ["Tudor's bench PSU is a Riden RD6024."]})

    try:
        while True:
            raw_msg = await ws.receive_text()
            data = json.loads(raw_msg)

            if MEMORY_DEMO and data.get("type") in ("memory_decision", "memory_undo",
                                                    "memory_auto_approve"):
                await memory_demo(ws, data, proposals)
                continue

            if data.get("type") == "compact":  # the input pill's circle button
                await ws.send_json({"type": "status", "state": "compacting", "label": "compacting"})
                await asyncio.sleep(3.0)
                history = history[-2:]
                await ws.send_json(context_event(history))
                await ws.send_json({"type": "status", "state": "idle", "label": "idle"})
                continue

            if data.get("type") == "user_text":
                user_text = data.get("text", "").strip()
                if not user_text:
                    continue

                history.append({"role": "user", "content": user_text})

                # Optional compaction demo / real context trimming
                if len(history) > 16 or "compact" in user_text.lower():
                    await ws.send_json({"type": "status", "state": "compacting", "label": "compacting"})
                    await asyncio.sleep(1.8)
                    history = history[-8:]
                    await ws.send_json(context_event(history))

                await ws.send_json({"type": "status", "state": "thinking", "label": "thinking"})

                if LLM_URL:
                    try:
                        result = await asyncio.to_thread(call_llm_sync, history)
                    except Exception as exc:
                        result = {
                            "text": f"Couldn't reach the LLM endpoint ({exc}).",
                            "expression": "skeptical",
                        }
                else:
                    await asyncio.sleep(1.1)
                    result = fallback_reply(user_text)

                history.append({"role": "assistant", "content": result["text"]})
                await ws.send_json(context_event(history))
                await ws.send_json(
                    {
                        "type": "response",
                        "text": result["text"],
                        "expression": result["expression"],
                    }
                )
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("MOCK_PORT", "8001")))