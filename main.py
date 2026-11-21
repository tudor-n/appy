import os
import re
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # Must run before importing voice/kokoro so ONNX_PROVIDER is set

from openai import OpenAI
from hearing import listen
from voice import Speaker

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def load_system_prompt() -> str:
    prompt_path = Path(os.getenv("PROMPT_FILE", "prompt.txt"))
    return prompt_path.read_text(encoding="utf-8").strip()


def main():
    client = OpenAI(
        base_url=os.getenv("BASE_URL", "http://100.103.162.57:1234/v1"),
        api_key=os.getenv("API_KEY", "ollama"),
    )
    model = os.getenv("MODEL", "google/gemma-4-26b-a4b")
    temperature = float(os.getenv("TEMPERATURE", "0.4"))

    speaker = Speaker()
    messages = [{"role": "system", "content": load_system_prompt()}]
    print("Appy ready. Type 'quit' to exit.\n")

    while True:
        user_input = listen()
        if user_input is None or user_input.lower() in {"quit", "exit"}:
            print("\nExiting...")
            break
        if not user_input:
            continue

        now = datetime.now().strftime("%I:%M %p")
        messages.append({"role": "user", "content": f"[{now}] {user_input}"})

        print("Appy: ", end="", flush=True)

        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            reasoning_effort="none",
            stream=True,
        )

        reply = ""
        buffer = ""

        for chunk in stream:
            token = chunk.choices[0].delta.content or ""
            print(token, end="", flush=True)
            reply += token
            buffer += token

            parts = SENTENCE_END.split(buffer)
            if len(parts) > 1:
                for complete_sentence in parts[:-1]:
                    speaker.speak(complete_sentence)
                buffer = parts[-1]

        speaker.speak(buffer)
        print("\n")

        messages.append({"role": "assistant", "content": reply.strip()})
        speaker.wait()

    speaker.close()


if __name__ == "__main__":
    main()