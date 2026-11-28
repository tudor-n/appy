import math
import os
import queue
import signal
import threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # Must run before importing voice/kokoro so ONNX_PROVIDER is set

from openai import OpenAI
import hearing_workaround  # HEARING-WORKAROUND(respeaker)
import tools
from agent import Agent
from dj import DJ, Ducker
from player import BackgroundPlayer
from hearing import Ear
from memory_client import MemoryClient, system_prompt
from memory_review import MemoryReview
from note_attach import NoteAttacher
from riff import ActivePlayer, Riff, RiffError, SpotifydPlayer
from spotify import Spotify
from spotify_auth import TokenStore
from session_log import SessionLogger, default_logs_dir
from status import StatusTracker
from voice import Speaker
from ui_bridge import UiBridge, VALID_EFFORTS

# How many of Tudor's artist/playlist names Whisper gets as hints (its prompt is ~224 tokens)
MUSIC_HOTWORDS = 40
# Keep the mic shut this long after Appy's last sound: speaker latency + room echo
ECHO_GRACE_S = 0.3
QUIT_WORDS = {"quit", "exit", "shutdown"}


def load_system_prompt() -> str:
    prompt_path = Path(os.getenv("PROMPT_FILE", "prompt.txt"))
    return prompt_path.read_text(encoding="utf-8").strip()


def _sigterm_as_interrupt(signum, frame):
    raise KeyboardInterrupt  # so `finally` runs: music back up, spotifyd stopped


