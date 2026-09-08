'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Field, inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Toggle } from '@/components/ui/Toggle';
import { api, ApiRequestError } from '@/lib/api';
import type { Skill, SkillDetail } from '@/lib/api-types';

/**
 * Folders of instructions Jarvis can follow — a house style, a process, a
 * template. Knowledge it does not already have.
 *
 * **Nothing Jarvis can already do appears on this screen, ever.** Its built-in
 * abilities are not Skills, and offering one here as though it could be
 * installed is a mistake this project has made three separate times. The screen
 * reads one route, which reads folders on disk and has no code path back to a
 * capability list — so the rule is kept by what this can reach, not by
 * remembering it.
 *
 * Three ways in, one route: a link to a public repository, a file pasted in, or
 * a zip. They differ only in where the folder comes from.
 */

const SOURCE_LABEL: Record<string, string> = {
  user: 'Written here',
  upload: 'Uploaded',
  repo: 'From a repository',
  github: 'From a repository',
};

export function SkillsScreen() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [openName, setOpenName] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSkills((await api.skills.list()).skills);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'Could not read your skills.');
      setSkills([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function setEnabled(skill: Skill, enabled: boolean) {
    setSkills((current) =>
      (current ?? []).map((s) => (s.name === skill.name ? { ...s, enabled } : s)));
    try {
      await api.skills.update(skill.name, { enabled });
    } catch {
      await load();  // it did not take; show what is really stored
    }
  }

  return (
    <>
      <div className="mb-4 flex items-center gap-2">
        <Button tone="primary" data-testid="add-skill" onClick={() => setAdding(true)}>
          Add a skill
        </Button>
      </div>

      {error && <p className="mb-4 text-[13px] text-state-danger">{error}</p>}

      {skills === null ? null : skills.length === 0 ? (
        <EmptyState
          title="No skills yet"
          body="A skill is something Jarvis does not already know — how you like a report written, the steps of a process you repeat. Write one here, or install one from a repository."
          action={<Button tone="primary" onClick={() => setAdding(true)}>Add a skill</Button>}
        />
      ) : (
        <div className="space-y-2" data-testid="skill-list">
          {skills.map((skill) => (
            <Card key={skill.name} interactive data-testid="skill-row"
                  className="flex items-center gap-4">
              <button
                type="button"
                data-testid="skill-open"
                onClick={() => setOpenName(skill.name)}
                className="min-w-0 flex-1 text-left focus-visible:outline-none"
              >
                <p className={`truncate text-[14px] ${skill.enabled ? 'text-ink' : 'text-ink-faint'}`}>
                  {skill.name}
                </p>
                <p className="mt-0.5 truncate text-[12px] text-ink-faint">
                  {skill.description || 'No description'}
                  {' · '}
                  {SOURCE_LABEL[skill.source?.type ?? 'user'] ?? skill.source?.type}
                  {!skill.enabled && ' · off'}
                </p>
              </button>
              <Toggle
                label={`${skill.enabled ? 'Turn off' : 'Turn on'} ${skill.name}`}
                data-testid="skill-toggle"
                checked={skill.enabled}
                onChange={(next) => void setEnabled(skill, next)}
              />
            </Card>
          ))}
        </div>
      )}

      {adding && (
        <AddSkill
          onClose={() => setAdding(false)}
          onAdded={async () => {
            setAdding(false);
            await load();
          }}
        />
      )}

      {openName && (
        <SkillDetailView
          name={openName}
          onClose={() => setOpenName(null)}
          onChanged={async () => {
            setOpenName(null);
            await load();
          }}
        />
      )}
    </>
  );
}

type Way = 'write' | 'repo' | 'file';

function AddSkill({ onClose, onAdded }: { onClose: () => void; onAdded: () => Promise<void> }) {
  const [way, setWay] = useState<Way>('write');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [instructions, setInstructions] = useState('');
  const [repo, setRepo] = useState('');
  const [markdown, setMarkdown] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const ready = way === 'write' ? Boolean(name.trim() && instructions.trim())
    : way === 'repo' ? Boolean(repo.trim())
      : Boolean(markdown.trim());

  async function add() {
    setBusy(true);
    setError(null);
    try {
      if (way === 'write') {
        await api.skills.create({
          name: name.trim(), description: description.trim(), instructions: instructions.trim(),
        });
      } else if (way === 'repo') {
        await api.skills.installFromRepo(repo.trim());
      } else {
        await api.skills.installFromMarkdown(markdown);
      }
      await onAdded();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title="Add a skill"
      onClose={onClose}
      footer={
        <Button tone="primary" data-testid="save-skill" disabled={busy || !ready}
                onClick={() => void add()}>
          {way === 'write' ? 'Create' : 'Install'}
        </Button>
      }
    >
      <div className="mb-3 inline-flex rounded-pill border border-surface-border p-0.5">
        {([['write', 'Write one'], ['repo', 'From a repository'], ['file', 'Paste a file']] as
          [Way, string][]).map(([id, label]) => (
          <button
            key={id}
            type="button"
            data-testid={`way-${id}`}
            aria-pressed={way === id}
            onClick={() => setWay(id)}
            className={[
              'rounded-pill px-3 py-1.5 text-[13px] transition duration-150 ease-out',
              way === id ? 'bg-accent/15 text-accent' : 'text-ink-muted hover:text-ink',
            ].join(' ')}
          >
            {label}
          </button>
        ))}
      </div>

      {way === 'write' && (
        <>
          <Field label="Name" hint="What Jarvis will call it. Lower case, no spaces.">
            <input className={inputClass} data-testid="skill-name" placeholder="weekly-report"
                   value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="What it is for"
                 hint="Jarvis reads this to decide when the skill applies, so say when to use it.">
            <input className={inputClass} data-testid="skill-description"
                   placeholder="How to write the Friday report"
                   value={description} onChange={(event) => setDescription(event.target.value)} />
          </Field>
          <Field label="The instructions">
            <textarea className={inputClass} rows={8} data-testid="skill-instructions"
                      placeholder="Open with the headline number. Then three bullets…"
                      value={instructions}
                      onChange={(event) => setInstructions(event.target.value)} />
          </Field>
        </>
      )}

      {way === 'repo' && (
        <Field
          label="Repository link"
          hint="A public repository holding a SKILL.md, or a folder of them."
        >
          <input className={inputClass} data-testid="skill-repo"
                 placeholder="https://github.com/someone/their-skills"
                 value={repo} onChange={(event) => setRepo(event.target.value)} />
        </Field>
      )}

      {way === 'file' && (
        <Field label="Paste a SKILL.md" hint="Frontmatter and all — it is read as written.">
          <textarea className={`${inputClass} font-mono text-[12px]`} rows={10}
                    data-testid="skill-markdown"
                    placeholder={'---\nname: weekly-report\ndescription: …\n---\n\nOpen with…'}
                    value={markdown} onChange={(event) => setMarkdown(event.target.value)} />
        </Field>
      )}

      {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
    </Modal>
  );
}

function SkillDetailView({ name, onClose, onChanged }: {
  name: string;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [skill, setSkill] = useState<SkillDetail | null>(null);
  const [description, setDescription] = useState('');
  const [instructions, setInstructions] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.skills.open(name)
      .then((found) => {
        setSkill(found);
        setDescription(found.description);
        setInstructions(found.body);
      })
      .catch((err) => setError(err instanceof ApiRequestError ? err.message : 'Could not read it.'));
  }, [name]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onChanged();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : 'That did not work.');
      setBusy(false);
    }
  }

  const changed = skill
    && (description !== skill.description || instructions !== skill.body);

  return (
    <Modal
      open
      title={name}
      onClose={onClose}
      footer={
        <>
          <Button tone="danger" data-testid="delete-skill" disabled={busy}
                  onClick={() => run(() => api.skills.remove(name))}>
            Delete
          </Button>
          <Button tone="primary" data-testid="save-skill-edit" disabled={busy || !changed}
                  onClick={() => run(() => api.skills.update(name, { description, instructions }))}>
            Save
          </Button>
        </>
      }
    >
      {!skill ? (
        error ? <p className="text-[13px] text-state-danger">{error}</p> : null
      ) : (
        <>
          <Field label="What it is for">
            <input className={inputClass} data-testid="detail-description"
                   value={description} onChange={(event) => setDescription(event.target.value)} />
          </Field>
          <Field label="The instructions">
            <textarea className={inputClass} rows={10} data-testid="detail-instructions"
                      value={instructions}
                      onChange={(event) => setInstructions(event.target.value)} />
          </Field>

          {skill.supportingFiles.length > 0 && (
            <Field label="Files that came with it">
              <ul className="space-y-1" data-testid="skill-files">
                {skill.supportingFiles.map((file) => (
                  <li key={file.name} className="text-[12px] text-ink-faint">
                    {file.name} · {Math.max(1, Math.round(file.size / 1024))} KB
                  </li>
                ))}
              </ul>
            </Field>
          )}

          {skill.pipelineErrors.length > 0 && (
            <Field label="Its pipeline has a problem">
              <ul className="space-y-1">
                {skill.pipelineErrors.map((problem) => (
                  <li key={problem} className="text-[12px] text-state-danger">{problem}</li>
                ))}
              </ul>
            </Field>
          )}

          <Field
            label="Where it came from"
            hint={skill.installedAt ? `Added ${skill.installedAt.slice(0, 10)}` : undefined}
          >
            <div className="flex items-center gap-2">
              <p className="text-[13px] text-ink-muted">
                {SOURCE_LABEL[skill.source?.type ?? 'user'] ?? skill.source?.type}
              </p>
              {/* A real download rather than a copy button: a skill is a folder,
                  and a folder is a file you save. */}
              <a
                href={api.skills.downloadUrl(name)}
                data-testid="download-skill"
                className="text-[12px] text-accent hover:underline"
              >
                Download it
              </a>
            </div>
          </Field>

          {error && <p className="mt-2 text-[13px] text-state-danger">{error}</p>}
        </>
      )}
    </Modal>
  );
}
