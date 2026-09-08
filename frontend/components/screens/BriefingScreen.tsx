'use client';

import { useCallback, useEffect, useState } from 'react';

import { ConnectorPicker } from '@/components/connectors/ConnectorPicker';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Field, inputClass } from '@/components/ui/Field';
import { PageHeader } from '@/components/ui/PageHeader';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type { BriefingConfig, BriefingPreview, Connector } from '@/lib/api-types';
import { isPickable } from '@/lib/connectors';

/**
 * What Jarvis says at the start of the day.
 *
 * **The facts are gathered in code and only narrated**, which is the guarantee
 * the briefing exists to keep: it cannot invent an item, because the model is
 * never in a position to go and fetch one. Chosen connectors are the one
 * deliberate exception — you picked exactly those, so it may actually call them.
 *
 * **Weather and headlines have no controls here, on purpose.** They are things
 * Jarvis can already do, not sources to add — an earlier version of this screen
 * offered them through an "add a source" picker as though its own abilities were
 * installable skills, which is the exact confusion that keeps having to be
 * undone. They are shown read-only so a briefing mentioning the weather is not a
 * mystery, and you change them by asking.
 */

const SECTIONS: [keyof BriefingConfig['sections'], string, string][] = [
  ['greeting', 'A greeting', 'Good morning, and your name.'],
  ['dateTime', 'The date and time', 'What day it is and roughly when.'],
  ['tasks', 'What is scheduled', 'Anything on the clock for today.'],
  ['goals', 'Your goals and notes', 'What you have told it to keep in mind.'],
  ['focus', 'A suggested focus', 'One thing worth starting with.'],
  ['custom', 'Your own note', 'Anything you always want mentioned.'],
];

export function BriefingScreen({ onNavigate }: { onNavigate?: (id: string) => void }) {
  const [config, setConfig] = useState<BriefingConfig | null>(null);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [preview, setPreview] = useState<BriefingPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [saved, apps] = await Promise.all([api.briefing.get(), api.connectors.list()]);
      setConfig(saved);
      setConnectors(apps.connectors.filter(isPickable));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your briefing settings.');
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Saved as you change it — this is a settings screen, not a form with a
   *  Save button that can be forgotten. Merged server-side, so sending one key
   *  never drops the others. */
  async function save(patch: Partial<BriefingConfig>) {
    setConfig((current) => (current ? { ...current, ...patch } : current));
    try {
      setConfig(await api.briefing.save(patch));
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not save.');
      await load();  // it did not take; show what is really stored
    }
  }

  async function runPreview() {
    setPreviewing(true);
    setPreview(null);
    try {
      setPreview(await api.briefing.preview());
    } catch (err) {
      setPreview({ ok: false, text: '',
                   error: err instanceof ApiRequestError ? err.message : 'Could not put one together.' });
    } finally {
      setPreviewing(false);
    }
  }

  if (!config) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-10">
        <PageHeader title="Morning Briefing" />
        {error && <p className="text-[13px] text-state-danger">{error}</p>}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <PageHeader
        title="Morning Briefing"
        blurb="What Jarvis tells you at the start of the day. It only ever says what it actually found."
      >
        <Button tone="primary" data-testid="briefing-preview" disabled={previewing}
                onClick={() => void runPreview()}>
          {previewing ? 'Putting one together…' : 'Hear one now'}
        </Button>
      </PageHeader>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {preview && (
        <Card className="mb-4" data-testid="briefing-result">
          {preview.ok ? (
            <>
              <p className="whitespace-pre-wrap text-[14px] leading-relaxed text-ink">
                {preview.text}
              </p>
              {preview.modelId && (
                <p className="mt-2 text-[12px] text-ink-faint">Written by {preview.modelId}.</p>
              )}
            </>
          ) : (
            <>
              <p className="text-[13px] text-state-danger">{preview.text || 'That did not work.'}</p>
              {preview.error && <p className="mt-1 text-[12px] text-ink-faint">{preview.error}</p>}
            </>
          )}
        </Card>
      )}

      <Card className="mb-4">
        <p className="mb-1 text-[13px] font-medium text-ink">What it includes</p>
        <div data-testid="briefing-sections">
          {SECTIONS.map(([key, label, hint]) => (
            <div key={key} className="flex items-center justify-between gap-4 py-2.5">
              <div className="min-w-0">
                <p className="text-[14px] text-ink">{label}</p>
                <p className="mt-0.5 text-[12px] text-ink-faint">{hint}</p>
              </div>
              <Toggle
                label={label}
                data-testid={`briefing-${key}`}
                checked={config.sections[key]}
                onChange={(on) => void save({ sections: { ...config.sections, [key]: on } })}
              />
            </div>
          ))}
        </div>

        {config.sections.custom && (
          <Field label="Your note" hint="Said as part of the briefing, in its own words.">
            <textarea
              className={inputClass}
              rows={2}
              data-testid="briefing-custom"
              placeholder="Remind me to stretch"
              value={config.customText}
              onChange={(event) =>
                setConfig({ ...config, customText: event.target.value })}
              onBlur={(event) => void save({ customText: event.target.value })}
            />
          </Field>
        )}
      </Card>

      <Card className="mb-4">
        <p className="mb-2 text-[13px] font-medium text-ink">Apps it may check</p>
        <p className="mb-3 text-[12px] text-ink-faint">
          Nothing is included automatically. Only what you tick here can be looked at,
          and it still says only what a real answer came back with.
        </p>
        <ConnectorPicker
          connectors={connectors}
          selected={config.connectors}
          onChange={(next) => void save({ connectors: next })}
          onManage={onNavigate ? () => onNavigate('app-control') : undefined}
        />
      </Card>

      {/* Read-only, deliberately. These are abilities Jarvis already has rather
          than sources to attach, and offering them as attachable is the exact
          confusion this project keeps having to undo. Shown rather than hidden
          so a briefing that mentions the weather is not a mystery. */}
      <Card>
        <p className="mb-1 text-[13px] font-medium text-ink">Set by asking</p>
        <p className="mb-3 text-[12px] text-ink-faint">
          These are things Jarvis can already do, so there is nothing to add — just
          tell it, in a conversation or out loud.
        </p>
        <div className="flex items-center justify-between gap-4 py-2" data-testid="briefing-weather">
          <p className="text-[14px] text-ink">Weather</p>
          <p className="text-[13px] text-ink-muted">
            {config.weatherPlace
              ? config.weatherPlace
              : 'Not mentioned — say “set my weather to Lagos”'}
          </p>
        </div>
        <div className="flex items-center justify-between gap-4 py-2" data-testid="briefing-headlines">
          <p className="text-[14px] text-ink">Headlines</p>
          <p className="text-[13px] text-ink-muted">
            {config.headlines ? 'Included' : 'Not mentioned — say “turn on headlines”'}
          </p>
        </div>
      </Card>
    </div>
  );
}
