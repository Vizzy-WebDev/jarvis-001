// One real, owned microphone capture for the duplex engine — resampled to
// 16kHz PCM16 and handed to a caller-supplied frame callback (the duplex
// engine decides whether those frames go to Deepgram over the WS, or
// nowhere at all in the browser-STT fallback mode).
//
// This is the whole reason the self-listening dance in pipeline-engine.js
// (suspend recognition the instant Jarvis starts talking, resume 700ms after
// it stops, three layers of echo-detection belt-and-braces) doesn't exist
// here: that dance exists ONLY because Chrome's SpeechRecognition opens its
// OWN separate, unprocessed mic capture that `echoCancellation: true` on
// getUserMedia() never reaches (see public/CLAUDE.md). Here there is exactly
// ONE MediaStream, captured with the browser's real echo cancellation, and
// Deepgram receives it continuously — including while Jarvis is speaking —
// the same way live-engine.js already does for Gemini Live. Real AEC
// handles the echo; nothing needs to be muted to prevent it.
//
// PCM conversion mirrors live-engine.js's resampleTo16k/floatTo16BitPCM
// (same standard Web Audio technique, not Gemini-specific) rather than
// importing from that file directly — kept local and pure so this module
// has no dependency on anything Gemini-Live-specific.

const TARGET_SAMPLE_RATE = 16000; // required by both Deepgram (configured in server/stt/deepgram.js) and Gemini Live

function resampleTo16k(float32Input, inputSampleRate) {
  if (inputSampleRate === TARGET_SAMPLE_RATE) return float32Input;
  const ratio = inputSampleRate / TARGET_SAMPLE_RATE;
  const outputLength = Math.round(float32Input.length / ratio);
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i++) {
    output[i] = float32Input[Math.floor(i * ratio)];
  }
  return output;
}

function floatTo16BitPCM(float32) {
  const int16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return int16;
}

function arrayBufferToBase64(buffer) {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

/** RMS of a Float32Array in [-1, 1] — same maths as turn-detector.js's MicLevelMonitor, for the raw processor-frame source instead of an AnalyserNode. */
function rmsOf(float32) {
  let sumSquares = 0;
  for (const sample of float32) sumSquares += sample * sample;
  return Math.sqrt(sumSquares / float32.length);
}

export class MicStream {
  constructor() {
    this.stream = null;
    this.context = null;
    this.sourceNode = null;
    this.processorNode = null;
    this._level = 0;
    /** (base64PCM16) => void, set by the caller once capture starts. Called on every ~4096-sample frame regardless of loudness — the caller decides what to do with silence. */
    this.onFrame = null;
  }

  /** Requests the mic and starts capture. Throws if permission is denied or no device exists — caller shows the message. */
  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });

    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    this.context = new AudioContextClass();
    this.sourceNode = this.context.createMediaStreamSource(this.stream);

    // ScriptProcessorNode is deprecated but universally supported — same
    // choice live-engine.js already made for the identical job, and the
    // same "revisit if a browser ever drops it" note applies here.
    this.processorNode = this.context.createScriptProcessor(4096, 1, 1);
    this.processorNode.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      this._level = rmsOf(input);
      if (!this.onFrame) return;
      const resampled = resampleTo16k(input, this.context.sampleRate);
      const pcm16 = floatTo16BitPCM(resampled);
      this.onFrame(arrayBufferToBase64(pcm16.buffer));
    };

    this.sourceNode.connect(this.processorNode);
    // Required by some browsers to keep a ScriptProcessorNode running; does
    // not route mic audio to the speakers (see live-engine.js's identical note).
    this.processorNode.connect(this.context.destination);
  }

  /** For the orb / VAD: current mic input energy, 0..1. */
  getLevel() {
    return this._level;
  }

  /** The raw MediaStream, for anything that needs its own tap (e.g. vad.js's MicLevelMonitor, which does its own AnalyserNode-based RMS independent of this class's onaudioprocess-based one — two different, already-proven techniques, not worth unifying). */
  getRawStream() {
    return this.stream;
  }

  stop() {
    this.onFrame = null;
    if (this.processorNode) {
      this.processorNode.disconnect();
      this.processorNode = null;
    }
    if (this.sourceNode) {
      this.sourceNode.disconnect();
      this.sourceNode = null;
    }
    if (this.context) {
      this.context.close().catch(() => {});
      this.context = null;
    }
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
  }
}
