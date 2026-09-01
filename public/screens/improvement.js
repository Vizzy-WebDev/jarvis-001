// Self-Improvement screen — what Jarvis has learned about its OWN work and
// changed about itself, distinct from Memory (facts about the USER). See
// root CLAUDE.md's "Self-Improvement" section and server/improvement/
// CLAUDE.md for the engine this is a window onto. server/improvement/
// improvement-store.js has been a complete engine since it shipped,
// deliberately screenless while every review was purely conversational —
// once something can auto-apply without being asked, seeing and undoing it
// stops being optional, same reasoning memory.js's own header comment
// gives for Memory.
//
// Four tabs (segmented, like a small in-page router): Suggestions (pending,
// needs a decision), Changes (the undo log), Learned (live rules + recent
// observations), Settings (the trust dial). Filtering/paging isn't needed
// here — every list is small and curated by design (the same reasoning
// memory.js gives for its own client-side-only filtering).
//
// Every row is clickable, opening a detail view (Rules/Lessons/Suggestions —
// found missing in the first real user test pass). Deletion follows
// Memory's own proven pattern rather than a new "Recycle Bin" concept:
// Archive (soft, reversible, hides it behind a "Show archived"/"Show
// rejected" toggle) with, from that view, Restore or an armed Delete
// permanently — see server/improvement/CLAUDE.md's Phase 7 notes for why a
// genuine hard-delete of a RULE must always go through archive first (an
// applied rule's Undo button in Changes depends on the row still existing).

import { sectionCard, fieldInput, fieldTextarea, fieldSelect, armedButton, postJson } from './_helpers.js';
import { segmented, toggleSwitch } from './_ui.js';
import { openModal } from './_modal.js';

const TRUST_OPTIONS = [
  ['ask', 'Ask me about everything'],
  ['balanced', 'Auto-apply small, well-evidenced fixes (recommended)'],
  ['auto', 'Auto-apply more readily'],
];

const RESEARCH_OPTIONS = [
  ['off', "Don't look outside its own experience"],
  ['weekly', 'Check docs/communities/web once a week (recommended)'],
];

function formatDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function kindLabel(kind) {
  switch (kind) {
    case 'rule':
      return 'Behaviour rule';
    case 'setting':
      return 'Setting change';
    case 'skill':
      return 'New Skill idea';
    case 'code':
      return 'Code change idea';
    case 'conflict':
      return 'Contradiction found';
    default:
      return 'Idea';
  }
}

function describeScope(scope) {
  if (!scope || scope === 'general') return 'Applies everywhere';
  if (scope.startsWith('job_kind:')) return `Applies to this kind of background job — ${scope.slice('job_kind:'.length)}`;
  if (scope.startsWith('task:')) return 'Applies to one recurring scheduled task';
  return scope;
}

/** The "Evidence (N)" expand every detail modal shares — fetches the real outcome summaries behind a rule/lesson/proposal's evidence array, appended into `container` in place. Silently shows nothing further for an id that's since aged out (server/improvement/improvement-store.js's lookupOutcomes() already degrades gracefully the same way). */
async function appendEvidenceExpand(container, evidenceIds) {
  const ids = Array.isArray(evidenceIds) ? evidenceIds : [];
  if (!ids.length) return;
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'link-btn';
  toggle.textContent = `Evidence (${ids.length}) — view`;
  const list = document.createElement('div');
  list.hidden = true;
  toggle.addEventListener('click', async () => {
    if (!list.hidden) {
      list.hidden = true;
      toggle.textContent = `Evidence (${ids.length}) — view`;
      return;
    }
    if (!list.dataset.loaded) {
      const data = await postJson('/api/improvement/outcomes/lookup', { ids });
      const outcomes = data.outcomes || [];
      if (!outcomes.length) {
        list.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Details for this evidence are no longer available.' }));
      }
      for (const o of outcomes) {
        const row = document.createElement('p');
        row.className = 'hint';
        row.textContent = `${formatDate(o.createdAt)} — ${o.title || o.goal || o.source}${o.error ? `: ${o.error}` : ''}`;
        list.appendChild(row);
      }
      list.dataset.loaded = 'true';
    }
    list.hidden = false;
    toggle.textContent = `Evidence (${ids.length}) — hide`;
  });
  container.appendChild(toggle);
  container.appendChild(list);
}

