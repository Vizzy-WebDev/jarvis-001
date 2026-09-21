'use client';

import { useEffect, useRef, useState } from 'react';

import { ConnectionCard, type Notice } from '@/components/models/ConnectionCard';
import { ConnectModal, EditModal } from '@/components/models/ConnectionForms';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { PlusIcon } from '@/components/ui/Icons';
import { Modal } from '@/components/ui/Modal';
import { Popover } from '@/components/ui/Popover';
import { api, ApiRequestError } from '@/lib/api';
import type { AddConnectionResult, ProviderConnection, ProviderFormat, ProviderKind, Prefs } from '@/lib/api-types';
import { announceModelsChanged, useModels } from '@/lib/useModels';

/**
 * Model Settings: connect a provider, see the models it makes available, and choose
 * one. The provider connection is the thing managed here — not each model, and not
 * a catalog of what models are.
 *
 * Deliberately not here: any per-model settings, any capability list, any "which
 * model does which job". One model is selected and every kind of turn uses it.
 */
export function ModelsScreen() {
  const { overview, failed } = useModels();
  const [kinds, setKinds] = useState<ProviderKind[]>([]);
  const [formats, setFormats] = useState<ProviderFormat[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [connecting, setConnecting] = useState<ProviderKind | null>(null);
  const [editing, setEditing] = useState<ProviderConnection | null>(null);
  const [deleting, setDeleting] = useState<ProviderConnection | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const addRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    api.models.kinds().then((k) => { setKinds(k.kinds); setFormats(k.formats); }).catch(() => undefined);
  }, []);

  const connections = overview?.connections ?? [];
  const selection = overview?.selection ?? { providerId: null, modelId: null, effort: null };
  const availability = overview?.availability;
  const trouble = availability && availability.state !== 'ok' && availability.state !== 'none';

  const inUse = (() => {
    const connection = connections.find((c) => c.id === selection.providerId);
    const model = connection?.models.find((m) => m.id === selection.modelId);
    return connection && model ? `${model.label} on ${connection.label}` : null;
  })();

  function added(result: AddConnectionResult) {
    setConnecting(null);
    const { connection, tested, discovery } = result;
    if (!tested.ok) {
      setNotice({ tone: 'warn', text: `${connection.label} was added, but isn’t working yet. ${tested.message}` });
    } else if (discovery?.ok) {
      setNotice({
        tone: 'ok',
        text: discovery.added
          ? `${connection.label} is connected, with ${discovery.added} model${discovery.added === 1 ? '' : 's'} available. Choose one to use.`
          : `${connection.label} is connected, but it lists no models yet — add one by its ID.`,
      });
    } else {
      setNotice({ tone: 'ok', text: `${connection.label}: ${tested.message}` });
    }
    announceModelsChanged();
  }

  async function confirmDelete() {
    if (!deleting) return;
    const gone = deleting;
    try {
      await api.models.remove(gone.id);
      setNotice({ tone: 'ok', text: `${gone.label} was deleted.` });
      announceModelsChanged();
    } catch (err) {
      setNotice({ tone: 'warn', text: err instanceof ApiRequestError ? err.message : 'That could not be deleted.' });
    }
    setDeleting(null);
  }

  return (
    <div className="space-y-5" data-testid="models-screen">
      <div className="flex items-start justify-between gap-4">
        <p className="max-w-xl text-[13px] leading-relaxed text-ink-muted">
          Connect a provider once and every model it offers becomes available. Then choose one to use —
          from here, or from the message box.
        </p>
        <span ref={addRef} className="shrink-0">
          <Button tone="primary" data-testid="add-provider" aria-haspopup="menu"
                  aria-expanded={menuOpen} onClick={() => setMenuOpen((now) => !now)}>
            <PlusIcon className="h-4 w-4" /> Add a provider
          </Button>
        </span>
        <Popover open={menuOpen} anchorRef={addRef} onClose={() => setMenuOpen(false)} width={320}>
          <div data-testid="add-provider-menu" className="py-1">
            {kinds.map((kind) => (
              <button key={kind.id} type="button" data-testid={`add-kind-${kind.id}`}
                      onClick={() => { setMenuOpen(false); setConnecting(kind); }}
                      className="block w-full px-3.5 py-2.5 text-left transition hover:bg-white/[0.05]">
                <span className="block text-[14px] text-ink">{kind.label}</span>
                <span className="block text-[12px] text-ink-faint">{kind.blurb}</span>
              </button>
            ))}
          </div>
        </Popover>
      </div>

      {notice && (
        <p data-testid="models-notice" role="status"
           className={`rounded border px-3 py-2 text-[13px] leading-relaxed ${
             notice.tone === 'ok'
               ? 'border-surface-border text-ink-muted'
               : 'border-state-warn/30 bg-state-warn/[0.06] text-state-warn'}`}>
          {notice.text}
        </p>
      )}

      {trouble && availability?.message && (
        <p data-testid="selection-warning" role="alert"
           className="rounded border border-state-warn/30 bg-state-warn/[0.06] px-3 py-2 text-[13px] leading-relaxed text-state-warn">
          {availability.message}
        </p>
      )}

      {inUse && !trouble && (
        <p data-testid="in-use-summary" className="text-[13px] text-ink-muted">
          Jarvis is using <span className="text-ink">{inUse}</span>.
        </p>
      )}

      {failed && !overview && (
        <p className="text-[13px] text-state-danger">Couldn’t load the connected providers. Is Jarvis running?</p>
      )}

      {overview && connections.length === 0 && (
        <EmptyState
          title="No provider is connected yet."
          body="Jarvis needs an AI model to think with. Add a provider — OpenAI, Anthropic, Gemini, or a model running on this computer — and choose a model."
        />
      )}

      <div className="space-y-3" data-testid="connection-list">
        {connections.map((connection) => (
          <ConnectionCard key={connection.id} connection={connection} selection={selection}
                          onNotice={setNotice} onEdit={setEditing} onDelete={setDeleting} />
        ))}
      </div>

      <TurnBalance />

      <ConnectModal kind={connecting} formats={formats} onClose={() => setConnecting(null)} onAdded={added} />
      <EditModal connection={editing} onClose={() => setEditing(null)}
                 onSaved={(connection) => {
                   setEditing(null);
                   setNotice({ tone: 'ok', text: `${connection.label} was saved. Test it to check the change.` });
                   announceModelsChanged();
                 }} />
      <Modal open={deleting !== null} title={deleting ? `Delete ${deleting.label}?` : ''} onClose={() => setDeleting(null)}
             footer={<Button tone="danger" data-testid="confirm-delete" onClick={confirmDelete}>Delete</Button>}>
        {deleting && (
          <p className="text-[13px] leading-relaxed text-ink-muted">
            This removes the connection, its saved key and its {deleting.models.length} listed
            model{deleting.models.length === 1 ? '' : 's'}.
            {selection.providerId === deleting.id
              ? ' It is the one Jarvis is using, so Jarvis won’t be able to answer until you choose another.'
              : ''}
          </p>
        )}
      </Modal>
    </div>
  );
}

