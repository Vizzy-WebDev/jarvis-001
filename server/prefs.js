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
};

export function getPrefs() {
  return { ...DEFAULTS, ...readJson(FILE, {}) };
}

export function setPrefs(partial) {
  const next = { ...getPrefs(), ...partial };
  writeJson(FILE, next);
  return next;
}
