'use client';

import { useMemo, useRef, useState } from 'react';

import { ChevronIcon, CheckIcon } from '@/components/ui/Icons';
import { Popover } from '@/components/ui/Popover';
import { api, ApiRequestError } from '@/lib/api';
import type { ProviderConnection, ProviderModel } from '@/lib/api-types';
import { announceModelsChanged, effortLabel, useModels } from '@/lib/useModels';

/** Past this many models, a search box earns its space. */
const SEARCH_FROM = 8;

/**
 * Which model Jarvis answers with — and, for a model whose provider says it has
 * levels, how hard it works.
 *
 * Two separate choices, kept apart on purpose. **The model** is a choice among the
 * models available through the connected providers. **Effort** is not a model and
 * never creates one: it is a setting on the selected model, and it appears ONLY
 * when the provider itself reported which levels that model accepts. Nothing here
 * knows what levels exist — they arrive with the model — so a model that reported
 * none simply has no effort section, and nothing is offered on its behalf.
 */
export function ModelPicker() {
  const { overview, refresh } = useModels();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);
  const anchor = useRef<HTMLButtonElement>(null);

  const connections = overview?.connections ?? [];
  const selection = overview?.selection;
  const availability = overview?.availability;

  const selected = useMemo(() => {
    const connection = connections.find((c) => c.id === selection?.providerId);
    const model = connection?.models.find((m) => m.id === selection?.modelId);
    return connection && model ? { connection, model } : null;
  }, [connections, selection]);

  const total = connections.reduce((n, c) => n + c.models.length, 0);
  const needle = query.trim().toLowerCase();
  const matches = (model: ProviderModel) =>
    !needle || model.label.toLowerCase().includes(needle) || model.id.toLowerCase().includes(needle);

  // What the trigger says. A selection that cannot be run says so here, where the
  // person will see it, rather than showing a model name that looks fine.
  const trouble = availability && availability.state !== 'ok' && availability.state !== 'none';
  const label = selected
    ? selected.model.label
    : connections.length
      ? 'Choose a model'
      : 'Add a model';

  async function choose(connection: ProviderConnection, model: ProviderModel) {
    setError(null);
    try {
      await api.models.select({ providerId: connection.id, modelId: model.id, effort: null });
      announceModelsChanged();
      // No effort to choose for this model, so there is nothing more to do here.
      if (!model.effort) setOpen(false);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That model could not be selected.');
    }
  }

  async function setEffort(level: string) {
    if (!selected) return;
    setError(null);
    try {
      await api.models.select({
        providerId: selected.connection.id,
        modelId: selected.model.id,
        effort: level,
      });
      announceModelsChanged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That could not be set.');
    }
  }

  const effort = selected?.model.effort ?? null;
  const currentEffort = selection?.effort ?? effort?.default ?? null;

  return (
    <>
      <button
        ref={anchor}
        type="button"
        data-testid="model-picker"
        aria-haspopup="dialog"
        aria-expanded={open}
        title={trouble ? (availability?.message ?? undefined) : 'Choose which model Jarvis uses'}
        onClick={() => {
          setOpen((now) => !now);
          setQuery('');
          setError(null);
          void refresh();
        }}
        className={[
          'inline-flex h-9 max-w-[11rem] items-center gap-1.5 rounded-full px-2.5 text-[12px]',
          'transition duration-150 ease-out hover:bg-white/[0.06]',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
          trouble ? 'text-state-warn' : 'text-ink-muted hover:text-ink',
        ].join(' ')}
      >
        <span className="truncate" data-testid="model-picker-label">{label}</span>
        <ChevronIcon className="h-3.5 w-3.5 shrink-0" />
      </button>

      <Popover open={open} anchorRef={anchor} onClose={() => setOpen(false)} width={320}>
        {trouble && availability?.message && (
          <p data-testid="picker-warning" className="border-b border-surface-border px-3.5 py-2.5 text-[12px] leading-relaxed text-state-warn">
            {availability.message}
          </p>
        )}

        {connections.length === 0 ? (
          <div className="px-3.5 py-4 text-[13px] leading-relaxed text-ink-muted">
            <p>No provider is connected yet.</p>
            <a
              href="#/models"
              data-testid="picker-open-settings"
              onClick={() => setOpen(false)}
              className="mt-2 inline-block text-accent underline-offset-2 hover:underline"
            >
              Connect one on the Model Settings screen
            </a>
          </div>
        ) : (
          <>
            {total > SEARCH_FROM && (
              <div className="border-b border-surface-border px-2.5 py-2">
                <input
                  value={query}
                  data-testid="picker-search"
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search models"
                  aria-label="Search models"
                  className="w-full rounded border border-surface-border bg-surface/60 px-2.5 py-1.5 text-[13px] text-ink outline-none placeholder:text-ink-faint focus:border-accent/50"
                />
              </div>
            )}
            <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto py-1" data-testid="picker-models">
              {connections.map((connection) => {
                const shown = connection.models.filter(matches);
                if (!shown.length && needle) return null;
                return (
                  <div key={connection.id}>
                    <p className="flex items-center gap-2 px-3.5 pb-1 pt-2.5 text-[11px] uppercase tracking-[0.08em] text-ink-faint">
                      <span className="truncate">{connection.label}</span>
                      {connection.state === 'error' && (
                        <span className="normal-case tracking-normal text-state-danger">needs attention</span>
                      )}
                    </p>
                    {connection.models.length === 0 && (
                      <p className="px-3.5 py-1.5 text-[12px] text-ink-faint">
                        No models yet — add one on the Model Settings screen.
                      </p>
                    )}
                    {shown.map((model) => {
                      const active =
                        selection?.providerId === connection.id && selection?.modelId === model.id;
                      return (
                        <button
                          key={model.id}
                          type="button"
                          data-testid="pick-model"
                          data-model-id={model.id}
                          aria-pressed={active}
                          onClick={() => void choose(connection, model)}
                          className="flex w-full items-center gap-2 px-3.5 py-2 text-left text-[13px] text-ink transition hover:bg-white/[0.05]"
                        >
                          <span className="min-w-0 flex-1 truncate">{model.label}</span>
                          {active && <CheckIcon className="h-4 w-4 shrink-0 text-accent" />}
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </div>

            {effort && selected && (
              <div className="border-t border-surface-border py-1" data-testid="effort-section">
                <p className="px-3.5 pb-1 pt-2 text-[12px] leading-relaxed text-ink-faint">
                  More effort means a more thorough answer, but it takes longer and uses more of your allowance.
                </p>
                {effort.levels.map((level) => (
                  <button
                    key={level}
                    type="button"
                    data-testid={`effort-${level}`}
                    aria-pressed={currentEffort === level}
                    onClick={() => void setEffort(level)}
                    className="flex w-full items-center gap-2 px-3.5 py-2 text-left text-[13px] text-ink transition hover:bg-white/[0.05]"
                  >
                    <span>{effortLabel(level)}</span>
                    {level === effort.default && (
                      <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-ink-muted">Default</span>
                    )}
                    {currentEffort === level && <CheckIcon className="ml-auto h-4 w-4 shrink-0 text-accent" />}
                  </button>
                ))}
              </div>
            )}
          </>
        )}

        {error && (
          <p data-testid="picker-error" className="border-t border-surface-border px-3.5 py-2 text-[12px] text-state-danger">
            {error}
          </p>
        )}
      </Popover>
    </>
  );
}
