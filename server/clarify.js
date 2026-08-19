// Decides whether a turn's transcription confidence is low enough to
// trigger the voice-clarity guard for "ifUnclear"-confirmed skills (see
// skills/index.js). Typed text is never gated here — it can't be misheard,
// so quick actions from the text box always run immediately; only the
// "always"-confirmed skills (schedules, briefing changes, ...) apply to
// typed input too, and those aren't affected by this file at all.

import { getPrefs } from './prefs.js';

const THRESHOLDS = { more: 0.85, balanced: 0.6, less: 0.35 };

/** Whether this turn's transcription confidence is low enough to ask before a quick action. */
export function isLowConfidence(confidence, source) {
  if (source !== 'voice') return false;
  if (confidence === undefined || confidence === null || Number.isNaN(Number(confidence))) return false;
  const threshold = THRESHOLDS[getPrefs().clarifySensitivity] ?? THRESHOLDS.balanced;
  return Number(confidence) < threshold;
}
