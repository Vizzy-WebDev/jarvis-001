// Safety preferences for computer control: which windows/processes/URLs are
// off-limits, how autonomous Jarvis is allowed to be, and how long
// screenshots are kept. Same "merge over defaults" pattern as prefs.js, so a
// new field added later is automatically backward-compatible with whatever's
// already saved on disk.
//
// Pre-filled with sensible defaults per the user's explicit choice during
// design: banking sites, the well-known password managers, and Windows'
// own security/UAC prompts are blocked from the start, not left for the user
// to discover the hard way.

import { readJson, writeJson } from '../store.js';

const FILE = 'safety';

const DEFAULTS = {
  // 'confirmPlan' (default) — Jarvis explains its plan once, the user
  // approves, then it works through the steps unattended, still stopping for
  // anything guard.js classifies as risky. Matches the user's chosen
  // autonomy level (see the design plan's "Confirm the plan, then work").
  autonomy: 'confirmPlan',
  blockedWindowPatterns: [
    'bank',
    'banking',
    '1password',
    'bitwarden',
    'lastpass',
    'keepass',
    'windows security',
    'user account control',
    // Jarvis's own server runs in a terminal window carrying exactly this
    // phrase (see "Start Jarvis.bat") — a control session clicking into it
    // and, say, sending Ctrl+C would take down the very server running it.
    'keep this window open',
  ],
  blockedProcesses: ['1password', 'bitwarden', 'keepassxc', 'lastpass', 'consent'], // consent.exe is the Windows UAC prompt host
  blockedUrlPatterns: ['chase.com', 'bankofamerica.com', 'wellsfargo.com', 'paypal.com/signin'],
  // How many recent screenshots to keep on disk before the oldest are
  // deleted automatically, per the user's "keep recent ones, auto-delete"
  // choice. See control/screenshot-store.js (Stage 2) for where this is read.
  screenshotRetention: { maxCount: 50, maxAgeHours: 24 },
};

export function getSafetyConfig() {
  const saved = readJson(FILE, {});
  return {
    ...DEFAULTS,
    ...saved,
    // Arrays merge as a whole replacement, not concatenation — a user who
    // deliberately removed a default entry (e.g. decided "bank" is too broad)
    // shouldn't have it silently reappear because DEFAULTS still lists it.
    blockedWindowPatterns: saved.blockedWindowPatterns ?? DEFAULTS.blockedWindowPatterns,
    blockedProcesses: saved.blockedProcesses ?? DEFAULTS.blockedProcesses,
    blockedUrlPatterns: saved.blockedUrlPatterns ?? DEFAULTS.blockedUrlPatterns,
    screenshotRetention: { ...DEFAULTS.screenshotRetention, ...(saved.screenshotRetention || {}) },
  };
}

export function setSafetyConfig(partial) {
  const next = { ...getSafetyConfig(), ...partial };
  writeJson(FILE, next);
  return next;
}
