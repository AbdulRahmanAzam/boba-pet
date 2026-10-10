"""Offline wake word: "Hey Bob" (also "Hey Babe", "Hey Boba") with Vosk.

A small Vosk English model listens on the default mic, limited to a tiny
grammar (the wake phrases plus "[unk]" for everything else), which keeps it
light on CPU and hard to trigger by accident. Audio never leaves the PC.
While paused (the pet is already listening or talking) audio is dropped.
"""

import json
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

import pet_paths

MODEL_DIR = pet_paths.MODELS_DIR / "vosk" / "vosk-model-small-en-us-0.15"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
RATE = 16000
PHRASES = ("hey bob", "hey babe", "hey boba")
# What the recognizer listens for. "boba" is not in the small model's
# vocabulary, so sound-alikes stand in for it.
GRAMMAR = {"hey bob": "hey bob", "hey babe": "hey babe", "hey bubba": "hey boba",
           "hey bob a": "hey boba", "hey bobo": "hey boba", "hey boa": "hey boba"}
MIN_CONFIDENCE = 0.6  # average word confidence needed to wake
COOLDOWN_SECONDS = 2.0
# Silence gate: the recognizer only runs while there is sound (plus a short
# tail so it can see the end of a phrase). A quiet room costs almost no CPU.
GATE_MIN = 0.003  # RMS below this is always silence
GATE_FACTOR = 3.0  # sound = this many times the room's noise floor
GATE_TAIL_SECONDS = 1.2


def available() -> str:
    """"" when the wake word can run, else what is missing."""
    try:
        import sounddevice  # noqa: F401
        import vosk  # noqa: F401
    except (ImportError, OSError):  # OSError: the PortAudio DLL failed to load
        return "Wake word needs vosk and sounddevice (pip install vosk sounddevice)."
    if not MODEL_DIR.is_dir():
        return f"Wake word model missing: {MODEL_DIR} (run boba-pet-setup)"
    return ""


def resample(chunk: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE or chunk.size == 0:
        return chunk
    n = int(round(chunk.size * RATE / rate))
    return np.interp(np.linspace(0, chunk.size - 1, n),
                     np.arange(chunk.size), chunk).astype(np.float32)


class Recognizer:
    """Wake phrase spotting on 16 kHz float audio (no mic; easy to test)."""

    def __init__(self) -> None:
        import vosk

        vosk.SetLogLevel(-1)
        self.model = vosk.Model(str(MODEL_DIR))
        self.reset()

    def reset(self) -> None:
        import vosk

        self.rec = vosk.KaldiRecognizer(self.model, RATE,
                                        json.dumps([*GRAMMAR, "[unk]"]))
        self.rec.SetWords(True)

    def feed(self, audio: np.ndarray) -> str:
        """Add audio; returns the wake phrase heard at an utterance end, else ""."""
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        if not self.rec.AcceptWaveform(pcm):
            return ""
        return self.match(json.loads(self.rec.Result()))

    def flush(self) -> str:
        return self.match(json.loads(self.rec.FinalResult()))

    @staticmethod
    def match(result: dict) -> str:
        text = result.get("text", "")
        words = result.get("result") or []
        # longest first, so "hey bob a" wins over "hey bob"
        for heard in sorted(GRAMMAR, key=len, reverse=True):
            if f" {heard} " in f" {text} ":
                conf = [w.get("conf", 0) for w in words if w.get("word") in heard.split()]
                if conf and sum(conf) / len(conf) >= MIN_CONFIDENCE:
                    return GRAMMAR[heard]
        return ""


class WakeWord:
    """Background listener that calls on_wake(phrase) on a worker thread."""

    def __init__(self, on_wake: Callable[[str], None],
                 on_error: Callable[[str], None]) -> None:
        self.on_wake = on_wake
        self.on_error = on_error
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def pause(self, on: bool) -> None:
        if on:
            self.paused.set()
        else:
            self.paused.clear()

    def run(self) -> None:
        try:
            import sounddevice as sd

            spotter = Recognizer()
            device_rate = int(sd.query_devices(kind="input")["default_samplerate"])
            chunks: queue.Queue[np.ndarray] = queue.Queue()

            def callback(data, _frames, _time, _status) -> None:
                if not self.paused.is_set():
                    chunks.put(data[:, 0].copy())

            last_wake, was_paused = 0.0, False
            noise, open_until, feeding = GATE_MIN, 0.0, False
            with sd.InputStream(samplerate=device_rate, channels=1, dtype="float32",
                                blocksize=int(device_rate * 0.1), callback=callback):
                while not self.stopped.is_set():
                    if self.paused.is_set():
                        was_paused = True
                        time.sleep(0.1)
                        continue
                    if was_paused:  # start fresh, not mid-sentence
                        was_paused = False
                        spotter.reset()
                        while not chunks.empty():
                            chunks.get_nowait()
                    try:
                        chunk = chunks.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    level = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
                    now = time.time()
                    if level > max(GATE_MIN, noise * GATE_FACTOR):
                        open_until = now + GATE_TAIL_SECONDS
                    else:
                        noise = 0.97 * noise + 0.03 * level
                    if now > open_until:
                        if not feeding:
                            continue
                        # sound just ended: take any phrase still pending,
                        # then start clean for the next sound
                        feeding = False
                        phrase = spotter.flush()
                        spotter.reset()
                    else:
                        feeding = True
                        phrase = spotter.feed(resample(chunk, device_rate))
                    if phrase and time.time() - last_wake > COOLDOWN_SECONDS:
                        last_wake = time.time()
                        self.paused.set()  # the pet resumes us when it's done
                        self.on_wake(phrase)
        except Exception as exc:  # mic or model trouble: tell the pet, stop
            self.on_error(f"Wake word stopped: {exc}")
