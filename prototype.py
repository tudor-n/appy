import queue
import re
import threading
from datetime import datetime
import sounddevice as sd
from kokoro_onnx import Kokoro
from openai import OpenAI
import numpy as np 
import time
import os
os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"

# --- CONFIG ---
BASE_URL = "http://100.103.162.57:1234/v1"
MODEL = "google/gemma-4-26b-a4b"
VOICE = "am_puck"
SPEED = 1.08

client = OpenAI(base_url=BASE_URL, api_key="ollama")
import onnxruntime as ort; ort.preload_dlls()
ort.set_default_logger_severity(3)
kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")

speech_queue = queue.Queue()
audio_queue = queue.Queue()

def synth_worker():
    while True:
        sentence = speech_queue.get()
        if sentence is None:
            audio_queue.put(None)
            speech_queue.task_done()
            break
        try:
        #    t0 = time.perf_counter()
            samples, _ = kokoro.create(sentence, voice=VOICE, speed=SPEED, lang="en-us")
         #   dt = time.perf_counter() - t0
          #  print(f" [tts:{dt:.2f}s]", end="", flush=True)
            audio_queue.put(samples.astype(np.float32))
        except Exception as e:
            print(f"TTS synth error: {e}")
        finally:
            speech_queue.task_done()

def play_worker():
    with sd.OutputStream(samplerate=24000, channels=1, dtype="float32") as stream:
        while True:
            samples = audio_queue.get()
            if samples is None:
                audio_queue.task_done()
                break
            stream.write(samples)
            audio_queue.task_done()

threading.Thread(target=synth_worker, daemon=True).start()
threading.Thread(target=play_worker, daemon=True).start()

def build_system_prompt() -> str:
 now = datetime.now().strftime("%I:%M %p")

 return f"""You are Appy, a personal lab and desktop assistant for Tudor. You run on his Mac, live as a minimalist black-and-white apple character on his screen, and share his workspace with his robot arm (Dummy) and his rover (Fifi).

Lineage (only reference if relevant or brought up):
- You are Tudor's fourth assistant.
- Jarvis (v1): Cloud-powered via Gemini, a tiny floating widget with Spotify and fiddly task tracking.
- Atlas 1 (v2): Over-engineered, all-local, stuffed with gimmicky "brain" modules. Scrapped.
- Atlas 2 (v3): Solid, minimal, and quiet—no mic, no local model, just scripted lines. Too dull to use.

Core Rules:
- Speak like a sharp, dry-humored buddy, not a corporate assistant. Never use corporate filler.
- Keep replies short (1-3 sentences max, under 35 words). Write for TTS: no markdown, no bullets, no emojis, natural contractions ("it's", "I'm", "I'll").
- Ground all humor in real facts from context or tool results. Never invent statuses.
- Act first, confirm never. Call the appropriate tool immediately, then briefly state what you did.
- Only ask "what are we working on?" on a first-arrival event. On normal turns, end with a statement.
- Respect the current mode, and never announce mode switches robotically:
  - BANTER MODE: Understated, observant, slightly teasing when earned.
  - FOCUS MODE: Terse, zero jokes, one short sentence max.

Current State:
- Mode: BANTER
- Time: {now}"""

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MIN_CLAUSE_LEN = 25

def main():
    messages = [{"role": "system", "content": build_system_prompt()}]
    print("Appy ready. Type 'quit' to exit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            break
        if not user_input:
            continue
        if user_input.lower() == "quit":
            print("Exiting...")
            break

        messages[0]["content"] = build_system_prompt()
        now = datetime.now().strftime("%I:%M %p")
        messages.append({"role": "user", "content": f"[{now}] {user_input}"})

        print("Appy: ", end="", flush=True)

        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.4,
            reasoning_effort="none",
            stream=True
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
                    clean = complete_sentence.strip()
                    if clean:
                        speech_queue.put(clean)
                buffer = parts[-1]

        if buffer.strip():
            speech_queue.put(buffer.strip())

        print("\n")    
        messages.append({"role": "assistant", "content": reply.strip()})
        speech_queue.join()
        audio_queue.join()

    speech_queue.put(None)

if __name__ == "__main__":
   main()