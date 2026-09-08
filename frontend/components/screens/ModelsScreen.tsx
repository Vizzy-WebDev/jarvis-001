'use client';

import { useCallback, useEffect, useState } from 'react';

import { AddModelFlow } from '@/components/models/AddModelFlow';
import { AppIcon } from '@/components/ui/AppIcon';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type {
  ConnectionEntry, ExternalService, ModelEntry, ModelHealth,
} from '@/lib/api-types';

/**
 * What Jarvis can think with.
 *
 * Grouped by CONNECTION, not flat by model, because a connection is the thing
 * that owns a key and an address: two models from the same place share one
 * credential, and a problem is almost always the connection's rather than any
 * one model's. Removing a connection takes its models with it, and the screen
 * says how many before it happens.
 *
 * The badges come from real, measured availability — a model this build has
 * actually failed to reach, with the reason and how long until it is retried —
 * never a guess from the model's name.
 */
export function ModelsScreen() {
  const [models, setModels] = useState<ModelEntry[] | null>(null);
  const [connections, setConnections] = useState<ConnectionEntry[]>([]);
  const [health, setHealth] = useState<ModelHealth>({});
  const [services, setServices] = useState<ExternalService[]>([]);
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<ModelEntry | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const answer = await api.models.list();
      setModels(answer.models);
      setConnections(answer.connections);
      setHealth(answer.health);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your models.');
      setModels([]);
    }
    try {
      setServices((await api.externalServices.list()).services);
    } catch {
      /* the models half of this screen still works without it */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function recheck() {
    setChecking(true);
    setError(null);
    try {
      // The cheap scope by default: a model already answering needs no proof,
      // and proving it again costs a real request on a roster that is routinely
      // rate-limited.
      await api.models.recheck('not_working');
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Those checks did not run.');
    } finally {
      setChecking(false);
    }
  }

  async function setEnabled(model: ModelEntry, enabled: boolean) {
    setModels((current) =>
      (current ?? []).map((m) => (m.id === model.id ? { ...m, enabled } : m)));
    await api.models.update(model.id, { enabled }).catch(() => void load());
  }

  async function removeConnection(connection: ConnectionEntry) {
    await api.connections.remove(connection.id).catch(() => undefined);
    await load();
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Button tone="primary" data-testid="add-model" onClick={() => setAdding(true)}>
          Add a model
        </Button>
        <Button
          data-testid="recheck"
          disabled={checking || !(models ?? []).length}
          onClick={() => void recheck()}
        >
          {checking ? 'Checking…' : 'Check what is not working'}
        </Button>
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {models === null ? (
        <p className="py-10 text-center text-[13px] text-ink-faint">Reading…</p>
      ) : connections.length === 0 ? (
        <EmptyState
          title="Jarvis has nothing to think with yet."
          body="Add a model and it can answer. Anything OpenAI-shaped works, including a server running on this machine — and nothing leaves the machine unless the model you pick lives somewhere else."
          action={<Button tone="primary" onClick={() => setAdding(true)}>Add a model</Button>}
        />
      ) : (
        <div className="space-y-3" data-testid="connection-list">
          {connections.map((connection) => (
            <ConnectionCard
              key={connection.id}
              connection={connection}
              models={(models ?? []).filter((m) => m.connectionId === connection.id)}
              health={health}
              onOpen={setOpen}
              onEnabled={setEnabled}
              onRemove={() => void removeConnection(connection)}
            />
          ))}
        </div>
      )}

      <ServiceKeys services={services} onChanged={load} />

      <AddModelFlow open={adding} onClose={() => setAdding(false)} onAdded={load} />

      <ModelDetail
        model={open}
        health={health}
        onClose={() => setOpen(null)}
        onChanged={async () => {
          setOpen(null);
          await load();
        }}
      />
    </>
  );
}

