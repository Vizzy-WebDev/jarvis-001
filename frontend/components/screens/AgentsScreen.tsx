'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type {
  Agent, AgentAbilities, AgentDraft, AgentNote, AgentRun, AgentRunSummary,
} from '@/lib/api-types';
import { useModels } from '@/lib/useModels';

/**
 * The specialists Jarvis hands work to — the built-in ones and any the person
 * makes. Both are the same kind of thing: the same editor, the same record of
 * what each one actually did. The only difference offered is reset (built-in)
 * versus delete (custom).
 *
 * **What an agent did is read from its runs, not from what it said.** Every run
 * is recorded with who asked, what it came back with, the tools it really used
 * and every other specialist it asked in turn — the tree is on each run.
 *
 * **Access is permissions, not a catalogue of Skills.** The picker keeps built-in
 * abilities, the person's own Skills and their connectors in three labelled
 * groups, each read from its own source.
 */

const RUN_STATUS: Record<AgentRunSummary['status'], [string, string]> = {
  running: ['Working', 'text-accent'],
  done: ['Finished', 'text-state-ok'],
  failed: ['Didn’t finish', 'text-state-danger'],
  awaiting_approval: ['Waiting on you', 'text-state-warn'],
};

function requester(run: AgentRunSummary, names: Record<string, string>): string {
  if (run.requestedBy === 'jarvis') return 'Jarvis';
  if (run.requestedBy === 'operator') return 'You, directly';
  if (run.requestedBy === 'schedule') return 'A scheduled task';
  if (run.requestedBy === 'job') return 'Background work';
  return names[run.requestedBy] ?? run.requestedBy;
}

const when = (iso: string | null) => (iso ? iso.slice(0, 16).replace('T', ' ') : '');

const BLANK: AgentDraft = {
  name: '', description: '', mission: '', doctrine: '', guardrails: '', modelPin: null,
  capabilityAccess: { mode: 'selected', names: ['look_it_up', 'read_web_page'], connectors: 'all' },
  memoryAccess: 'read', collaborators: 'any', enabled: true,
};

