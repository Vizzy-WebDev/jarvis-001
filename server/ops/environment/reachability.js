// "Can Jarvis actually reach the things it depends on, right now" — a single
// read across models, connectors, and voice services, so a caller (or the
// model itself, before starting something) can ask "is X reachable" without
// knowing which of three different subsystems tracks that fact. Never polls
// anything live on its own — reads whatever each subsystem already knows
// (health.js's in-memory breaker + registry.js's persisted
// availability.state for models; connectors/store.js's own last-tested
// status.state for connectors; each voice provider's own isConfigured()).
//
// Leaf-adjacent: every import here is itself a leaf (registry.js, health.js,
// connectors/store.js, tts/index.js, stt/deepgram.js — none touch
// tools/index.js/capabilities.js/runner.js/scheduler/control/session.js),
// so this stays safe for server/tools/check_environment.js.

import { listModels } from '../../models/registry.js';
import { isHealthy, getHealthStatus } from '../../models/health.js';
import { listConnectors } from '../../connectors/store.js';
import * as tts from '../../tts/index.js';
import * as deepgram from '../../stt/deepgram.js';

/**
 * Every enabled model's real reachability — 'working' (recently succeeded,
 * or never marked otherwise), 'cooling_down' (health.js's in-memory breaker
 * is currently skipping it), or whatever its own persisted
 * availability.state says (registry.js's own field, written by runner.js
 * after a real call). Two independent signals reported honestly as two
 * fields, never collapsed into one guess — see root CLAUDE.md's Model
 * system section on why health.js and availability.state are deliberately
 * separate.
 */
export function modelReachability() {
  const health = getHealthStatus();
  return listModels()
    .filter((m) => m.enabled)
    .map((m) => ({
      id: m.id,
      label: m.label,
      inMemoryHealthy: isHealthy(m.id),
      inMemoryCooldown: health[m.id] || null,
      persistedAvailability: m.availability?.state || 'unknown',
      persistedCheckedAt: m.availability?.checkedAt || null,
    }));
}

/** Every connector's own last-tested status — see connectors/store.js's header comment: this is a stale record of the last human-triggered test or last real dispatch failure, never a live probe of its own. */
export function connectorReachability() {
  return listConnectors().map((c) => ({
    id: c.id,
    label: c.label,
    type: c.type,
    state: c.status?.state || 'untested',
    checkedAt: c.status?.checkedAt || null,
    detail: c.status?.detail || null,
  }));
}

/** Voice services this build can check without a network call — real reachability of a specific STT/TTS provider needs a live connection attempt, which this deliberately doesn't do on every check (see check_environment.js's own probe option for that). */
export function voiceServiceReachability() {
  return {
    ttsConfigured: tts.isConfigured(),
    sttDeepgramConfigured: deepgram.isConfigured(),
    // The browser's own SpeechRecognition/speechSynthesis fallback needs no
    // key and no server-side check at all — always available as the
    // last-resort path, same fact root CLAUDE.md's TTS provider section
    // documents.
    browserFallbackAlwaysAvailable: true,
  };
}

/** The one call site check_environment.js actually needs — everything above, assembled. */
export function fullReachability() {
  return {
    models: modelReachability(),
    connectors: connectorReachability(),
    voice: voiceServiceReachability(),
  };
}
