import os
import queue
import threading
import numpy as np
import onnxruntime as ort
import sounddevice as sd
from kokoro_onnx import Kokoro

from status import SpeechActivity


def provider_warning(requested: str | None, active: list[str]) -> str | None:
    """A message if Kokoro asked for an ONNX provider but onnxruntime fell back.

    onnxruntime falls back to the CPU silently (we turn its logging down), which made
    every sentence 4x slower to synthesise without anyone noticing.
    """
    if not requested or requested in active:
        return None
    return (
        f"[voice] WARNING: ONNX_PROVIDER={requested} but Kokoro is running on {active}. "
        "The CPU-only onnxruntime has probably overwritten onnxruntime-gpu; fix with: "
        ".venv/bin/pip install --force-reinstall --no-deps onnxruntime-gpu"
    )


class Speaker:
    def __init__(self, on_audio=None):
        """on_audio(state) reports "queued", "playing" (the audio has actually started)
        and "silent" (everything queued has played or failed); see SpeechActivity."""
        ort.set_default_logger_severity(3)
        ort.preload_dlls()

        self._activity = SpeechActivity(on_audio)

        model = os.getenv("KOKORO_MODEL", "kokoro-v1.0.onnx")
        voices = os.getenv("KOKORO_VOICES", "voices-v1.0.bin")
        self.voice = os.getenv("VOICE", "am_puck")
        self.speed = float(os.getenv("SPEED", "1.08"))

        self.kokoro = Kokoro(model, voices)
        warning = provider_warning(os.getenv("ONNX_PROVIDER"), self.kokoro.sess.get_providers())
        if warning:
            print(warning, flush=True)
        self.speech_queue = queue.Queue()
        self.audio_queue = queue.Queue()

        threading.Thread(target=self._synth_worker, daemon=True).start()
        threading.Thread(target=self._play_worker, daemon=True).start()
        threading.Thread(
            target=lambda: self.kokoro.create(
                "a", voice=self.voice, speed=self.speed, lang="en-us"
            ),
            daemon=True,
        ).start()

    def _synth_worker(self):
        while True:
            sentence = self.speech_queue.get()
            if sentence is None:
                self.audio_queue.put(None)
                self.speech_queue.task_done()
                break
            try:
                samples, _ = self.kokoro.create(
                    sentence, voice=self.voice, speed=self.speed, lang="en-us"
                )
                self.audio_queue.put(samples.astype(np.float32))
            except Exception as e:
                print(f"TTS synth error: {e}")
                self._activity.done()  # nothing will play for this sentence
            finally:
                self.speech_queue.task_done()

    def _play_worker(self):
        with sd.OutputStream(samplerate=24000, channels=1, dtype="float32") as stream:
            while True:
                samples = self.audio_queue.get()
                if samples is None:
                    self.audio_queue.task_done()
                    break

                try:
                    self._activity.playing()
                    stream.write(samples)
                finally:
                    self.audio_queue.task_done()
                    self._activity.done()

    def speak(self, text: str):
        clean = text.strip()
        if clean:
            self._activity.add()
            self.speech_queue.put(clean)

    def wait(self):
        self.speech_queue.join()
        self.audio_queue.join()

    def close(self):
        self.speech_queue.put(None)