function ConnectionCard({
  connection,
  models,
  health,
  onOpen,
  onEnabled,
  onRemove,
}: {
  connection: ConnectionEntry;
  models: ModelEntry[];
  health: ModelHealth;
  onOpen: (model: ModelEntry) => void;
  onEnabled: (model: ModelEntry, enabled: boolean) => void;
  onRemove: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <Card className="p-0" data-testid="connection-card">
      <div className="flex items-start gap-3 p-4">
        <AppIcon label={connection.label} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[14px] text-ink">{connection.label}</p>
          <p className="mt-0.5 truncate text-[12px] text-ink-faint">
            {connection.baseUrl || connection.adapter}
            {' · '}
            {connection.hasSecret ? 'key set' : 'no key'}
            {' · '}
            {connection.modelCount} model{connection.modelCount === 1 ? '' : 's'}
          </p>
        </div>
        <Button tone="danger" onClick={() => setConfirming(true)}>Remove</Button>
      </div>

      {models.length > 0 && (
        <div className="border-t border-surface-border">
          {models.map((model) => (
            <div
              key={model.id}
              data-testid="model-row"
              className="flex items-center gap-3 border-b border-surface-border px-4 py-2.5 last:border-b-0"
            >
              <button
                type="button"
                data-testid="model-open"
                onClick={() => onOpen(model)}
                className="min-w-0 flex-1 text-left focus-visible:outline-none"
              >
                <span className={`block truncate text-[13px] ${model.enabled ? 'text-ink' : 'text-ink-faint'}`}>
                  {model.label}
                </span>
                <Badge model={model} health={health} />
              </button>
              <Toggle
                label={`${model.enabled ? 'Turn off' : 'Turn on'} ${model.label}`}
                checked={model.enabled}
                onChange={(next) => onEnabled(model, next)}
              />
            </div>
          ))}
        </div>
      )}

      <Modal
        nested
        open={confirming}
        title={`Remove ${connection.label}?`}
        onClose={() => setConfirming(false)}
        footer={
          <>
            <Button tone="danger" data-testid="confirm-remove" onClick={onRemove}>
              Remove it
            </Button>
            <Button onClick={() => setConfirming(false)}>Keep it</Button>
          </>
        }
      >
        <p className="text-[14px] leading-relaxed text-ink">
          {connection.modelCount === 0
            ? 'Nothing is using this connection.'
            : `Its ${connection.modelCount} model${connection.modelCount === 1 ? '' : 's'} `
              + 'will go too — they cannot answer without its key.'}
        </p>
      </Modal>
    </Card>
  );
}

/** What is actually known about whether this model answers. */
function Badge({ model, health }: { model: ModelEntry; health: ModelHealth }) {
  const trouble = health[model.id];
  if (trouble) {
    return (
      <span className="mt-0.5 block truncate text-[11px] text-state-danger">
        {trouble.reason || trouble.kind || 'Not answering'}
        {trouble.retryInMs > 0 && ` · retrying in ${Math.ceil(trouble.retryInMs / 60000)}m`}
      </span>
    );
  }
  if (!model.ready) {
    return <span className="mt-0.5 block text-[11px] text-state-warn">Needs a key</span>;
  }
  return <span className="mt-0.5 block truncate text-[11px] text-ink-faint">{model.model}</span>;
}

