'use client';

import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { AddConnectionResult, ProviderConnection, ProviderFormat, ProviderKind } from '@/lib/api-types';

/**
 * Connect a provider: only the fields that kind actually needs.
 *
 * A provider with a fixed address is never asked for one, a keyless local server
 * is never asked for a key, and only Custom asks which type of API it speaks —
 * because only Custom is a provider the app cannot already tell the format of.
 */
export function ConnectModal({
  kind,
  formats,
  onClose,
  onAdded,
}: {
  kind: ProviderKind | null;
  formats: ProviderFormat[];
  onClose: () => void;
  onAdded: (result: AddConnectionResult) => void;
}) {
  const [name, setName] = useState('');
  const [address, setAddress] = useState('');
  const [format, setFormat] = useState('');
  const [key, setKey] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  useEffect(() => {
    setName('');
    setAddress(kind?.defaultAddress ?? '');
    setFormat(formats[0]?.id ?? '');
    setKey('');
    setError(null);
    setWorking(false);
  }, [kind, formats]);

  if (!kind) return null;

  async function submit() {
    if (!kind) return;
    setError(null);
    setWorking(true);
    try {
      const result = await api.models.add({
        kind: kind.id,
        label: name.trim() || undefined,
        address: kind.address === 'fixed' ? undefined : address.trim() || undefined,
        format: kind.chooseFormat ? format : undefined,
        apiKey: key.trim() || undefined,
      });
      onAdded(result);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That could not be connected.');
    } finally {
      setWorking(false);
    }
  }

  return (
    <Modal
      open
      title={`Connect ${kind.label}`}
      onClose={onClose}
      footer={
        <Button tone="primary" data-testid="connect-submit" disabled={working} onClick={submit}>
          {working ? 'Connecting…' : 'Connect'}
        </Button>
      }
    >
      <p className="pb-1 text-[13px] leading-relaxed text-ink-muted">{kind.blurb}</p>
      {error && (
        <p data-testid="connect-error" className="my-2 text-[13px] text-state-danger">{error}</p>
      )}
      <Field label="Name" hint={`What to call it. Leave blank to use “${kind.label}”.`}>
        <input className={inputClass} value={name} data-testid="connect-name"
               onChange={(event) => setName(event.target.value)} />
      </Field>
      {kind.address !== 'fixed' && (
        <Field
          label="Address"
          hint={kind.address === 'editable'
            ? 'Where it is running. The usual address is filled in.'
            : 'The address of the provider’s API, like https://example.com/v1.'}
        >
          <input className={inputClass} value={address} data-testid="connect-address"
                 placeholder="https://…" autoComplete="off"
                 onChange={(event) => setAddress(event.target.value)} />
        </Field>
      )}
      {kind.chooseFormat && (
        <Field label="Type of API" hint="How this provider expects to be spoken to. If it says it is OpenAI-compatible, choose that.">
          <select className={inputClass} value={format} data-testid="connect-format"
                  onChange={(event) => setFormat(event.target.value)}>
            {formats.map((option) => (
              <option key={option.id} value={option.id}>{option.label}</option>
            ))}
          </select>
        </Field>
      )}
      {kind.key !== 'none' && (
        <Field label={kind.key === 'required' ? 'API key' : 'API key (optional)'}>
          <input type="password" className={inputClass} value={key} data-testid="connect-key"
                 autoComplete="off" onChange={(event) => setKey(event.target.value)} />
        </Field>
      )}
    </Modal>
  );
}

/** Change a connection's name, address or key. The key is never shown back. */
export function EditModal({
  connection,
  onClose,
  onSaved,
}: {
  connection: ProviderConnection | null;
  onClose: () => void;
  onSaved: (connection: ProviderConnection) => void;
}) {
  const [name, setName] = useState('');
  const [address, setAddress] = useState('');
  const [key, setKey] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  useEffect(() => {
    setName(connection?.label ?? '');
    setAddress(connection?.address ?? '');
    setKey('');
    setError(null);
    setWorking(false);
  }, [connection]);

  if (!connection) return null;

  async function save() {
    if (!connection) return;
    setError(null);
    setWorking(true);
    try {
      const body: { label?: string; address?: string; apiKey?: string } = { label: name };
      if (connection.addressEditable) body.address = address;
      if (key.trim()) body.apiKey = key.trim();
      const saved = await api.models.edit(connection.id, body);
      onSaved(saved.connection);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That could not be saved.');
    } finally {
      setWorking(false);
    }
  }

  return (
    <Modal
      open
      title={`Edit ${connection.label}`}
      onClose={onClose}
      footer={
        <Button tone="primary" data-testid="edit-submit" disabled={working} onClick={save}>
          {working ? 'Saving…' : 'Save'}
        </Button>
      }
    >
      {error && <p data-testid="edit-error" className="mb-2 text-[13px] text-state-danger">{error}</p>}
      <Field label="Name">
        <input className={inputClass} value={name} data-testid="edit-name"
               onChange={(event) => setName(event.target.value)} />
      </Field>
      {connection.addressEditable && (
        <Field label="Address">
          <input className={inputClass} value={address} data-testid="edit-address" autoComplete="off"
                 onChange={(event) => setAddress(event.target.value)} />
        </Field>
      )}
      {connection.keyNeeded !== 'none' && (
        <Field
          label="API key"
          hint={connection.hasKey ? 'Leave blank to keep the key that is saved.' : undefined}
        >
          <input type="password" className={inputClass} value={key} data-testid="edit-key"
                 autoComplete="off" onChange={(event) => setKey(event.target.value)} />
        </Field>
      )}
      <p className="pt-1 text-[12px] text-ink-faint">
        Changing the address or key clears the last connection test — test it again to check the change.
      </p>
    </Modal>
  );
}
