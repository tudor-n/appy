import collections
import math
import os
import queue
import re
import subprocess
import threading
import time
import numpy as np
import onnxruntime as ort
import torch
from faster_whisper import WhisperModel
from huggingface_hub import hf_hub_download
from silero_vad import load_silero_vad
from transformers import WhisperFeatureExtractor

os.environ["KMP_DUPLICATE_LIB_OK"] = "True"

SAMPLE_RATE = 16000
CHUNK_SIZE = 512  # 32ms at 16kHz
SMART_TURN_SAMPLES = SAMPLE_RATE * 8  # 8-second window
# Without an explicit latency, parec (on PipeWire) buffers ~2s before handing audio
# over. The ear would then still be reading the tail of Appy's own reply after it
# resumes, and transcribe it as the user.
PAREC_LATENCY_MS = 30

JUNK_PHRASES = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching.",
    "okay", "okay.", "bye", "bye.", "subscribe", "you", "you.",
}


class SmartTurnDetector:
    """Pipecat Smart Turn v3.2 (8MB int8 ONNX on CPU)."""

    def __init__(self):
        model_path = hf_hub_download(
            repo_id="pipecat-ai/smart-turn-v3",
            filename="smart-turn-v3.2-cpu.onnx",
        )
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(
            model_path, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self.extractor = WhisperFeatureExtractor(chunk_length=8)

    def is_complete(self, audio: np.ndarray) -> tuple[bool, float]:
        # Keep the last 8s; zero-pad at the BEGINNING so speech ends at the right edge
        if len(audio) > SMART_TURN_SAMPLES:
            window = audio[-SMART_TURN_SAMPLES:]
        else:
            window = np.pad(audio, (SMART_TURN_SAMPLES - len(audio), 0), mode="constant")

        features = self.extractor(
            window,
            sampling_rate=SAMPLE_RATE,
            return_tensors="np",
            padding="max_length",
            max_length=SMART_TURN_SAMPLES,
            truncation=True,
            do_normalize=True,
        )["input_features"].astype(np.float32)

        prob = float(self.session.run(None, {"input_features": features})[0][0].item())
        return prob > 0.5, prob


# Whisper's context: names it should expect. Over noise it sometimes just repeats this
# (or the hotwords) back, which is_prompt_echo catches.
WHISPER_PROMPT = "Hey Appy, this is Tudor talking about Dummy, Fifi, Sibiu, Atlas, and Jarvis."


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def is_prompt_echo(text: str, prompt: str, hotwords: str | None = None) -> bool:
    """Is this transcript just Whisper repeating its prompt or hotwords (it does that
    when it can't make out the audio)? Three or more words, all a run of the prompt, or
    all hotwords."""
    words = _words(text)
    if len(words) < 3:
        return False
    joined = " ".join(words)
    if f" {joined} " in f" {' '.join(_words(prompt))} ":
        return True
    hot = set(_words(hotwords or ""))
    return bool(hot) and all(w in hot for w in words)


def filter_transcript(text: str) -> str | None:
    """Return the cleaned transcript, or None if it looks like a Whisper hallucination."""
    text = text.strip()
    if len(text) < 2:
        return None

    # Filter repetitive hallucinations
    words = text.lower().replace(".", "").replace(",", "").split()
    if len(words) > 5 and (len(set(words)) / len(words)) < 0.4:
        return None

    # Filter classic Whisper silence hallucinations
    if text.lower() in JUNK_PHRASES:
        return None
    return text


def parec_cmd(mic_device: str | None) -> list[str]:
    cmd = [
        "parec",
        f"--rate={SAMPLE_RATE}",
        "--channels=1",
        "--format=float32le",
        f"--latency-msec={PAREC_LATENCY_MS}",
    ]
    if mic_device:
        cmd.extend(["-d", mic_device])
    return cmd


def over_cap(chunks: int, cap_s: float | None) -> bool:
    """HEARING-WORKAROUND(respeaker): has an utterance of `chunks` reached `cap_s`?"""
    return cap_s is not None and chunks * CHUNK_SIZE / SAMPLE_RATE >= cap_s


class Ear:
    def __init__(self, on_speech_start=None, on_speech_discarded=None, utterance_cap=None):
        """on_speech_start fires when speech begins; on_speech_discarded fires if that
        speech is then thrown away (too quiet, or filtered as a hallucination).
        utterance_cap() -> seconds or None, asked at each speech start: the longest that
        utterance may run (HEARING-WORKAROUND(respeaker): leaked song vocals)."""
        self.on_speech_start = on_speech_start
        self.on_speech_discarded = on_speech_discarded
        self.utterance_cap = utterance_cap  # HEARING-WORKAROUND(respeaker)
        # Names Whisper should expect (artists, playlists), set once the music library is read
        self.hotwords: str | None = None
        print("[hearing] Loading Silero VAD (ONNX)...", flush=True)
        self.vad = load_silero_vad(onnx=True)

        print("[hearing] Loading Smart Turn v3.2 (CPU)...", flush=True)
        self.turn_detector = SmartTurnDetector()

        model_size = os.getenv("WHISPER_MODEL", "turbo")
        device = os.getenv("WHISPER_DEVICE", "cuda")
        compute = os.getenv("WHISPER_COMPUTE", "float16")
        print(f"[hearing] Loading Whisper ({model_size}) on {device}...", flush=True)
        self.asr = WhisperModel(model_size, device=device, compute_type=compute)

        self.queue = queue.Queue()
        # Bumped at each speech start, so a slow transcription of an earlier utterance
        # can tell that the user has started talking again
        self._utterance = 0
        self.running = False
        self._deaf_until = 0.0  # monotonic time; mic audio before it is thrown away
        self._proc = None
        self._bt_card = None

    def _set_bt_profile(self, profile: str) -> str | None:
        """Flip connected Bluetooth card to HFP or back to A2DP."""
        try:
            out = subprocess.check_output(["pactl", "list", "cards", "short"], text=True)
            for line in out.splitlines():
                if "bluez_card." in line:
                    card = line.split()[1]
                    subprocess.run(
                        ["pactl", "set-card-profile", card, profile],
                        stderr=subprocess.DEVNULL,
                    )
                    return card
        except Exception:
            pass
        return None

    def _find_bt_source(self) -> str | None:
        try:
            out = subprocess.check_output(["pactl", "list", "sources", "short"], text=True)
            for line in out.splitlines():
                if "bluez_input." in line:
                    return line.split()[1]
        except Exception:
            pass
        return None

    def start(self):
        self.running = True
        threading.Thread(target=self._vad_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self._proc:
            self._proc.terminate()
        if self._bt_card:
            subprocess.run(
                ["pactl", "set-card-profile", self._bt_card, "a2dp-sink"],
                stderr=subprocess.DEVNULL,
            )

    def pause(self):
        self._deaf_until = math.inf

    def resume(self, grace: float = 0.0, now: float | None = None):
        """Start listening again, after `grace` seconds (lets the speakers' last
        sound and the room's echo die away first)."""
        self._deaf_until = (time.monotonic() if now is None else now) + grace

    def is_deaf(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) < self._deaf_until

    def _vad_loop(self):
        ring_buffer = collections.deque(maxlen=15)  # ~480ms pre-roll
        buffer = []
        is_speaking = False
        speech_chunks = 0
        silence_chunks = 0
        checked_pause = False
        cap = None  # HEARING-WORKAROUND(respeaker): this utterance's length limit

        # 200ms pause -> ask Smart Turn; 2.0s pause -> force-close fallback
        smart_turn_chunks = int((SAMPLE_RATE / CHUNK_SIZE) * 0.20)
        max_silence_chunks = int((SAMPLE_RATE / CHUNK_SIZE) * 2.0)

        mic_device = os.getenv("MIC_DEVICE") or self._find_bt_source()
        if mic_device:
            print(f"[hearing] Using mic: {mic_device}", flush=True)
            subprocess.run(
                ["pactl", "set-source-mute", mic_device, "0"],
                stderr=subprocess.DEVNULL,
            )

        self._proc = subprocess.Popen(parec_cmd(mic_device), stdout=subprocess.PIPE)
        if mic_device and "bluez_input." in mic_device:
            self._bt_card = self._set_bt_profile("headset-head-unit")

        chunk_bytes = CHUNK_SIZE * 4  # 512 float32 samples = 2048 bytes

        try:
            while self.running:
                raw = self._proc.stdout.read(chunk_bytes)
                if len(raw) < chunk_bytes:
                    break
                if self.is_deaf():
                    ring_buffer.clear()  # no pre-roll from before the pause
                    is_speaking = False
                    speech_chunks = 0
                    silence_chunks = 0
                    checked_pause = False
                    buffer.clear()
                    continue

                audio_np = np.frombuffer(raw, dtype=np.float32).copy()
                ring_buffer.append(audio_np)

                speech_prob = self.vad(
                    torch.from_numpy(audio_np), SAMPLE_RATE
                ).item()

                if speech_prob > 0.45:
                    if not is_speaking:
                        speech_chunks += 1
                        if speech_chunks >= 3:  # ~96ms sustained speech
                            is_speaking = True
                            silence_chunks = 0
                            checked_pause = False
                            buffer = list(ring_buffer)
                            self._utterance += 1
                            # HEARING-WORKAROUND(respeaker)
                            cap = self.utterance_cap() if self.utterance_cap else None
                            if self.on_speech_start:
                                self.on_speech_start()
                    else:
                        buffer.append(audio_np)
                        silence_chunks = 0
                        checked_pause = False
                else:
                    speech_chunks = 0
                    if is_speaking:
                        buffer.append(audio_np)
                        silence_chunks += 1

                        should_finish = False
                        if silence_chunks >= smart_turn_chunks and not checked_pause:
                            checked_pause = True
                            t0 = time.monotonic()
                            complete, prob = self.turn_detector.is_complete(
                                np.concatenate(buffer)
                            )
                            dt_ms = (time.monotonic() - t0) * 1000
                            if complete:
                                print(
                                    f"[turn: {dt_ms:.0f}ms, p={prob:.2f}] ",
                                    end="",
                                    flush=True,
                                )
                                should_finish = True
                        elif silence_chunks >= max_silence_chunks:
                            # Smart Turn said "not yet" at 200ms; if this shows up often
                            # for finished sentences, the 2s wait is worth tuning
                            print("[turn: no end detected, 2s fallback] ", end="", flush=True)
                            should_finish = True

                        if should_finish:
                            is_speaking = False
                            silence_chunks = 0
                            checked_pause = False
                            full_audio = np.concatenate(buffer)
                            buffer = []
                            threading.Thread(
                                target=self._transcribe,
                                args=(full_audio, self._utterance),
                                daemon=True,
                            ).start()

                # HEARING-WORKAROUND(respeaker): song vocals leaking into the mic can
                # hide the pause that ends a turn; close it at the cap instead
                if is_speaking and over_cap(len(buffer), cap):
                    print(f"[turn: {cap:.0f}s cap (music)] ", end="", flush=True)
                    is_speaking = False
                    silence_chunks = 0
                    checked_pause = False
                    full_audio = np.concatenate(buffer)
                    buffer = []
                    threading.Thread(
                        target=self._transcribe,
                        args=(full_audio, self._utterance),
                        daemon=True,
                    ).start()
        finally:
            self._proc.terminate()

    def _transcribe(self, audio_array: np.ndarray, utterance: int):
        try:
            text = self._recognize(audio_array)
        except Exception as e:
            print(f"[hearing] transcription failed: {e}", flush=True)
            text = None
        if text is not None:
            self.queue.put(text)
            return
        # Only reset the UI if nobody has started speaking since this utterance;
        # otherwise we'd knock a live "listening" state back to idle.
        if utterance == self._utterance and self.on_speech_discarded:
            self.on_speech_discarded()

    def set_hotwords(self, hotwords: str | None) -> None:
        """Bias Whisper towards these names (comma-separated), e.g. Tudor's artists."""
        self.hotwords = hotwords or None

    def _recognize(self, audio_array: np.ndarray) -> str | None:
        if np.abs(audio_array).mean() < 0.004:
            return None

        t0 = time.monotonic()
        segments, _ = self.asr.transcribe(
            audio_array,
            language="en",
            beam_size=3,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            initial_prompt=WHISPER_PROMPT,
            hotwords=self.hotwords,
        )
        text = filter_transcript(" ".join(s.text.strip() for s in segments))
        if text is not None and is_prompt_echo(text, WHISPER_PROMPT, self.hotwords):
            print(f"[hearing] dropped a prompt echo: {text!r}", flush=True)
            text = None
        dt_ms = (time.monotonic() - t0) * 1000
        if text is not None:
            print(f"[stt: {dt_ms:.0f}ms]")
        return text

    def listen(self) -> str | None:
        try:
            text = self.queue.get()
            print(f"You: {text}")
            return text
        except KeyboardInterrupt:
            return None