/** For a 'skill'/'code' idea: asks which coding assistant, generates the brief, then shows it read-only with a Copy button — this IS the approval action for these two kinds, since Jarvis never writes the code itself. */
async function implementationPromptFlow(p, onChange) {
  let targetField;
  const targetResult = await openModal({
    title: 'Generate a prompt for a coding assistant',
    submitLabel: 'Generate',
    build(body) {
      body.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: 'Which coding assistant are you using? (e.g. "Claude Code", "Cursor", "ChatGPT" — whatever you actually use.)',
        })
      );
      targetField = fieldInput('Coding assistant', 'text', 'e.g. Claude Code');
      body.appendChild(targetField.wrapper);
    },
    async onSubmit(api) {
      const target = targetField.input.value.trim();
      if (!target) {
        api.setError('Tell Jarvis which assistant this is for.');
        return false;
      }
      api.setBusy(true);
      api.setSubmitLabel('Generating…');
      try {
        const result = await postJson(`/api/improvement/proposals/${encodeURIComponent(p.id)}/implementation-prompt`, { target });
        if (!result.ok) throw new Error(result.error || 'Could not generate a prompt.');
        return result;
      } catch (err) {
        api.setError(err?.message || 'Could not generate a prompt.');
        api.setBusy(false);
        api.setSubmitLabel('Generate');
        return false;
      }
    },
  });
  if (!targetResult) return;

  let copyBtn;
  await openModal({
    title: `Ready to paste into ${targetResult.implementationTarget}`,
    submitLabel: 'Done',
    size: 'wide',
    build(body) {
      const textarea = document.createElement('textarea');
      textarea.readOnly = true;
      textarea.value = targetResult.implementationPrompt;
      textarea.rows = 16;
      textarea.style.width = '100%';
      textarea.style.fontFamily = 'monospace';
      body.appendChild(textarea);
      copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'btn';
      copyBtn.textContent = 'Copy to clipboard';
      copyBtn.style.marginTop = '8px';
      copyBtn.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(targetResult.implementationPrompt);
          copyBtn.textContent = 'Copied';
          setTimeout(() => (copyBtn.textContent = 'Copy to clipboard'), 2000);
        } catch {
          copyBtn.textContent = "Couldn't copy — select the text above manually";
        }
      });
      body.appendChild(copyBtn);
    },
    async onSubmit() {
      return true;
    },
  });
  await onChange();
}

// ---------- Suggestions ----------

