'use client';

import { useCallback, useEffect, useState } from 'react';

import { AppIcon } from '@/components/ui/AppIcon';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { api, ApiRequestError } from '@/lib/api';
import type { ExternalService } from '@/lib/api-types';

/**
 * Keys for the speech services — speech recognition, a paid voice. Lives in
 * Settings, not Model Settings: none of these is an AI model.
 */
export function ServiceKeys() {
  const [services, setServices] = useState<ExternalService[]>([]);
  const [adding, setAdding] = useState(false);
  const [label, setLabel] = useState('');
  const [key, setKey] = useState('');
  const [error, setError] = useState<string | null>(null);

  const onChanged = useCallback(async () => {
    try {
      setServices((await api.externalServices.list()).services);
    } catch {
      /* the list stays as it was */
    }
  }, []);

  useEffect(() => {
    void onChanged();
  }, [onChanged]);

  return (
    <section className="py-3">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 className="text-[14px] font-medium text-ink">Speech service keys</h2>
          <p className="mt-0.5 text-[12px] text-ink-muted">
            Speech recognition and paid voices.
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
