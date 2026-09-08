'use client';

import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { DiscoveredModel, Provider } from '@/lib/api-types';

/**
 * Adding a model, in the three steps it actually takes.
 *
 * **Pick a provider, not a wire format.** The tiles name OpenAI, Anthropic,
 * Gemini, a local server, or something custom — never "openai-compatible",
 * which is an implementation detail that used to leak straight into this screen.
 * A gateway (OpenRouter, Groq, Together, anything else) goes through Custom.
 *
 * **Custom has no fixed wire format, so it is probed.** The server tries the
 * OpenAI shape, then Anthropic's, then Gemini's, against the real address, and
 * reports every attempt in plain language. That report is shown on success AND
 * on failure — the single sentence "that connection didn't work" is the exact
 * failure this whole flow was rebuilt to fix.
 *
 * **Nothing is saved until models are chosen.** The key is held in this
 * component until the final step, and only then does it leave the browser.
 */
type Step = 'provider' | 'connect' | 'models';

export function AddModelFlow({
  open,
  onClose,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  onAdded: () => void;
}) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [step, setStep] = useState<Step>('provider');
  const [provider, setProvider] = useState<Provider | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [secret, setSecret] = useState('');
  const [label, setLabel] = useState('');
  const [found, setFound] = useState<DiscoveredModel[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [resolved, setResolved] = useState<Record<string, unknown> | null>(null);
  const [steps, setSteps] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    api.models.providers().then((answer) => setProviders(answer.providers)).catch(() => undefined);
  }, [open]);

  function reset() {
    setStep('provider');
    setProvider(null);
    setBaseUrl('');
    setSecret('');
    setLabel('');
    setFound([]);
    setPicked([]);
    setResolved(null);
    setSteps([]);
    setError(null);
  }

  function choose(entry: Provider) {
    setProvider(entry);
    setBaseUrl(entry.baseUrl ?? '');
    setLabel(entry.label);
    setError(null);
    setSteps([]);
    setStep('connect');
  }

  async function findModels() {
    if (!provider) return;
    setBusy(true);
    setError(null);
    setSteps([]);
    try {
      if (provider.id === 'custom') {
        const probe = await api.connections.probe(baseUrl, secret || undefined);
        setSteps(probe.steps);
        if (!probe.ok) {
          setError(probe.error || 'That address could not be used.');
          return;
        }
        setResolved({
          adapter: probe.adapter, baseUrl: probe.baseUrl,
          kind: probe.kind, keyRequired: probe.keyRequired,
        });
        setFound(probe.models);
      } else {
        const answer = await api.connections.discover({
          adapter: undefined, baseUrl: baseUrl || undefined, secret: secret || undefined,
        });
        if (answer.error) {
          setError(answer.error);
          return;
        }
        setFound(answer.models);
      }
      setStep('models');
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!provider || picked.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const answer = await api.connections.add({
        provider: provider.id,
        baseUrl: baseUrl || undefined,
        secret: secret || undefined,
        label: label || provider.label,
        models: found.filter((m) => picked.includes(m.model)),
        resolved: resolved ?? undefined,
      });
      if (answer.failed?.length) {
        setError(`Added ${answer.added.length}. ${answer.failed[0]!.error}`);
      }
      onAdded();
      reset();
      onClose();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That could not be added.');
    } finally {
      setBusy(false);
    }
  }

  const needsAddress = provider?.urlEditable || provider?.id === 'custom';

  return (
    <Modal
      open={open}
      title={step === 'provider' ? 'Add a model' : `Add a model · ${provider?.label ?? ''}`}
      onClose={() => {
        reset();
        onClose();
      }}
      footer={
        step === 'connect' ? (
          <>
            <Button tone="primary" disabled={busy} data-testid="find-models" onClick={findModels}>
              {busy ? 'Looking…' : 'Find models'}
            </Button>
            <Button onClick={() => setStep('provider')}>Back</Button>
          </>
        ) : step === 'models' ? (
          <>
            <Button tone="primary" disabled={busy || !picked.length} data-testid="add-models" onClick={save}>
              {busy ? 'Adding…' : `Add ${picked.length || ''}`.trim()}
            </Button>
            <Button onClick={() => setStep('connect')}>Back</Button>
          </>
        ) : undefined
      }
    >
      {error && <p className="mb-3 text-[13px] text-state-danger">{error}</p>}

      {step === 'provider' && (
        <div className="grid grid-cols-2 gap-2" data-testid="provider-tiles">
          {providers.map((entry) => (
            <button
              key={entry.id}
              type="button"
              data-testid={`provider-${entry.id}`}
              onClick={() => choose(entry)}
              className="flex items-center gap-3 rounded-lg border border-surface-border
                         bg-surface-raised/60 p-3 text-left transition duration-150 ease-out
                         hover:border-surface-border-strong hover:bg-surface-raised
                         focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
            >
              <span
                aria-hidden
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded text-[16px]"
                style={{ background: entry.iconBg }}
              >
                {entry.icon}
              </span>
              <span className="text-[14px] text-ink">{entry.label}</span>
            </button>
          ))}
        </div>
      )}

      {step === 'connect' && provider && (
        <>
          {needsAddress && (
            <Field
              label="Address"
              hint={provider.id === 'custom'
                ? 'Whatever the service gave you. Jarvis works out what is behind it.'
                : undefined}
            >
              <input
                className={inputClass}
                value={baseUrl}
                data-testid="base-url"
                placeholder="http://localhost:11434/v1"
                onChange={(event) => setBaseUrl(event.target.value)}
              />
            </Field>
          )}

          <Field label="Key" hint={provider.keyHint}>
            <input
              type="password"
              className={inputClass}
              value={secret}
              data-testid="secret"
              autoComplete="off"
              placeholder={provider.keyRequired ? '' : 'Usually not needed'}
              onChange={(event) => setSecret(event.target.value)}
            />
          </Field>

          <Field label="Name" hint="What this connection is called in your own list.">
            <input
              className={inputClass}
              value={label}
              onChange={(event) => setLabel(event.target.value)}
            />
          </Field>

          {steps.length > 0 && <Attempts steps={steps} />}
        </>
      )}

      {step === 'models' && (
        <>
          {found.length === 0 ? (
            <p className="py-6 text-center text-[13px] text-ink-muted">
              That address answered, but offered no models.
            </p>
          ) : (
            <div className="-mx-1" data-testid="found-models">
              {found.map((model) => {
                const on = picked.includes(model.model);
                return (
                  <button
                    key={model.model}
                    type="button"
                    data-testid={`found-${model.model}`}
                    onClick={() => setPicked(on
                      ? picked.filter((m) => m !== model.model)
                      : [...picked, model.model])}
                    className={`flex w-full items-center gap-3 rounded px-2.5 py-2 text-left
                                transition ${on ? 'bg-accent/[0.10]' : 'hover:bg-white/[0.04]'}`}
                  >
                    <span
                      aria-hidden
                      className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border
                                  text-[10px] ${on ? 'border-accent bg-accent text-surface' : 'border-surface-border-strong'}`}
                    >
                      {on ? '✓' : ''}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[13px] text-ink">{model.label}</span>
                    {model.billing === 'free' && (
                      <span className="rounded-pill bg-state-ok/10 px-2 py-0.5 text-[10px] text-state-ok">
                        free
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
          {steps.length > 0 && <Attempts steps={steps} />}
        </>
      )}
    </Modal>
  );
}

/** What the probe actually tried. Shown on success as well as failure: knowing
 *  it found the OpenAI shape at `/v1` is how someone confirms it guessed right. */
function Attempts({ steps }: { steps: string[] }) {
  return (
    <details className="mt-3 rounded border border-surface-border bg-white/[0.02] p-2.5">
      <summary className="cursor-pointer text-[12px] text-ink-muted">What Jarvis tried</summary>
      <ul className="mt-2 space-y-1">
        {steps.map((line, index) => (
          <li key={index} className="text-[11px] leading-relaxed text-ink-faint">{line}</li>
        ))}
      </ul>
    </details>
  );
}
