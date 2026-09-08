'use client';

/**
 * An on/off switch.
 *
 * A raw checkbox is the one control a dark interface cannot restyle into
 * agreement with the rest of it — it keeps the platform's own metal look and
 * reads as unfinished next to everything else. Every setting across every
 * screen uses this instead.
 */
export function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        'relative h-[22px] w-[38px] shrink-0 rounded-pill border transition duration-150 ease-out',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
        'disabled:opacity-40',
        checked ? 'border-accent/40 bg-accent/25' : 'border-surface-border bg-white/[0.06]',
      ].join(' ')}
    >
      <span
        aria-hidden
        className={[
          'absolute top-1/2 h-[14px] w-[14px] -translate-y-1/2 rounded-full transition-all duration-150 ease-out',
          checked ? 'left-[19px] bg-accent' : 'left-[3px] bg-ink-faint',
        ].join(' ')}
      />
    </button>
  );
}
