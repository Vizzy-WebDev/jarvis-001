'use client';

import { MicIcon } from '@/components/ui/Icons';

/**
 * Jarvis's own voice control — mute and unmute, nothing else.
 *
 * It sits in the stage's lower band, which is reserved space: the orb's box is
 * everything above it, so neither can ever resize the other. Distinct from the
 * composer's dictation mic, which is for speaking a message instead of typing
 * it.
 *
 * The voice engines land in their own wave; until then this is honest about
 * being unavailable rather than pretending to listen.
 */
export function MicButton({
  listening,
  disabled = false,
  hint,
  onToggle,
}: {
  listening: boolean;
  disabled?: boolean;
  hint: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={disabled}
      data-testid="mic"
      aria-pressed={listening}
      aria-label={listening ? 'Mute the microphone' : 'Unmute the microphone'}
      title={disabled ? hint : listening ? 'Mute' : 'Unmute'}
      className={[
        'inline-flex h-[60px] w-[60px] items-center justify-center rounded-full border',
        'transition duration-200 ease-out',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
        disabled
          ? 'border-surface-border bg-surface-raised/50 text-ink-faint'
          : listening
            ? 'border-accent/40 bg-accent/15 text-accent shadow-focus'
            : 'border-surface-border bg-surface-raised text-ink-muted hover:border-surface-border-strong hover:text-ink',
      ].join(' ')}
    >
      <MicIcon className="h-6 w-6" muted={!listening} />
    </button>
  );
}
