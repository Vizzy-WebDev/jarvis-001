'use client';

/**
 * The one container every screen is built from.
 *
 * Every section screen is the same shape — a title, a row of controls, then a
 * list of these — so twelve screens read as one product rather than twelve
 * layouts. Depth comes from a hairline border and a slightly raised ground, not
 * from a shadow: a page of shadowed boxes is noise.
 */
export function Card({
  className = '',
  interactive = false,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { interactive?: boolean }) {
  return (
    <div
      className={[
        'rounded-lg border border-surface-border bg-surface-raised/60 p-4',
        interactive
          ? 'cursor-pointer transition duration-150 ease-out hover:border-surface-border-strong hover:bg-surface-raised'
          : '',
        className,
      ].join(' ')}
      {...props}
    />
  );
}

/** A row inside a card-shaped list: label on the left, value or control right. */
export function Row({
  label,
  hint,
  children,
}: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5">
      <div className="min-w-0">
        <p className="truncate text-[14px] text-ink">{label}</p>
        {hint && <p className="mt-0.5 text-[12px] text-ink-faint">{hint}</p>}
      </div>
      {children}
    </div>
  );
}