async function approveProposal(p, onChange) {
  await postJson(`/api/improvement/proposals/${encodeURIComponent(p.id)}/approve`, {});
  await onChange();
}
async function rejectProposal(p, onChange) {
  await postJson(`/api/improvement/proposals/${encodeURIComponent(p.id)}/reject`, {});
  await onChange();
}
async function restoreProposal(p, onChange) {
  await postJson(`/api/improvement/proposals/${encodeURIComponent(p.id)}/restore`, {});
  await onChange();
}
async function deleteProposalPermanently(p, onChange) {
  await fetch(`/api/improvement/proposals/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
  await onChange();
}

/** Click-to-detail for a suggestion — full rationale/helps-Jarvis/helps-user/evidence, plus the same actions the row itself offers so opening detail is never a dead end. */
async function proposalDetailFlow(p, onChange) {
  await openModal({
    title: p.title,
    submitLabel: 'Close',
    build(body, api) {
      body.appendChild(Object.assign(document.createElement('p'), { textContent: kindLabel(p.kind) }));
      if (p.rationale) body.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Why: ${p.rationale}` }));
      if (p.helpsJarvis) body.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Helps Jarvis: ${p.helpsJarvis}` }));
      if (p.helpsUser) body.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Helps you: ${p.helpsUser}` }));
      body.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: p.sourceTier === 1 ? "From Jarvis's own experience." : 'From outside research — needs your review.',
        })
      );
      appendEvidenceExpand(body, p.evidence);

      const actions = document.createElement('div');
      actions.className = 'button-row';
      actions.style.marginTop = '12px';
      // Every terminal action here closes the dialog itself (api.close) —
      // none of these leave the item in the same state the modal was
      // opened to show, so leaving the modal open over a now-stale
      // background list would just be confusing. A real bug found live in
      // this same test pass: the FIRST version of this only called
      // onChange() and never api.close(), so the modal (and its scrim)
      // stayed up, silently blocking the whole page underneath it.
      if (p.status === 'pending') {
        if (p.kind === 'skill' || p.kind === 'code') {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'btn btn-primary';
          btn.textContent = 'Generate a prompt for a coding assistant';
          btn.addEventListener('click', async () => {
            api.close(true);
            await implementationPromptFlow(p, onChange);
          });
          actions.appendChild(btn);
        } else if (p.kind !== 'conflict') {
          const approveBtn = document.createElement('button');
          approveBtn.type = 'button';
          approveBtn.className = 'btn btn-primary';
          approveBtn.textContent = p.kind === 'rule' || p.kind === 'setting' ? 'Approve' : 'Acknowledge';
          approveBtn.addEventListener('click', async () => {
            await approveProposal(p, onChange);
            api.close(true);
          });
          actions.appendChild(approveBtn);
        }
        const rejectBtn = document.createElement('button');
        rejectBtn.type = 'button';
        rejectBtn.className = 'btn';
        rejectBtn.textContent = p.kind === 'conflict' ? 'Dismiss' : 'Reject';
        rejectBtn.addEventListener('click', async () => {
          await rejectProposal(p, onChange);
          api.close(true);
        });
        actions.appendChild(rejectBtn);
      } else if (p.status === 'rejected') {
        const restoreBtn = document.createElement('button');
        restoreBtn.type = 'button';
        restoreBtn.className = 'btn';
        restoreBtn.textContent = 'Restore';
        restoreBtn.addEventListener('click', async () => {
          await restoreProposal(p, onChange);
          api.close(true);
        });
        actions.appendChild(restoreBtn);
        actions.appendChild(
          armedButton('Delete permanently', 'Really delete?', async () => {
            await deleteProposalPermanently(p, onChange);
            api.close(true);
          })
        );
      }
      body.appendChild(actions);
    },
    async onSubmit() {
      return true;
    },
  });
}

function buildProposalRow(p, onChange, { rejectedView = false } = {}) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-stacked list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.addEventListener('click', () => proposalDetailFlow(p, onChange));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      proposalDetailFlow(p, onChange);
    }
  });

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: p.title }));
  const sub = document.createElement('span');
  sub.className = 'list-row-sub';
  sub.textContent = `${kindLabel(p.kind)}${p.rationale ? ' — ' + p.rationale : ''}`;
  main.appendChild(sub);
  const badge = document.createElement('span');
  badge.className = `badge ${p.kind === 'conflict' ? 'warn' : 'good'}`;
  badge.textContent = p.sourceTier === 1 ? "From Jarvis's own experience" : 'From outside research — needs your review';
  main.appendChild(badge);
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  actions.addEventListener('click', (e) => e.stopPropagation());

  if (rejectedView) {
    const restoreBtn = document.createElement('button');
    restoreBtn.type = 'button';
    restoreBtn.className = 'btn';
    restoreBtn.textContent = 'Restore';
    restoreBtn.addEventListener('click', () => restoreProposal(p, onChange));
    actions.appendChild(restoreBtn);
    actions.appendChild(armedButton('Delete permanently', 'Really delete?', () => deleteProposalPermanently(p, onChange)));
  } else {
    if (p.kind === 'skill' || p.kind === 'code') {
      // These never apply through the plain approve route — Jarvis never
      // writes code itself; generating the implementation brief IS the
      // approval action for these two kinds (see implementationPromptFlow()).
      const promptBtn = document.createElement('button');
      promptBtn.type = 'button';
      promptBtn.className = 'btn btn-primary';
      promptBtn.textContent = 'Generate a prompt for a coding assistant';
      promptBtn.addEventListener('click', () => implementationPromptFlow(p, onChange));
      actions.appendChild(promptBtn);
    } else if (p.kind !== 'conflict') {
      const approveBtn = document.createElement('button');
      approveBtn.type = 'button';
      approveBtn.className = 'btn btn-primary';
      approveBtn.textContent = p.kind === 'rule' || p.kind === 'setting' ? 'Approve' : 'Acknowledge';
      approveBtn.addEventListener('click', () => approveProposal(p, onChange));
      actions.appendChild(approveBtn);
    }

    const rejectBtn = armedButton(p.kind === 'conflict' ? 'Dismiss' : 'Reject', 'Really?', () => rejectProposal(p, onChange));
    actions.appendChild(rejectBtn);
  }

  row.appendChild(actions);
  return row;
}

