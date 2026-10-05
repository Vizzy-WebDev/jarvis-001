'use client';

/**
 * A labelled field. One component so every form in the app agrees about where
 * a caption sits and what a focused control looks like.
 *
 * **Deliberately a `div`, not a `label`.** A `<label>` wrapping its control is
 * the usual advice, and it is wrong the moment a field holds more than one
 * interactive thing: the browser forwards a click anywhere inside a label to the
 * FIRST labelable control in it. Found the hard way — the connector field holds
 * an "Add connector" button and, inside the popover it opens, a switch per app;
 * clicking either re-fired a click on the button and reopened the popover the
 * instant it was told to close. A caption that focuses its input is not worth a
 * whole class of bug where any control in a field triggers a different one.
 */
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="py-2">
      <span className="mb-1.5 block text-[12px] font-medium text-ink-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-ink-faint">{hint}</span>}
    </div>
  );
}

export const inputClass =
  'w-full rounded border border-surface-border bg-surface/60 px-3 py-2 text-[14px] text-ink ' +
  'outline-none transition duration-150 ease-out placeholder:text-ink-faint ' +
  'focus:border-accent/50 focus:ring-2 focus:ring-accent/20';
