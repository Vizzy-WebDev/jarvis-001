// The Self-Model assembler — Jarvis's own grounded, structured self-
// knowledge. See root CLAUDE.md's "Self-Model" section for the full design;
// server/self/CLAUDE.md for the module breakdown.
//
// THE INVARIANT THIS FILE EXISTS TO PROTECT, same shape as personality.js's
// own header comment: everything here is a READ. This module has no import
// path to, and must NEVER gain one to, memory-policy.js's decide(),
// improvement-policy.js's decide(), capabilities.js's confirm gate, or
// jobs/job-actions.js. A self-assessment computed here can inform what
// Jarvis SAYS; it can never grant a permission or skip an approval a real
// policy module would otherwise require. If a future edit here ever needs
// to write, or to influence a decision another module owns, that is a sign
// it belongs somewhere else, not in this file.
//
// The OTHER invariant: every field returned here carries how sure it is and
// where it came from. Nothing in this module invents a number, a ratio, or
// a claim it can't point at a real row or a real live value for — where
// there's no evidence, the honest return is an explicit "no track record",
// never an estimate dressed up as one. This is what "grounded" means for
// this build; see the build's own overriding rule in root CLAUDE.md.
//
// Not leaf (imports several stores), but every one of those is itself a
// leaf or leaf-adjacent module — none of this file's imports ever reach
// capabilities.js, tools/index.js, models/runner.js, scheduler/*, or
// control/session.js. This is what keeps server/tools/check_myself.js safe
// to import this file directly without tripping the loader circular-import
// invariant (root CLAUDE.md's Gotchas section).

import * as selfStore from './self-store.js';
import { detectSelfSignals } from './self-signals.js';
import * as jobStore from '../jobs/job-store.js';
import * as improvementStore from '../improvement/improvement-store.js';
import * as memoryStore from '../memory/memory-store.js';
import { listConnectors } from '../connectors/store.js';
import { listUserSkills } from '../skills/store/skill-files.js';
import { getPrefs } from '../prefs.js';
import { listModels } from '../models/registry.js';
import { THRESHOLDS as MEMORY_TRUST_THRESHOLDS } from '../memory/memory-policy.js';
import { MIN_EVIDENCE_BY_TRUST as IMPROVEMENT_MIN_EVIDENCE } from '../improvement/improvement-policy.js';

// A rolling tally below this many recorded attempts is treated as "no real
// track record yet" rather than reported as a ratio — five successes (or
// failures) is a coin-flip's worth of evidence, and a ratio computed from
// fewer than that reads as far more confident than it actually is.
const MIN_ATTEMPTS_FOR_RATIO = 5;

function reliabilityFromCounts(attempts, failures) {
  if (!attempts || attempts < MIN_ATTEMPTS_FOR_RATIO) {
    return { grounded: attempts > 0, verdict: 'no_track_record', attempts: attempts || 0, failures: failures || 0 };
  }
  const successRate = Math.round(((attempts - failures) / attempts) * 100) / 100;
  return { grounded: true, verdict: 'has_track_record', attempts, failures, successRate };
}

// ---------- 1. What it is ----------

/**
 * Live counts, never a guess — same discipline connectorsSection()/
 * skillsSection() (prompt.js) already use for "what's connected"/"how many
 * Skills." The `structure` field is the one piece of this dimension that
 * ISN'T derivable from live state (there's no registry of "what an
 * orchestrator is") — it's a short, hand-written description, explicitly
 * marked `verified: false` because a static description can drift from the
 * real code in a way none of the counts beside it can.
 */
function whatItIs({ capabilities } = {}) {
  const models = listModels();
  const skills = listUserSkills().filter((s) => s.enabled);
  const connectorsUsable = listConnectors().filter(
    (c) => c.type === 'mcp' && c.config?.secretRef && Array.isArray(c.mcpTools) && c.mcpTools.length > 0
  );
  const activeJobs = jobStore.listActiveJobs();

  return {
    counts: {
      enabledModels: models.filter((m) => m.enabled).length,
      totalModels: models.length,
      connectedApps: connectorsUsable.length,
      installedSkills: skills.length,
      builtInTools: typeof capabilities?.length === 'number' ? capabilities.length : null,
      activeJobs: activeJobs.length,
    },
    countsGrounded: true, // every count above is a live read, this instant
    structure: {
      text:
        'A conversation layer that talks to you directly, one or more model connections it routes turns through, ' +
        'a background Job orchestrator for work it chooses to run apart from this conversation, a Memory store for ' +
        'durable facts about you, and a Self-Improvement loop that turns its own recurring patterns into behaviour ' +
        'rules it follows going forward.',
      verified: false, // hand-written, not derived — the weakest-grounded field in this whole dimension
    },
  };
}

