'use client';

import { useEffect, useRef, useState } from 'react';

import { Popover } from '@/components/ui/Popover';
import { api } from '@/lib/api';
import type { Agent } from '@/lib/api-types';

/**
 * Who the conversation is talking to: Jarvis, or one specialist directly.
 *
 * Jarvis is the default and hands work to specialists itself. Switching to one
 * is for a conversation that IS that specialist's — a lesson with the Teacher,
 * say — so each message costs one model call instead of Jarvis relaying it.
 * Switching back is one click, and the choice is shown in the panel's own header
 * so it is never unclear who is answering.
 */
export function TalkToPicker({
  talkingTo,
  onChange,
}: {
  talkingTo: { id: string; name: string } | null;
  onChange: (next: { id: string; name: string } | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const anchor = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    api.agents.list()
      .then((found) => setAgents(found.agents.filter((agent) => agent.enabled)))
      .catch(() => setAgents([]));
  }, [open]);

  const pick = (next: { id: string; name: string } | null) => {
    onChange(next);
    setOpen(false);
  };

  return (
    <>
      <button
        ref={anchor}
        type="button"
        data-testid="talk-to"
        onClick={() => setOpen((value) => !value)}
        className={[
          'flex max-w-full items-center gap-1.5 truncate rounded-[9px] px-2 py-1.5 text-[14px] font-medium',
          'transition duration-150 ease-out hover:bg-line/[0.08] focus-visible:outline-none focus-visible:ring-2',
          'focus-visible:ring-accent/60',
          talkingTo ? 'text-[#9fd0f2]' : 'text-ink-strong',
        ].join(' ')}
        title="Who you’re talking to"
      >
        <span className="truncate">{talkingTo ? `Talking to ${talkingTo.name}` : 'Jarvis'}</span>
        <svg viewBox="0 0 24 24" aria-hidden className="h-3.5 w-3.5 shrink-0 fill-none stroke-current stroke-2 text-ink-muted" strokeLinecap="round">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      <Popover open={open} anchorRef={anchor} onClose={() => setOpen(false)} width={280}>
        <div className="max-h-[360px] overflow-y-auto py-1" data-testid="talk-to-menu">
          <Option label="Jarvis" hint="Hands work to specialists itself" active={!talkingTo}
                  onClick={() => pick(null)} testId="talk-to-jarvis" />
          <p className="px-3 pb-1 pt-2 text-[11px] font-medium text-ink-faint">
            Or talk to a specialist directly
          </p>
          {agents === null ? (
            <p className="px-3 py-2 text-[12px] text-ink-faint">Loading…</p>
          ) : agents.map((agent) => (
            <Option key={agent.id} label={agent.name} hint={agent.description}
                    active={talkingTo?.id === agent.id} testId={`talk-to-${agent.id}`}
                    onClick={() => pick({ id: agent.id, name: agent.name })} />
          ))}
        </div>
      </Popover>
    </>
  );
}

function Option({ label, hint, active, onClick, testId }: {
  label: string;
  hint: string;
  active: boolean;
  onClick: () => void;
  testId: string;
}) {
  return (
    <button type="button" onClick={onClick} data-testid={testId}
            className={['block w-full px-3 py-1.5 text-left transition duration-150 ease-out',
              'hover:bg-white/[0.05]', active ? 'bg-accent/10' : ''].join(' ')}>
      <span className={`block text-[13px] ${active ? 'text-accent' : 'text-ink'}`}>{label}</span>
      <span className="block truncate text-[11px] text-ink-faint">{hint}</span>
    </button>
  );
}
