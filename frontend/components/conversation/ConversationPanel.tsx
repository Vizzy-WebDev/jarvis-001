'use client';

import { Composer } from '@/components/composer/Composer';
import { ChatHistoryDrawer } from '@/components/conversation/ChatHistoryDrawer';
import { TalkToPicker } from '@/components/conversation/TalkToPicker';
import { Transcript } from '@/components/conversation/Transcript';
import type { Turn } from '@/components/conversation/Message';
import { ChevronRightIcon } from '@/components/ui/Icons';

/**
 * The conversation — ONE panel, not a transcript with a composer bolted under
 * it (design Home v6).
 *
 * One border, one ground, one radius, with hairlines dividing the bands inside
 * it: who you are talking to, what was said, and where you say the next thing.
 * Chat history slides in over it, inside it. It floats over the right edge of
 * the stage — the caller positions it — and everything that scrolls, scrolls
 * inside here, so the page never grows a scrollbar of its own and the core is
 * never moved by anything that happens in this panel.
 *
 * Docked (the "Ask Jarvis" panel on other pages) it fills its dock, titled
 * "Conversation", with a way to fold it away instead of the history.
 */
export function ConversationPanel({
  turns,
  notConfigured,
  busy,
  onSend,
  onNewChat,
  onDecide,
  editing,
  onStartEdit,
  onCancelEdit,
  onRetryMessage,
  onDictationStart,
  voiceEngineActive,
  isSpeaking,
  draftText,
  onDraftConsumed,
  history,
  talkingTo = null,
  onTalkTo,
  onCollapse,
  glow = 'transparent',
}: {
  turns: Turn[];
  notConfigured: boolean;
  busy: boolean;
  onSend: (text: string, attachments: { id: string; name: string; kind: string }[], editOf?: string) => void;
  onNewChat: () => void;
  onDecide?: (approvalId: string, decision: 'allow' | 'deny') => void;
  /** The sent message being edited in the composer (design 1h). */
  editing?: Turn | null;
  onStartEdit?: (turn: Turn) => void;
  onCancelEdit?: () => void;
  onRetryMessage?: (userTurn: Turn) => void;
  /** Passed straight through to the composer's dictation — see there. */
  onDictationStart?: () => void;
  /** Whether the main voice engine is running — passed straight through so
   *  the composer's own dictation can stand down when it starts. */
  voiceEngineActive?: boolean;
  isSpeaking?: () => boolean;
  draftText?: string | null;
  onDraftConsumed?: () => void;
  /** The chat-history slide-in, when this panel offers one. */
  history?: {
    open: boolean;
    onToggle: () => void;
    onClose: () => void;
    onViewAll: () => void;
    onResume: (id: string) => void;
    currentId: string | null;
    accent: string;
  };
  /** A specialist the person is talking to directly, or null for Jarvis. */
  talkingTo?: { id: string; name: string } | null;
  onTalkTo?: (next: { id: string; name: string } | null) => void;
  /** Set when the panel is docked at the side of another page ("Ask Jarvis"). */
  onCollapse?: () => void;
  /** Jarvis's state colour as a glow around the panel's edge. */
  glow?: string;
}) {
  const docked = Boolean(onCollapse);
  const iconBtn = 'inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[9px] transition-colors duration-200';
  return (
    <div className="relative h-full">
      {!docked && (
        <span aria-hidden className="pointer-events-none absolute left-[22px] right-[22px] top-0 z-[2] h-px bg-[linear-gradient(90deg,transparent,rgba(170,210,240,0.55),transparent)]" />
      )}
      <div
        data-testid="conversation"
        className={docked
          ? 'relative flex h-full min-h-0 flex-col overflow-hidden'
          : 'relative flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-line/[0.26] bg-gradient-to-b from-[rgb(12_18_26/0.86)] to-[rgb(6_9_13/0.88)] backdrop-blur-md'}
        style={docked ? undefined : {
          boxShadow: `inset 0 1px 0 rgba(255,255,255,0.05), inset 0 0 40px rgba(127,168,201,0.04), 0 24px 60px -20px rgba(0,0,0,0.8), 0 0 44px -16px ${glow}`,
        }}
      >
        <div className="flex shrink-0 items-center gap-1.5 px-2.5 py-2">
          {history && (
            <button type="button" title="Chat history" aria-label="Chat history" data-testid="chat-history-menu"
                    aria-pressed={history.open} onClick={history.onToggle}
                    className={`${iconBtn} ${history.open ? 'bg-line/[0.14] text-white' : 'text-ink-soft hover:bg-line/[0.08]'}`}>
              <svg viewBox="0 0 24 24" aria-hidden className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.75]" strokeLinecap="round">
                <path d="M4 7h16M4 12h16M4 17h16" />
              </svg>
            </button>
          )}
          <h2 className="min-w-0">
            {onTalkTo ? (
              <TalkToPicker talkingTo={talkingTo} onChange={onTalkTo} />
            ) : (
              <span className="px-2 text-[14px] font-medium text-ink-strong">
                {talkingTo ? `Talking to ${talkingTo.name}` : 'Conversation'}
              </span>
            )}
          </h2>
          <button type="button" title="New chat" aria-label="Start a new chat" data-testid="new-chat" onClick={onNewChat}
                  className={`${iconBtn} ml-auto text-ink-soft hover:bg-line/[0.08] hover:text-white`}>
            <svg viewBox="0 0 24 24" aria-hidden className="h-[17px] w-[17px] fill-none stroke-current stroke-[1.75]" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
            </svg>
          </button>
          {onCollapse && (
            <button type="button" title="Collapse" aria-label="Collapse" data-testid="ask-jarvis-collapse" onClick={onCollapse}
                    className={`${iconBtn} text-ink-soft hover:bg-line/10`}>
              <ChevronRightIcon className="h-4 w-4" />
            </button>
          )}
        </div>

        <div className="h-px shrink-0 bg-line/[0.14]" />

        {history && (
          <ChatHistoryDrawer
            open={history.open}
            onClose={history.onClose}
            onViewAll={history.onViewAll}
            onResume={history.onResume}
            currentId={history.currentId}
            accent={history.accent}
          />
        )}

        <Transcript
          turns={turns}
          notConfigured={notConfigured}
          editingId={editing?.id ?? null}
          onDecide={onDecide}
          onStartEdit={onStartEdit}
          onRetryMessage={onRetryMessage}
        />
        <Composer
          disabled={notConfigured}
          busy={busy}
          onSend={onSend}
          onDictationStart={onDictationStart}
          voiceEngineActive={voiceEngineActive}
          isSpeaking={isSpeaking}
          draftText={draftText}
          onDraftConsumed={onDraftConsumed}
          editing={editing}
          onCancelEdit={onCancelEdit}
          placeholder={talkingTo ? `Message ${talkingTo.name}…` : 'Message Jarvis…'}
        />
      </div>
    </div>
  );
}
