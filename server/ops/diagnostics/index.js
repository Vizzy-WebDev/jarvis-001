// Registers every self-diagnosis check — the one place the full list lives,
// mirroring heartbeat/index.js's own startHeartbeat() pattern. Adding a new
// check is one import + one registerCheck() call here, never a change to
// registry.js or source.js.

import { registerCheck } from './registry.js';

import * as memoryCheck from './checks/memory.js';
import * as databaseCheck from './checks/database.js';
import * as jobsCheck from './checks/jobs.js';
import * as schedulerCheck from './checks/scheduler.js';
import * as captureHealthCheck from './checks/capture-health.js';
import * as voiceCheck from './checks/voice.js';
import * as configIntegrityCheck from './checks/security/config-integrity.js';
import * as eventSpikesCheck from './checks/security/event-spikes.js';
import * as listenersCheck from './checks/security/listeners.js';

let registered = false;

/** Called once from server.js, before startHeartbeat() so the 'diagnosis' source has real checks to run on its very first tick. Idempotent. */
export function registerDiagnosticChecks() {
  if (registered) return;
  registered = true;

  registerCheck(memoryCheck);
  registerCheck(databaseCheck);
  registerCheck(jobsCheck);
  registerCheck(schedulerCheck);
  registerCheck(captureHealthCheck);
  registerCheck(voiceCheck);
  registerCheck(configIntegrityCheck);
  registerCheck(eventSpikesCheck);
  registerCheck(listenersCheck);
}
