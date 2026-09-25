"""Wake-word detection, on this machine (§15).

"Hey Jarvis" is recognised locally by openWakeWord — a small ONNX model that
runs on the CPU. Nothing is sent anywhere to decide whether the user said the
wake word, which matters more here than almost anywhere else in the system: a
wake word means a microphone is always listening, and the only acceptable answer
to "where does that audio go?" is "nowhere".

**What is honest about this module when the model is missing.** openWakeWord
downloads its models on first use. If the package or the model is unavailable,
`WakeDetector.available` is False and `feed()` returns None forever — it never
pretends, and never silently degrades into "wakes on any noise", which would be
far worse than not working. `status()` says which it is.

Audio contract: 16 kHz, 16-bit signed little-endian, mono. That is what the model
was trained on; resampling belongs to whatever captures the audio, not here.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

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
        self._lock = threading.RLock()

    # --- availability --------------------------------------------------------

    def load(self) -> bool:
        """Load the model. Safe to call repeatedly; returns whether it is usable."""
        with self._lock:
            if self._model is not None:
                return True
            if self._error is not None:
                return False
            try:
                from openwakeword.model import Model  # imported here: a heavy import
                self._model = Model(wakeword_models=[self.model_name],
                                    inference_framework="onnx")
                return True
            except Exception as err:  # noqa: BLE001
                # Includes the model simply not being downloaded yet. Recorded
                # once and reported, never retried on every audio frame.
                self._error = f"{err.__class__.__name__}: {err}"
                logger.warning("wake word unavailable: %s", self._error)
                return False

    @property
    def available(self) -> bool:
        return self.load()

    def status(self) -> dict[str, object]:
        return {
            "available": self.available,
            "model": self.model_name,
            "threshold": self.threshold,
            "sampleRate": SAMPLE_RATE,
            "error": self._error,
            # Said plainly, because "the wake word isn't working" and "the wake
            # word is off" are different problems with different fixes.
            "note": ("Listening happens on this machine; no audio leaves it."
                     if self._error is None else
                     "Wake word is not available — the model could not be loaded."),
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