const BALANCE: { id: Prefs['balance']; label: string; body: string }[] = [
  { id: 'fast', label: 'Answer quickly',
    body: 'Fewer rounds of using tools before answering, and no extra check of the answer afterwards.' },
  { id: 'balanced', label: 'Balanced', body: 'Jarvis’s normal approach.' },
  { id: 'quality', label: 'Answer well',
    body: 'Checks each important answer after giving it. Slower, and it makes an extra model call.' },
];

/**
 * How much work Jarvis does AROUND a model on a turn.
 *
 * Not a choice of model, and not effort: it applies to every model alike —
 * including a local one that has no effort control at all — and says nothing about
 * what any model can do. It only changes what Jarvis itself does.
 */
function TurnBalance() {
  const [balance, setBalance] = useState<Prefs['balance'] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.prefs.get().then((p) => setBalance(p.balance)).catch(() => undefined);
  }, []);

  async function choose(next: Prefs['balance']) {
    const before = balance;
    setBalance(next);
    setError(null);
    try {
      await api.prefs.update({ balance: next });
    } catch (err) {
      setBalance(before);
      setError(err instanceof ApiRequestError ? err.message : 'That could not be saved.');
    }
  }

  if (balance === null) return null;
  return (
    <Card data-testid="turn-balance">
      <h2 className="text-[14px] font-medium text-ink">How Jarvis spends a turn</h2>
      <p className="mt-0.5 text-[12px] leading-relaxed text-ink-faint">
        This is about Jarvis’s own work around a model — not which model answers, or how hard it thinks.
      </p>
      <div role="radiogroup" aria-label="How Jarvis spends a turn" className="mt-3 grid gap-2 sm:grid-cols-3">
        {BALANCE.map((option) => (
          <button key={option.id} type="button" role="radio" aria-checked={balance === option.id}
                  data-testid={`balance-${option.id}`} onClick={() => void choose(option.id)}
                  className={`flex flex-col items-start justify-start rounded border px-3 py-2.5 text-left transition ${
                    balance === option.id
                      ? 'border-accent/40 bg-accent/10'
                      : 'border-surface-border hover:bg-white/[0.04]'}`}>
            <span className="block text-[13px] text-ink">{option.label}</span>
            <span className="mt-0.5 block text-[11px] leading-relaxed text-ink-faint">{option.body}</span>
          </button>
        ))}
      </div>
      {error && <p className="mt-2 text-[12px] text-state-danger">{error}</p>}
    </Card>
  );
}
