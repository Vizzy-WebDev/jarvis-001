'use client';

/**
 * The contract every voice engine keeps, so the interface can drive whichever
 * one is running without knowing or caring which it is.
 *
 * Methods: `start` (take the mic and wire up), `stop` (release it), `sendText`
 * (a typed message through the same path as speech), `interrupt` (stop it
 * mid-reply), `setMuted` (input only — it never touches the turn in progress).
 *
 * Events are listed on `EngineEvents` below. Two are worth reading before
 * changing anything: `tts_failure` is NOT `error`, because the reply itself
 * generated and rendered fine and only its audio failed — reporting it as an
 * error would clear the reply the user is reading. And `reaction` is handled by
 * the engines themselves rather than passed up, since what it needs is a sound
 * queued at the right place in the speech.
 *
 * `error` carries which KIND of failure it is, because the two need opposite
 * handling and conflating them is what made a failed spoken turn vanish:
 *   - `turn`  — the model or its connection failed to answer THIS turn. The
 *               engine is fine and says so itself by going back to listening, so
 *               the interface must show the failure in the transcript and leave
 *               the microphone alone. Tearing the session down here is the bug.
 *   - `fatal` — the device, the permission or the browser's support itself
 *               failed: there is nothing left to listen with, so the engine must
 *               be stopped for real rather than merely believed to be stopped.
 *   - neither — a recoverable recognition problem the browser retries on its
 *               own. Untagged on purpose: the interface's default is to stop,
 *               which is exactly today's behaviour for these, unchanged.
 */
export type EngineState =
  | 'idle' | 'listening' | 'thinking' | 'speaking'
  | 'hearing_speech' | 'tool_running' | 'interrupted';

export interface EngineEvents {
  state: { state: EngineState };
  /** The live transcript as someone speaks. */
  transcript: { text: string; final: boolean };
  chunk: { text: string };
  tool: { name: string };
  tool_result: { name: string; ok: boolean; summary?: string;
                 needs_confirmation?: boolean; ui_action?: unknown };
  model_switch: { from: string | null; to: string; reason: string };
  /** Debug only: a style floor fired this turn. Never shown unless asked for. */
  style_floors: { floors: string[]; sticky?: string };
  reaction: { kind: string };
  /** Clear what is shown and spoken; a fresh reply follows on the same stream. */
  restart: Record<string, never>;
  /** Landed on the browser's own recognition instead of a proxied provider. */
  stt_fallback: { mode: string };
  /** Nothing could complete the turn — nothing was answered. */
  paused: { reason: string };
  /** The configured voice failed to produce audio, every retry spent. The reply
   *  itself is fine: this is deliberately not an error. */
  tts_failure: { provider?: string };
  done: { text: string };
  /** The turn stopped to ask the person's go-ahead for something (a tool set to
   *  "Need approval", say). The stream ends right after it, with no `done` of
   *  its own — without handling it, a spoken turn that asked read as a dropped
   *  connection and the question was never shown. A `done` follows from the
   *  engine so the turn closes the normal way. */
  approval: { approvalId: string; capability: string; reason?: string };
  /** The server's saved ids for this spoken turn — the message the person said
   *  (from `routed`) and Jarvis's reply (from `done`). Without them a spoken
   *  message keeps a placeholder id, and Edit/Retry on it cut nothing on the
   *  server: the replaced exchange stayed in Jarvis's memory and came back on
   *  reload. Typed messages get the same swap in `send()`. */
  saved: { userMessageId?: string; replyMessageId?: string };
  error: { message: string; code?: string; turn?: boolean; fatal?: boolean };
}

type Handler<K extends keyof EngineEvents> = (payload: EngineEvents[K]) => void;

/** How long without any sign of progress before a turn is treated as hung. */
export const STUCK_AFTER_MS = 45_000;

export abstract class VoiceEngine {
  state: EngineState = 'idle';
  muted = false;

  private readonly listeners = new Map<string, Set<(payload: never) => void>>();
  private stuckTimer: ReturnType<typeof setTimeout> | null = null;

  on<K extends keyof EngineEvents>(event: K, handler: Handler<K>): () => void {
    const set = this.listeners.get(event as string) ?? new Set();
    this.listeners.set(event as string, set);
    set.add(handler as (payload: never) => void);
    return () => set.delete(handler as (payload: never) => void);
  }

  protected emit<K extends keyof EngineEvents>(event: K, payload: EngineEvents[K]): void {
    for (const handler of this.listeners.get(event as string) ?? []) {
      try {
        (handler as Handler<K>)(payload);
      } catch (err) {
        console.error(`[voice] a listener for "${String(event)}" threw:`, err);
      }
    }
  }

  protected setState(state: EngineState): void {
    this.state = state;
    this.emit('state', { state });
  }

  /**
   * Arm the "nothing is happening" backstop. Re-armed on every real sign of
   * progress, so a slow but genuinely working turn never trips it; disarmed the
   * moment a turn ends for any reason.
   */
  protected armStuckWatchdog(onStuck: () => void, afterMs = STUCK_AFTER_MS): void {
    this.disarmStuckWatchdog();
    this.stuckTimer = setTimeout(onStuck, afterMs);
  }

  protected disarmStuckWatchdog(): void {
    if (this.stuckTimer) clearTimeout(this.stuckTimer);
    this.stuckTimer = null;
  }

  abstract start(): Promise<void>;
  abstract stop(): void;
  abstract sendText(text: string, options?: { attachments?: string[] }): void;
  abstract interrupt(options?: { keepListening?: boolean }): void;

  setMuted(muted: boolean): void {
    this.muted = muted;
  }
}
