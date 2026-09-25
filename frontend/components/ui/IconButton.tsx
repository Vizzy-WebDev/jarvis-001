'use client';

import type { ButtonHTMLAttributes } from 'react';

/**
 * Every square control in the header and the composer.
 *
 * One component so they cannot drift apart: the same size, the same hover, the
 * same focus ring. `active` is for a control that is currently ON — screen
 * sharing, the settings panel — which the original showed only by tooltip.
 */
export function IconButton({
  label,
  active = false,
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; active?: boolean }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={active || undefined}
      className={[
        'inline-flex h-9 w-9 items-center justify-center rounded-full',
        'text-ink-muted transition duration-150 ease-out',
        'hover:bg-white/[0.06] hover:text-ink',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
        'disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-ink-muted',
        active ? 'bg-accent/15 text-accent hover:bg-accent/20 hover:text-accent' : '',
        className,
      ].join(' ')}
      {...props}
    />
  );
}
