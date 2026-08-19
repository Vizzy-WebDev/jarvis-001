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
};

export function getPrefs() {
  return { ...DEFAULTS, ...readJson(FILE, {}) };
}

export function setPrefs(partial) {
  const next = { ...getPrefs(), ...partial };
  writeJson(FILE, next);
  return next;
}
