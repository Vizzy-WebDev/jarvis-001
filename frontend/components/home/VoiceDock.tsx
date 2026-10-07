'use client';

import { STATE_COLOUR, type JarvisState } from '@/lib/jarvis-state';

import { rgba } from './HomeHeader';

/**
 * The band under the core: a level meter, the mic, and what Jarvis is doing in
 * words (design Home v6 + 1c).
 *
 * The mic starts a voice session when none is running — the same as saying the
 * wake word — and mutes or unmutes one that is: muting never interrupts Jarvis,
 * it only stops the microphone. **Interrupt** is its own control (1c): a red ring
 * to the LEFT of the mic, there only while Jarvis is speaking, so the mic never
 * moves. Esc does the same.
 */
export function VoiceDock({
  state,
  listening,
  muted,
  speaking,
  status,
  visible,
  width,
  bottom,
  onMic,
  onInterrupt,
  waveMeter,
}: {
  state: JarvisState;
  /** A voice session is running. */
  listening: boolean;
  muted: boolean;
  speaking: boolean;
  status: string;
  visible: boolean;
  width: number;
  bottom: number;
  onMic: () => void;
  onInterrupt: () => void;
  waveMeter: (canvas: HTMLCanvasElement | null) => void;
}) {
  const colour = STATE_COLOUR[state];
  const tip = !listening ? 'Click to talk' : muted ? 'Unmute the microphone' : 'Mute the microphone';
  return (
    <div
      className="pointer-events-none absolute inset-x-0 z-[3] flex h-[150px] flex-col items-center justify-end gap-3.5 pb-[22px]
                 transition-[opacity,transform] duration-700 ease-out"
      style={{ bottom, opacity: visible ? 1 : 0, transform: visible ? 'none' : 'translateY(14px)' }}
    >
      <div className="relative flex h-20 items-center justify-center" style={{ width }}>
        <canvas ref={waveMeter} aria-hidden className="absolute inset-0 h-full w-full" />
        <span aria-hidden className="pointer-events-none absolute left-1/2 top-1/2 -ml-[52px] -mt-[52px] h-[104px] w-[104px] rounded-full border transition-colors duration-[600ms]"
              style={{ borderColor: rgba(colour, 0.3) }} />
        <span aria-hidden className="pointer-events-none absolute left-1/2 top-1/2 -ml-[66px] -mt-[66px] h-[132px] w-[132px] rounded-full border transition-colors duration-[600ms]"
              style={{ borderColor: rgba(colour, 0.1) }} />
        <button
          type="button"
          id="mic-button"
          data-testid="mic"
          aria-label={tip}
          title={tip}
          aria-pressed={listening}
          data-muted={muted ? 'true' : 'false'}
          onClick={onMic}
          className="pointer-events-auto relative inline-flex h-[76px] w-[76px] items-center justify-center rounded-full border-2 text-white transition-all duration-[600ms] ease-out"
          style={{
            borderColor: colour,
            background: `radial-gradient(circle at 50% 50%, #0a0d12 55%, ${rgba(colour, 0.18)})`,
            boxShadow: `0 0 0 6px ${rgba(colour, 0.07)}, 0 0 36px ${rgba(colour, 0.45)}, inset 0 0 18px ${rgba(colour, 0.18)}`,
          }}
        >
          <svg viewBox="0 0 24 24" className="h-[26px] w-[26px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <rect x="9" y="3" width="6" height="11" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
            <path d="M4 4l16 16" style={{ opacity: muted ? 1 : 0, transition: 'opacity 200ms' }} />
          </svg>
        </button>
        <button
          type="button"
          data-testid="interrupt"
          aria-label="Stop Jarvis speaking"
          title="Stop Jarvis speaking (Esc)"
          aria-hidden={!speaking}
          tabIndex={speaking ? 0 : -1}
          onClick={onInterrupt}
          className="absolute left-[calc(50%-100px)] top-1/2 flex h-[52px] w-[52px] -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full
                     border-[1.5px] border-[rgb(255_59_48/0.65)] bg-[rgb(255_59_48/0.12)] text-[#ff8a82] shadow-[0_0_18px_rgba(255,59,48,0.35)]
                     transition-opacity duration-200"
          style={{ opacity: speaking ? 1 : 0, pointerEvents: speaking ? 'auto' : 'none' }}
        >
          <svg viewBox="0 0 24 24" className="h-[22px] w-[22px] fill-none stroke-current stroke-[1.8]" strokeLinecap="round" aria-hidden>
            <path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" />
            <path d="M3 21 21 3" />
          </svg>
        </button>
      </div>
      <p data-testid="status" aria-live="polite" className="m-0 text-[14px] tracking-[0.01em] text-ink-soft">{status}</p>
    </div>
  );
}
