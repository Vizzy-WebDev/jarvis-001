// Heartbeat source #2 (day one): time-sensitive Memory commitments — things
// the user said they'd do by a certain time, surfaced from ordinary
// approved memories (`memories` has no deadline column; this derives one).
//
// Deterministic parsing (date-parse.js) is the always-on, zero-quota path
// and runs on every tick for every candidate memory. A budgeted model call
// (heartbeat/budget.js, its own ledger — never Self-Improvement's) is spent
// AT MOST ONCE per memory TEXT VERSION, and only when that text is new or
// has changed since the last attempt AND the deterministic parser came back
// empty — see date-parse.js's own header comment on why the parser is a
// deliberately incomplete safety-netted first pass, not a claim of full
// coverage. Leaf-adjacent: memory/memory-store.js, ai.js, date-parse.js,
// budget.js, schedule-store.js — same shape as improvement/reflect.js.

import { listMemories, getMemory } from '../../memory/memory-store.js';
import { askModel } from '../../ai.js';
import { parseDeadline, looksTimeReferencing } from '../date-parse.js';
import { tryConsumeDailyBudget } from '../budget.js';
import { getItem as getScheduleItem } from '../schedule-store.js';

export const SOURCE_ID = 'commitments';
const CHECK_INTERVAL_MS = 30 * 60 * 1000; // deterministic-only checks are cheap; the model spend is budget-gated separately
const APPROACHING_WINDOW_MS = 24 * 60 * 60 * 1000;
const BUDGET_KEY = 'heartbeat_commitments_daily_budget';
const DAILY_BUDGET = 8;

function fmt(iso) {
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

/**
 * One model-call attempt at extracting a deadline the deterministic parser
 * missed. Returns `{iso, confidence:'model'}` or `null` — never throws;
 * askModel()'s own `{ok:false}` (no model available, quota gone — the
 * documented normal state for this project) is treated the same as "no
 * deadline found," not as an error worth surfacing.
 */
async function tryModelExtract(text, now) {
  const result = await askModel({
    background: true,
    json: true,
    system:
      'You extract a single commitment deadline from one short note, if one is genuinely stated or clearly implied. ' +
      `Today's date is ${now.toISOString().slice(0, 10)}. Reply with JSON only: ` +
      '{"hasDeadline": true|false, "date": "YYYY-MM-DD"|null}. ' +
      'Only set hasDeadline true if a real date or time commitment is actually implied — never guess one that is not there.',
    prompt: `Note: "${text}"`,
  });
  if (!result.ok || !result.data?.hasDeadline || !result.data?.date) return null;
  const parsed = new Date(`${result.data.date}T23:59:59`);
  if (Number.isNaN(parsed.getTime())) return null;
  return { iso: parsed.toISOString(), confidence: 'model' };
}

export async function listItems() {
  const memories = listMemories({ includeArchived: false });
  return memories.filter((m) => looksTimeReferencing(m.text)).map((m) => ({ itemKey: m.id, intervalMs: CHECK_INTERVAL_MS }));
}

export async function check(itemKey) {
  const memory = getMemory(itemKey);
  if (!memory || memory.archived) return { finding: null };

  const prior = getScheduleItem(SOURCE_ID, itemKey)?.checkState || {};
  const now = new Date();
  let { deadlineIso = null, confidence = null, resolved = false, notifiedApproaching = false, notifiedOverdue = false } = prior;

  const textChanged = prior.lastText !== memory.text;
  if (textChanged || !resolved) {
    const parsed = parseDeadline(memory.text, now);
    if (parsed) {
      deadlineIso = parsed.iso;
      confidence = parsed.confidence;
      resolved = true;
    } else if (await tryConsumeDailyBudget(BUDGET_KEY, DAILY_BUDGET)) {
      const modelResult = await tryModelExtract(memory.text, now);
      deadlineIso = modelResult?.iso ?? null;
      confidence = modelResult?.confidence ?? null;
      resolved = true; // one real attempt spent either way — don't retry this exact text again
    } else {
      // Budget exhausted for today — leave `resolved` false so a future
      // tick (once the daily ledger rolls over) gives this a real attempt
      // instead of permanently giving up on it.
      deadlineIso = null;
      confidence = null;
      resolved = false;
    }
    if (textChanged) {
      notifiedApproaching = false;
      notifiedOverdue = false;
    }
  }

  const checkState = { lastText: memory.text, deadlineIso, confidence, resolved, notifiedApproaching, notifiedOverdue };

  if (!deadlineIso) return { finding: null, checkState };

  const deadline = new Date(deadlineIso);
  const msUntil = deadline.getTime() - now.getTime();

  if (msUntil < 0 && !notifiedOverdue) {
    checkState.notifiedOverdue = true;
    return {
      finding: {
        summary: `A commitment you noted looks overdue: "${memory.text}" — was due ${fmt(deadlineIso)}.`,
        detail: JSON.stringify({ memoryId: itemKey, deadlineIso }),
      },
      checkState,
    };
  }
  if (msUntil >= 0 && msUntil <= APPROACHING_WINDOW_MS && !notifiedApproaching) {
    checkState.notifiedApproaching = true;
    return {
      finding: {
        summary: `Coming up: "${memory.text}" — due ${fmt(deadlineIso)}.`,
        detail: JSON.stringify({ memoryId: itemKey, deadlineIso }),
      },
      checkState,
    };
  }
  return { finding: null, checkState };
}

export const source = { id: SOURCE_ID, defaultIntervalMs: CHECK_INTERVAL_MS, listItems, check };