async function renderSuggestions(container) {
  container.innerHTML = '';

  let showRejected = false;

  const toggleRow = document.createElement('label');
  toggleRow.className = 'settings-row';
  toggleRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Show rejected' }));
  const archToggle = toggleSwitch({ value: false, onChange: (v) => { showRejected = v; load(); } });
  toggleRow.appendChild(archToggle.wrapper);
  container.appendChild(toggleRow);

  const listWrap = document.createElement('div');
  container.appendChild(listWrap);

  async function load() {
    listWrap.innerHTML = '';
    const status = showRejected ? 'rejected' : 'pending';
    const res = await fetch(`/api/improvement/proposals?status=${status}`);
    const data = await res.json().catch(() => ({ proposals: [] }));
    const items = data.proposals || [];

    if (!items.length) {
      listWrap.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: showRejected ? 'Nothing rejected.' : 'Nothing waiting for you right now.',
        })
      );
      return;
    }

    const card = sectionCard(showRejected ? (items.length === 1 ? 'A rejected suggestion' : `${items.length} rejected suggestions`) : items.length === 1 ? 'A suggestion' : `${items.length} suggestions`);
    for (const p of items) card.appendChild(buildProposalRow(p, load, { rejectedView: showRejected }));
    listWrap.appendChild(card);
  }

  await load();
}

// ---------- Changes ----------

async function confirmForcedUndo() {
  return openModal({
    title: 'This has changed since',
    submitLabel: 'Undo anyway',
    build(body) {
      body.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: "You (or something else) changed this since Jarvis applied it — undoing now would overwrite that. Undo anyway?",
        })
      );
    },
    async onSubmit() {
      return true;
    },
  });
}

async function showDeletedTargetNotice() {
  return openModal({
    title: "Can't undo this",
    submitLabel: 'OK',
    build(body) {
      body.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: 'The rule this change was about has since been deleted, so there is nothing left to restore.',
        })
      );
    },
    async onSubmit() {
      return true;
    },
  });
}

