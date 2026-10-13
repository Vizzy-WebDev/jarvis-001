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
  // Declared rather than left to be passed through, because TypeScript does NOT
  // check hyphenated JSX attributes against a component's props: writing
  // data-testid on a component that does not accept it typechecks cleanly and
  // is then silently dropped, so the attribute simply never reaches the DOM.
  'data-testid': testId,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  'data-testid'?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      data-testid={testId}
      onClick={() => onChange(!checked)}
      className={[
        'relative h-5 w-[34px] shrink-0 rounded-pill transition-colors duration-200 ease-out',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
        'disabled:opacity-40',
        checked ? 'bg-[#3f7fae]' : 'bg-line/[0.22]',
      ].join(' ')}
    >
      <span
        aria-hidden
        className={[
          'absolute top-0.5 h-4 w-4 rounded-full bg-white transition-[left] duration-200 ease-out',
          checked ? 'left-4' : 'left-0.5',
        ].join(' ')}
      />
    </button>
  );
}
