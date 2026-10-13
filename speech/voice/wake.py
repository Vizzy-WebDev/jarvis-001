"""Wake-word detection, on this machine (§15).

Every wake phrase in the person's list is recognised locally by openWakeWord — one small
ONNX model per phrase, all loaded into ONE openWakeWord model and scored together on the
CPU. Nothing is sent anywhere to decide whether a wake phrase was said, which matters more
here than almost anywhere else in the system: a wake word means a microphone is always
listening, and the only acceptable answer to "where does that audio go?" is "nowhere".

**One model per phrase.** openWakeWord hears exactly the phrases it has a trained model
for. A phrase's model is found in `data/wakeword/` by name: "Wake up Jarvis" is
`wake_up_jarvis.onnx` (or `wake_up_jarvis_v<n>.onnx`, the pretrained models' naming). The
pretrained ones openWakeWord publishes ("Hey Jarvis", "Alexa", …) are fetched once, in the
background, the first time a list holding them is listened for; any other phrase needs a
model trained for it (openWakeWord's own training notebook) and added through
`add_model()`. Until then the phrase is kept and reported as needing its model — never
claimed to work. Only listed phrases count: a model on disk for a phrase that is no longer
in the list is not loaded.

**What is honest about this module when no model is usable.** `available` is False and
`feed()` returns None forever — it never pretends, and never silently degrades into "wakes
on any noise", which would be far worse than not working. `status()` says which it is,
including "still getting ready", per phrase.

Audio contract: 16 kHz, 16-bit signed little-endian, mono. That is what the models were
trained on; resampling belongs to whatever captures the audio, not here.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
#: openWakeWord's own frame size: 1280 samples = 80 ms at 16 kHz.
FRAME_SAMPLES = 1280
FRAME_BYTES = FRAME_SAMPLES * 2

DEFAULT_PHRASES = ("Hey Jarvis", "Hello Jarvis", "Wake up Jarvis", "Jarvis wake up")
#: Kept for callers that name the one pretrained model directly.
DEFAULT_MODEL = "hey_jarvis_v0.1"
#: Above this score, the wake phrase was said. The pretrained models' own recommended
#: operating point; a lower value trades false wakes for sensitivity.
DEFAULT_THRESHOLD = 0.5
#: After a wake, ignore further detections for this long. Without it a single phrase
#: fires several times as it passes through the window, and the assistant appears to
#: wake up three times to one greeting.
REFRACTORY_S = 2.0
#: Fetching a model reaches the network, so it is an interlock like every other thing
#: Jarvis does on its own: off unless `main()` (the real launch) turns it on. A test that
#: asks for a detector never downloads anything.
DOWNLOAD_ENV = "JARVIS_WAKE_MODEL_DOWNLOAD"
#: The phrase models openWakeWord publishes, by the name its downloader takes. Others
#: ("timer", "weather") are commands, not wake phrases.
PRETRAINED = ("hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy")
#: What every phrase model takes in: 16 frames of 96 audio features.
MODEL_INPUT_SHAPE = [1, 16, 96]


@dataclass(frozen=True)
class Wake:
    model: str
    score: float
    at: float
    phrase: str = ""


def _openwakeword_model():
    """openWakeWord's `Model`, imported without scikit-learn.

    The package's `__init__` imports its custom-verifier TRAINING helper, which imports
    scikit-learn — about half of the whole import (measured: 14 s with it, 6.9 s without, on
    this laptop under load), paid before the wake word can listen, for a feature Jarvis
    never uses (`Model` itself does not import it). A stand-in module takes its place; if a
    later openWakeWord needs more of it, the import fails and the wake word reports itself
    unavailable rather than misbehaving."""
    import sys
    import types

    name = "openwakeword.custom_verifier_model"
    if name not in sys.modules:
        stand_in = types.ModuleType(name)

        def train_custom_verifier(*_args, **_kwargs):
            raise RuntimeError("openWakeWord's verifier training is not part of Jarvis")

        stand_in.train_custom_verifier = train_custom_verifier  # type: ignore[attr-defined]
        sys.modules[name] = stand_in
    from openwakeword.model import Model
    return Model


def slug(phrase: str) -> str:
    """A phrase as a model file's name: "Wake up Jarvis" -> "wake_up_jarvis"."""
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