function buildChangeRow(c, onChange) {
  const row = document.createElement('div');
  row.className = 'list-row';

  const main = document.createElement('div');
  main.className = 'list-row-main';
  // For a 'rule' change, `c.target` is the rule's own id — never shown
  // directly; the human-readable text lives in the change's own `after`
  // snapshot (or `before`, for an undo). A 'setting' change's target IS the
  // meaningful thing to show (the pref's own key name).
  const title =
    c.kind === 'undo'
      ? `Undid an earlier change${c.after?.text ? ` — reverted "${c.after.text}"` : ''}`
      : c.kind === 'rule'
        ? `${c.before ? 'Rule edited' : 'New rule'}: ${c.after?.text || c.target}`
        : `Setting changed: ${c.target}`;
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: title }));
  main.appendChild(
    Object.assign(document.createElement('span'), {
      className: 'list-row-sub',
      textContent: `${formatDate(c.appliedAt)}${c.reason ? ' · ' + c.reason : ''}`,
    })
  );
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  if (c.undoneAt) {
    actions.appendChild(Object.assign(document.createElement('span'), { className: 'badge', textContent: 'Undone' }));
  } else if (c.kind !== 'undo') {
    const undoBtn = document.createElement('button');
    undoBtn.type = 'button';
    undoBtn.className = 'btn';
    undoBtn.textContent = 'Undo';
    undoBtn.addEventListener('click', async () => {
      undoBtn.disabled = true;
      let result = await postJson(`/api/improvement/changes/${encodeURIComponent(c.id)}/undo`, {});
      if (result.ok === false && result.reason === 'target_deleted') {
        await showDeletedTargetNotice();
        undoBtn.disabled = false;
        return;
      }
      if (result.ok === false && result.reason === 'changed_since') {
        const confirmed = await confirmForcedUndo();
        if (!confirmed) {
          undoBtn.disabled = false;
          return;
        }
        result = await postJson(`/api/improvement/changes/${encodeURIComponent(c.id)}/undo`, { force: true });
      }
      await onChange();
    });
    actions.appendChild(undoBtn);
  }
  row.appendChild(actions);
  return row;
}

async function renderChanges(container) {
  container.innerHTML = '';
  const res = await fetch('/api/improvement/changes');
  const data = await res.json().catch(() => ({ changes: [] }));
  const changes = data.changes || [];

  if (!changes.length) {
    container.appendChild(
      Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Nothing has changed yet — this fills in once Jarvis applies its first behaviour rule or setting.' })
    );
    return;
  }

  const card = sectionCard('Recent changes');
  for (const c of changes) card.appendChild(buildChangeRow(c, () => renderChanges(container)));
  container.appendChild(card);
}

// ---------- Learned (rules + lessons) ----------

async function toggleRuleActive(r, onChange) {
  await postJson(`/api/improvement/rules/${encodeURIComponent(r.id)}/toggle`, { active: !r.active });
  await onChange();
}
async function archiveRule(r, onChange) {
  await postJson(`/api/improvement/rules/${encodeURIComponent(r.id)}/archive`, {});
  await onChange();
}
async function restoreRule(r, onChange) {
  await postJson(`/api/improvement/rules/${encodeURIComponent(r.id)}/restore`, {});
  await onChange();
}
async function deleteRulePermanently(r, onChange) {
  await fetch(`/api/improvement/rules/${encodeURIComponent(r.id)}`, { method: 'DELETE' });
  await onChange();
}

