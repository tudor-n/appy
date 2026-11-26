import os
import queue
import threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # Must run before importing voice/kokoro so ONNX_PROVIDER is set

from openai import OpenAI
import tools
from agent import Agent
from hearing import Ear
from status import StatusTracker
from voice import Speaker
from ui_bridge import UiBridge, VALID_EFFORTS

QUIT_WORDS = {"quit", "exit", "shutdown"}


def load_system_prompt() -> str:
    prompt_path = Path(os.getenv("PROMPT_FILE", "prompt.txt"))
    return prompt_path.read_text(encoding="utf-8").strip()


def main():
    turn_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    # Reasoning effort is switchable at runtime from the UI's effort picker
    initial_effort = os.getenv("REASONING_EFFORT", "none").strip().lower()
    effort_state = {"value": initial_effort if initial_effort in VALID_EFFORTS else "none"}

    def set_effort(value: str):
        effort_state["value"] = value
        print(f"[effort -> {value}]")
        ui.effort(value)  # echo so every connected UI converges on the same value

    # Start WebSocket bridge immediately so the UI shows "loading..." during boot
    ui = UiBridge(
        on_user_text=lambda txt: turn_queue.put(("text", txt)),
        on_set_effort=set_effort,
        get_effort=lambda: effort_state["value"],
    )
    ui.start()
    status = StatusTracker(ui.status)
    status.announce("booting")

    client = OpenAI(
        base_url=os.getenv("BASE_URL") or "http://127.0.0.1:1234/v1",
        api_key=os.getenv("API_KEY", "ollama"),
    )

    speaker = Speaker(
        on_speak_start=lambda: status.set_speaking(True),
        on_speak_end=lambda: status.set_speaking(False),
    )
    # A wake flips the UI to "listening"; if that speech is then discarded as noise,
    # re-send the real state so the mascot doesn't stay stuck listening.
    ear = Ear(on_speech_start=ui.wake, on_speech_discarded=lambda: status.publish(force=True))
    ear.start()

    agent = Agent(
        client,
        model=os.getenv("MODEL", "google/gemma-4-26b-a4b"),
        temperature=float(os.getenv("TEMPERATURE", "0.4")),
        system_prompt=load_system_prompt(),
        speaker=speaker,
        ui=ui,
        status=status,
    )

    def _voice_forwarder():
        while ear.running:
            spoken = ear.listen()
            if spoken is None:
                turn_queue.put(("quit", ""))
                break
            if spoken:
                turn_queue.put(("voice", spoken))

    threading.Thread(target=_voice_forwarder, daemon=True).start()
    # Pre-heat the LLM server's KV cache in the background
    threading.Thread(target=agent.preheat, daemon=True).start()
    tools.prefetch()

    status.publish(force=True)  # clears "booting"
    print("Appy ready (voice + UI). Say 'quit' or press Ctrl+C to exit.\n")

    try:
        while True:
            source, user_input = turn_queue.get()
            if source == "quit" or user_input.lower().strip(".!?") in QUIT_WORDS:
                print("\nExiting...")
                break
            if not user_input:
                continue

            if source == "voice":
                ui.transcript(user_input, is_final=True)
            else:
                print(f"You (UI): {user_input}")

            now = datetime.now().strftime("%I:%M %p")
            ear.pause()  # don't let Appy hear itself
            try:
                agent.run_turn(f"[{now}] {user_input}", effort=effort_state["value"])
                speaker.wait()
            finally:
                ear.resume()
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        ear.stop()
        speaker.close()


if __name__ == "__main__":
    main()