class WakeDetector:
    def __init__(self, *, phrases: tuple[str, ...] | list[str] = DEFAULT_PHRASES,
                 threshold: float = DEFAULT_THRESHOLD, refractory_s: float = REFRACTORY_S,
                 model: str | None = None) -> None:
        # `model` is the old single-model form: listen for that one pretrained model.
        self.phrases: tuple[str, ...] = (tuple(phrases) if model is None
                                         else (model.split("_v")[0].replace("_", " "),))
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._buffer = bytearray()
        self._model = None
        #: openWakeWord's prediction key (the model file's stem) -> the phrase it hears.
        self._keys: dict[str, str] = {}
        #: The model files the loaded model was built from, to know when to rebuild.
        self._loaded_files: tuple[str, ...] = ()
        self._error: str | None = None
        self._last_wake = 0.0
        self._lock = threading.RLock()          # the audio buffer and the model in use
        self._load_lock = threading.Lock()      # fetching and building the model
        self._prepare_lock = threading.Lock()   # starting the background load once
        self._preparing: threading.Thread | None = None

    @property
    def model_name(self) -> str:
        return ", ".join(self._keys) or DEFAULT_MODEL

    # --- which files hear which phrase ----------------------------------------

    @staticmethod
    def model_dir() -> Path:
        from ..store import data_dir
        return data_dir() / "wakeword"

    def _feature_files(self) -> dict[str, Path]:
        base = self.model_dir()
        return {"melspec": base / "melspectrogram.onnx", "embedding": base / "embedding_model.onnx"}

    def model_file(self, phrase: str) -> Path | None:
        """The model on this machine that hears `phrase`, if there is one. A trained file
        named exactly after the phrase wins over a versioned pretrained one."""
        base, name = self.model_dir(), slug(phrase)
        exact = base / f"{name}.onnx"
        if exact.exists():
            return exact
        versions = sorted(base.glob(f"{name}_v*.onnx")) if base.exists() else []
        return versions[-1] if versions else None

    def _downloadable(self, phrase: str) -> bool:
        return slug(phrase) in PRETRAINED and os.environ.get(DOWNLOAD_ENV) == "1"

    def set_phrases(self, phrases: list[str] | tuple[str, ...]) -> None:
        """Listen for this list from now on. Rebuilds the model only when the set of model
        files it needs changed — in the background, never on the caller's thread."""
        cleaned = tuple(dict.fromkeys(p.strip() for p in phrases if p and p.strip()))
        with self._lock:
            if cleaned == self.phrases:
                return
            self.phrases = cleaned
            wanted = self._wanted_files()
            if wanted != self._loaded_files or self._model is None:
                self._model = None
                self._keys = {}
                self._loaded_files = ()
                self._error = None
                self._buffer.clear()
        self.prepare()

    def _wanted_files(self) -> tuple[str, ...]:
        files = {str(f) for p in self.phrases if (f := self.model_file(p)) is not None}
        return tuple(sorted(files))

    # --- availability --------------------------------------------------------

    def load(self) -> bool:
        """Build the model from every listed phrase that has one, fetching published ones
        and the two shared feature models first if this machine lacks them. Blocking —
        anything that must not wait calls `prepare()`. Returns whether it is usable.

        The download and the model's construction happen under their own lock, never the
        audio lock: `status()` and `prepare()` are called from the server's event loop, and
        once waited behind a download — which froze every request until it finished."""
        if self._model is not None:
            return True
        if self._error is not None:
            return False
        with self._load_lock:
            if self._model is not None or self._error is not None:
                return self._model is not None
            try:
                self._fetch_missing()
                pairs = [(p, f) for p in self.phrases if (f := self.model_file(p)) is not None]
                if not pairs:
                    raise FileNotFoundError("none of the wake phrases has its model on this machine yet")
                features = self._feature_files()
                if not all(path.exists() for path in features.values()):
                    raise FileNotFoundError("the wake word's shared models are not on this machine yet")
                Model = _openwakeword_model()
                files = list(dict.fromkeys(str(f) for _, f in pairs))
                model = Model(wakeword_models=files,
                              melspec_model_path=str(features["melspec"]),
                              embedding_model_path=str(features["embedding"]),
                              inference_framework="onnx")
                keys = {Path(f).stem: p for p, f in reversed(pairs)}
                with self._lock:
                    self._keys = {k: keys[k] for k in model.models if k in keys}
                    self._loaded_files = tuple(sorted(files))
                    self._model = model
                return True
            except Exception as err:  # noqa: BLE001
                # Includes the files not being reachable to download (no network). Recorded
                # once and reported, never retried on every audio frame; a changed phrase
                # list or a restart tries again.
                self._error = f"{err.__class__.__name__}: {err}"
                logger.warning("wake word unavailable: %s", self._error)
                return False

    def _fetch_missing(self) -> None:
        missing = [slug(p) for p in self.phrases
                   if self.model_file(p) is None and self._downloadable(p)]
        features_missing = not all(f.exists() for f in self._feature_files().values())
        if not missing and not (features_missing and os.environ.get(DOWNLOAD_ENV) == "1"):
            return
        self.model_dir().mkdir(parents=True, exist_ok=True)
        _openwakeword_model()   # the package without scikit-learn, before anything else imports it
        if missing:
            # Also fetches the feature models when they are missing.
            from openwakeword.utils import download_models
            download_models(model_names=missing, target_directory=str(self.model_dir()))
        else:
            import openwakeword
            from openwakeword.utils import download_file
            for feature in openwakeword.FEATURE_MODELS.values():
                url = feature["download_url"].replace(".tflite", ".onnx")
                if not (self.model_dir() / url.split("/")[-1]).exists():
                    download_file(url, str(self.model_dir()))

    def prepare(self) -> None:
        """Start loading in the background, once. Returns at once — it never waits on a
        load already running."""
        with self._prepare_lock:
            if self._error is not None and self._error.startswith("FileNotFoundError")                     and self._wanted_files() and all(f.exists() for f in self._feature_files().values()):
                # It found no model before, and one is here now (copied in, or a download
                # another detector finished): try again rather than waiting for a restart.
                self._error = None
            if self._model is not None or self._error is not None:
                return
            if self._preparing is not None and self._preparing.is_alive():
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

    def phrase_states(self) -> list[dict[str, str]]:
        """Each listed phrase and whether Jarvis can hear it: `ready` (its model is
        listening), `loading` (its model is on the way or being loaded) or `needs_model`."""
        heard = set(self._keys.values()) if self._model is not None else set()
        out = []
        for phrase in self.phrases:
            if phrase in heard:
                state = "ready"
            elif self.model_file(phrase) is not None or self._downloadable(phrase):
                state = "loading" if self._error is None else "needs_model"
            else:
                state = "needs_model"
            out.append({"phrase": phrase, "state": state})
        return out

    def status(self) -> dict[str, object]:
        """What the detector can do right now, without waiting on it. A detector nobody has
        asked for yet starts getting ready here."""
        self.prepare()
        ready = self._model is not None
        phrases = self.phrase_states()
        none_possible = not any(p["state"] != "needs_model" for p in phrases)
        return {
            "available": ready,
            "preparing": self.preparing,
            "model": self.model_name,
            "phrases": phrases,
            "threshold": self.threshold,
            "sampleRate": SAMPLE_RATE,
            "error": self._error,
            # Said plainly, because "the wake word isn't working" and "the wake word is
            # off" are different problems with different fixes.
            "note": ("Listening happens on this machine; no audio leaves it." if ready
                     else "Wake word is getting ready — its model is being fetched once."
                     if self.preparing
                     else "Wake word is not available — none of your wake phrases has its model yet."
                     if none_possible
                     else "Wake word is not available — its model could not be loaded."
                     if self._error is not None
                     else "Wake word is getting ready — its model is being fetched once."),
        }

    # --- adding a phrase's model ---------------------------------------------

    def add_model(self, name: str, data: bytes) -> str:
        """Keep a trained phrase model (an openWakeWord `.onnx` file) and listen with it.
        Returns the phrase it hears, from its file name. Raises ValueError, in words a
        person can act on, for anything that is not such a model."""
        stem = slug(Path(name).stem.replace(".", "_"))
        stem = re.sub(r"_v\d+(_\d+)*$", "", stem) if stem.split("_v")[0] in PRETRAINED else stem
        if not stem:
            raise ValueError("That file has no usable name. Name it after its phrase, like hello_jarvis.onnx.")
        try:
            import onnxruntime as ort
            session = ort.InferenceSession(data, providers=["CPUExecutionProvider"])
            shape = session.get_inputs()[0].shape
        except Exception as err:  # noqa: BLE001
            raise ValueError("That file is not a model Jarvis can open.") from err
        if list(shape) != MODEL_INPUT_SHAPE:
            raise ValueError("That file is a model, but not an openWakeWord wake-phrase model.")
        self.model_dir().mkdir(parents=True, exist_ok=True)
        target = self.model_dir() / f"{stem}.onnx"
        part = target.with_suffix(".part")
        part.write_bytes(data)
        os.replace(part, target)
        with self._lock:
            self._model = None
            self._keys = {}
            self._loaded_files = ()
            self._error = None
            self._buffer.clear()
        self.prepare()
        return stem.replace("_", " ")

    # --- detection -----------------------------------------------------------

    def feed(self, pcm: bytes, now: float | None = None) -> Wake | None:
        """Feed raw PCM. Returns a Wake the moment one is detected, else None.

        Chunks arrive at whatever size the transport happens to deliver, so they are
        buffered into the exact frame size the model expects rather than being padded or
        truncated — a padded frame is silence the model did not hear, and silently changes
        what it scores.
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
                if self._model is None:      # rebuilt for a new list mid-stream
                    continue
                key, score = self._best(frame)
                if score < self.threshold:
                    continue
                if now - self._last_wake < self.refractory_s:
                    continue        # the same phrase still passing through
                self._last_wake = now
                detection = Wake(model=key, score=score, at=now, phrase=self._keys.get(key, key))
                # Keep draining the buffer: dropping audio here would leave the next
                # detection scoring a discontinuous signal.
            return detection

    def _best(self, frame: bytes) -> tuple[str, float]:
        """The phrase model scoring highest on this frame, and its score."""
        import numpy as np

        samples = np.frombuffer(frame, dtype=np.int16)
        scores = self._model.predict(samples)  # type: ignore[union-attr]
        if not scores:
            return "", 0.0
        key = max(scores, key=lambda k: scores[k])
        return key, float(scores[key])

    def reset(self) -> None:
        """Forget buffered audio and the refractory window — for a new session."""
        with self._lock:
            self._buffer.clear()
            self._last_wake = 0.0