function ModelDetail({
  model,
  health,
  onClose,
  onChanged,
}: {
  model: ModelEntry | null;
  health: ModelHealth;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [label, setLabel] = useState('');
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setLabel(model?.label ?? '');
    setResult(null);
  }, [model]);

  if (!model) return null;
  const trouble = health[model.id];

  return (
    <Modal
      open
      title={model.label}
      onClose={onClose}
      footer={
        <>
          <Button
            tone="primary"
            disabled={busy}
            data-testid="save-model"
            onClick={async () => {
              setBusy(true);
              await api.models.update(model.id, { label }).catch(() => undefined);
              setBusy(false);
              onChanged();
            }}
          >
            Save
          </Button>
          <Button
            disabled={busy}
            data-testid="test-model"
            onClick={async () => {
              setBusy(true);
              setResult(null);
              try {
                const answer = await api.models.test(model.id);
                setResult(answer.ok ? 'It answered.' : answer.error || 'It did not answer.');
              } catch (err) {
                setResult(err instanceof ApiRequestError ? err.message : 'It did not answer.');
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? 'Testing…' : 'Test it'}
          </Button>
          <Button
            tone="danger"
            className="ml-auto"
            onClick={async () => {
              await api.models.remove(model.id).catch(() => undefined);
              onChanged();
            }}
          >
            Remove
          </Button>
        </>
      }
    >
      <Field label="Name" hint="What you call it. The model itself is unchanged.">
        <input className={inputClass} value={label} data-testid="model-label"
               onChange={(event) => setLabel(event.target.value)} />
      </Field>

      <dl className="mt-2 space-y-2 border-t border-surface-border pt-3 text-[12px]">
        <Detail term="Model" value={model.model} />
        <Detail term="Ready" value={model.ready ? 'Yes' : 'No — its connection needs a key'} />
        {trouble && (
          <Detail term="Last problem" value={trouble.reason || trouble.kind || 'Not answering'} />
        )}
      </dl>

      {result && <p className="mt-3 text-[13px] text-ink">{result}</p>}
    </Modal>
  );
}

function Detail({ term, value }: { term: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-ink-faint">{term}</dt>
      <dd className="min-w-0 truncate text-ink-muted">{value}</dd>
    </div>
  );
}

/**
 * Keys for services that are not model providers — speech recognition, a paid
 * voice. Deliberately a separate store from a model connection's key, so a
 * mistake here cannot corrupt a connection that is currently answering.
 */
function ServiceKeys({
  services,
  onChanged,
}: {
  services: ExternalService[];
  onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [label, setLabel] = useState('');
  const [key, setKey] = useState('');
  const [error, setError] = useState<string | null>(null);

  return (
    <section className="mt-8">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-medium text-ink">Other service keys</h2>
          <p className="mt-0.5 text-[12px] text-ink-muted">
            For things that are not models — speech recognition, a paid voice.
          </p>
        </div>
        <Button data-testid="add-service" onClick={() => setAdding(true)}>Add a key</Button>
      </div>

      {services.length === 0 ? (
        <Card className="text-[13px] text-ink-faint">Nothing added yet.</Card>
      ) : (
        <div className="space-y-2" data-testid="service-list">
          {services.map((service) => (
            <Card key={service.ref} className="flex items-center gap-3">
              <AppIcon label={service.label} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-[14px] text-ink">{service.label}</p>
                <p className="text-[12px] text-ink-faint">
                  {service.configured ? 'Key set' : 'No key'}
                </p>
              </div>
              <Button
                tone="danger"
                onClick={async () => {
                  await api.externalServices.remove(service.ref).catch(() => undefined);
                  onChanged();
                }}
              >
                Remove
              </Button>
            </Card>
          ))}
        </div>
      )}

      <Modal
        open={adding}
        title="Add a key"
        onClose={() => {
          setAdding(false);
          setError(null);
        }}
        footer={
          <Button
            tone="primary"
            data-testid="save-service"
            onClick={async () => {
              setError(null);
              try {
                await api.externalServices.add({ label, key });
                setLabel('');
                setKey('');
                setAdding(false);
                onChanged();
              } catch (err) {
                setError(err instanceof ApiRequestError ? err.message : 'That could not be saved.');
              }
            }}
          >
            Save
          </Button>
        }
      >
        {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}
        <Field label="Service" hint="Whatever it is called — Deepgram, ElevenLabs, anything.">
          <input className={inputClass} value={label} data-testid="service-label"
                 onChange={(event) => setLabel(event.target.value)} />
        </Field>
        <Field label="Key">
          <input type="password" className={inputClass} value={key} data-testid="service-key"
                 autoComplete="off" onChange={(event) => setKey(event.target.value)} />
        </Field>
      </Modal>
    </section>
  );
}
