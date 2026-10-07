'use client';

import { MicStream } from './mic-stream';
import { base64ToBytes } from './pcm';

/**
 * Listening for "Hey Jarvis" while nothing else is.
 *
 * The microphone's audio goes to this app's own server (`/api/voice/wake`),
 * which scores it with a small model on this machine — never further. The
 * server existed and was tested on its own, but nothing in the interface ever
 * opened it, so the wake word could not wake anything.
 *
 * Only one recognition can hold the microphone reliably, so the page stops this
 * the moment a voice session starts and starts it again when the session ends.
 * The server may still be fetching its model the first time (`preparing`): the
 * listener then tries again a little later instead of giving up.
 */
export type WakeStatus =
  | { kind: 'off' }
  | { kind: 'starting' }
  | { kind: 'listening' }
  | { kind: 'preparing'; note: string }
  | { kind: 'unavailable'; note: string }
  | { kind: 'blocked'; note: string };

export class WakeListener {
  private mic: MicStream | null = null;
  private socket: WebSocket | null = null;
  private retry: ReturnType<typeof setTimeout> | null = null;
  private wanted = false;

  constructor(private readonly handlers: {
    onWake: () => void;
    onStatus: (status: WakeStatus) => void;
  }) {}

  /** Idempotent: calling it while already listening does nothing. */
  start(): void {
    if (this.wanted) return;
    this.wanted = true;
    void this.open();
  }

  stop(): void {
    this.wanted = false;
    if (this.retry) clearTimeout(this.retry);
    this.retry = null;
    this.release();
    this.handlers.onStatus({ kind: 'off' });
  }

  private release(): void {
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    this.mic?.stop();
    this.mic = null;
  }

  private later(seconds: number): void {
    this.release();
    if (!this.wanted) return;
    this.retry = setTimeout(() => {
      this.retry = null;
      if (this.wanted) void this.open();
    }, seconds * 1000);
  }

  private async open(): Promise<void> {
    this.handlers.onStatus({ kind: 'starting' });
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${scheme}://${window.location.host}/api/voice/wake`);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.onmessage = async (event) => {
      if (socket !== this.socket) return;
      let message: { type?: string; note?: string };
      try {
        message = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (message.type === 'ready') {
        try {
          const mic = new MicStream();
          await mic.start();
          if (socket !== this.socket) {
            mic.stop();
            return;
          }
          this.mic = mic;
          mic.onFrame = (frame) => {
            if (socket.readyState === WebSocket.OPEN) socket.send(base64ToBytes(frame));
          };
          this.handlers.onStatus({ kind: 'listening' });
        } catch {
          this.wanted = false;
          this.release();
          this.handlers.onStatus({ kind: 'blocked',
            note: 'The microphone is blocked, so the wake word cannot hear you.' });
        }
      } else if (message.type === 'wake') {
        this.handlers.onWake();
      } else if (message.type === 'preparing') {
        this.handlers.onStatus({ kind: 'preparing', note: message.note ?? 'The wake word is getting ready.' });
        this.later(10);
      } else if (message.type === 'unavailable') {
        this.wanted = false;
        this.release();
        this.handlers.onStatus({ kind: 'unavailable', note: message.note ?? 'The wake word is not available.' });
      }
    };
    socket.onclose = () => {
      // A server restart or a dropped connection: try again shortly rather
      // than going deaf without saying so.
      if (socket === this.socket && this.wanted) this.later(5);
    };
  }
}
