'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { inputClass } from '@/components/ui/Field';
import { api } from '@/lib/api';
import type { ModelEntry, ModelRole, Prefs } from '@/lib/api-types';

/**
 * Which model does which job.
 *
 * **Unassigned is the normal state and the section says so rather than hiding
 * it.** A fresh install with one model needs no configuration at all; all five
 * jobs are listed because the unset ones are the ones a person most needs to
 * see in order to set them.
 *
 * **An assignment leads the ranking, it never restricts it.** Picking a model
 * here moves it to the front of the candidate list — the rest of the roster
 * stays behind it, so a job whose model is rate-limited still gets answered by
 * something. That is why a pin at a model which is switched off, or which was
 * deleted, is shown as a note rather than refused: it quietly falls back.
 *
 * **The thinking levels come from the chosen model, not from a fixed list.**
 * The ladders genuinely differ between providers, so a dropdown offering one
 * list for every model would be offering a setting that silently clamps.
 *
 * **The balance dial sits here because it answers the same question.** It is
 * what decides between models for every job left on Auto, and the router reads
 * it fresh on each turn — so a change takes effect on the next thing you say,
 * with no restart.
 */
export function RoleAssignments({ models }: { models: ModelEntry[] }) {
  const [roles, setRoles] = useState<ModelRole[] | null>(null);
  const [balance, setBalance] = useState<Prefs['balance'] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRoles((await api.roles.list()).roles);
    } catch {
      setRoles([]);
    }
    try {
      setBalance((await api.prefs.get()).balance);
    } catch {
      /* the roles half of this section still works without it */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function set(role: ModelRole, patch: { deploymentId?: string | null; effort?: string | null }) {
    setBusy(role.id);
    try {
      const answer = await api.roles.set(role.id, patch);
      setRoles((current) => (current ?? []).map((r) => (r.id === role.id ? answer.role : r)));
    } catch {
      await load();
    } finally {
      setBusy(null);
    }
  }

  if (!roles || roles.length === 0) return null;

  return (
    <section className="mt-8" data-testid="role-section">
      <div className="mb-3">
        <h2 className="text-[15px] font-medium text-ink">Which model does which job</h2>
        <p className="mt-0.5 text-[12px] text-ink-muted">
          All optional. Anything left on Auto is chosen per turn — and a model picked
          here still falls back to the others if it is busy, so a choice can never leave
          Jarvis unable to answer.
        </p>
      </div>

      {balance !== null && (
        <Card className="mb-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2"
              data-testid="balance-row">
          <div className="min-w-0 flex-1">
            <p className="text-[14px] text-ink">When Jarvis chooses for itself</p>
            <p className="mt-0.5 text-[12px] text-ink-muted">
              What to favour for any job left on Auto. Takes effect on your next message.
            </p>
          </div>
          <select
            className={`${inputClass} w-auto`}
            data-testid="balance"
            value={balance}
            onChange={async (event) => {
              const next = event.target.value as Prefs['balance'];
              setBalance(next);
              await api.prefs.update({ balance: next }).catch(() => void load());
            }}
          >
            <option value="fast">Answer quickly</option>
            <option value="balanced">Balanced</option>
            <option value="quality">Answer well</option>
          </select>
        </Card>
      )}

      <Card className="p-0" data-testid="role-list">
        {roles.map((role) => (
          <div
            key={role.id}
            data-testid={`role-${role.id}`}
            className="border-b border-surface-border p-4 last:border-b-0"
          >
            <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
              <div className="min-w-0 flex-1">
                <p className="text-[14px] text-ink">{role.label}</p>
                <p className="mt-0.5 text-[12px] leading-relaxed text-ink-muted">
                  {role.description}
                </p>
              </div>

              <div className="flex shrink-0 flex-wrap items-center gap-2">
                <select
                  className={`${inputClass} w-auto min-w-[11rem]`}
                  data-testid={`role-model-${role.id}`}
                  disabled={busy === role.id}
                  value={role.deploymentId ?? ''}
                  onChange={(event) =>
                    void set(role, { deploymentId: event.target.value || null })}
                >
                  <option value="">Auto — pick per turn</option>
                  {models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.label}
                      {model.enabled ? '' : ' — off'}
                    </option>
                  ))}
                  {/* A pin at something no longer here stays selectable, so the
                      dropdown shows what was set rather than silently reading
                      as Auto while the stored preference says otherwise. */}
                  {role.deploymentId && !role.deployment && (
                    <option value={role.deploymentId}>{role.deploymentId} — no longer here</option>
                  )}
                </select>

                <select
                  className={`${inputClass} w-auto`}
                  data-testid={`role-effort-${role.id}`}
                  disabled={busy === role.id || role.effortChoices.length === 0}
                  value={role.effort ?? ''}
                  onChange={(event) => void set(role, { effort: event.target.value || null })}
                >
                  <option value="">
                    {role.effortChoices.length === 0
                      ? 'No thinking setting'
                      : 'Its own default'}
                  </option>
                  {role.effortChoices.map((choice) => (
                    <option key={choice.id} value={choice.id}>{choice.label}</option>
                  ))}
                </select>

                {role.assigned && (
                  <Button
                    data-testid={`role-clear-${role.id}`}
                    disabled={busy === role.id}
                    onClick={async () => {
                      setBusy(role.id);
                      try {
                        const answer = await api.roles.clear(role.id);
                        setRoles((current) =>
                          (current ?? []).map((r) => (r.id === role.id ? answer.role : r)));
                      } finally {
                        setBusy(null);
                      }
                    }}
                  >
                    Reset
                  </Button>
                )}
              </div>
            </div>

            <RoleNote role={role} />
          </div>
        ))}
      </Card>
    </section>
  );
}

/** Said plainly, because both cases still work and neither is an error. */
function RoleNote({ role }: { role: ModelRole }) {
  if (role.deploymentId && !role.deployment) {
    return (
      <p className="mt-2 text-[11px] text-state-warn" data-testid={`role-note-${role.id}`}>
        That model is no longer here, so this job is being chosen per turn.
      </p>
    );
  }
  if (role.deployment && !role.deployment.enabled) {
    return (
      <p className="mt-2 text-[11px] text-state-warn" data-testid={`role-note-${role.id}`}>
        That model is switched off, so this job is being chosen per turn.
      </p>
    );
  }
  if (role.deployment && !role.deployment.ready) {
    return (
      <p className="mt-2 text-[11px] text-state-warn" data-testid={`role-note-${role.id}`}>
        That model’s connection still needs a key.
      </p>
    );
  }
  return null;
}
