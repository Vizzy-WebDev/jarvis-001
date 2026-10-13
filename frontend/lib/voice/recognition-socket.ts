'use client';

/**
 * The one way the browser gets speech turned into text: microphone frames go to this
 * app's own server (`/api/duplex`), which recognises them on this computer (Moonshine) or
 * through the cloud service the person chose — never the browser's built-in recognition,
 * which sent the audio to Google from the page and opened a second, unprocessed capture of
 * the microphone.
 *
 * Both voice modes use it, so they hear the same way and differ only in how they take turns.
 * Messages (from `routes/realtime.py`): `downloading` / `loading` while the on-device model is
 * being fetched or opened (this can take minutes the first time, so the ready timeout only
 * counts while nothing at all is heard from the server), `ready`, then `speech_started`,
 * `transcript` and `utterance_end`, or `error`.
 */

export interface RecognitionProgress {
  kind: 'downloading' | 'loading';
  label?: string;
  done?: number;
  total?: number;
}

export interface RecognitionHandlers {
  onTranscript: (text: string, isFinal: boolean, speechFinal: boolean, confidence?: number) => void;
  onSpeechStarted?: () => void;
  onUtteranceEnd?: () => void;
  onProgress?: (progress: RecognitionProgress) => void;
  /** `fatal`: nothing will be recognised on this connection (no key, nothing installed). */
  onError: (message: string, fatal: boolean) => void;
  /** The connection dropped after it was working. */
  onLost?: () => void;
}

interface Message {
  type?: string;
  mode?: string;
  text?: string;
  isFinal?: boolean;
  speechFinal?: boolean;
  confidence?: number | null;
  error?: string;
  fatal?: boolean;
  label?: string;
  done?: number;
  total?: number;
}

/** Silence from the server for this long, before it is ready, is a failure. */
const QUIET_TIMEOUT_MS = 10_000;

export class RecognitionSocket {
  private socket: WebSocket | null = null;
  private ready = false;
  /** Which recogniser answered (`local`, or a cloud service's name). */
  mode: string | null = null;

  constructor(private readonly handlers: RecognitionHandlers) {}

  /** Resolves true once recognition is genuinely running, false on any failure (already
   *  reported through `onError`). */
  connect(): Promise<boolean> {
    return new Promise((resolve) => {
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${scheme}://${location.host}/api/duplex`);
      this.socket = socket;
      let settled = false;
      let quiet: ReturnType<typeof setTimeout> | null = null;
      const settle = (ok: boolean) => {
        if (settled) return;
        settled = true;
        if (quiet) clearTimeout(quiet);
        resolve(ok);
      };
      const armQuiet = () => {
        if (quiet) clearTimeout(quiet);
        quiet = setTimeout(() => {
          this.handlers.onError('Could not reach Jarvis.', true);
          settle(false);
          this.close();
        }, QUIET_TIMEOUT_MS);
      };
      armQuiet();

      socket.onmessage = (event) => {
        if (socket !== this.socket) return;
        let message: Message;
        try {
          message = JSON.parse(String(event.data));
        } catch {
          return;
        }
        switch (message.type) {
          case 'downloading':
          case 'loading':
            armQuiet();
            this.handlers.onProgress?.({ kind: message.type, label: message.label,
                                         done: message.done, total: message.total });
            break;
          case 'ready':
            this.ready = true;
            this.mode = message.mode ?? null;
            settle(true);
            break;
          case 'speech_started':
            this.handlers.onSpeechStarted?.();
            break;
          case 'transcript':
            if (message.text) {
              this.handlers.onTranscript(message.text, Boolean(message.isFinal), Boolean(message.speechFinal),
                                         typeof message.confidence === 'number' ? message.confidence : undefined);
            }
            break;
          case 'utterance_end':
            this.handlers.onUtteranceEnd?.();
            break;
          case 'error':
            this.handlers.onError(message.error ?? 'Speech recognition stopped working.',
                                  Boolean(message.fatal) || !this.ready);
            settle(false);
            break;
          default:
            break;
        }
      };
      socket.onerror = () => {
        if (socket !== this.socket) return;
        if (!settled) this.handlers.onError('Could not reach Jarvis.', true);
        settle(false);
      };
      socket.onclose = () => {
        if (socket !== this.socket) return;
        this.socket = null;
        if (!settled) {
          this.handlers.onError('Could not reach Jarvis.', true);
          settle(false);
        } else if (this.ready) {
          this.ready = false;
          this.handlers.onLost?.();
        }
      };
    });
  }

  /** One microphone frame (16 kHz 16-bit mono, base64) — dropped unless running. */
  send(frame: string): void {
    if (this.ready && this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: 'audio', data: frame }));
    }
  }

  /** Deliberate, so never reported as a lost connection. */
  close(): void {
    const socket = this.socket;
    this.socket = null;
    this.ready = false;
    if (!socket) return;
    try {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'end' }));
      socket.close();
    } catch {
      // already closed
    }
  }
}

/** "Getting speech recognition ready — 34%", for the status line. */
export function describeProgress(progress: RecognitionProgress): string {
  if (progress.kind === 'loading') return 'Getting speech recognition ready…';
  const pct = progress.total ? Math.floor(((progress.done ?? 0) / progress.total) * 100) : null;
  return `Downloading ${progress.label ?? 'the speech model'} (once only)${pct !== null ? ` — ${pct}%` : '…'}`;
}
