// Small store for app-wide preferences that aren't a single model's
// business: whether auto-select is on, the speed/quality balance dial, a
// manually-pinned model, and the voice-clarity confirmation sensitivity.
// Follows the same "merge over defaults" pattern as public/settings.js, so
// adding a new preference later is automatically backward-compatible with
// whatever's already saved on disk.

import { readJson, writeJson } from './store.js';

const FILE = 'prefs';

const DEFAULTS = {
  autoSelect: true,
  balance: 'balanced', // 'fast' | 'balanced' | 'quality'
  manualModelId: null, // used when autoSelect is false, and leads the candidate list either way
  clarifySensitivity: 'balanced', // 'more' | 'balanced' | 'less' — confidence gate for quick actions
  // One model pinned specifically for voice turns (source: 'voice') — outranks
  // manualModelId for voice the same way manualModelId outranks auto-ranking,
  // so a spoken conversation never lands on whatever wins router.js's speed/
  // cost tie-break (see router.js's rankCandidates() header comment for why
  // that mattered: a free code-completion model was winning by file order).
  // null means "no pin, fall through to manualModelId / auto-ranking as before".
  voiceModelId: null,
  // Which server-side TTS provider stream() (server/tts/index.js) resolves
  // to when a caller doesn't specify one explicitly — in practice the
  // client always sends its own choice (public/app.js's voiceOutput select
  // IS the provider ref), so this is a last-resort fallback only. null, not
  // any specific provider name — there is no provider that's always
  // guaranteed configured the way Gemini implicitly was before it was
  // removed; tts/index.js's resolveAdapter() handles null cleanly (falls
  // through to "no provider configured"). The free browser voice is a
  // separate, client-only setting (public/settings.js's voiceOutput) and
  // has no entry here at all — it never reaches the server (see
  // tts/index.js's header comment).
  ttsProvider: null,
  // How much Jarvis saves on its own without asking — see
  // server/memory/memory-policy.js's decide(). 'ask' reproduces the
  // original approval-first behavior exactly (nothing auto-saves); this is
  // the default so nothing changes for anyone who hasn't opened the Memory
  // screen and turned the dial up themselves. A candidate that conflicts
  // with an existing memory always requires approval regardless of this
  // setting — that floor lives in memory-policy.js, not here.
  memoryTrust: 'ask', // 'ask' | 'balanced' | 'auto'
  // How many background Jobs may be actively running at once — see
  // server/jobs/job-policy.js's hasCapacity(). A starting default, not a
  // hardcoded ceiling: the user's own build spec explicitly asked for this
  // to be tunable, never fixed in code, since the right number depends on
  // real usage and how heavy a "job" typically turns out to be.
  maxBackgroundJobs: 2,
  // Self-Improvement (server/improvement/) — see server/improvement/CLAUDE.md.
  // Master on/off switch; the cycle still runs when false only to keep
  // capture cheap and current, but reflect/synthesize/research/life-pattern
  // model calls are skipped entirely.
  improvementEnabled: true,
  // How much auto-applies without asking — same shape and same reasoning
  // as memoryTrust above, but a hard floor lives in improvement-policy.js's
  // decide() that NO trust level ever overrides: only a 'rule' or 'setting'
  // proposal sourced from Jarvis's own task history (never docs/communities/
  // web) can ever auto-apply, and only with enough independent evidence
  // behind it. 'balanced' is the default here (not 'ask', unlike
  // memoryTrust) per the user's own explicit choice — small, well-evidenced
  // behaviour fixes are meant to just happen and be reported afterward.
  improvementTrust: 'balanced', // 'ask' | 'balanced' | 'auto'
  // Whether the weekly outside-research/life-pattern passes run at all —
  // 'off' skips both, 'weekly' (default) runs the tier 2-4 research pass
  // and the life-pattern pass on a real weekly budget (see
  // improvement-store.js's WEEKLY_BUDGET). Never runs more often than
  // weekly — the "light" budget the user chose has no faster setting.
  improvementResearch: 'weekly', // 'off' | 'weekly'
  // Heartbeat + Trigger + Proactive Attention (server/heartbeat/) — the
  // fixed schedule the user sets for "no proactive contact." Only a
  // genuine emergency (heartbeat/decision.js's own reasoned verdict, never
  // a hardcoded category) breaks this — see root CLAUDE.md's Heartbeat
  // section. `start`/`end` are 'HH:MM' 24h strings; a wrap past midnight
  // (e.g. 23:00 -> 08:00) is handled by heartbeat/quiet-hours.js. Enabled
  // by default with a sensible night window — unlike memoryTrust/
  // improvementTrust, this isn't an opt-in dial: a brand-new install should
  // never get proactive contact overnight before the user has had a chance
  // to even see the setting.
  quietHours: { enabled: true, start: '23:00', end: '08:00' },
};

export function getPrefs() {
  return { ...DEFAULTS, ...readJson(FILE, {}) };
}

export function setPrefs(partial) {
  const next = { ...getPrefs(), ...partial };
  writeJson(FILE, next);
  return next;
}