def main():
    # bin/appy sends SIGTERM if Ctrl+C takes more than 5 s; the default handler would
    # skip the cleanup below and leave the "Appy" player running
    signal.signal(signal.SIGTERM, _sigterm_as_interrupt)
    turn_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    # Reasoning effort is switchable at runtime from the UI's effort picker
    initial_effort = os.getenv("REASONING_EFFORT", "none").strip().lower()
    effort_state = {"value": initial_effort if initial_effort in VALID_EFFORTS else "none"}

    def set_effort(value: str):
        effort_state["value"] = value
        print(f"[effort -> {value}]")
        ui.effort(value)  # echo so every connected UI converges on the same value

    def on_user_text(text: str):
        turn_queue.put(("text", text))
        status.queue_turn()

    # Created before the bridge so a message arriving right away finds it
    status = StatusTracker(lambda state: ui.status(state))
    # The memory review coordinator, once memory is set up (the bridge's callbacks need it)
    review_box: dict = {"review": None}

    def on_memory_decision(slug: str, decision: str, text, before=None):
        if review_box["review"]:
            review_box["review"].decide(slug, decision, text, before)

    def on_memory_undo(commits=None):
        if review_box["review"]:
            review_box["review"].undo(commits)

    def on_memory_auto_approve(on: bool):
        if review_box["review"]:
            review_box["review"].set_auto_approve(on)

    # Start WebSocket bridge immediately so the UI shows "loading..." during boot
    ui = UiBridge(
        on_user_text=on_user_text,
        on_set_effort=set_effort,
        get_effort=lambda: effort_state["value"],
        get_mode=tools.current_mode,
        on_compact=lambda: turn_queue.put(("compact", "")),
        on_memory_decision=on_memory_decision,
        on_memory_undo=on_memory_undo,
        on_memory_auto_approve=on_memory_auto_approve,
    )
    ui.start()
    status.announce("booting")

    client = OpenAI(
        base_url=os.getenv("BASE_URL") or "http://127.0.0.1:1234/v1",
        api_key=os.getenv("API_KEY", "ollama"),
    )

    # Music: Appy's own background player (spotifyd) for playing things by name; transport
    # follows whichever player is playing, his or Riff
    background = BackgroundPlayer()
    if not background.start():
        print(f"[music] background player off: {background.problem}")
    # Pause/skip/volume/ducking go to whichever is playing (his player first when both are)
    riff = Riff()
    appy_player = SpotifydPlayer(pid=background.pid)  # his own instance only
    music = ActivePlayer([appy_player, riff])

    def pause_riff():
        """Appy's player took over; Spotify doesn't stop Riff by itself."""
        try:
            if riff.is_playing():
                riff.pause()
        except RiffError:
            pass

    spotify = Spotify(TokenStore()) if os.getenv("SPOTIFY_CLIENT_ID", "").strip() else None
    dj = DJ(music, spotify, player_running=background.running, pause_others=pause_riff,
            own_player=appy_player)
    tools.set_dj(dj)
    # Riff goes quiet while Tudor talks to Appy and while Appy answers
    ducker = Ducker(music)
    dj.set_volume_hook(ducker.on_volume_change, ducker.saved_volume)
    print(f"[music] Riff + background player via MPRIS; Spotify "
          f"{'on' if spotify else 'off (SPOTIFY_CLIENT_ID not set)'}")

    def on_audio(state: str):
        status.set_audio(state)
        if state == "silent":
            ducker.release("appy")
        else:
            ducker.hold("appy")
            ducker.release("user")  # Tudor's done: his music comes back up to "softer"

    speaker = Speaker(on_audio=on_audio)
    # A wake flips the UI to "listening"; if that speech is then discarded as noise,
    # re-send the real state so the mascot doesn't stay stuck listening.
    def on_speech_start():
        status.heard_speech()
        ui.wake()
        ducker.hold("user")

    def on_speech_discarded():
        status.speech_discarded()
        ducker.release("user")

    ear = Ear(on_speech_start=on_speech_start, on_speech_discarded=on_speech_discarded,
              # HEARING-WORKAROUND(respeaker): cap turns while the mic hears music
              utterance_cap=lambda: hearing_workaround.utterance_cap(music))
    ear.start()

    def load_music_names():
        """Teach Whisper the names in Tudor's library ("Kiss" was heard as "give me a
        kiss", "Prince" as "grace")."""
        try:
            names = spotify.music_vocabulary()[:MUSIC_HOTWORDS]
        except Exception as e:
            print(f"[music] couldn't read the library for Whisper: {e}")
            return
        ear.set_hotwords(", ".join(names))
        print(f"[music] Whisper knows {len(names)} artist/playlist names")
    if spotify:
        threading.Thread(target=load_music_names, daemon=True).start()

    def show_tool_card(name: str, result: str):
        """Pop a card out next to Appy for tools that have one."""
        if name == "get_weather" and (card := tools.weather_card(result)):
            ui.widget(card)
        if card := dj.music_card(name, result):
            ui.widget(card)
        if name == "set_mode":  # the mascot's resting face shows the mode
            ui.mode(tools.current_mode())
        if review_box["review"]:  # "Remembered: ..." chip with Undo
            review_box["review"].tool_committed(name, result)
        if attacher and name in ("memory_remember", "memory_forget"):
            # a new or changed note: re-read the index off the turn's thread
            threading.Thread(target=attacher.refresh, daemon=True).start()

    model = os.getenv("MODEL", "google/gemma-4-26b-a4b")
    log = SessionLogger(default_logs_dir())
    log.start(model=model, effort=effort_state["value"])

    # Long-term memory lives on the Mac. Its context goes into the system prompt once,
    # here, so the prompt stays byte-for-byte the same all run (KV cache); changes made
    # during the run reach Appy as a note on his next message (Agent.note_memory_change).
    memory = MemoryClient.from_env(log.session_id)
    tools.set_memory(memory)
    context = memory.context() if memory else None
    if context is not None:
        notes = len(context["index"].splitlines())
        print(f"[memory] core ~{math.ceil(len(context['core']) / 3.2)} tokens, {notes} notes")
    elif not os.getenv("MEMORY_URL", "").strip():
        print("[memory] off (MEMORY_URL not set)")
    prompt = system_prompt(load_system_prompt(), context)
    # Notes the user names get attached to their message (Gemma rarely calls memory_read)
    attacher = NoteAttacher(memory, context["index"]) if memory and context else None
    if os.getenv("APPY_DUMP_PROMPT"):  # exactly what the model gets as its system prompt
        dump = log.path.with_suffix(".prompt.txt")
        try:
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(prompt, encoding="utf-8")
            print(f"[memory] system prompt -> {dump}")
        except OSError as exc:
            print(f"[memory] couldn't write {dump}: {exc}")
    if memory:  # logs left behind by a crash or while the Mac was down
        threading.Thread(target=memory.upload_leftovers, args=(log.path.parent, log.path),
                         daemon=True).start()

    agent = Agent(
        client,
        model=model,
        temperature=float(os.getenv("TEMPERATURE", "0.4")),
        system_prompt=prompt,
        tools=tools.TOOL_SCHEMAS + (tools.MEMORY_TOOL_SCHEMAS if memory else []),
        speaker=speaker,
        ui=ui,
        status=status,
        on_tool_result=show_tool_card,
        stale_tools=tools.STALE_TOOLS,
        log=log,
        compact_at=int(os.getenv("COMPACT_AT", "9500")),
        tail_budget=int(os.getenv("TAIL_BUDGET", "2500")),
        summary_max=int(os.getenv("SUMMARY_MAX", "700")),
        context_limit=int(os.getenv("CONTEXT_TOKENS", "16384")),
        # APPY_DUMP_COMPACTION=1: write each compaction's prompt + summary next to the log
        compaction_dump=log.path.with_suffix("") if os.getenv("APPY_DUMP_COMPACTION") else None,
        note_attacher=attacher,
        pinned_expression=tools.pinned_expression,
    )

    def on_memory_change(line: str):
        agent.note_memory_change(line)
        if attacher:
            attacher.refresh()
    if memory:  # the overlay's review panel for what the consolidator learned
        review_box["review"] = MemoryReview(memory, ui.memory_proposals, ui.memory_committed,
                                            emit_error=ui.memory_error,
                                            emit_settings=ui.memory_settings,
                                            on_change=on_memory_change)
        review_box["review"].start()

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

    agent.report_context()  # the compact ring starts at the system prompt + tools
    status.publish(force=True)  # clears "booting"
    print("Appy ready (voice + UI). Say 'quit' or press Ctrl+C to exit.\n")

    reason = "quit"
    try:
        while True:
            source, user_input = turn_queue.get()
            if source == "compact":  # the UI's compact button; runs between turns
                agent.maybe_compact(force=True)
                agent.report_context()  # also when there was nothing to fold
                continue
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
            log.user(text=user_input, source=source)
            if memory:  # the Mac's consolidator yields to live chat
                threading.Thread(target=memory.activity, daemon=True).start()
            ear.pause()  # don't let Appy hear itself
            status.begin_turn(queued=source == "text")
            try:
                agent.run_turn(
                    user_input,
                    stamp=tools.turn_stamp(now),
                    effort=effort_state["value"],
                    force=tools.forced_call(
                        user_input, agent.recent_tools,
                        # HEARING-WORKAROUND(respeaker): only short music commands count
                        strict_music=hearing_workaround.strict_music(source, music)),
                )
                speaker.wait()
            finally:
                ear.resume(grace=ECHO_GRACE_S)
                ducker.release("user")  # Tudor's turn is over; Appy's voice holds its own
            # Between turns, once Appy has finished speaking; anything said meanwhile is
            # queued in turn_queue, not lost
            agent.maybe_compact()
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception:
        reason = "error"
        raise
    finally:
        ducker.close()  # first: the launcher SIGTERMs us 5 s after Ctrl+C
        background.stop()  # before the slow parts (log upload), so no "Appy" device lingers
        log.end(reason=reason)
        if review_box["review"]:
            review_box["review"].stop()
        if memory:
            uploaded = memory.upload_log(log.path)
            print(f"[memory] log {'uploaded' if uploaded else 'kept locally'}")
        ear.stop()
        speaker.close()


if __name__ == "__main__":
    main()
