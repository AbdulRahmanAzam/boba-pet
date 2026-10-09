"""Live offline speech-to-text with Whisper (faster-whisper, CPU int8).

Whisper is not a streaming model, so live text comes from re-reading the
current phrase about once a second while you talk. When you pause, the
phrase is committed and a new one starts, so each pass stays short and fast.
On stop, one last pass catches the final words before reporting done.
"""

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

import pet_paths

MODEL_NAME = "base"  # multilingual: English, Urdu and mixed speech
MODELS_DIR = pet_paths.MODELS_DIR / "whisper"
RATE = 16000
REFRESH_SECONDS = 0.8  # how often live text updates
PAUSE_SECONDS = 0.8  # silence that ends a phrase
MAX_PHRASE_SECONDS = 12  # force-commit long phrases to keep passes fast
LANG_PROBE_SECONDS = 1.5  # audio needed before trusting language detection

_model = None
_model_lock = threading.Lock()


def load_model():
    """Load Whisper once; it stays in memory for the next mic use."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8",
                                  cpu_threads=4, download_root=str(MODELS_DIR))
    return _model


def preload() -> None:
    """Warm the model in the background so the first mic click is instant."""
    threading.Thread(target=lambda: _safe(load_model), daemon=True).start()


def _safe(fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception:  # surfaced later when the mic is actually used
        pass


def rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0


class LiveListener:
    """Listens on the default mic until stop() and reports text as it arrives.

    on_text(final, partial): finished phrases so far + the phrase in progress.
    on_done(): after stop(), once the last words are transcribed.
    on_error(message), on_ready(): mic failures / mic is open.
    on_phrase(): optional, after each finished phrase (a pause in speech);
    hands-free mode uses it to stop and send.
    All callbacks run on a worker thread.
    """

    def __init__(self, on_text: Callable[[str, str], None],
                 on_error: Callable[[str], None],
                 on_ready: Callable[[], None],
                 on_done: Callable[[], None],
                 on_phrase: Callable[[], None] | None = None) -> None:
        self.on_text = on_text
        self.on_phrase = on_phrase
        self.on_error = on_error
        self.on_ready = on_ready
        self.on_done = on_done
        self.stopped = threading.Event()
        self.language: str | None = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def transcribe(self, audio: np.ndarray) -> str:
        model = load_model()
        probe = self.language is None and len(audio) >= LANG_PROBE_SECONDS * RATE
        segments, info = model.transcribe(
            audio, language=self.language, beam_size=1, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        if probe:
            self.language = info.language if info.language_probability > 0.6 else "en"
        return text

    def run(self) -> None:
        try:
            import sounddevice as sd

            load_model()
            device_rate = int(sd.query_devices(kind="input")["default_samplerate"])
            chunks: queue.Queue[np.ndarray] = queue.Queue()

            def callback(data, _frames, _time, _status) -> None:
                chunks.put(data[:, 0].copy())

            finals: list[str] = []
            phrase = np.zeros(0, np.float32)
            noise_floor = 0.004
            heard_speech = False
            last_pass = time.time()

            with sd.InputStream(samplerate=device_rate, channels=1,
                                dtype="float32", callback=callback):
                self.on_ready()
                while not self.stopped.is_set():
                    try:
                        pending = [chunks.get(timeout=0.1)]
                    except queue.Empty:
                        continue
                    while not chunks.empty():  # catch up on audio that arrived
                        pending.append(chunks.get_nowait())  # during the last pass
                    for raw in pending:
                        chunk = resample(raw, device_rate)
                        phrase = np.concatenate([phrase, chunk])
                        level = rms(chunk)
                        if level > noise_floor * 3:
                            heard_speech = True
                        else:  # adapt slowly to the room's background noise
                            noise_floor = 0.95 * noise_floor + 0.05 * level

                    if time.time() - last_pass < REFRESH_SECONDS:
                        continue
                    last_pass = time.time()
                    if not heard_speech:
                        phrase = phrase[-int(0.5 * RATE):]  # keep a little lead-in
                        continue

                    tail = phrase[-int(PAUSE_SECONDS * RATE):]
                    paused = rms(tail) < noise_floor * 2.5
                    text = self.transcribe(phrase)
                    if paused or len(phrase) > MAX_PHRASE_SECONDS * RATE:
                        if text:
                            finals.append(text)
                        phrase = np.zeros(0, np.float32)
                        heard_speech = False
                        self.on_text(" ".join(finals), "")
                        if finals and self.on_phrase:
                            self.on_phrase()
                    else:
                        self.on_text(" ".join(finals), text)

            # Stopped: drain buffered audio and transcribe the last words.
            while not chunks.empty():
                phrase = np.concatenate([phrase, resample(chunks.get(), device_rate)])
            if heard_speech and len(phrase) > 0.3 * RATE:
                text = self.transcribe(phrase)
                if text:
                    finals.append(text)
            self.on_text(" ".join(finals), "")
        except Exception as exc:  # report any mic/model failure to the UI
            self.on_error(f"Mic error: {exc}")
        finally:
            self.on_done()


def resample(chunk: np.ndarray, rate: int) -> np.ndarray:
    """Linear resample to 16 kHz (Whisper's input rate)."""
    if rate == RATE or chunk.size == 0:
        return chunk.astype(np.float32)
    n = int(round(chunk.size * RATE / rate))
    x = np.linspace(0, chunk.size - 1, n)
    return np.interp(x, np.arange(chunk.size), chunk).astype(np.float32)
