'use client';

import { useState } from 'react';

import { AppIcon } from '@/components/ui/AppIcon';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { inputClass } from '@/components/ui/Field';
import { CloseIcon } from '@/components/ui/Icons';
import { api, ApiRequestError } from '@/lib/api';
import type { ProviderConnection } from '@/lib/api-types';
import { announceModelsChanged } from '@/lib/useModels';

export type Notice = { tone: 'ok' | 'warn'; text: string };

/** Past this many models, a search box earns its space. */
const SEARCH_FROM = 8;

const DOT = { ok: 'bg-state-ok', error: 'bg-state-danger', untested: 'bg-ink-faint' } as const;
const STATE_WORDS = { ok: 'Connected', error: 'Needs attention', untested: 'Not tested yet' } as const;

function keyLine(connection: ProviderConnection): { text: string; bad: boolean } {
  if (connection.hasKey) return { text: 'Key saved', bad: false };
  if (connection.keyNeeded === 'required') return { text: 'No key saved', bad: true };
  if (connection.keyNeeded === 'optional') return { text: 'No key (optional)', bad: false };
  return { text: 'No key needed', bad: false };
}

/**
 * One provider connection — the thing the person manages.
 *
 * Says which provider it is, where it is, whether a key is saved, whether it was
 * last found working, and which models are available through it. Its three
 * actions are kept apart because they answer different questions: **Test** asks
 * whether the connection works and is the only thing that changes its status;
 * **Refresh models** asks what it offers and never touches the status. There is
 * no "use" or "enable" step: a model that is listed is available, and which one
 * answers is chosen in the composer (or left to Auto). Adding a model by hand is always here, because a
 * provider that cannot list its models — or could not just now — is still usable.
 */