// ---------- 2. What it can/can't actually do ----------

/**
 * `axis`/`key` follow self-store.js's schema exactly. For 'tool', reads the
 * rolling self_capability_stats tally (self-capture.js writes this on every
 * live tool call). For 'job_kind'/'task_type', no rolling tally exists (see
 * self-store.js's header comment on why) — reliabilityForJobOrTask() below
 * reads the real aggregate straight from improvement_outcomes instead.
 */
function reliabilityForTool(toolName) {
  const stat = selfStore.getStat('tool', toolName);
  if (!stat) return { axis: 'tool', key: toolName, grounded: false, verdict: 'no_track_record', attempts: 0, failures: 0 };
  return { axis: 'tool', key: toolName, ...reliabilityFromCounts(stat.attempts, stat.failures) };
}

function reliabilityForJobKind(kind) {
  const { attempts, failures } = improvementStore.outcomeReliability({ source: 'job', kind });
  return { axis: 'job_kind', key: kind, ...reliabilityFromCounts(attempts, failures) };
}

function reliabilityForTaskType(type) {
  const { attempts, failures } = improvementStore.outcomeReliability({ source: 'task', kind: type });
  return { axis: 'task_type', key: type, ...reliabilityFromCounts(attempts, failures) };
}

/**
 * `about` selects what to check: `{tools: [...]}`, `{jobKinds: [...]}`,
 * `{taskTypes: [...]}` — any combination, all optional. Omitting `about`
 * entirely returns nothing (never a blanket "here's everything I've ever
 * tried," which would be exactly the unbounded, ungrounded sprawl this
 * whole build exists to avoid) — a caller asks about something specific.
 */
function whatItCanDo({ about } = {}) {
  const tools = (about?.tools || []).map(reliabilityForTool);
  const jobKinds = (about?.jobKinds || []).map(reliabilityForJobKind);
  const taskTypes = (about?.taskTypes || []).map(reliabilityForTaskType);
  return {
    tools,
    jobKinds,
    taskTypes,
    instruction:
      'Report confidence ONLY when verdict is has_track_record — state the real success rate and how many attempts it is ' +
      'based on. For no_track_record, say plainly there is no track record yet; never estimate a number in its place.',
  };
}

// ---------- 3. How it behaves ----------

/** A read-only VIEW over Self-Improvement's existing tables — this build owns none of this data, per the settled decision in root CLAUDE.md's Self-Model section. */
function howItBehaves() {
  const rules = improvementStore.listRules({ activeOnly: true, scope: 'general' });
  return {
    generalRules: rules.map((r) => ({ text: r.text, since: r.createdAt })),
    grounded: true,
    note: rules.length ? null : 'No general behaviour rules have been learned yet — too little history to say anything general.',
  };
}

// ---------- 4. What it's doing now, and why ----------

/**
 * `style` is personality.js's readStyle() result, ALREADY computed this
 * turn by models/runner.js — passed in, never recomputed here. This
 * dimension REPORTS Personality's own decision; it never makes one (see
 * this file's header invariant).
 */
function whatItsDoingNow({ style, sessionId } = {}) {
  const activeJobs = jobStore.listActiveJobs();
  const prefs = getPrefs();
  const activeGoal = sessionId ? selfStore.getActiveGoal('conversation', sessionId) : null;
  return {
    activeJobs: activeJobs.map((j) => ({ id: j.id, title: j.title, status: j.status, kind: j.kind })),
    currentGoal: activeGoal ? { text: activeGoal.goalText, declaredAt: activeGoal.declaredAt } : null,
    styleFloors: style?.floors || null, // Personality's own already-computed decision, reported, not re-derived
    styleSticky: style?.sticky || null,
    operatingAssumptions: {
      manualModelPinned: Boolean(prefs.manualModelId),
      autoSelect: Boolean(prefs.autoSelect),
      memoryTrust: prefs.memoryTrust,
      improvementTrust: prefs.improvementTrust,
    },
    note: 'Assumptions stated earlier IN this conversation are not tracked here — only system-level operating state is.',
  };
}

// ---------- 5. How it knows what it claims to know ----------

