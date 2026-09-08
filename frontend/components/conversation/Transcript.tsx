'use client';

import { useEffect, useRef } from 'react';

import { Message, type Turn } from './Message';

/**
 * The conversation itself.
 *
 * Scrolls internally so the page never grows one of its own, and follows new
 * text only when the reader is already at the bottom — scrolling up to read
 * something and being yanked back down is the behaviour this avoids.
 *
 * A short conversation sits at the BOTTOM, against the composer, rather than
 * hanging from the top of a mostly empty panel: a conversation grows upwards
 * from where you are typing, and the first exchange should not look marooned.
 */
export function Transcript({ turns, notConfigured }: { turns: Turn[]; notConfigured: boolean }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [turns]);

  return (
    <div
      ref={scrollRef}
      data-testid="transcript"
      onScroll={(event) => {
        const el = event.currentTarget;
        pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
      }}
      className="scroll-quiet flex min-h-0 flex-1 flex-col justify-end gap-3 overflow-y-auto px-4 pb-4 pt-4"
      aria-live="polite"
    >
      {notConfigured && (
        <div className="rounded border border-surface-border bg-white/[0.03] p-3 text-[13px] text-ink-muted">
          <p>Jarvis isn&apos;t connected to a model yet, so it can&apos;t answer anything.</p>
          <a
            href="#/models"
            className="mt-2 inline-block text-accent underline-offset-2 hover:underline"
          >
            Add a model →
          </a>
        </div>
      )}

      {turns.length === 0 && !notConfigured && (
        <p className="pt-6 text-center text-[13px] text-ink-faint">
          Say something, or type below.
        </p>
      )}

      {turns.map((turn) => (
        <Message key={turn.id} turn={turn} />
      ))}
    </div>
  );
}
