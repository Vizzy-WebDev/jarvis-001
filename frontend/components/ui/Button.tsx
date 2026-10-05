'use client';

import type { ButtonHTMLAttributes } from 'react';

/**
 * A labelled button, in the three weights this interface actually needs.
 *
 * `primary` is the one action a screen is for, and there is never more than one
 * on screen at a time; `quiet` is everything else; `danger` is for something
 * that removes. Anything needing a fourth weight is probably two screens.
 */
type Tone = 'primary' | 'quiet' | 'danger';

const TONES: Record<Tone, string> = {
  primary: 'bg-accent/15 text-accent hover:bg-accent/25 border-accent/25',
  quiet: 'bg-white/[0.04] text-ink-muted hover:bg-white/[0.08] hover:text-ink border-surface-border',
  danger: 'bg-state-danger/10 text-state-danger hover:bg-state-danger/20 border-state-danger/25',
};

export function Button({
  tone = 'quiet',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone }) {
  return (
    <button
      type="button"
      className={[
        'inline-flex items-center gap-2 rounded-pill border px-3.5 py-1.5 text-[13px] font-medium',
        'transition duration-150 ease-out',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
        'disabled:opacity-40 disabled:hover:bg-transparent',
        TONES[tone],
        className,
      ].join(' ')}
      {...props}
    />
  );
}
