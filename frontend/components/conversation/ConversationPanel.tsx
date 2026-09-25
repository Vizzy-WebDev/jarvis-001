'use client';

import { Composer } from '@/components/composer/Composer';
import { TalkToPicker } from '@/components/conversation/TalkToPicker';
import { Transcript } from '@/components/conversation/Transcript';
import type { Turn } from '@/components/conversation/Message';
import { MenuIcon, NewChatIcon } from '@/components/ui/Icons';
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
  onEditMessage,
  onRetryMessage,
  onDictationStart,
  voiceEngineActive,
  isSpeaking,
  draftText,
  onDraftConsumed,
  historyOpen,
  onToggleHistory,
  talkingTo = null,
  onTalkTo,
}: {
  turns: Turn[];
  notConfigured: boolean;
  busy: boolean;
  onSend: (text: string, attachments: { id: string; name: string; kind: string }[]) => void;
  onNewChat: () => void;
  onDecide?: (approvalId: string, decision: 'allow' | 'deny') => void;
  /** Passed straight through to the transcript — see `Transcript.tsx`. */
  onEditMessage?: (turn: Turn, newText: string) => void;
  onRetryMessage?: (userTurn: Turn) => void;
  /** Passed straight through to the composer's dictation — see there. */
  onDictationStart?: () => void;
  /** Whether the main voice engine is running — passed straight through so
   *  the composer's own dictation can stand down when it starts. */
  voiceEngineActive?: boolean;
  isSpeaking?: () => boolean;
  /** Passed straight through to the composer — see there. */
  draftText?: string | null;
  onDraftConsumed?: () => void;
  /** Whether the chat-history slide-out is open. Owned by the caller, not
   *  this panel — the drawer itself is rendered at the page's top level, not
   *  nested in here: this container's own `backdrop-blur-xl` creates a
   *  containing block for `position: fixed` descendants, which trapped an
   *  earlier version of the drawer inside this small floating panel instead
   *  of the real viewport (confirmed live — it slid in on top of the very
   *  button that opened it). */
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  /** A specialist the person is talking to directly, or null for Jarvis. */
  talkingTo?: { id: string; name: string } | null;
  onTalkTo?: (next: { id: string; name: string } | null) => void;
}) {
  return (
    <div
      data-testid="conversation"
      className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-surface-border
                 bg-surface-panel shadow-panel backdrop-blur-xl transition-colors duration-150 ease-out
                 focus-within:border-accent/25"
    >
      <div className="flex items-center gap-2 px-4 py-2">
        {/* Same area as New chat, on the left: chat history is a sibling
            action on this panel, not a separate destination. */}
        <IconButton
          label="Chat history"
          data-testid="chat-history-menu"
          className="-ml-1 h-8 w-8"
          active={historyOpen}
          onClick={onToggleHistory}
        >
          <MenuIcon className="h-[18px] w-[18px]" />
        </IconButton>
        <h2 className="min-w-0">
          {onTalkTo ? (
            <TalkToPicker talkingTo={talkingTo} onChange={onTalkTo} />
          ) : (
            <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-faint">
              Conversation
            </span>
          )}
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
      <Transcript
        turns={turns}
        notConfigured={notConfigured}
        onDecide={onDecide}
        onEditMessage={onEditMessage}
        onRetryMessage={onRetryMessage}
      />
      <div className="h-px shrink-0 bg-surface-border" />
      <Composer
        disabled={notConfigured}
        busy={busy}
        onSend={onSend}
        onDictationStart={onDictationStart}
        voiceEngineActive={voiceEngineActive}
        isSpeaking={isSpeaking}
        draftText={draftText}
        onDraftConsumed={onDraftConsumed}
      />
    </div>
  );
}