/** Case-insensitive substring match over listMemories() — same small, duplicated matcher server/tools/forget_something.js and update_memory.js already use rather than sharing, per that pair's own precedent. */
function findMemoryProvenance(query) {
  if (!query) return [];
  const needle = String(query).toLowerCase();
  return memoryStore
    .listMemories({})
    .filter((m) => m.text.toLowerCase().includes(needle))
    .slice(0, 5)
    .map((m) => ({ text: m.text, origin: m.origin, sourceKind: m.sourceKind, notedAt: m.updatedAt || m.createdAt }));
}

function howItKnows({ memoryQuery } = {}) {
  const memories = memoryStore.listMemories({});
  const byOrigin = {};
  for (const m of memories) byOrigin[m.origin] = (byOrigin[m.origin] || 0) + 1;
  return {
    memoryProvenanceCounts: byOrigin, // real counts by real origin — 'approved' | 'auto' | 'explicit' | 'legacy'
    matches: findMemoryProvenance(memoryQuery), // only populated when a specific claim was asked about
    provenanceKinds: [
      "stated directly ('explicit') — the user said this themselves and confirmed it",
      "approved from a suggestion ('approved') — noticed, then the user approved it",
      "inferred and auto-saved ('auto') — noticed and saved without asking, per the user's own trust setting",
      "migrated from old notes ('legacy')",
      'found via search_conversations — cite the date it was said, and that things may have changed since',
      'looked up via research — cite via: web or via: model-search, per server/research.js',
      'general knowledge — no record backs this; say so plainly, never dress it up as recalled or looked up',
    ],
    instruction:
      'Only cite a provenance kind above when a real record actually backs the specific claim being made. Anything else is general knowledge — say that plainly rather than picking a more impressive-sounding category.',
  };
}

// ---------- 6. What's actually its call to make ----------

/**
 * Reads the REAL, live values out of the policy modules that actually gate
 * these decisions — never a paraphrase that could drift from the code. The
 * `why` lines are the one place this dimension explains reasoning rather
 * than reporting a number, so a novel situation can be reasoned about by
 * analogy rather than needing an exact-match rule.
 */
function whatsItsCall() {
  const prefs = getPrefs();
  return {
    memoryApproval: {
      currentTrust: prefs.memoryTrust,
      autoSaveThreshold: MEMORY_TRUST_THRESHOLDS[prefs.memoryTrust] ?? MEMORY_TRUST_THRESHOLDS.ask,
      hardFloor: 'A memory that conflicts with an existing one, or carries no usable confidence score, always requires approval — no trust level overrides this.',
      why: 'Resolving a conflict changes or duplicates something that already exists in the record of the user\'s own life — that is never done silently, at any trust level.',
    },
    selfImprovementApproval: {
      currentTrust: prefs.improvementTrust,
      minEvidence: IMPROVEMENT_MIN_EVIDENCE[prefs.improvementTrust] ?? IMPROVEMENT_MIN_EVIDENCE.ask,
      hardFloors: [
        "must be a plain 'rule' or 'setting' — a Skill or code change always asks",
        'must come from tier 1 evidence (my own directly-observed history) — anything read from outside always asks',
        'must not conflict with an existing rule — a contradiction always needs a human',
      ],
      why: 'Auto-applying a behaviour change is only ever safe when the evidence is Jarvis\'s own, uncontested, and repeated — a single incident, an outside source, or a real conflict all need a person to weigh in.',
    },
    toolConfirmation: {
      rule: "A tool marked confirm:'always' or 'ifUnclear' never runs on its first call — it reads back what it would do and waits for a real yes.",
      why: 'An action with a real, outward effect deserves the same pause a careful person would take before doing it on someone else\'s behalf.',
    },
    backgroundWork: {
      rule: "A background Job's own confirm-gated action parks as awaiting_decision instead of running — nobody is present for a live read-back, and unlike a scheduled task, nobody pre-consented to this specific action.",
      computerControl: "A fresh 'computer'-kind job never starts unattended, full stop, regardless of prior success — operating the real desktop autonomously always asks first.",
      why: 'The durable record of consent for unattended work has to be a real decision on record, not a self-assessment of how well it went last time.',
    },
    generalPrinciple:
      'A strong track record can raise how confident I sound about the SUBSTANCE of an answer. It can never be the reason I skip an approval, a confirmation, or a boundary a real policy module already enforces — that would let my own self-assessment grant myself a permission, which is exactly what this is built never to do.',
  };
}

// ---------- 7. How it specifically tends to fail ----------

