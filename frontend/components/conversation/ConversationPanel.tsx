'use client';

import { Composer } from '@/components/composer/Composer';
import { Transcript } from '@/components/conversation/Transcript';
import type { Turn } from '@/components/conversation/Message';
import { NewChatIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';

/**
 * The conversation — ONE panel, not a transcript with a composer bolted under
 * it.
 *
 * One border, one ground, one radius, with hairlines dividing the three bands
 * inside it: what was said, and where you say the next thing. Two stacked cards
 * read as two objects that happen to be adjacent, which is not what this is.
 *
 * It floats over the right edge of the stage — the caller positions it — and
 * everything that scrolls, scrolls inside here, so the page never grows a
 * scrollbar of its own and the orb is never moved by anything that happens in
 * this panel.
 */
export function ConversationPanel({
  turns,
  notConfigured,
  busy,
  onSend,
  onNewChat,
  onDecide,
  onDictationStart,
  isSpeaking,
}: {
  turns: Turn[];
  notConfigured: boolean;
  busy: boolean;
  onSend: (text: string, attachments: string[]) => void;
  onNewChat: () => void;
  onDecide?: (approvalId: string, decision: 'allow' | 'deny') => void;
  /** Passed straight through to the composer's dictation — see there. */
  onDictationStart?: () => void;
  isSpeaking?: () => boolean;
}) {
  return (
    <div
      data-testid="conversation"
      className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-surface-border
                 bg-surface-panel shadow-panel backdrop-blur-xl transition-colors duration-150 ease-out
                 focus-within:border-accent/25"
    >
      <div className="flex items-center gap-2 px-4 py-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-faint">
          Conversation
        </h2>
        {/* Starting a new one acts on this panel, so it lives on this panel. */}
        <IconButton
          label="Start a new chat"
          data-testid="new-chat"
          className="-mr-1 ml-auto h-8 w-8"
          onClick={onNewChat}
        >
          <NewChatIcon className="h-[18px] w-[18px]" />
        </IconButton>
      </div>

      <div className="h-px shrink-0 bg-surface-border" />
      <Transcript turns={turns} notConfigured={notConfigured} onDecide={onDecide} />
      <div className="h-px shrink-0 bg-surface-border" />
      <Composer
        disabled={notConfigured}
        busy={busy}
        onSend={onSend}
        onDictationStart={onDictationStart}
        isSpeaking={isSpeaking}
      />
    </div>
  );
}
