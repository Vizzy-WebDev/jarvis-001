'use client';

/**
 * The top of every section screen: the name of the thing, one line saying what
 * it is for, and the controls that act on the whole screen.
 *
 * One component so all twelve screens agree about where the title sits and
 * where the buttons are — which is most of what makes them feel like one app.
 */
export function PageHeader({
  title,
  blurb,
  children,
}: {
  title: string;
  blurb?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-[20px] font-medium tracking-[-0.01em] text-ink">{title}</h1>
        {blurb && <p className="mt-1 text-[13px] text-ink-muted">{blurb}</p>}
      </div>
      {children && <div className="flex items-center gap-2">{children}</div>}
    </div>
  );
}
