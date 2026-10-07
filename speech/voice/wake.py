"""Wake-word detection, on this machine (§15).

"Hey Jarvis" is recognised locally by openWakeWord — a small ONNX model that
runs on the CPU. Nothing is sent anywhere to decide whether the user said the
wake word, which matters more here than almost anywhere else in the system: a
wake word means a microphone is always listening, and the only acceptable answer
to "where does that audio go?" is "nowhere".

**Getting the model.** openWakeWord does NOT fetch its models by itself (an
earlier note here said it did, and the detector never worked on a fresh install
because of it). The three small files it needs — the "hey jarvis" model and the
two feature models — are downloaded once into `data/wakeword/`, in the
background, the first time anything asks (`prepare()`), and loaded from there.
Never on the audio path, never into the installed package.

**What is honest about this module when the model is missing.** If the package or
the model is unavailable, `WakeDetector.available` is False and `feed()` returns
None forever — it never pretends, and never silently degrades into "wakes on any
noise", which would be far worse than not working. `status()` says which it is,
including "still getting ready".

Audio contract: 16 kHz, 16-bit signed little-endian, mono. That is what the model
was trained on; resampling belongs to whatever captures the audio, not here.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
#: openWakeWord's own frame size: 1280 samples = 80 ms at 16 kHz.
FRAME_SAMPLES = 1280
FRAME_BYTES = FRAME_SAMPLES * 2

DEFAULT_MODEL = "hey_jarvis_v0.1"
#: Above this score, the wake word was said. The pretrained model's own
#: recommended operating point; a lower value trades false wakes for sensitivity
#: and is exposed rather than buried.
DEFAULT_THRESHOLD = 0.5
#: After a wake, ignore further detections for this long. Without it a single
#: "hey Jarvis" fires several times as the phrase passes through the window,
#: and the assistant appears to wake up three times to one greeting.
REFRACTORY_S = 2.0
#: Fetching the model reaches the network, so it is an interlock like every other
#: thing Jarvis does on its own: off unless `main()` (the real launch) turns it
#: on. A test that asks for a detector never downloads anything.
DOWNLOAD_ENV = "JARVIS_WAKE_MODEL_DOWNLOAD"


@dataclass(frozen=True)
class Wake:
    model: str
    score: float
    at: float


class WakeDetector:
    def __init__(self, *, model: str = DEFAULT_MODEL, threshold: float = DEFAULT_THRESHOLD,
                 refractory_s: float = REFRACTORY_S) -> None:
        self.model_name = model
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._buffer = bytearray()
        self._model = None
        self._error: str | None = None
        self._last_wake = 0.0
        self._lock = threading.RLock()          # the audio buffer
        self._load_lock = threading.Lock()      # fetching and building the model
        self._prepare_lock = threading.Lock()   # starting the background load once
        self._preparing: threading.Thread | None = None

    # --- availability --------------------------------------------------------

    @staticmethod
    def model_dir() -> Path:
        from ..store import data_dir
        return data_dir() / "wakeword"

    def _files(self) -> dict[str, Path]:
        base = self.model_dir()
        return {"wake": base / f"{self.model_name}.onnx",
                "melspec": base / "melspectrogram.onnx",
                "embedding": base / "embedding_model.onnx"}

    def load(self) -> bool:
        """Load the model, fetching its files first if this machine does not
        have them yet. Blocking — anything that must not wait calls `prepare()`.
        Safe to call repeatedly; returns whether it is usable.

        The download and the model's construction happen under their own lock,
        never the audio lock: `status()` and `prepare()` are called from the
        server's event loop, and once waited behind a download — which froze
        every request until it finished."""
        if self._model is not None:
            return True
        if self._error is not None:
            return False
        with self._load_lock:
            if self._model is not None or self._error is not None:
                return self._model is not None
            try:
                files = self._files()
                if not all(path.exists() for path in files.values()):
                    if os.environ.get(DOWNLOAD_ENV) != "1":
                        raise FileNotFoundError("the wake model is not on this machine yet")
                    from openwakeword.utils import download_models
                    self.model_dir().mkdir(parents=True, exist_ok=True)
                    download_models(model_names=[self.model_name.split("_v")[0]],
                                    target_directory=str(self.model_dir()))
                from openwakeword.model import Model  # imported here: a heavy import
                self._model = Model(wakeword_models=[str(files["wake"])],
                                    melspec_model_path=str(files["melspec"]),
                                    embedding_model_path=str(files["embedding"]),
                                    inference_framework="onnx")
                return True
            except Exception as err:  # noqa: BLE001
                # Includes the files not being reachable to download (no
                # network). Recorded once and reported, never retried on every
                # audio frame; a restart tries again.
                self._error = f"{err.__class__.__name__}: {err}"
                logger.warning("wake word unavailable: %s", self._error)
                return False

    def prepare(self) -> None:
        """Start loading in the background, once. Returns at once — it never
        waits on a load already running."""
        with self._prepare_lock:
            if self._model is not None or self._error is not None or self._preparing is not None:
                return
            self._preparing = threading.Thread(target=self.load, name="wake-word-prepare",
                                               daemon=True)
            self._preparing.start()

    @property
    def preparing(self) -> bool:
        thread = self._preparing
        return thread is not None and thread.is_alive() and self._model is None

    @property
    def available(self) -> bool:
        return self.load()

    def status(self) -> dict[str, object]:
        """What the detector can do right now, without waiting on it. A detector
        nobody has asked for yet starts getting ready here."""
        self.prepare()
        ready = self._model is not None
        return {
            "available": ready,
            "preparing": self.preparing,
            "model": self.model_name,
            "threshold": self.threshold,
            "sampleRate": SAMPLE_RATE,
            "error": self._error,
            # Said plainly, because "the wake word isn't working" and "the wake
            # word is off" are different problems with different fixes.
            "note": ("Listening happens on this machine; no audio leaves it." if ready
                     else "Wake word is getting ready — its model is being fetched once."
                     if self._error is None
                     else "Wake word is not available — its model could not be loaded."),
        }

    # --- detection -----------------------------------------------------------

    def feed(self, pcm: bytes, now: float | None = None) -> Wake | None:
        """Feed raw PCM. Returns a Wake the moment one is detected, else None.

        Chunks arrive at whatever size the transport happens to deliver, so they
        are buffered into the exact frame size the model expects rather than
        being padded or truncated — a padded frame is silence the model did not
        hear, and silently changes what it scores.
        """
        if not self.load():
            return None
        now = time.time() if now is None else now

        with self._lock:
            self._buffer.extend(pcm)
            detection: Wake | None = None

            while len(self._buffer) >= FRAME_BYTES:
                frame = bytes(self._buffer[:FRAME_BYTES])
                del self._buffer[:FRAME_BYTES]
                score = self._score(frame)
                if score < self.threshold:
                    continue
                if now - self._last_wake < self.refractory_s:
                    continue        # the same phrase still passing through
                self._last_wake = now
                detection = Wake(model=self.model_name, score=score, at=now)
                # Keep draining the buffer: dropping audio here would leave the
                # next detection scoring a discontinuous signal.
            return detection

    def _score(self, frame: bytes) -> float:
        import numpy as np

        samples = np.frombuffer(frame, dtype=np.int16)
        scores = self._model.predict(samples)  # type: ignore[union-attr]
        return float(max(scores.values())) if scores else 0.0

    def reset(self) -> None:
        """Forget buffered audio and the refractory window — for a new session."""
        with self._lock:
            self._buffer.clear()
            self._last_wake = 0.0
