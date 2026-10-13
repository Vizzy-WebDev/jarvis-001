'use client';

import { MicIcon } from '@/components/ui/Icons';

/**
 * "Ask Jarvis…" on every page that is not Home (design 10b).
 *
 * Closed, it is a pill at the bottom right. Open, it is the SAME conversation
 * as Home, docked at the right edge under the header — the caller passes the
 * one conversation panel in, so there is never a second transcript to keep in
 * step with the first. It is rendered only while open, so a closed dock costs
 * nothing and the page never holds two copies of the composer.
 */
export function AskJarvis({
  open,
  onToggle,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  /** The conversation panel, docked. */
  children: React.ReactNode;
}) {
  if (!open) {
    return (
      <button
        type="button"
        data-testid="ask-jarvis"
        onClick={onToggle}
        className="fixed bottom-6 right-6 z-30 flex h-12 items-center gap-2.5 rounded-full border border-line/[0.26]
                   bg-gradient-to-b from-surface-raised/[0.92] to-surface/[0.92] pl-3 pr-2 text-ink-soft
                   shadow-[0_18px_40px_-16px_rgb(0_0_0/0.9)] transition-colors duration-200 hover:border-line/[0.45]"
      >
        <span aria-hidden className="h-[26px] w-[26px] rounded-full bg-[radial-gradient(circle_at_35%_30%,#ff6a5f,#b3140c)]" />
        <span className="pr-2 text-[14px]">Ask Jarvis…</span>
        <span aria-hidden className="inline-flex h-[34px] w-[34px] items-center justify-center rounded-full bg-accent/10">
          <MicIcon className="h-4 w-4" />
        </span>
      </button>
    );
  }
  return (
    <aside
      data-testid="ask-jarvis-panel"
      aria-label="Conversation"
      className="fixed bottom-0 right-0 top-[var(--shell-header)] z-30 flex w-[400px] max-w-full flex-col
                 border-l border-line/[0.22] bg-gradient-to-b from-surface-raised/[0.97] to-surface/[0.98]
                 shadow-[-24px_0_60px_-20px_rgb(0_0_0/0.9)]"
    >
      {children}
    </aside>
  );
}