export function AgentsScreen() {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setAgents((await api.agents.list()).agents);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your specialists.');
      setAgents([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Specialists work while the person is elsewhere; their status changes under
  // this screen as a matter of course.
  useEffect(() => {
    const source = new EventSource('/api/events');
    source.onmessage = (raw) => {
      try {
        const type = (JSON.parse(raw.data) as { type?: string }).type ?? '';
        if (type.startsWith('agent.')) void load();
      } catch {
        /* a frame we cannot read is not worth acting on */
      }
    };
    return () => source.close();
  }, [load]);

  async function setEnabled(agent: Agent, enabled: boolean) {
    setAgents((current) => current?.map((a) => (a.id === agent.id ? { ...a, enabled } : a)) ?? null);
    try {
      await api.agents.update(agent.id, { enabled });
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not save.');
      void load();
    }
  }

  const builtIns = (agents ?? []).filter((a) => a.builtin);
  const custom = (agents ?? []).filter((a) => !a.builtin);

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Button tone="primary" data-testid="agent-create" onClick={() => setCreating(true)}>
          Create a specialist
        </Button>
        <p className="text-[12px] text-ink-faint">
          Jarvis decides when to hand work over. You can also talk to one directly from the
          conversation panel.
        </p>
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {agents === null ? null : (
        <>
          <Group title="Yours" agents={custom} onOpen={setOpenId} onToggle={setEnabled}
                 empty="Specialists you create appear here, working exactly like the built-in ones." />
          <Group title="Built in" agents={builtIns} onOpen={setOpenId} onToggle={setEnabled} />
        </>
      )}

      {creating && (
        <AgentEditor
          agents={agents ?? []}
          onClose={() => setCreating(false)}
          onSaved={async (agent) => {
            setCreating(false);
            await load();
            setOpenId(agent.id);
          }}
        />
      )}
      {openId && (
        <AgentDetail agentId={openId} agents={agents ?? []} onClose={() => setOpenId(null)}
                     onChanged={load} />
      )}
    </>
  );
}

function Group({ title, agents, onOpen, onToggle, empty }: {
  title: string;
  agents: Agent[];
  onOpen: (id: string) => void;
  onToggle: (agent: Agent, enabled: boolean) => void;
  empty?: string;
}) {
  return (
    <section className="mb-6">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-faint">{title}</h3>
      {agents.length === 0 ? (
        empty ? <p className="text-[12px] text-ink-faint">{empty}</p> : null
      ) : (
        <div className="space-y-2" data-testid={`agent-list-${title === 'Yours' ? 'custom' : 'builtin'}`}>
          {agents.map((agent) => (
            <Card key={agent.id} interactive data-testid="agent-row" data-agent={agent.id}
                  onClick={() => onOpen(agent.id)}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className={`text-[14px] ${agent.enabled ? 'text-ink' : 'text-ink-faint'}`}>
                    {agent.name}
                  </p>
                  <p className="mt-1 line-clamp-2 text-[12px] text-ink-faint">{agent.description}</p>
                  {agent.lastRun && (
                    <p className="mt-1 truncate text-[11px] text-ink-faint">
                      Last: <span className={RUN_STATUS[agent.lastRun.status][1]}>
                        {RUN_STATUS[agent.lastRun.status][0]}</span> · {agent.lastRun.task}
                    </p>
                  )}
                </div>
                <div onClick={(event) => event.stopPropagation()}>
                  <Toggle checked={agent.enabled} label={`${agent.name} on`}
                          data-testid="agent-enabled" onChange={(next) => onToggle(agent, next)} />
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

// --- one agent ---------------------------------------------------------------

function AgentDetail({ agentId, agents, onClose, onChanged }: {
  agentId: string;
  agents: Agent[];
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof api.agents.open>> | null>(null);
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState<'reset' | 'delete' | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const names = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a.name])), [agents]);

  const load = useCallback(async () => {
    try {
      setDetail(await api.agents.open(agentId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read that specialist.');
    }
  }, [agentId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(action: () => Promise<unknown>, after?: () => void) {
    try {
      await action();
      setConfirm(null);
      await onChanged();
      if (after) after();
      else await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
    }
  }

  if (!detail) {
    return (
      <Modal open title="Loading" onClose={onClose}>
        {error ? <p className="text-[13px] text-state-danger">{error}</p> : null}
      </Modal>
    );
  }
  const { agent, runs, notes } = detail;
  const access = agent.capabilityAccess;

  return (
    <Modal
      open
      title={agent.name}
      onClose={onClose}
      footer={
        <>
          {agent.builtin ? (
            <Button data-testid="agent-reset" onClick={() => setConfirm('reset')}>Reset to default</Button>
          ) : (
            <Button tone="danger" data-testid="agent-delete" onClick={() => setConfirm('delete')}>Delete</Button>
          )}
          <Button tone="primary" data-testid="agent-edit" onClick={() => setEditing(true)}>Edit</Button>
        </>
      }
    >
      <p className="text-[12px] text-ink-faint">
        {agent.builtin ? 'Built in' : 'Yours'} · {agent.enabled ? 'On' : 'Off'} ·{' '}
        {agent.modelPin ? `Always uses ${agent.modelPin}` : 'Uses whatever Jarvis is set to'}
      </p>
      <p className="mt-3 text-[14px] text-ink">{agent.description}</p>
      {agent.mission && <Field label="Mission"><p className="text-[13px] text-ink-muted">{agent.mission}</p></Field>}

      <Field label="What it can use">
        <p className="text-[12px] text-ink-muted" data-testid="agent-access">
          {access.mode === 'all' ? 'Everything Jarvis can use' : access.names.join(', ') || 'Only its own notes'}
          {' · '}
          {access.connectors === 'all' ? 'every connector' : `${access.connectors.length} connector(s)`}
          {' · '}
          {agent.memoryAccess === 'read' ? 'can see what Jarvis remembers about you' : 'no access to your memory'}
        </p>
        <p className="mt-1 text-[11px] text-ink-faint">
          Anything that needs your go-ahead still asks you, whoever is doing the work.
        </p>
      </Field>
      <Field label="Can ask for help from">
        <p className="text-[12px] text-ink-muted">
          {agent.collaborators === 'any' ? 'Any specialist'
            : agent.collaborators.map((id) => names[id] ?? id).join(', ') || 'Nobody — it works alone'}
        </p>
      </Field>

      <Field label="What it has done" hint="Every run, with who asked and what came back.">
        {runs.length === 0 ? (
          <p className="text-[12px] text-ink-faint">Nothing yet.</p>
        ) : (
          <ol className="space-y-1" data-testid="agent-runs">
            {runs.map((run) => (
              <li key={run.id}>
                <button type="button" data-testid="agent-run" onClick={() => setRunId(run.id)}
                        className="block w-full truncate text-left text-[12px] hover:underline">
                  <span className={RUN_STATUS[run.status][1]}>{RUN_STATUS[run.status][0]}</span>
                  <span className="text-ink-faint"> · {when(run.startedAt)} · {requester(run, names)} · </span>
                  <span className="text-ink-muted">{run.task}</span>
                </button>
              </li>
            ))}
          </ol>
        )}
      </Field>

      <Notes agentId={agent.id} notes={notes} onChanged={load} />

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}

      {editing && (
        <AgentEditor agents={agents} existing={agent} onClose={() => setEditing(false)}
                     onSaved={async () => {
                       setEditing(false);
                       await onChanged();
                       await load();
                     }} />
      )}
      {runId && <RunDetail runId={runId} onClose={() => setRunId(null)} />}
      {confirm && (
        <Modal
          open nested
          title={confirm === 'reset' ? `Reset ${agent.name}?` : `Delete ${agent.name}?`}
          onClose={() => setConfirm(null)}
          footer={confirm === 'reset' ? (
            <Button tone="danger" data-testid="agent-reset-confirm"
                    onClick={() => void act(() => api.agents.reset(agent.id))}>Reset</Button>
          ) : (
            <Button tone="danger" data-testid="agent-delete-confirm"
                    onClick={() => void act(() => api.agents.remove(agent.id), onClose)}>Delete</Button>
          )}
        >
          <p className="text-[13px] text-ink">
            {confirm === 'reset'
              ? 'Its instructions, access and settings go back to how they came. Its notes and history stay.'
              : 'It stops being offered straight away, and its notes are deleted. Its history stays.'}
          </p>
        </Modal>
      )}
    </Modal>
  );
}

function Notes({ agentId, notes, onChanged }: {
  agentId: string;
  notes: AgentNote[];
  onChanged: () => Promise<void>;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  return (
    <Field label="Its notes" hint="What it keeps between tasks — you can correct or remove any of it.">
      {notes.length === 0 ? (
        <p className="text-[12px] text-ink-faint">None kept yet.</p>
      ) : (
        <ul className="space-y-2" data-testid="agent-notes">
          {notes.map((note) => (
            <li key={note.id} className="rounded border border-surface-border p-2" data-testid="agent-note">
              <p className="text-[12px] font-medium text-ink-muted">{note.topic}</p>
              {editingId === note.id ? (
                <>
                  <textarea className={`${inputClass} mt-1`} rows={4} value={draft}
                            data-testid="agent-note-input" onChange={(e) => setDraft(e.target.value)} />
                  <div className="mt-1 flex gap-1.5">
                    <Button onClick={() => setEditingId(null)}>Cancel</Button>
                    <Button tone="primary" data-testid="agent-note-save" onClick={async () => {
                      await api.agents.updateNote(agentId, note.id, { text: draft });
                      setEditingId(null);
                      await onChanged();
                    }}>Save</Button>
                  </div>
                </>
              ) : (
                <>
                  <p className="mt-1 whitespace-pre-wrap text-[12px] text-ink-faint">{note.text}</p>
                  <div className="mt-1 flex gap-3 text-[11px]">
                    <button type="button" className="text-accent hover:underline"
                            onClick={() => { setEditingId(note.id); setDraft(note.text); }}>Edit</button>
                    <button type="button" className="text-state-danger hover:underline"
                            data-testid="agent-note-delete"
                            onClick={async () => { await api.agents.removeNote(agentId, note.id); await onChanged(); }}>
                      Remove
                    </button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </Field>
  );
}

function RunDetail({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<{ run: AgentRun; tree: AgentRun[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState(runId);

  useEffect(() => {
    api.agents.run(runId).then(setDetail).catch((err) =>
      setError(err instanceof ApiRequestError ? err.message : 'Could not read that run.'));
  }, [runId]);

  const shown = detail?.tree.find((r) => r.id === focus) ?? detail?.run;
  return (
    <Modal open nested title="What happened" onClose={onClose}>
      {error && <p className="text-[13px] text-state-danger">{error}</p>}
      {detail && shown && (
        <>
          <Field label="Everyone who worked on this request">
            <ol className="space-y-1" data-testid="run-tree">
              {detail.tree.map((run) => (
                <li key={run.id} style={{ paddingLeft: `${(run.depth - 1) * 16}px` }}>
                  <button type="button" onClick={() => setFocus(run.id)} data-testid="run-tree-item"
                          className={`text-left text-[12px] hover:underline ${run.id === focus ? 'text-accent' : 'text-ink-muted'}`}>
                    {run.depth > 1 ? '↳ ' : ''}{run.agentName} — {RUN_STATUS[run.status][0].toLowerCase()}
                  </button>
                </li>
              ))}
            </ol>
          </Field>
          <Field label="Asked to">
            <p className="whitespace-pre-wrap text-[13px] text-ink">{shown.task}</p>
          </Field>
          {shown.result && (
            <Field label="Came back with">
              <p className="max-h-[300px] overflow-y-auto whitespace-pre-wrap text-[13px] text-ink-muted"
                 data-testid="run-result">{shown.result}</p>
            </Field>
          )}
          {shown.error && (
            <Field label="What went wrong"><p className="text-[13px] text-state-danger">{shown.error}</p></Field>
          )}
          <p className="text-[11px] text-ink-faint">
            {shown.toolsUsed.length ? `Used: ${Array.from(new Set(shown.toolsUsed)).join(', ')}` : 'Used no tools'}
            {shown.modelId ? ` · answered by ${shown.modelId}` : ''}
          </p>
        </>
      )}
    </Modal>
  );
}

// --- creating and editing ------------------------------------------------------

function AgentEditor({ agents, existing, onClose, onSaved }: {
  agents: Agent[];
  existing?: Agent;
  onClose: () => void;
  onSaved: (agent: Agent) => Promise<void>;
}) {
  const [draft, setDraft] = useState<AgentDraft>(() => (existing ? {
    name: existing.name, description: existing.description, mission: existing.mission,
    doctrine: existing.doctrine, guardrails: existing.guardrails, modelPin: existing.modelPin,
    capabilityAccess: existing.capabilityAccess, memoryAccess: existing.memoryAccess,
    collaborators: existing.collaborators, enabled: existing.enabled,
  } : BLANK));
  const [abilities, setAbilities] = useState<AgentAbilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const { overview } = useModels();

  useEffect(() => {
    api.agents.abilities().then(setAbilities).catch(() => setAbilities(
      { builtIn: [], skills: [], connectors: [] }));
  }, []);

  const set = <K extends keyof AgentDraft>(key: K, value: AgentDraft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const access = draft.capabilityAccess;
  const toggleName = (name: string, on: boolean) => set('capabilityAccess', {
    ...access, names: on ? [...access.names, name] : access.names.filter((n) => n !== name),
  });
  const modelIds = useMemo(() => Array.from(new Set(
    (overview?.connections ?? []).flatMap((c) => c.models.map((m) => m.id)))).sort(), [overview]);
  const others = agents.filter((a) => a.id !== existing?.id);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const saved = existing
        ? (await api.agents.update(existing.id, draft)).agent
        : (await api.agents.create(draft)).agent;
      await onSaved(saved);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not save.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open nested={Boolean(existing)}
      title={existing ? `Edit ${existing.name}` : 'Create a specialist'}
      onClose={onClose}
      footer={<Button tone="primary" data-testid="agent-save" disabled={saving || !draft.name.trim()}
                      onClick={() => void save()}>{existing ? 'Save' : 'Create'}</Button>}
    >
      <Field label="Name">
        <input className={inputClass} data-testid="agent-name" value={draft.name}
               placeholder="Podcast Producer" onChange={(e) => set('name', e.target.value)} />
      </Field>
      <Field label="What it does" hint="One line. Jarvis reads this to decide when to hand work over.">
        <input className={inputClass} data-testid="agent-description" value={draft.description}
               placeholder="Plans, scripts and writes show notes for podcast episodes"
               onChange={(e) => set('description', e.target.value)} />
      </Field>
      <Field label="Mission" hint="What it is ultimately for.">
        <textarea className={inputClass} rows={2} data-testid="agent-mission" value={draft.mission}
                  onChange={(e) => set('mission', e.target.value)} />
      </Field>
      <Field label="How it works" hint="Its instructions: the steps and standards it follows.">
        <textarea className={inputClass} rows={7} data-testid="agent-doctrine" value={draft.doctrine}
                  onChange={(e) => set('doctrine', e.target.value)} />
      </Field>
      <Field label="Rules it never breaks"
             hint="On top of the ones every specialist keeps: nothing that spends, signs, contacts or deletes without your go-ahead.">
        <textarea className={inputClass} rows={2} data-testid="agent-guardrails" value={draft.guardrails}
                  onChange={(e) => set('guardrails', e.target.value)} />
      </Field>

      <Field label="Model" hint="Leave empty to use whatever Jarvis is set to (including Auto).">
        <input className={inputClass} data-testid="agent-model" list="agent-model-options"
               value={draft.modelPin ?? ''} placeholder="Same as Jarvis"
               onChange={(e) => set('modelPin', e.target.value.trim() || null)} />
        <datalist id="agent-model-options">
          {modelIds.map((id) => <option key={id} value={id} />)}
        </datalist>
      </Field>

      <Field label="What it can use" hint="Anything that needs your go-ahead still asks you.">
        <div className="flex items-center gap-2">
          <Toggle checked={access.mode === 'all'} label="Everything Jarvis can use"
                  data-testid="agent-access-all"
                  onChange={(all) => set('capabilityAccess', { ...access, mode: all ? 'all' : 'selected' })} />
          <span className="text-[12px] text-ink-muted">Everything Jarvis can use</span>
        </div>
        {access.mode === 'selected' && abilities && (
          <div className="mt-2 max-h-[260px] space-y-3 overflow-y-auto rounded border border-surface-border p-2"
               data-testid="agent-abilities">
            <PickList title="Built-in abilities" testId="ability-builtin"
                      items={abilities.builtIn.map((a) => ({ key: a.name, label: a.name, hint: a.description }))}
                      picked={access.names} onPick={toggleName} />
            <PickList title="Your Skills" testId="ability-skill"
                      items={abilities.skills.map((s) => ({ key: s.name, label: s.name, hint: s.description }))}
                      picked={access.names} onPick={toggleName}
                      empty="No Skills installed. Any you install are always available to it." />
            <div>
              <p className="mb-1 text-[11px] font-medium text-ink-faint">Connectors</p>
              <div className="flex items-center gap-2">
                <Toggle checked={access.connectors === 'all'} label="Every connector"
                        data-testid="agent-connectors-all"
                        onChange={(all) => set('capabilityAccess', { ...access, connectors: all ? 'all' : [] })} />
                <span className="text-[12px] text-ink-muted">Every connector you set up</span>
              </div>
              {access.connectors !== 'all' && (
                <PickList title="" testId="ability-connector"
                          items={abilities.connectors.map((c) => ({ key: c.id, label: c.label, hint: c.type ?? '' }))}
                          picked={access.connectors}
                          onPick={(id, on) => {
                            const current = access.connectors === 'all' ? [] : access.connectors;
                            set('capabilityAccess', { ...access,
                              connectors: on ? [...current, id] : current.filter((c) => c !== id) });
                          }} />
              )}
            </div>
          </div>
        )}
      </Field>

      <Field label="Your memory">
        <div className="flex items-center gap-2">
          <Toggle checked={draft.memoryAccess === 'read'} label="Can see what Jarvis remembers about you"
                  data-testid="agent-memory"
                  onChange={(on) => set('memoryAccess', on ? 'read' : 'none')} />
          <span className="text-[12px] text-ink-muted">Can see what Jarvis remembers about you</span>
        </div>
      </Field>

      <Field label="Who it can ask for help">
        <div className="flex items-center gap-2">
          <Toggle checked={draft.collaborators === 'any'} label="Any specialist"
                  data-testid="agent-collaborators-any"
                  onChange={(any) => set('collaborators', any ? 'any' : [])} />
          <span className="text-[12px] text-ink-muted">Any specialist</span>
        </div>
        {draft.collaborators !== 'any' && (
          <div className="mt-2 rounded border border-surface-border p-2">
            <PickList title="" testId="agent-collaborator"
                      items={others.map((a) => ({ key: a.id, label: a.name, hint: a.description }))}
                      picked={draft.collaborators}
                      onPick={(id, on) => {
                        const current = draft.collaborators === 'any' ? [] : draft.collaborators;
                        set('collaborators', on ? [...current, id] : current.filter((c) => c !== id));
                      }}
                      empty="There are no other specialists." />
          </div>
        )}
      </Field>

      <Field label="On">
        <Toggle checked={draft.enabled} label="On" data-testid="agent-on"
                onChange={(on) => set('enabled', on)} />
      </Field>

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

function PickList({ title, items, picked, onPick, empty, testId }: {
  title: string;
  items: { key: string; label: string; hint: string }[];
  picked: string[];
  onPick: (key: string, on: boolean) => void;
  empty?: string;
  testId: string;
}) {
  return (
    <div>
      {title && <p className="mb-1 text-[11px] font-medium text-ink-faint">{title}</p>}
      {items.length === 0 ? (
        empty ? <p className="text-[11px] text-ink-faint">{empty}</p> : null
      ) : (
        <ul className="space-y-1">
          {items.map((item) => {
            const on = picked.includes(item.key);
            return (
              <li key={item.key} className="flex items-start gap-2">
                <Toggle checked={on} label={item.label} data-testid={`${testId}-${item.key}`}
                        onChange={(next) => onPick(item.key, next)} />
                <span className="min-w-0">
                  <span className="block text-[12px] text-ink">{item.label}</span>
                  <span className="block truncate text-[11px] text-ink-faint">{item.hint}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

