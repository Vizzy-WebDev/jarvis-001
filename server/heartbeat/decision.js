// The urgency-reasoning step — the ONE place a Heartbeat/Trigger finding is
// judged "worth surfacing, and how urgently," from real context rather than
// a hardcoded category list (the build's own explicit requirement: a fixed
// "emergency categories" list breaks the moment the user's life or
// priorities change). Jobs' own tier assignment at its own call sites
// (worker.js, orchestrator.js) is untouched and never calls this — those
// are mechanical "does this need the owner" facts, not a judgment call.
//
// One model call does two jobs at once (urgency tier, and — only when
// quiet hours are active — whether this clears the emergency bar) to keep
// quota cost down, per root CLAUDE.md's own documented quota constraint.
// No model available, or an unparseable reply, is NEVER treated as Tier 1
// or an emergency by default — it falls back to Tier 3 (a quiet record
// only). Silence is the safe failure direction in both places.
//
// Leaf-adjacent: ai.js, memory/memory-store.js, quiet-hours.js.

import { askModel } from '../ai.js';
import { approvedMemoriesText } from '../memory/memory-store.js';
import { isQuietNow } from './quiet-hours.js';

const VALID_TIERS = new Set([1, 2, 3]);

function fallbackVerdict(reason) {
  return { tier: 3, reason, emergency: false, emergencyReason: null };
}

/**
 * `finding` is `{summary, detail?}` from a source's check(). Returns
 * `{tier, reason, emergency, emergencyReason}` — `emergency` is only ever
 * true when quiet hours are actually active; outside quiet hours it's
 * always false regardless of what the model itself said, since it has no
 * meaning there.
 */
export async function decideAttention(finding, { now = new Date() } = {}) {
  const quiet = isQuietNow(now);
  const memories = approvedMemoriesText();

  const system = [
    "You decide whether something Jarvis just noticed is worth interrupting the user about right now, and how urgently.",
    "Weigh it against what is actually known about the user's current priorities below — never match it against a fixed list of \"emergency\" categories or keywords, since that breaks the moment their life or priorities change. Real money, real risk, irreversible harm, or something they've clearly indicated matters a lot right now are signals to reason from, not a checklist.",
    'Tier 1 means it needs the user\'s attention now. Tier 2 means it\'s worth mentioning at a natural moment, not urgent. Tier 3 means it\'s just worth a record, nothing to interrupt for.',
    quiet
      ? 'It is currently the user\'s quiet hours — nothing proactive should reach them right now UNLESS this is a genuine emergency: something urgent AND costly or harmful if it waits (real money, irreversible harm, a real safety issue). Set that bar noticeably higher than an ordinary daytime "important." When genuinely in doubt, it is NOT an emergency.'
      : 'It is not currently quiet hours.',
    'Judge the real-world stakes actually being described, never the TONE the finding happens to be phrased in. A background job\'s own outbox summary is always worded as a polite permission request ("OK to start?") no matter how urgent the underlying situation actually is — that phrasing is just how the job asks, not a signal that nothing urgent is happening. Read past it to what would actually go wrong if this waited.',
    'Reply with JSON only: {"tier": 1, "reason": "one short sentence", "emergency": false, "emergencyReason": null}.',
  ].join(' ');

  const prompt = [
    memories ? `What's known about the user's current priorities:\n${memories}` : "Nothing is currently known about the user's priorities beyond this finding itself.",
    '',
    `What Jarvis just noticed: ${finding.summary}`,
  ].join('\n');

  const result = await askModel({ background: true, json: true, system, prompt });
  if (!result.ok || !result.data) {
    return fallbackVerdict('No model was available to judge this, so it was held rather than guessed at.');
  }

  const tier = VALID_TIERS.has(result.data.tier) ? result.data.tier : 3;
  const emergency = quiet && result.data.emergency === true && Boolean(result.data.emergencyReason);
  return {
    tier,
    reason: typeof result.data.reason === 'string' && result.data.reason ? result.data.reason : 'No reason given.',
    emergency,
    emergencyReason: emergency ? result.data.emergencyReason : null,
  };
}
