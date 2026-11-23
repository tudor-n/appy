import os
import queue
import re
import threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # Must run before importing voice/kokoro so ONNX_PROVIDER is set

from openai import OpenAI
from hearing import Ear
from voice import Speaker
from tools import TOOL_SCHEMAS, run_tool
from ui_bridge import UiBridge

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
CLAUSE_END = re.compile(r"(?<=[.!?,;:\u2014])\s+")
MIN_FIRST_CLAUSE = 24

TAG_RE = re.compile(r"^\s*\[([a-zA-Z]+)\]\s*(.*)$", re.DOTALL)
VALID_EXPRESSIONS = {
    "neutral",
    "happy",
    "excited",
    "curious",
    "surprised",
    "focused",
    "skeptical",
}


def pop_ready_chunk(buf: str, first_chunk: bool) -> tuple[str | None, str]:
    pattern = CLAUSE_END if first_chunk else SENTENCE_END
    parts = pattern.split(buf)
    if len(parts) <= 1:
        return None, buf

    acc = ""
    for idx, part in enumerate(parts[:-1]):
        acc = f"{acc} {part}".strip() if acc else part.strip()
        if not first_chunk or len(acc) >= MIN_FIRST_CLAUSE:
            rest = " ".join(parts[idx + 1 :])
            return acc, rest
    return None, buf


def load_system_prompt() -> str:
    prompt_path = Path(os.getenv("PROMPT_FILE", "prompt.txt"))
    return prompt_path.read_text(encoding="utf-8").strip()


def preheat_llm(client, model, system_prompt):
    try:
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "."},
            ],
            tools=TOOL_SCHEMAS,
            max_tokens=1,
            reasoning_effort="none",
        )
    except Exception:
        pass


def prune_old_tool_outputs(messages, keep_recent=6):
    if len(messages) <= keep_recent:
        return
    for msg in messages[:-keep_recent]:
        if msg.get("role") == "tool" and msg.get("content") != "[pruned]":
            msg["content"] = "[pruned]"


def stream_turn(client, model, temperature, messages, speaker, ui, speak_aloud=True):
    while True:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            temperature=temperature,
            presence_penalty=0.3,
            reasoning_effort="none",
            stream=True,
        )

        reply = ""
        buffer = ""
        tool_calls = {}
        sent_first_chunk = False

        tag_resolved = False
        tag_buf = ""
        expression = "neutral"

        for chunk in stream:
            delta = chunk.choices[0].delta

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    entry = tool_calls.setdefault(
                        tc.index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function and tc.function.name:
                        entry["function"]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        entry["function"]["arguments"] += tc.function.arguments

            if delta.content:
                piece = delta.content

                # Peel leading [expression] tag before feeding Kokoro TTS
                if not tag_resolved:
                    tag_buf += piece
                    stripped = tag_buf.lstrip()
                    if not stripped:
                        continue
                    if not stripped.startswith("["):
                        tag_resolved = True
                        piece = tag_buf
                    elif "]" in tag_buf:
                        m = TAG_RE.match(tag_buf)
                        if m and m.group(1).lower() in VALID_EXPRESSIONS:
                            expression = m.group(1).lower()
                            piece = m.group(2)
                        else:
                            piece = tag_buf
                        tag_resolved = True
                    elif len(tag_buf) > 14:
                        tag_resolved = True
                        piece = tag_buf
                    else:
                        continue

                if not reply:
                    piece = piece.lstrip()
                    if not piece:
                        continue
                    print(f"Appy [{expression}]: ", end="", flush=True)

                print(piece, end="", flush=True)
                reply += piece
                buffer += piece

                if speak_aloud:
                    while True:
                        chunk_to_speak, buffer = pop_ready_chunk(buffer, not sent_first_chunk)
                        if not chunk_to_speak:
                            break
                        speaker.speak(chunk_to_speak)
                        sent_first_chunk = True

        # Flush if the whole reply was super short without a closing bracket
        if not tag_resolved and tag_buf:
            reply += tag_buf
            buffer += tag_buf

        if tool_calls:
            calls_list = [tool_calls[i] for i in sorted(tool_calls)]
            messages.append({"role": "assistant", "content": reply or None, "tool_calls": calls_list})
            for call in calls_list:
                name = call["function"]["name"]
                args = call["function"]["arguments"]
                print(f"[tool: {name}] ", end="", flush=True)
                result = run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            continue  # Loop back so Appy speaks the tool result

        if speak_aloud and buffer.strip():
            speaker.speak(buffer)

        clean_reply = reply.strip()
        print("\n")

        if clean_reply:
            ui.response(clean_reply, expression)
            messages.append({"role": "assistant", "content": clean_reply})
        else:
            ui.status("idle")

        prune_old_tool_outputs(messages)
        break


def main():
    turn_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    # Start WebSocket bridge immediately so the UI shows "loading..." during boot
    ui = UiBridge(on_user_text=lambda txt: turn_queue.put(("text", txt)))
    ui.start()
    ui.status("booting", "loading")

    client = OpenAI(
        base_url=os.getenv("BASE_URL", "http://100.103.162.57:1234/v1"),
        api_key=os.getenv("API_KEY", "ollama"),
    )
    model = os.getenv("MODEL", "google/gemma-4-26b-a4b")
    temperature = float(os.getenv("TEMPERATURE", "0.4"))

    speaker = Speaker(
        on_speak_start=lambda: ui.status("speaking"),
        on_speak_end=lambda: ui.status("idle"),
    )
    ear = Ear(on_speech_start=ui.wake)
    ear.start()

    system_prompt = load_system_prompt()
    messages = [{"role": "system", "content": system_prompt}]

    def _voice_forwarder():
        while ear.running:
            spoken = ear.listen()
            if spoken is None:
                turn_queue.put(("quit", ""))
                break
            if spoken:
                turn_queue.put(("voice", spoken))

    threading.Thread(target=_voice_forwarder, daemon=True).start()

    # Pre-heat LM Studio KV cache in the background
    threading.Thread(
        target=preheat_llm,
        args=(client, model, system_prompt),
        daemon=True,
    ).start()

    ui.status("idle")
    print("Appy ready (voice + UI). Say 'quit' or press Ctrl+C to exit.\n")

    try:
        while True:
            source, user_input = turn_queue.get()
            if source == "quit" or user_input.lower().strip(".!?") in {"quit", "exit", "shutdown"}:
                print("\nExiting...")
                break
            if not user_input:
                continue

            if source == "voice":
                ui.transcript(user_input, is_final=True)
                ui.status("thinking")
            else:
                print(f"You (UI): {user_input}")

            now = datetime.now().strftime("%I:%M %p")
            messages.append({"role": "user", "content": f"[{now}] {user_input}"})

            ear.pause()
            stream_turn(client, model, temperature, messages, speaker, ui, speak_aloud=True)
            speaker.wait()
            ear.resume()
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        ear.stop()
        speaker.close()


if __name__ == "__main__":
    main()