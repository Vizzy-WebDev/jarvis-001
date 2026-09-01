// Notices patterns in what the user is working on, building, studying, or
// spending on/traveling to — NEVER their emotional state or relationships,
// per the user's own explicit boundary. domains.js's isExcludedDomain() is
// the actual guard, applied on BOTH sides: excluded-domain material is
// stripped out of the input before it ever reaches a model call, and any
// produced insight that still matches is dropped afterward too — a belt
// AND suspenders check, since the input filter alone relies on the source
// text itself carrying an obvious marker, which a model's own paraphrase
// might not.
//
// Inputs are STRICTLY: approved memories, tasks + their run history,
// background jobs, projects (the Planning Partner), and content the user
// shared (Content Analysis) — never raw conversation search, which is both
// far larger and far less curated than what's already distilled into these
// stores. Every insight this module produces must cite at least one real
// id from what it was actually given, or it's discarded in code — the
// structural guard against inventing knowledge about a part of the user's
// life they never actually shared with Jarvis.
//
// Surfaces as 'idea'-kind proposals (never 'rule'/'setting') so
// improvement-policy.js's decide() always requires approval, by
// construction — Jarvis can notice a pattern here, never act on one.
// Shares the WEEKLY budget with improve-research.js. Not a leaf module —
// safe to import from cycle.js only.

import { askModel } from '../ai.js';
import * as store from './improvement-store.js';
import { getPrefs } from '../prefs.js';
import { isExcludedDomain } from './domains.js';
import { listMemories } from '../memory/memory-store.js';
import { listTasks, listRuns } from '../scheduler/task-store.js';
import * as jobStore from '../jobs/job-store.js';
import { listProjects } from '../projects/project-store.js';
import { listContent } from '../content/content-store.js';
import { addNotification } from '../notifications.js';
import { broadcast } from '../events.js';

const MIN_HOURS_BETWEEN_RUNS = 24 * 7; // weekly, same as improve-research.js

function hoursSince(iso) {
  if (!iso) return Infinity;
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? (Date.now() - parsed) / 3600000 : Infinity;
}

export function shouldNotice() {
  const prefs = getPrefs();
  if (!prefs.improvementEnabled || prefs.improvementResearch === 'off') return false;
  return hoursSince(store.getLastRunAt('life_patterns')) >= MIN_HOURS_BETWEEN_RUNS;
}

/** Gathers the strictly-scoped input set, with excluded-domain material already stripped — the INPUT-side half of the guard. Returns {block, knownIds} — the formatted prompt text and the set of ids an insight is allowed to cite. */
function gatherInputs() {
  const knownIds = new Set();
  const lines = [];

  const memories = listMemories({}).filter((m) => !isExcludedDomain(m.text));
  if (memories.length) {
    lines.push('Things Jarvis has been explicitly told or has noticed about the user (approved memories):');
    for (const m of memories) {
      lines.push(`- [${m.id}] (${m.category}) ${m.text}`);
      knownIds.add(m.id);
    }
  }

  const tasks = listTasks();
  if (tasks.length) {
    lines.push('', 'Scheduled tasks the user has set up:');
    for (const t of tasks) {
      const runs = listRuns(t.id).slice(0, 3);
      const runsText = runs.length ? ` — recent runs: ${runs.map((r) => (r.ok ? 'ok' : 'failed')).join(', ')}` : '';
      lines.push(`- [${t.id}] ${t.title}${runsText}`);
      knownIds.add(t.id);
    }
  }

  const jobs = jobStore.listJobs({}).slice(-30);
  if (jobs.length) {
    lines.push('', "Recent background work Jarvis has done for the user:");
    for (const j of jobs) {
      if (isExcludedDomain(j.goal || '')) continue;
      lines.push(`- [${j.id}] ${j.title} (${j.kind}, ${j.status})`);
      knownIds.add(j.id);
    }
  }

  const projects = listProjects().filter((p) => !isExcludedDomain(p.idea || ''));
  if (projects.length) {
    lines.push('', 'Ideas the user has been planning with Jarvis:');
    for (const p of projects) {
      lines.push(`- [${p.id}] ${p.title}: ${String(p.idea || '').slice(0, 200)}`);
      knownIds.add(p.id);
    }
  }

  const content = listContent().filter((c) => !isExcludedDomain(c.title || ''));
  if (content.length) {
    lines.push('', 'Content the user has shared with Jarvis:');
    for (const c of content.slice(0, 20)) {
      lines.push(`- [${c.id}] ${c.title || 'Untitled'}`);
      knownIds.add(c.id);
    }
  }

  return { block: lines.join('\n'), knownIds };
}

