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
 *
 * **That bottom-anchoring is done with a `margin-top: auto` spacer as the
 * first child, never `justify-content: flex-end` on the scroll container
 * itself — confirmed live as a real, severe bug, not a style preference.**
 * With `justify-end`, once content is taller than the container, Chromium
 * never extends `scrollHeight` past `clientHeight` at all: everything that
 * overflows renders at a NEGATIVE `offsetTop`, genuinely unreachable by
 * scrolling — not a wrong scroll position, a scrollbar that cannot reach the
 * older messages at all, on a completely fresh page load, independent of how
 * the conversation became long or was opened. A spacer with an auto top
 * margin gets the identical bottom-anchored look for SHORT content (the
 * margin expands into whatever free space is left) while leaving the
 * container's own `justify-content` at its default, top-anchored value —
 * which is what makes `scrollHeight` grow normally and the browser's own
 * scrolling actually reach everything once content overflows.
 */
export function Transcript({
  turns,
  notConfigured,
  editingId,
  onDecide,
  onStartEdit,
  onRetryMessage,
}: {
  turns: Turn[];
  notConfigured: boolean;
  /** The user turn loaded into the composer for editing, if any. */
  editingId?: string | null;
  onDecide?: (approvalId: string, decision: 'allow' | 'deny') => void;
  /** Edit loads the user's message into the composer (design 1h); the
   *  truncate-and-resend happens when it is sent from there. */
  onStartEdit?: (turn: Turn) => void;
  /** Retry redoes an exchange from a user message: the one Retry was pressed
   *  on, or — on a reply — the user message it answered. */
  onRetryMessage?: (userTurn: Turn) => void;
}) {
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
      className="scroll-quiet flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto overflow-x-hidden px-3.5 py-4
                 [mask-image:linear-gradient(to_bottom,transparent_0,#000_48px)]"
      aria-live="polite"
    >
      {/* See this file's own note on why this, not `justify-content: flex-end`. */}
      <div aria-hidden className="mt-auto" />

      {notConfigured && (
        <div className="rounded-xl border border-line/[0.16] bg-line/[0.04] p-3 text-[13px] text-ink-muted">
          <p>Jarvis isn&apos;t connected to a model yet, so it can&apos;t answer anything.</p>
          <a href="#/settings/models" className="mt-2 inline-block underline-offset-2 hover:underline">
            Model Settings →
          </a>
        </div>
      )}

      {turns.length === 0 && !notConfigured && (
        <p className="self-center pt-6 text-center text-[13px] text-ink-muted">
          New chat. Say or type anything.
        </p>
      )}

      {turns.map((turn, index) => {
        const redoFrom = turn.role === 'user'
          ? turn
          : turn.role === 'assistant'
            ? turns.slice(0, index).reverse().find((t) => t.role === 'user')
            : undefined;
        return (
          <Message
            key={turn.id}
            turn={turn}
            editing={editingId === turn.id}
            onDecide={onDecide}
            onEdit={turn.role === 'user' && onStartEdit ? () => onStartEdit(turn) : undefined}
            onRetry={redoFrom && onRetryMessage ? () => onRetryMessage(redoFrom) : undefined}
          />
        );
      })}
    </div>
  );
}
