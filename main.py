import os
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

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


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


def stream_turn(client, model, temperature, messages, speaker):
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
                if not reply:
                    print("Appy: ", end="", flush=True)
                print(delta.content, end="", flush=True)
                reply += delta.content
                buffer += delta.content

                parts = SENTENCE_END.split(buffer)
                if len(parts) > 1:
                    for complete_sentence in parts[:-1]:
                        speaker.speak(complete_sentence)
                    buffer = parts[-1]

        if tool_calls:
            calls_list = [tool_calls[i] for i in sorted(tool_calls)]
            messages.append({"role": "assistant", "content": reply or None, "tool_calls": calls_list})
            for call in calls_list:
                name = call["function"]["name"]
                args = call["function"]["arguments"]
                print(f"[tool: {name}] ", end="", flush=True)
                result = run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            continue  # Loop back so Appy speaks the result

        speaker.speak(buffer)
        print("\n")
        messages.append({"role": "assistant", "content": reply.strip()})
        prune_old_tool_outputs(messages)
        break


def main():
    client = OpenAI(
        base_url=os.getenv("BASE_URL", "http://100.103.162.57:1234/v1"),
        api_key=os.getenv("API_KEY", "ollama"),
    )
    model = os.getenv("MODEL", "google/gemma-4-26b-a4b")
    temperature = float(os.getenv("TEMPERATURE", "0.4"))

    speaker = Speaker()
    ear = Ear()
    ear.start()

    system_prompt = load_system_prompt()
    messages = [{"role": "system", "content": system_prompt}]

    # Pre-heat LM Studio KV cache in the background
    threading.Thread(
        target=preheat_llm,
        args=(client, model, system_prompt),
        daemon=True,
    ).start()

    print("Appy ready. Say 'quit' or press Ctrl+C to exit.\n")

    while True:
        user_input = ear.listen()
        if user_input is None or user_input.lower().strip(".!?") in {"quit", "exit", "shutdown"}:
            print("\nExiting...")
            break
        if not user_input:
            continue

        now = datetime.now().strftime("%I:%M %p")
        messages.append({"role": "user", "content": f"[{now}] {user_input}"})

        ear.pause()
        stream_turn(client, model, temperature, messages, speaker)
        speaker.wait()
        ear.resume()

    ear.stop()
    speaker.close()

if __name__ == "__main__":
    main()