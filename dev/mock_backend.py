"""Standalone mock backend for UI work: no audio stack or LLM needed.

Serves the same WebSocket on :8001 as main.py. Replies come from APPY_LLM_URL (any
OpenAI-compatible chat endpoint) if set, otherwise from canned keyword responses.
Run: .venv/bin/python dev/mock_backend.py [--memory]
`--memory` also sends two sample memory proposals and answers the review card's buttons.
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
]


async def memory_demo(ws: WebSocket, data: dict, items: list) -> None:
    """Answer the review card like the real backend would (without a memory service)."""
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
            await ws.send_json({"type": "memory_committed", "kind": kind, "summary": item["summary"]})
    await ws.send_json({"type": "memory_proposals", "items": items})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    # The overlay starts out showing "booting"; tell it the (instant) mock is ready
    await ws.send_json({"type": "status", "state": "idle", "label": "idle"})
    history: List[Dict[str, str]] = []
    proposals = [dict(p) for p in SAMPLE_PROPOSALS] if MEMORY_DEMO else []
    if MEMORY_DEMO:
        await ws.send_json({"type": "memory_proposals", "items": proposals})
        await ws.send_json({"type": "memory_committed", "kind": "remember",
                            "summary": "- Tudor's bench PSU is a Riden RD6024."})

    try:
        while True:
            raw_msg = await ws.receive_text()
            data = json.loads(raw_msg)

            if MEMORY_DEMO and data.get("type") in ("memory_decision", "memory_undo"):
                await memory_demo(ws, data, proposals)
                continue

            if data.get("type") == "compact":  # the input pill's circle button
                await ws.send_json({"type": "status", "state": "compacting", "label": "compacting"})
                await asyncio.sleep(3.0)
                history = history[-2:]
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
    uvicorn.run(app, host="127.0.0.1", port=8001)