'use client';

import { useEffect, useState } from 'react';

import { Row } from '@/components/ui/Card';
import { Toggle } from '@/components/ui/Toggle';
import { api } from '@/lib/api';
import type { VoiceOptions } from '@/lib/api-types';

/**
 * The settings that belong to the moment rather than to a screen: how Jarvis
 * listens, and which voice answers.
 *
 * **Every option here is offered because something says it is available**, not
 * because this code knows a provider's name — the server computes that from a
 * connected model's declared capabilities and a configured key, and an
 * unavailable engine always arrives with a reason. "Not available" on its own is
 * the shape of thing people file bugs about when the fix was ten seconds away.
 */
export function SettingsPanel({
  open,
  speakReplies,
  onSpeakReplies,
  engine,
  onEngine,
  voice,
  onVoice,
  onNavigate,
}: {
  open: boolean;
  speakReplies: boolean;
  onSpeakReplies: (next: boolean) => void;
  engine: string;
  onEngine: (id: string) => void;
  voice: string;
  onVoice: (id: string) => void;
  onNavigate: (id: string) => void;
}) {
  const [options, setOptions] = useState<VoiceOptions | null>(null);

  useEffect(() => {
    if (!open) return;
    api.voice.options().then(setOptions).catch(() => setOptions(null));
  }, [open]);

  if (!open) return null;
  return (
    <div
      data-testid="settings-panel"
      className="animate-fade-up absolute right-5 z-30 w-[min(380px,90vw)] rounded-lg border
                 border-surface-border bg-surface-panel px-4 py-2 shadow-panel backdrop-blur-xl"
      style={{ top: 'calc(var(--header-reserve) - 4px)' }}
    >
      <Row label="Speak replies out loud" hint={voiceHint(options, voice)}>
        <Toggle label="Speak replies out loud" checked={speakReplies} onChange={onSpeakReplies} />
      </Row>

      <div className="h-px bg-surface-border" />

      <div className="py-3">
        <p className="mb-1.5 text-[12px] font-medium text-ink-muted">How it listens</p>
        <div className="space-y-1" data-testid="engine-options">
          {(options?.engines ?? []).map((option) => (
            <button
              key={option.id}
              type="button"
              disabled={!option.available}
              data-testid={`engine-${option.id}`}
              onClick={() => onEngine(option.id)}
              className={[
                'w-full rounded px-2.5 py-2 text-left transition duration-150',
                'disabled:cursor-not-allowed disabled:opacity-55',
                option.id === engine && option.available
                  ? 'bg-accent/[0.10]'
                  : 'hover:bg-white/[0.05] disabled:hover:bg-transparent',
              ].join(' ')}
            >
              <span className={`block text-[13px] ${option.id === engine ? 'text-ink' : 'text-ink-muted'}`}>
                {option.label}
              </span>
              <span className="mt-0.5 block text-[11px] leading-relaxed text-ink-faint">
                {option.available ? option.description : option.reason}
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="h-px bg-surface-border" />

      <div className="py-3">
        <p className="mb-1.5 text-[12px] font-medium text-ink-muted">Which voice</p>
        <div className="flex flex-wrap gap-1.5" data-testid="voice-options">
          {(options?.voices ?? []).map((option) => (
            <button
              key={option.id}
              type="button"
              data-testid={`voice-${option.id}`}
              onClick={() => onVoice(option.id)}
              className={[
                'rounded-pill border px-3 py-1 text-[12px] transition duration-150',
                option.id === voice
                  ? 'border-accent/40 bg-accent/15 text-accent'
                  : 'border-surface-border text-ink-muted hover:text-ink',
              ].join(' ')}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="h-px bg-surface-border" />

      <p className="py-3 text-[12px] leading-relaxed text-ink-muted">
        Models and keys live on the{' '}
        <button
          type="button"
          className="text-accent underline-offset-2 hover:underline"
          onClick={() => onNavigate('models')}
        >
          Model Settings
        </button>{' '}
        screen.
      </p>
    </div>
  );
}

function voiceHint(options: VoiceOptions | null, chosen: string): string {
  const voice = options?.voices.find((entry) => entry.id === chosen);
  if (!voice) return 'Uses the browser’s own voice.';
  return voice.needsKey ? `Uses ${voice.label}.` : 'Free, offline, no key needed.';
}