/** Rule detail — the one place text is actually editable, per the user's own confirmed choice; an edit writes a real Change row through the existing undo machinery (server.js's PATCH route), so it's undoable like anything else here. */
async function ruleDetailFlow(r, onChange) {
  let textField;
  await openModal({
    title: 'Behaviour rule',
    submitLabel: 'Save changes',
    build(body, api) {
      textField = fieldTextarea('Rule text', '');
      textField.textarea.value = r.text;
      textField.textarea.rows = 3;
      body.appendChild(textField.wrapper);
      body.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: describeScope(r.scope) }));
      body.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Applied ${formatDate(r.createdAt)}` }));

      const actions = document.createElement('div');
      actions.className = 'button-row';
      actions.style.marginTop = '12px';
      // Mute/Unmute stays open (it's a toggle you might flip back and forth
      // while deciding, and the button relabels itself in place). Archive/
      // Restore/Delete permanently are terminal for this view, so each
      // closes the dialog — see proposalDetailFlow's comment on the same
      // point for the real bug this fixes (the modal used to stay open,
      // its scrim silently blocking the whole page underneath it).
      const toggleBtn = document.createElement('button');
      toggleBtn.type = 'button';
      toggleBtn.className = 'btn';
      toggleBtn.textContent = r.active ? 'Mute' : 'Unmute';
      toggleBtn.addEventListener('click', async () => {
        await toggleRuleActive(r, () => {});
        toggleBtn.textContent = toggleBtn.textContent === 'Mute' ? 'Unmute' : 'Mute';
      });
      actions.appendChild(toggleBtn);

      if (r.archivedAt) {
        const restoreBtn = document.createElement('button');
        restoreBtn.type = 'button';
        restoreBtn.className = 'btn';
        restoreBtn.textContent = 'Restore';
        restoreBtn.addEventListener('click', async () => {
          await restoreRule(r, onChange);
          api.close(true);
        });
        actions.appendChild(restoreBtn);
        actions.appendChild(
          armedButton('Delete permanently', 'Really delete?', async () => {
            await deleteRulePermanently(r, onChange);
            api.close(true);
          })
        );
      } else {
        const archiveBtn = document.createElement('button');
        archiveBtn.type = 'button';
        archiveBtn.className = 'btn';
        archiveBtn.textContent = 'Archive';
        archiveBtn.addEventListener('click', async () => {
          await archiveRule(r, onChange);
          api.close(true);
        });
        actions.appendChild(archiveBtn);
      }
      body.appendChild(actions);
    },
    async onSubmit(api) {
      const text = textField.textarea.value.trim();
      if (!text) {
        api.setError('A rule needs some text.');
        return false;
      }
      if (text === r.text) return true; // nothing to save
      const result = await postJson(`/api/improvement/rules/${encodeURIComponent(r.id)}`, { text }, 'PATCH');
      if (!result.ok) {
        api.setError(result.error || 'Could not save that.');
        return false;
      }
      return true;
    },
  });
  await onChange();
}

function buildRuleRow(r, onChange, { archivedView = false } = {}) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.addEventListener('click', () => ruleDetailFlow(r, onChange));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      ruleDetailFlow(r, onChange);
    }
  });

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: r.text }));
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: describeScope(r.scope) }));
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';
  actions.addEventListener('click', (e) => e.stopPropagation());

  if (archivedView) {
    const restoreBtn = document.createElement('button');
    restoreBtn.type = 'button';
    restoreBtn.className = 'btn';
    restoreBtn.textContent = 'Restore';
    restoreBtn.addEventListener('click', () => restoreRule(r, onChange));
    actions.appendChild(restoreBtn);
    actions.appendChild(armedButton('Delete permanently', 'Really delete?', () => deleteRulePermanently(r, onChange)));
  } else {
    actions.appendChild(Object.assign(document.createElement('span'), { textContent: 'On' }));
    const toggle = toggleSwitch({ value: r.active, onChange: () => toggleRuleActive(r, onChange) });
    actions.appendChild(toggle.wrapper);
  }
  row.appendChild(actions);
  return row;
}

async function archiveLesson(l, onChange) {
  await postJson(`/api/improvement/lessons/${encodeURIComponent(l.id)}/archive`, {});
  await onChange();
}
async function restoreLesson(l, onChange) {
  await postJson(`/api/improvement/lessons/${encodeURIComponent(l.id)}/restore`, {});
  await onChange();
}
async function deleteLessonPermanently(l, onChange) {
  await fetch(`/api/improvement/lessons/${encodeURIComponent(l.id)}`, { method: 'DELETE' });
  await onChange();
}

async function lessonDetailFlow(l, onChange) {
  await openModal({
    title: 'Observation',
    submitLabel: 'Close',
    build(body, api) {
      body.appendChild(Object.assign(document.createElement('p'), { textContent: l.text }));
      body.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: l.scope === 'general' ? 'General observation' : l.scope }));
      body.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: `Noticed ${formatDate(l.createdAt)}` }));
      appendEvidenceExpand(body, l.evidence);

      const actions = document.createElement('div');
      actions.className = 'button-row';
      actions.style.marginTop = '12px';
      if (l.status === 'archived') {
        const restoreBtn = document.createElement('button');
        restoreBtn.type = 'button';
        restoreBtn.className = 'btn';
        restoreBtn.textContent = 'Restore';
        restoreBtn.addEventListener('click', async () => {
          await restoreLesson(l, onChange);
          api.close(true);
        });
        actions.appendChild(restoreBtn);
        actions.appendChild(
          armedButton('Delete permanently', 'Really delete?', async () => {
            await deleteLessonPermanently(l, onChange);
            api.close(true);
          })
        );
      } else {
        const archiveBtn = document.createElement('button');
        archiveBtn.type = 'button';
        archiveBtn.className = 'btn';
        archiveBtn.textContent = 'Archive';
        archiveBtn.addEventListener('click', async () => {
          await archiveLesson(l, onChange);
          api.close(true);
        });
        actions.appendChild(archiveBtn);
      }
      body.appendChild(actions);
    },
    async onSubmit() {
      return true;
    },
  });
}

function buildLessonRow(l, onChange, { archivedView = false } = {}) {
  const row = document.createElement('div');
  row.className = 'list-row list-row-clickable';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.addEventListener('click', () => lessonDetailFlow(l, onChange));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      lessonDetailFlow(l, onChange);
    }
  });

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: l.text }));
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: l.scope === 'general' ? 'General observation' : l.scope }));
  row.appendChild(main);

  if (archivedView) {
    const actions = document.createElement('div');
    actions.className = 'list-row-actions';
    actions.addEventListener('click', (e) => e.stopPropagation());
    const restoreBtn = document.createElement('button');
    restoreBtn.type = 'button';
    restoreBtn.className = 'btn';
    restoreBtn.textContent = 'Restore';
    restoreBtn.addEventListener('click', () => restoreLesson(l, onChange));
    actions.appendChild(restoreBtn);
    actions.appendChild(armedButton('Delete permanently', 'Really delete?', () => deleteLessonPermanently(l, onChange)));
    row.appendChild(actions);
  }
  return row;
}

async function renderLearned(container) {
  container.innerHTML = '';

  let showArchivedRules = false;
  let showArchivedLessons = false;

  const rulesCard = sectionCard('Live behaviour rules');
  const rulesToggleRow = document.createElement('label');
  rulesToggleRow.className = 'settings-row';
  rulesToggleRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Show archived' }));
  const rulesArchToggle = toggleSwitch({ value: false, onChange: (v) => { showArchivedRules = v; loadRules(); } });
  rulesToggleRow.appendChild(rulesArchToggle.wrapper);
  rulesCard.appendChild(rulesToggleRow);
  const rulesListWrap = document.createElement('div');
  rulesCard.appendChild(rulesListWrap);
  container.appendChild(rulesCard);

  async function loadRules() {
    rulesListWrap.innerHTML = '';
    const res = await fetch(`/api/improvement/rules${showArchivedRules ? '?includeArchived=true' : ''}`);
    const data = await res.json().catch(() => ({ rules: [] }));
    const all = data.rules || [];
    const rules = showArchivedRules ? all.filter((r) => r.archivedAt) : all;
    if (!rules.length) {
      rulesListWrap.appendChild(
        Object.assign(document.createElement('p'), {
          className: 'hint',
          textContent: showArchivedRules ? 'Nothing archived.' : "Nothing yet — Jarvis hasn't settled on a real, recurring pattern to follow yet.",
        })
      );
      return;
    }
    for (const r of rules) rulesListWrap.appendChild(buildRuleRow(r, loadRules, { archivedView: showArchivedRules }));
  }
  await loadRules();

  const lessonsCard = sectionCard('Recently noticed');
  const lessonsToggleRow = document.createElement('label');
  lessonsToggleRow.className = 'settings-row';
  lessonsToggleRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Show archived' }));
  const lessonsArchToggle = toggleSwitch({ value: false, onChange: (v) => { showArchivedLessons = v; loadLessons(); } });
  lessonsToggleRow.appendChild(lessonsArchToggle.wrapper);
  lessonsCard.appendChild(lessonsToggleRow);
  const lessonsListWrap = document.createElement('div');
  lessonsCard.appendChild(lessonsListWrap);
  container.appendChild(lessonsCard);

  async function loadLessons() {
    lessonsListWrap.innerHTML = '';
    const res = await fetch(`/api/improvement/lessons?status=${showArchivedLessons ? 'archived' : 'active'}`);
    const data = await res.json().catch(() => ({ lessons: [] }));
    const lessons = (data.lessons || []).slice(0, 20);
    if (!lessons.length) {
      lessonsListWrap.appendChild(
        Object.assign(document.createElement('p'), { className: 'hint', textContent: showArchivedLessons ? 'Nothing archived.' : 'Nothing noticed yet.' })
      );
      return;
    }
    for (const l of lessons) lessonsListWrap.appendChild(buildLessonRow(l, loadLessons, { archivedView: showArchivedLessons }));
  }
  await loadLessons();
}

// ---------- Settings ----------

async function renderSettings(container) {
  container.innerHTML = '';
  const [prefsRes, statusRes] = await Promise.all([fetch('/api/prefs'), fetch('/api/improvement/status')]);
  const prefs = await prefsRes.json();
  const status = await statusRes.json().catch(() => ({}));

  const card = sectionCard('How Jarvis improves itself');

  const enabledRow = document.createElement('label');
  enabledRow.className = 'settings-row';
  enabledRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Review its own work and learn from it' }));
  const enabledToggle = toggleSwitch({
    value: prefs.improvementEnabled,
    onChange: (v) => postJson('/api/prefs', { improvementEnabled: v }),
  });
  enabledRow.appendChild(enabledToggle.wrapper);
  card.appendChild(enabledRow);

  const trustRow = document.createElement('label');
  trustRow.className = 'settings-row';
  trustRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'What it may apply on its own' }));
  const trustSelect = fieldSelect('', TRUST_OPTIONS);
  trustSelect.select.value = prefs.improvementTrust;
  trustSelect.select.addEventListener('change', () => postJson('/api/prefs', { improvementTrust: trustSelect.select.value }));
  trustRow.appendChild(trustSelect.select);
  card.appendChild(trustRow);

  const researchRow = document.createElement('label');
  researchRow.className = 'settings-row';
  researchRow.appendChild(Object.assign(document.createElement('span'), { textContent: 'Looking outside its own experience' }));
  const researchSelect = fieldSelect('', RESEARCH_OPTIONS);
  researchSelect.select.value = prefs.improvementResearch;
  researchSelect.select.addEventListener('change', () => postJson('/api/prefs', { improvementResearch: researchSelect.select.value }));
  researchRow.appendChild(researchSelect.select);
  card.appendChild(researchRow);

  card.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent:
        'Only a behaviour rule or a setting, backed by more than one real example from its own work, is ever applied without asking — ' +
        'and only when that trust dial allows it. Anything from outside research, a new Skill, or a code change always waits for you. ' +
        'You\'ll always see it here, with a plain undo, either way.',
    })
  );
  container.appendChild(card);

  const budgetCard = sectionCard('How much this costs');
  budgetCard.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent: `Today: ${status.dailyBudgetRemaining ?? '?'} review${status.dailyBudgetRemaining === 1 ? '' : 's'} left. This week: ${status.weeklyBudgetRemaining ?? '?'} outside-research pass${status.weeklyBudgetRemaining === 1 ? '' : 'es'} left. Deliberately light, to leave room for everything else you ask Jarvis to do.`,
    })
  );
  container.appendChild(budgetCard);
}

const TABS = [
  ['suggestions', 'Suggestions', renderSuggestions],
  ['changes', 'Changes', renderChanges],
  ['learned', 'Learned', renderLearned],
  ['settings', 'Settings', renderSettings],
];

export async function render(container) {
  container.innerHTML = '';

  const tabs = segmented(
    TABS.map(([id, label]) => [id, label]),
    { value: 'suggestions', onChange: (id) => showTab(id) }
  );
  container.appendChild(tabs.wrapper);

  const content = document.createElement('div');
  container.appendChild(content);

  function showTab(id) {
    const entry = TABS.find(([tabId]) => tabId === id);
    if (entry) entry[2](content);
  }

  showTab('suggestions');
}
