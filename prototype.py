import json
from datetime import datetime
from openai import OpenAI

# --- CONFIG ---
# Point this at your Mac over Tailscale (or localhost if running on the Mac)
BASE_URL = "http://100.103.162.57:1234/v1"
MODEL = "google/gemma-4-26b-a4b" # Match whatever `ollama list` shows on your Mac

client = OpenAI(base_url=BASE_URL, api_key="ollama")
def build_system_prompt() -> str:
 now = datetime.now().strftime("%A, %I:%M %p")

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

        messages.append({"role": "user", "content": user_input})

        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.4,
            reasoning_effort="none"
        )

        reply = (resp.choices[0].message.content or "").strip()
        messages.append({"role": "assistant", "content": reply})

        print(f"Appy: {reply}\n")

if __name__ == "__main__":
   main()