/** Same read-only-VIEW discipline as howItBehaves() — scoped lessons, not general ones, so this stays specific rather than vague. */
function howItFails({ scopesInPlay } = {}) {
  const scoped = [];
  for (const scope of scopesInPlay || []) {
    const lessons = improvementStore.listLessons({ scope });
    for (const l of lessons) scoped.push({ scope, text: l.text, evidenceCount: Array.isArray(l.evidence) ? l.evidence.length : 0 });
  }
  return {
    scopedLessons: scoped,
    grounded: true,
    note: scoped.length ? null : 'No known failure pattern recorded for what\'s relevant right now.',
  };
}

// ---------- 8. How it specifically works with the user ----------

function worksWithUser() {
  const corrections = improvementStore.listUnreviewedOutcomes({ limit: 200 }).filter((o) => o.source === 'correction' || o.source === 'explicit');
  return {
    recentCorrectionCount: corrections.length,
    grounded: true,
    note:
      corrections.length < MIN_ATTEMPTS_FOR_RATIO
        ? 'Too little history yet to say anything general about how this user specifically wants things done — treat this as genuinely thin.'
        : null,
  };
}

// ---------- 9. What it's trying to accomplish, and whether it's still on track ----------

function trackGoal({ sessionId } = {}) {
  if (!sessionId) return { grounded: false, activeGoal: null };
  const goal = selfStore.getActiveGoal('conversation', sessionId);
  return {
    grounded: Boolean(goal),
    activeGoal: goal ? { text: goal.goalText, declaredAt: goal.declaredAt } : null,
    instruction: 'This is what I recorded I understood the goal to be, not a verified account of what the user actually wanted — say it that way.',
  };
}

// ---------- assembler ----------

const DIMENSION_BUILDERS = {
  what_it_is: whatItIs,
  can_do: whatItCanDo,
  behavior: howItBehaves,
  doing_now: whatItsDoingNow,
  how_it_knows: howItKnows,
  authority: whatsItsCall,
  failure_modes: howItFails,
  works_with_you: worksWithUser,
  goal: trackGoal,
};

/**
 * `only` (string[]) selects which dimension keys to build — see
 * DIMENSION_BUILDERS above for the valid keys. Omitting it builds nothing
 * (never a default "everything," which would be exactly the unbounded
 * sprawl this build exists to avoid) — a caller always asks for something
 * specific. Every other arg is optional context a builder may use:
 * `capabilities` (listCapabilities() result, injected via ctx — see
 * capabilities.js), `sessionId`, `style` (personality.js's readStyle()
 * result, already computed this turn), `memoryQuery`, `about`
 * ({tools,jobKinds,taskTypes}), `scopesInPlay`.
 */
export function buildSelfModel({ only = [], ...ctx } = {}) {
  const out = {};
  for (const key of only) {
    const builder = DIMENSION_BUILDERS[key];
    if (builder) out[key] = builder(ctx);
  }
  return out;
}

export const DIMENSION_KEYS = Object.keys(DIMENSION_BUILDERS);

// ---------- turn-level signal computation ----------

/**
 * The live-data half of self-signals.js's pure detectSelfSignals() — reads
 * the real, current state (jobs, pending memory conflicts, active
 * lessons/rules, tool stats) and hands it in as plain data, keeping
 * self-signals.js itself zero-import and independently testable. Called
 * once per step from models/runner.js's own tool-calling loop (never once
 * per turn only) — `usedToolNames` is whatever this TURN has already
 * called in an earlier step, so a known-failure/no-track-record warning can
 * only ever surface starting on the step AFTER a tool's first use within
 * this same turn (or is caught proactively via the check_myself tool
 * instead) — an accepted limitation of a push-only mechanism with no
 * foreknowledge of what the model is about to decide to call; see root
 * CLAUDE.md's Self-Model section.
 */
export function computeTurnSignals({ sessionId, correctionDetected = false, usedToolNames = [] } = {}) {
  const activeJobs = jobStore.listActiveJobs().map((j) => ({ status: j.status }));
  const pendingMemoryConflict = memoryStore.listPendingCandidates().some((c) => c.conflictWith);
  const activeLessonScopes = [
    ...improvementStore.listRules({ activeOnly: true }).map((r) => r.scope),
    ...improvementStore.listLessons({}).map((l) => l.scope),
  ];
  const scopesInPlay = usedToolNames.map((name) => `tool:${name}`);
  const noTrackRecordChecks = usedToolNames.map((name) => {
    const stat = selfStore.getStat('tool', name);
    return { axis: 'tool', key: name, attempts: stat?.attempts || 0 };
  });
  return detectSelfSignals({
    activeJobs,
    pendingMemoryConflict,
    correctionDetected,
    scopesInPlay,
    activeLessonScopes,
    noTrackRecordChecks,
  });
}
