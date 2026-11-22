import collections
import os
import queue
import threading
import numpy as np
import sounddevice as sd
import torch
from faster_whisper import WhisperModel

os.environ["KMP_DUPLICATE_LIB_OK"] = "True"

JUNK_PHRASES = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching.",
    "okay", "okay.", "bye", "bye.", "subscribe", "you", "you.",
}


class Ear:
    def __init__(self):
        model_size = os.getenv("WHISPER_MODEL", "small.en")
        device = os.getenv("WHISPER_DEVICE", "cuda")
        compute = os.getenv("WHISPER_COMPUTE", "float16")

        print(f"[hearing] Loading Whisper ({model_size}) on {device}...", flush=True)
        self.whisper = WhisperModel(model_size, device=device, compute_type=compute)

        print("[hearing] Loading Silero VAD...", flush=True)
        self.vad, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
        )
        # Silero VAD is tiny (~2MB) and runs in <0.2ms on CPU—keeps VRAM free
        self.vad.to("cpu")

        self.sample_rate = 16000
        self.chunk_size = 512
        self.queue = queue.Queue()
        self.running = False
        self.paused = False

    def start(self):
        self.running = True
        threading.Thread(target=self._vad_loop, daemon=True).start()

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def _vad_loop(self):
        ring_buffer = collections.deque(maxlen=15)  # ~0.5s pre-roll
        buffer = []
        is_speaking = False
        speech_chunks = 0
        silence_chunks = 0
        max_silence = int((self.sample_rate / self.chunk_size) * 0.6)  # ~0.6s silence

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.chunk_size,
        ) as stream:
            while self.running:
                audio_chunk, overflow = stream.read(self.chunk_size)
                if overflow or self.paused:
                    is_speaking = False
                    speech_chunks = 0
                    buffer.clear()
                    continue

                audio_np = audio_chunk.flatten()
                ring_buffer.append(audio_np)

                with torch.no_grad():
                    speech_prob = self.vad(
                        torch.from_numpy(audio_np).unsqueeze(0), self.sample_rate
                    ).item()

                if speech_prob > 0.45:
                    if not is_speaking:
                        speech_chunks += 1
                        if speech_chunks >= 3:  # ~96ms of sustained speech
                            is_speaking = True
                            silence_chunks = 0
                            buffer = list(ring_buffer)
                    else:
                        buffer.append(audio_np)
                        silence_chunks = 0
                else:
                    speech_chunks = 0
                    if is_speaking:
                        buffer.append(audio_np)
                        silence_chunks += 1
                        if silence_chunks > max_silence:
                            is_speaking = False
                            full_audio = np.concatenate(buffer)
                            buffer = []
                            threading.Thread(
                                target=self._transcribe, args=(full_audio,), daemon=True
                            ).start()

    def _transcribe(self, audio_array):
        if np.abs(audio_array).mean() < 0.005:
            return

        segments, _ = self.whisper.transcribe(
            audio_array,
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            initial_prompt="Appy, Dummy, Fifi",
        )
        text = " ".join(s.text.strip() for s in segments).strip()

        if len(text) < 3:
            return

        # Filter repetitive hallucinations
        words = text.lower().replace(".", "").replace(",", "").split()
        if len(words) > 5 and (len(set(words)) / len(words)) < 0.4:
            return

        # Filter classic Whisper silence hallucinations
        if text.lower() in JUNK_PHRASES:
            return

        self.queue.put(text)

    def listen(self) -> str | None:
        try:
            text = self.queue.get()
            print(f"You: {text}")
            return text
        except KeyboardInterrupt:
            return None