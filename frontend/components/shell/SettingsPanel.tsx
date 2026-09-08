'use client';

import { Row } from '@/components/ui/Card';
import { Toggle } from '@/components/ui/Toggle';

/**
 * The settings panel, dropping from the control it belongs to.
 *
 * Only what this wave can honestly offer is here. The voice engine, the voice
 * itself, the model picker and the external service keys all need routes that
 * do not exist yet — a dead control is worse than an absent one, so what is
 * coming is named instead of mocked up.
 */
export function SettingsPanel({
  open,
  speakReplies,
  onSpeakReplies,
  onNavigate,
}: {
  open: boolean;
  speakReplies: boolean;
  onSpeakReplies: (next: boolean) => void;
  onNavigate: (id: string) => void;
}) {
  if (!open) return null;
  return (
    <div
      data-testid="settings-panel"
      className="animate-fade-up absolute right-5 z-30 w-[min(380px,90vw)] rounded-lg border
                 border-surface-border bg-surface-panel px-4 py-2 shadow-panel backdrop-blur-xl"
      style={{ top: 'calc(var(--header-reserve) - 4px)' }}
    >
      <Row label="Speak replies out loud" hint="Uses the browser's own voice for now.">
        <Toggle
          label="Speak replies out loud"
          checked={speakReplies}
          onChange={onSpeakReplies}
        />
      </Row>

      <div className="h-px bg-surface-border" />

      <p className="py-3 text-[12px] leading-relaxed text-ink-muted">
        Which model answers, which voice speaks and which engine listens are chosen on the{' '}
        <button
          type="button"
          className="text-accent underline-offset-2 hover:underline"
          onClick={() => onNavigate('models')}
        >
          Model Settings
        </button>{' '}
        screen. It lands with the next wave, along with the voice engines.
      </p>
    </div>
  );
}
