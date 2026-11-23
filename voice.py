import os
import queue
import threading
import numpy as np
import onnxruntime as ort
import sounddevice as sd
from kokoro_onnx import Kokoro


class Speaker:
    def __init__(self, on_speak_start=None, on_speak_end=None):
        ort.set_default_logger_severity(3)
        ort.preload_dlls()

        self.on_speak_start = on_speak_start
        self.on_speak_end = on_speak_end
        self._is_playing = False

        model = os.getenv("KOKORO_MODEL", "kokoro-v1.0.onnx")
        voices = os.getenv("KOKORO_VOICES", "voices-v1.0.bin")
        self.voice = os.getenv("VOICE", "am_puck")
        self.speed = float(os.getenv("SPEED", "1.08"))

        self.kokoro = Kokoro(model, voices)
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
            finally:
                self.speech_queue.task_done()

    def _play_worker(self):
        with sd.OutputStream(samplerate=24000, channels=1, dtype="float32") as stream:
            while True:
                samples = self.audio_queue.get()
                if samples is None:
                    self.audio_queue.task_done()
                    break

                if not self._is_playing:
                    self._is_playing = True
                    if self.on_speak_start:
                        self.on_speak_start()

                stream.write(samples)
                self.audio_queue.task_done()

                if self.audio_queue.empty() and self.speech_queue.unfinished_tasks == 0:
                    self._is_playing = False
                    if self.on_speak_end:
                        self.on_speak_end()

    def speak(self, text: str):
        clean = text.strip()
        if clean:
            self.speech_queue.put(clean)

    def wait(self):
        self.speech_queue.join()
        self.audio_queue.join()

    def close(self):
        self.speech_queue.put(None)