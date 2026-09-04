// Provider-reported balance/usage polling — the "PROVIDER-REPORTED" third of
// the three separately-labelled cost numbers (see cost-store.js's header
// comment). Only ever polls a real, documented endpoint; a provider with no
// such endpoint (Gemini, Anthropic, Deepgram — see the exploration behind
// this build) simply never gets a row here, and report.js says so rather
// than inventing one.
//
// Not leaf-safe (imports tts/elevenlabs.js, which imports
// external-services.js — still a leaf itself, so this file stays safe for
// server/tools/check_spending.js to import, just not for anything under the
// stricter server/tools/ -> tools/index.js loader chain to worry about,
// since nothing here touches that chain either).

import { getSecret } from '../config.js';
import { listConnections } from '../models/connections.js';
import * as elevenlabs from '../tts/elevenlabs.js';
import { recordBalance } from './cost-store.js';

/**
 * OpenRouter's own key-status endpoint. Unlike models/quota.js's
 * quotaStatusFor() (which exists purely to show a pre-recheck cost
 * ESTIMATE and deliberately drops everything but is_free_tier/
 * limit_remaining), this reads `data.usage` too — a real cumulative spend
 * figure OpenRouter already computes and returns, previously read into the
 * response body and then never mapped out anywhere in this codebase.
 */
async function pollOpenRouter(connection) {
  const secret = connection.secretRef ? getSecret(connection.secretRef) : null;
  if (!secret) return null;
  try {
    const res = await fetch('https://openrouter.ai/api/v1/key', { headers: { Authorization: `Bearer ${secret}` } });
    if (!res.ok) return null;
    const body = await res.json().catch(() => null);
    const data = body?.data;
    if (!data) return null;
    return {
      isFreeTier: Boolean(data.is_free_tier),
      usageUsd: typeof data.usage === 'number' ? data.usage : null,
      limitUsd: typeof data.limit === 'number' ? data.limit : null,
      remainingUsd: typeof data.limit_remaining === 'number' ? data.limit_remaining : null,
    };
  } catch (err) {
    console.error('[cost] OpenRouter balance poll failed:', err?.message || err);
    return null;
  }
}

/**
 * ElevenLabs' account endpoint — `GET /v1/user` already gets called today
 * by testKey() purely to check the key is valid, and its real response body
 * (subscription.character_count/character_limit) has never been read. See
 * root CLAUDE.md's Cost tracking section.
 */
async function pollElevenLabs() {
  const resolved = elevenlabs.resolvedKey();
  if (!resolved) return null;
  try {
    const res = await fetch('https://api.elevenlabs.io/v1/user', { headers: { 'xi-api-key': resolved.key } });
    if (!res.ok) return null;
    const body = await res.json().catch(() => null);
    const sub = body?.subscription;
    if (!sub) return null;
    return {
      charactersUsed: typeof sub.character_count === 'number' ? sub.character_count : null,
      characterLimit: typeof sub.character_limit === 'number' ? sub.character_limit : null,
      nextResetUnix: typeof sub.next_character_count_reset_unix === 'number' ? sub.next_character_count_reset_unix : null,
      tier: sub.tier || null,
    };
  } catch (err) {
    console.error('[cost] ElevenLabs balance poll failed:', err?.message || err);
    return null;
  }
}

/**
 * Polls every provider this build knows how to poll and records whatever it
 * actually got — never throws, safe to call from a Heartbeat-scheduled tick
 * or an on-demand check_spending call alike. Returns how many providers
 * were successfully updated, for a caller that wants to report it.
 */
export async function refreshAllBalances() {
  let updated = 0;

  const openRouterConnections = listConnections().filter((c) => c.provider === 'openrouter');
  for (const conn of openRouterConnections) {
    const result = await pollOpenRouter(conn);
    if (result) {
      recordBalance(`openrouter:${conn.id}`, result);
      updated++;
    }
  }

  const elevenLabsResult = await pollElevenLabs();
  if (elevenLabsResult) {
    recordBalance('elevenlabs', elevenLabsResult);
    updated++;
  }

  return { updated };
}

// A plain periodic timer, deliberately NOT routed through the Heartbeat's
// registerSource()/routeFinding() pipeline — that mechanism exists to judge
// whether something is worth INTERRUPTING the owner for (decision.js spends
// a real model call per finding, even a Tier 3 one), and a silent balance
// refresh has no finding to judge at all. Six hours is frequent enough to
// keep "what's my ElevenLabs balance" answerable without a manual refresh,
// without hammering either provider's own account endpoint.
const REFRESH_INTERVAL_MS = 6 * 60 * 60 * 1000;
let pollTimer = null;

/** Starts the periodic balance refresh — called once from server.js, same as every other subsystem's own start*() call. Idempotent. */
export function startBalancePolling() {
  if (pollTimer) return;
  refreshAllBalances().catch((err) => console.error('[cost] initial balance refresh failed:', err));
  pollTimer = setInterval(() => {
    refreshAllBalances().catch((err) => console.error('[cost] periodic balance refresh failed:', err));
  }, REFRESH_INTERVAL_MS);
  pollTimer.unref?.();
}