/**
 * The actual work — spends ONE weekly-budget unit if there's anything worth
 * looking at AND the (shared, with improve-research.js) weekly budget has
 * room. Returns `{ranAt, ideasCreated}` or null if it skipped.
 */
export async function maybeNoticeLifePatterns() {
  if (!shouldNotice()) return null;
  const { block, knownIds } = gatherInputs();
  if (!block.trim()) return null; // nothing to look at yet — don't spend the budget on an empty prompt
  if (!store.tryConsumeWeeklyBudget()) return null;
  store.setLastRunAt('life_patterns');

  const prompt = [
    "Below is everything the user has explicitly shared with Jarvis or asked it to do — memories, scheduled tasks, background work, plans, and shared content.",
    'Look for a genuine, specific pattern worth mentioning: something recurring in their WORK, PROJECTS, STUDY, FINANCES, or TRAVEL — never their emotional state or relationships, and never something you are only guessing at from a single data point.',
    'A good example: recurring travel to the same place worth a standing reminder; a repeated expense category worth flagging; a study habit that could use a Skill built for it. A bad example: anything about how they seem to be feeling, or about a relationship.',
    '',
    block,
    '',
    'Reply with JSON: {"insights": [{"text": "...", "evidenceRefs": ["id", "id"], "confidence": 0.0-1.0}]}.',
    '"evidenceRefs": the bracketed id(s) above that this insight is actually based on — never invent one, never cite an id not shown above.',
    'An empty insights array is completely normal, and is the right answer most of the time. Never invent a pattern the material above does not actually show, and never comment on feelings or relationships even if they are mentioned in passing above.',
  ].join('\n');

  const result = await askModel({
    prompt,
    system:
      "You notice genuine, evidence-backed patterns in what the user is doing — work, projects, study, finances, travel — strictly from what they have actually shared. You never comment on emotional state or relationships, and never invent a pattern the material does not show.",
    json: true,
    background: true,
  });

  let ideasCreated = 0;
  const batchId = `batch${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const titles = [];

  if (result.ok && result.data && Array.isArray(result.data.insights)) {
    for (const raw of result.data.insights) {
      const text = String(raw?.text || '').trim();
      if (!text) continue;
      // OUTPUT-side domain guard — the second half of the belt-and-
      // suspenders check (see this file's header comment).
      if (isExcludedDomain(text)) continue;

      const evidenceRefs = Array.isArray(raw?.evidenceRefs) ? raw.evidenceRefs.filter((id) => knownIds.has(id)) : [];
      if (!evidenceRefs.length) continue; // never file an insight with no real evidence behind it

      const proposal = store.createProposal({
        kind: 'idea',
        title: text.slice(0, 100),
        rationale: text,
        helpsUser: text,
        evidence: evidenceRefs,
        sourceTier: 1, // Jarvis's own direct observation of what the user actually shared — not outside research
        batchId,
      });
      ideasCreated++;
      titles.push(proposal.title);
    }
  }

  if (titles.length) {
    addNotification({
      kind: 'improvement',
      level: 'info',
      title: titles.length === 1 ? 'Jarvis noticed something that might help' : `Jarvis noticed ${titles.length} things that might help`,
      body: titles.join(' · '),
      action: { label: 'Review Self-Improvement', section: 'improvement' },
    });
    broadcast({ type: 'improvement_proposals_ready', count: titles.length, batchId });
  }

  return { ranAt: new Date().toISOString(), ideasCreated };
}
