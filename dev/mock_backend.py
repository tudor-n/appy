"""Standalone mock backend for UI work: no audio stack or LLM needed.

Serves the same WebSocket on :8001 as main.py. Replies come from APPY_LLM_URL (any
OpenAI-compatible chat endpoint) if set, otherwise from canned keyword responses.
Run: .venv/bin/python dev/mock_backend.py
"""
import asyncio
import json
import os
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


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    history: List[Dict[str, str]] = []

    try:
        while True:
            raw_msg = await ws.receive_text()
            data = json.loads(raw_msg)

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