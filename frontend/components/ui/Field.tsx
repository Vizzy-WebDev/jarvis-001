'use client';

/**
 * A labelled input. One component so every form in the app agrees about where
 * a label sits and what a focused field looks like.
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
    <label className="block py-2">
      <span className="mb-1.5 block text-[12px] font-medium text-ink-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-ink-faint">{hint}</span>}
    </label>
  );
}

export const inputClass =
  'w-full rounded border border-surface-border bg-surface/60 px-3 py-2 text-[14px] text-ink ' +
  'outline-none transition duration-150 ease-out placeholder:text-ink-faint ' +
  'focus:border-accent/50 focus:ring-2 focus:ring-accent/20';