export function ConnectionCard({
  connection,
  onNotice,
  onEdit,
  onDelete,
}: {
  connection: ProviderConnection;
  onNotice: (notice: Notice | null) => void;
  onEdit: (connection: ProviderConnection) => void;
  onDelete: (connection: ProviderConnection) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [typed, setTyped] = useState('');
  const [error, setError] = useState<string | null>(null);

  const key = keyLine(connection);
  const needle = query.trim().toLowerCase();
  const shown = connection.models.filter(
    (model) => !needle || model.label.toLowerCase().includes(needle) || model.id.toLowerCase().includes(needle),
  );

  async function run(name: string, work: () => Promise<void>) {
    setError(null);
    setBusy(name);
    try {
      await work();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
    } finally {
      setBusy(null);
    }
  }

  const test = () => run('test', async () => {
    const result = await api.models.test(connection.id);
    onNotice({ tone: result.ok ? 'ok' : 'warn', text: `${connection.label}: ${result.message}` });
    announceModelsChanged();
  });

  const refreshModels = () => run('discover', async () => {
    try {
      const result = await api.models.discover(connection.id);
      onNotice({
        tone: 'ok',
        text: result.added
          ? `${connection.label}: found ${result.added} new model${result.added === 1 ? '' : 's'}.`
          : `${connection.label}: nothing new — the list is up to date.`,
      });
    } catch (err) {
      // Two different answers, and neither blocks adding a model by hand below.
      if (err instanceof ApiRequestError && (err.status === 501 || err.status === 502)) {
        onNotice({ tone: 'warn', text: `${connection.label}: ${err.message}` });
      } else {
        throw err;
      }
    }
    announceModelsChanged();
  });

  const addTyped = () => {
    const modelId = typed.trim();
    if (!modelId) return;
    void run('add', async () => {
      await api.models.addModel(connection.id, modelId);
      setTyped('');
      announceModelsChanged();
    });
  };

  const remove = (modelId: string) => run(`remove:${modelId}`, async () => {
    await api.models.removeModel(connection.id, modelId);
    announceModelsChanged();
  });

  return (
    <Card data-testid="connection-card" data-connection-label={connection.label} className="space-y-3">
      <div className="flex items-start gap-3">
        <AppIcon label={connection.kindLabel} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[14px] text-ink">{connection.label}</p>
          <p className="truncate text-[12px] text-ink-faint">
            {connection.kindLabel}
            {connection.addressEditable && ` · ${connection.address}`}
          </p>
          <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px]">
            <span className="inline-flex items-center gap-1.5 text-ink-muted" data-testid="connection-status"
                  data-state={connection.state}>
              <span aria-hidden className={`h-2 w-2 rounded-full ${DOT[connection.state]}`} />
              {STATE_WORDS[connection.state]}
            </span>
            <span className={key.bad ? 'text-state-danger' : 'text-ink-faint'}>{key.text}</span>
            <span className="text-ink-faint">
              {connection.models.length} model{connection.models.length === 1 ? '' : 's'}
            </span>
          </p>
          {connection.detail && connection.state !== 'untested' && (
            <p data-testid="connection-detail"
               className={`mt-1 text-[12px] leading-relaxed ${connection.state === 'error' ? 'text-state-danger' : 'text-ink-faint'}`}>
              {connection.detail}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
          <Button data-testid="test-connection" disabled={busy !== null} onClick={test}>
            {busy === 'test' ? 'Testing…' : 'Test'}
          </Button>
          <Button data-testid="edit-connection" onClick={() => onEdit(connection)}>Edit</Button>
          <Button tone="danger" data-testid="delete-connection" onClick={() => onDelete(connection)}>Delete</Button>
        </div>
      </div>

      {error && <p data-testid="connection-error" className="text-[12px] text-state-danger">{error}</p>}

      <div className="rounded border border-surface-border">
        <div className="flex items-center gap-2 px-3 py-2">
          <p className="flex-1 text-[12px] text-ink-muted">Models available through this connection</p>
          <Button data-testid="refresh-models" disabled={busy !== null} onClick={refreshModels}
                  className="!px-2.5 !py-1 !text-[12px]">
            {busy === 'discover' ? 'Looking…' : 'Refresh models'}
          </Button>
        </div>

        {connection.models.length > SEARCH_FROM && (
          <div className="border-t border-surface-border px-3 py-2">
            <input className={inputClass} value={query} placeholder="Search models"
                   aria-label="Search models" data-testid="model-search"
                   onChange={(event) => setQuery(event.target.value)} />
          </div>
        )}

        {connection.models.length === 0 ? (
          <p data-testid="no-models" className="border-t border-surface-border px-3 py-3 text-[12px] leading-relaxed text-ink-faint">
            No models listed. Add one below by its ID — you don’t need a list to use a model.
          </p>
        ) : (
          <ul className="scroll-quiet max-h-72 overflow-y-auto border-t border-surface-border" data-testid="model-list">
            {shown.map((model) => {
              return (
                <li key={model.id} data-testid="model-row" data-model-id={model.id}
                    className="flex items-center gap-2 border-b border-surface-border px-3 py-2 last:border-b-0">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13px] text-ink">{model.label}</p>
                    {(model.label !== model.id || model.source === 'manual' || !model.stillListed) && (
                      <p className="truncate text-[11px] text-ink-faint">
                        {model.label !== model.id && <span>{model.id}</span>}
                        {model.source === 'manual' && <span> · added by hand</span>}
                        {!model.stillListed && (
                          <span data-testid="not-listed"> · no longer listed by the provider — it may still work</span>
                        )}
                      </p>
                    )}
                  </div>
                  <button type="button" aria-label={`Remove ${model.label} from the list`}
                          title="Remove from this list" data-testid="remove-model"
                          disabled={busy !== null} onClick={() => void remove(model.id)}
                          className="rounded-full p-1 text-ink-faint transition hover:bg-white/[0.06] hover:text-ink disabled:opacity-40">
                    <CloseIcon className="h-3.5 w-3.5" />
                  </button>
                </li>
              );
            })}
            {shown.length === 0 && (
              <li className="px-3 py-3 text-[12px] text-ink-faint">No model matches that.</li>
            )}
          </ul>
        )}

        {/* Always here — whether or not a list was found. */}
        <form className="flex items-center gap-2 border-t border-surface-border px-3 py-2.5"
              onSubmit={(event) => { event.preventDefault(); addTyped(); }}>
          <input className={inputClass} value={typed} data-testid="add-model-input" autoComplete="off"
                 placeholder={`Add a model by its ID, as ${connection.kindLabel} names it`}
                 aria-label="Model ID" onChange={(event) => setTyped(event.target.value)} />
          <Button type="submit" data-testid="add-model-submit" disabled={busy !== null || !typed.trim()}>
            Add
          </Button>
        </form>
      </div>
    </Card>
  );
}
