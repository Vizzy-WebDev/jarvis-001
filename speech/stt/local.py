"""Speech to text on this computer: Silero finds the speech, Moonshine writes it down.

The on-device half of the recognition seam, beside the cloud provider (`deepgram.py`). Audio
arrives as 16 kHz 16-bit mono PCM — the same frames the browser sends every recogniser — and
never leaves this machine. Moonshine is not a streaming model: the speech detector cuts the
audio into utterances, and each finished one is transcribed whole (measured on this laptop:
Base ~230 ms per utterance, Tiny ~100 ms). So words arrive when the person pauses, not while
they are still talking.

**A session speaks the same canonical events as the cloud relay** (`routes/realtime.py`):
`speech_started`, `transcript` (`isFinal`/`speechFinal`) and `utterance_end`. The engines in
the browser cannot tell which recogniser they are talking to, which is the point.

**Memory.** The models are loaded on first use (~190 MB for Base) and released again after
`IDLE_UNLOAD_S` with no session open, so a laptop that is not being spoken to gets it back.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
#: Silero's own window: 512 samples (32 ms) at 16 kHz.
VAD_WINDOW = 512
#: Silence that ends an utterance. The engines add their own wait for a trailing thought
#: (turn-detector.ts), so this only has to say "a pause happened".
MIN_SILENCE_S = 0.5
MIN_SPEECH_S = 0.25
#: Quiet after the last words before `utterance_end` ("they have finished") — the same
#: 1 s the cloud service is asked for (deepgram.SESSION_PARAMS), so Full duplex takes
#: turns the same way whichever recogniser hears.
UTTERANCE_END_S = 1.0
#: An utterance longer than this is cut and transcribed, so one long ramble still arrives.
MAX_SPEECH_S = 20.0
IDLE_UNLOAD_S = 10 * 60
DEFAULT_MODEL = "moonshine-base-en"
MODELS = ("moonshine-base-en", "moonshine-tiny-en")


class _Shared:
    """One loaded recogniser for the process, whichever session asks."""

    def __init__(self) -> None:
        self.lock = threading.Lock()          # loading and decoding
        self.recognizer = None
        self.model_id: str | None = None
        self.sessions = 0
        self.last_used = time.monotonic()
        self.load_seconds: float | None = None

    def recognizer_for(self, model_id: str):
        import sherpa_onnx

        from .. import speech_models

        with self.lock:
            if self.recognizer is not None and self.model_id == model_id:
                return self.recognizer
            started = time.perf_counter()
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
                encoder=speech_models.path(model_id, "encoder_model.ort"),
                decoder=speech_models.path(model_id, "decoder_model_merged.ort"),
                tokens=speech_models.path(model_id, "tokens.txt"),
                num_threads=2,
            )
            self.model_id = model_id
            self.load_seconds = round(time.perf_counter() - started, 2)
            logger.info("on-device recognition: %s loaded in %.1fs", model_id, self.load_seconds)
            return self.recognizer

    def transcribe(self, model_id: str, samples) -> str:
        recognizer = self.recognizer_for(model_id)
        with self.lock:
            stream = recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            recognizer.decode_stream(stream)
            self.last_used = time.monotonic()
            return stream.result.text.strip()

    def release_if_idle(self) -> bool:
        with self.lock:
            if (self.recognizer is not None and self.sessions == 0
                    and time.monotonic() - self.last_used >= IDLE_UNLOAD_S):
                self.recognizer = None
                self.model_id = None
                return True
        return False


_shared = _Shared()


def ready_models() -> dict[str, Any]:
    """What on-device recognition needs and whether it is here."""
    from .. import speech_models

    return {"vad": speech_models.status("silero-vad"),
            "models": {m: speech_models.status(m) for m in MODELS}}


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return True


class LocalSession:
    """One person speaking: PCM in through `send()`, canonical events out by iterating."""

    def __init__(self, model_id: str = DEFAULT_MODEL) -> None:
        import sherpa_onnx

        from .. import speech_models

        self.model_id = model_id
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = speech_models.path("silero-vad", "silero_vad.onnx")
        config.silero_vad.min_silence_duration = MIN_SILENCE_S
        config.silero_vad.min_speech_duration = MIN_SPEECH_S
        config.silero_vad.max_speech_duration = MAX_SPEECH_S
        config.silero_vad.window_size = VAD_WINDOW
        config.sample_rate = SAMPLE_RATE
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)
        self._pending = None            # samples not yet a full window
        self._speaking = False
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._closed = False
        #: The last utterance being transcribed: each waits for the one before, so words
        #: arrive in the order they were said while new audio keeps flowing in meanwhile.
        self._tail: asyncio.Task | None = None
        #: Quiet samples since the last utterance was cut, or None when no end is owed.
        self._quiet: int | None = None
        _shared.sessions += 1
        #: Time from the end of an utterance's audio to its words, per utterance — kept for
        #: the latency report, newest last.
        self.decode_ms: list[int] = []

    async def warm(self) -> None:
        """Load the recogniser now, so the first utterance is not also paying for that."""
        await asyncio.to_thread(_shared.recognizer_for, self.model_id)

    async def send(self, pcm: bytes) -> None:
        import numpy as np

        if self._closed:
            return
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if self._pending is not None:
            samples = np.concatenate([self._pending, samples])
        whole = len(samples) - len(samples) % VAD_WINDOW
        self._pending = samples[whole:] if whole < len(samples) else None
        for start in range(0, whole, VAD_WINDOW):
            self._vad.accept_waveform(samples[start:start + VAD_WINDOW])
        speaking = self._vad.is_speech_detected()
        if speaking and not self._speaking:
            await self._events.put({"type": "speech_started"})
        self._speaking = speaking
        cut = False
        while not self._vad.empty():
            segment = np.array(self._vad.front.samples, dtype=np.float32)
            self._vad.pop()
            self._tail = asyncio.create_task(self._transcribe(segment, self._tail))
            cut = True
        if speaking:
            self._quiet = None                      # still talking: no end owed yet
        elif cut:
            self._quiet = int(MIN_SILENCE_S * SAMPLE_RATE)   # the detector waited this long
        elif self._quiet is not None:
            self._quiet += whole
            if self._quiet >= UTTERANCE_END_S * SAMPLE_RATE:
                self._quiet = None
                self._tail = asyncio.create_task(self._end(self._tail))

    async def _transcribe(self, segment, before: asyncio.Task | None) -> None:
        if before is not None:
            await asyncio.gather(before, return_exceptions=True)
        started = time.perf_counter()
        try:
            text = await asyncio.to_thread(_shared.transcribe, self.model_id, segment)
        except Exception:  # noqa: BLE001 — one bad utterance must not end the session
            logger.exception("on-device recognition failed on one utterance")
            return
        self.decode_ms.append(round((time.perf_counter() - started) * 1000))
        if text:
            await self._events.put({"type": "transcript", "text": text, "isFinal": True,
                                    "speechFinal": True, "confidence": None})

    async def _end(self, before: asyncio.Task | None) -> None:
        """After the words it follows, never before them."""
        if before is not None:
            await asyncio.gather(before, return_exceptions=True)
        await self._events.put({"type": "utterance_end"})

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        event = await self._events.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._tail is not None:
            await asyncio.gather(self._tail, return_exceptions=True)
        _shared.sessions = max(0, _shared.sessions - 1)
        _shared.last_used = time.monotonic()
        await self._events.put(None)
        timer = threading.Timer(IDLE_UNLOAD_S + 1, _shared.release_if_idle)
        timer.daemon = True         # never what keeps the process alive at shutdown
        timer.start()


def reset_for_tests() -> None:
    global _shared
    _shared = _Shared()
