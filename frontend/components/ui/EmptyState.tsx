'use client';

/**
 * What a screen says when there is genuinely nothing to show.
 *
 * Always two things: what would be here, and what to do about it. An empty
 * screen that only says "nothing yet" makes the user wonder whether it is
 * broken.
 */
export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="mx-auto max-w-md py-14 text-center">
      <p className="text-[15px] text-ink">{title}</p>
      {body && <p className="mt-2 text-[13px] leading-relaxed text-ink-muted">{body}</p>}
      {action && <div className="mt-5 flex justify-center">{action}</div>}
    </div>
  );
}
