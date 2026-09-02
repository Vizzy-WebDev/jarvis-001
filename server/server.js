// Jarvis's local server: serves the browser UI and its API endpoints.
// Deliberately bound to 127.0.0.1 only — it is not reachable from other
// devices on the network.

import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { chat, chatStream, resetConversation, getActiveSessionId, activateConversation, abortActiveTurn } from './brain.js';
import { markLastAssistantInterrupted } from './conversation.js';
import * as chatStore from './chat-store.js';
// Direct runner.js import (not through brain.js) so a monitor's follow-up
// can run WITHOUT autoConfirm — a risky action must still pause and ask,
// even though nobody may be watching the moment it fires (see the monitor
// trigger handler below). scheduler.js's own runOneTurn is the precedent
// for a server-side (not server/tools/ or server/skills/) file importing
// runner.js directly.
import { runTurn } from './models/runner.js';
import * as tts from './tts/index.js';
import * as stt from './stt/index.js';
import { classifyTurnComplete } from './turn-check.js';
import { isLowConfidence } from './clarify.js';
import { createLiveWss } from './live.js';
import { createDuplexWss } from './duplex.js';
import { addClient, removeClient, broadcast } from './events.js';
import { addNotification, listNotifications, markRead, markAllRead, removeNotification, clearAll as clearAllNotifications } from './notifications.js';
import { PROVIDERS, providerForLegacy } from './models/providers.js';
import { getHealthStatus, markHealthy, markUnhealthy } from './models/health.js';
import { classifyError, AVAILABILITY_STATE_FOR_KIND } from './models/error-kind.js';
import { friendlyMessage, friendlyMessageFor } from './friendly-message.js';
import { listCapabilities, hasCapability, reservedSkillNames, listStepCandidates } from './capabilities.js';
import {
  listUserSkills,
  getSkill,
  readSkillMd,
  readSkillToml,
  createSkill,
  updateSkillMd,
  updateSkillState,
  deleteSkill,
  skillRoot,
} from './skills/store/skill-files.js';
import { parseToml, validatePipeline } from './skills/store/skill-toml.js';
import { confirmRequiringSteps } from './skills/pipeline.js';
import { installFromUpload, replaceFromUpload, buildSkillZip } from './skills/store/skill-zip.js';
import {
  listModels,
  listConnections,
  getConnection,
  addModels,
  updateModel,
  deleteModel,
  updateConnection,
  deleteConnection,
  createConnectionWithModels,
  isReady,
  testModelConnection,
  discoverModels,
} from './models/registry.js';
import { probeEndpoint } from './models/probe.js';
import { redactSecrets } from './models/redact.js';
import { quotaStatusFor } from './models/quota.js';
import { getPrefs, setPrefs } from './prefs.js';
import {
  listTasks,
  createTask,
  updateTask,
  deleteTask,
  listRuns,
  runTaskNow,
  startScheduler,
} from './scheduler/scheduler.js';
import { getBriefingConfig, setBriefingConfig, composeBriefing } from './scheduler/briefing.js';
import * as jobStore from './jobs/job-store.js';
import {
  startOrchestrator,
  createJobIfCapacity,
  resumeOrphan,
  restartOrphan,
  resumeStuckJob,
  cancelJob,
} from './jobs/orchestrator.js';
import { startImprovementCycle } from './improvement/cycle.js';
import { startHeartbeat } from './heartbeat/index.js';
import { showOverlay, hideOverlay, updateStep, isOverlayActive, currentOverlayStep } from './control/overlay-bridge.js';
import { isIndicatorActive } from './control/observation-bridge.js';
import { runControlSession, requestStop, confirmPendingAction, getSessionStatus } from './control/session.js';
import { monitorEvents, stopWatching, resumeActiveMonitors, stopAllVisionWatches } from './monitor/engine.js';
import { listMonitors, getMonitor } from './monitor/monitor-store.js';
import { sandboxStatus } from './sandbox/runner.js';
import { listScreenshots, screenshotPath, clearScreenshots } from './control/screenshot-store.js';
import {
  listConnectors,
  addConnector,
  updateConnector,
  deleteConnector,
  getOrCreateSingleton,
  getConnector,
  recordStatus,
} from './connectors/store.js';
import { listCatalog as listConnectorCatalog, getCatalogEntry as getConnectorCatalogEntry } from './connectors/get-catalog.js';
import * as filesConnector from './connectors/files.js';
import * as browserConnector from './connectors/browser.js';
import * as mcpClient from './connectors/mcp-client.js';
import * as mcpRemoteClient from './connectors/mcp-remote-client.js';
import * as oauth from './connectors/oauth.js';
import { cimdUrl, clientMetadataDocument, getPublicBaseUrl, setPublicBaseUrl, CLIENT_METADATA_PATH } from './connectors/client-identity.js';
import { getCatalogClient, getCatalogClientSecret, saveCatalogClient, clearCatalogClient } from './connectors/catalog-credentials.js';
import * as apiClient from './connectors/api-client.js';
import * as cliClient from './connectors/cli-client.js';
import { classifyToolRisk } from './connectors/index.js';
import { resolveConnectorIcon, backfillConnectorIcons, isIconStale, resolveCatalogIcons, getCatalogIcon } from './connectors/icon-resolver.js';
import { saveSecret, deleteSecret, getSecret } from './config.js';
import * as externalServices from './external-services.js';
import {
  listEntries as listProfileEntries,
  addEntry as addProfileEntry,
  deleteEntry as deleteProfileEntry,
  updateEntry as updateProfileEntry,
  listVersions as listProfileEntryVersions,
} from './profile.js';
import {
  listPendingCandidates as listPendingMemoryCandidates,
  approveCandidate as approveMemoryCandidate,
  rejectCandidate as rejectMemoryCandidate,
  resolveConflict as resolveMemoryConflict,
  listMemories,
  listCategories as listMemoryCategories,
  getVersionHistory as getMemoryVersionHistory,
  updateMemory,
  archiveMemory,
  restoreMemory,
  deleteMemory,
} from './memory/memory-store.js';
import {
  listUnreviewedOutcomes as listUnreviewedImprovementOutcomes,
  listLessons as listImprovementLessons,
  updateLessonStatus as updateImprovementLessonStatus,
  deleteLesson as deleteImprovementLesson,
  listProposals as listImprovementProposals,
  getProposal as getImprovementProposal,
  setProposalStatus as setImprovementProposalStatus,
  restoreProposal as restoreImprovementProposal,
  deleteProposal as deleteImprovementProposal,
  listRules as listImprovementRules,
  setRuleActive as setImprovementRuleActive,
  updateRuleText as updateImprovementRuleText,
  archiveRule as archiveImprovementRule,
  restoreRule as restoreImprovementRule,
  deleteRule as deleteImprovementRule,
  getRule as getImprovementRule,
  listChanges as listImprovementChanges,
  recordChange as recordImprovementChange,
  dailyBudgetRemaining as improvementDailyBudgetRemaining,
  weeklyBudgetRemaining as improvementWeeklyBudgetRemaining,
  lookupOutcomes as lookupImprovementOutcomes,
} from './improvement/improvement-store.js';
import { applyProposal as applyImprovementProposal, undoChange as undoImprovementChange } from './improvement/apply.js';
import { generateImplementationPrompt } from './improvement/implementation-prompt.js';
// The Planning Partner and Content Analysis have no routes of their own:
// they had a screen each, and a screen each is what kept them apart. Both
// are driven entirely from conversation (server/skills/*.js) and both write
// into the one transcript, so there is nothing left for a page to call.
// Their engines and stores live under server/projects/ and server/content/.
import { saveUpload } from './uploads.js';
import { prepareForTurn, composeMessage } from './attachments.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Overridable so this server can be run on a second port for testing
// without touching a copy the user already has open (see CLAUDE.md).
const PORT = process.env.PORT ? Number(process.env.PORT) : 3000;
const HOST = '127.0.0.1';

const app = express();
// Mounted BEFORE the global JSON parser, and only for this one path: a
// browser tags a .json file's upload as Content-Type: application/json, and
// if express.json() saw it first it would parse the body into an object and
// mark the request as already consumed — saveUpload() then gets handed an
// object instead of a Buffer and fails with "That upload was empty." Raw
// bytes, on this one path, before anything else gets a chance to interpret them.
app.use('/api/uploads', express.raw({ type: '*/*', limit: '512mb' }));
app.use(express.json());
app.use(express.static(path.join(__dirname, '..', 'public')));

// Strips secretRef (an internal storage key, not the secret itself) and adds
// a plain hasSecret flag — the UI never needs, and should never see, more.
function publicModel(entry) {
  const { secretRef, ...rest } = entry;
  return { ...rest, hasSecret: Boolean(secretRef), ready: isReady(entry) };
}

// A connection saved before the provider catalog existed has no
// provider/kind of its own — backfilled here at read time via
// providerForLegacy() (server/models/providers.js), the same read-time
// pattern catalog.js's withCapabilityDefaults() already uses. Never
// migrates connections.json; a connection saved through the new flow
// already has these set and is passed through untouched.
function publicConnection(conn, models) {
  const { secretRef, ...rest } = conn;
  const backfill = conn.provider ? {} : providerForLegacy(conn.adapter, conn.baseUrl);
  return {
    ...rest,
    ...backfill,
    hasSecret: Boolean(secretRef),
    modelCount: models.filter((m) => m.connectionId === conn.id).length,
  };
}

// ---------- status ----------

app.get('/api/status', (req, res) => {
  res.json({ configured: listModels().some((e) => e.enabled && isReady(e)) });
});

// ---------- models & connections ----------

app.get('/api/models', (req, res) => {
  const models = listModels();
  res.json({
    connections: listConnections().map((c) => publicConnection(c, models)),
    models: models.map(publicModel),
    health: getHealthStatus(),
  });
});

// Replaces the old /api/models/adapters, which asked the user to pick a
// wire protocol by name ("OpenAI-compatible (OpenAI, Ollama, LM Studio,
// OpenRouter, Groq, ...)") — see the Provider System Refactor design note
// in CLAUDE.md. Returns the five user-facing provider tiles; the
// adapter/kind/keyRequired each one resolves to stays entirely server-side.
app.get('/api/models/providers', (req, res) => {
  res.json({ providers: PROVIDERS });
});

app.get('/api/skills', (req, res) => {
  res.json({ skills: listCapabilities({ includeMeta: req.query.all === '1' }) });
});

// ---------- the Skills screen ----------
//
// Distinct from /api/skills above, which lists every runnable skill (built-in
// + user Skill + connector tool) in the flat shape the task/briefing pickers
// need. These routes manage ONLY the user's own Skills — the from-scratch
// rebuild against Claude's real interface (see CLAUDE.md's Skills section).
// `listUserSkills()` (server/skills/store/skill-files.js) is the sole source
// this screen is allowed to read from — it has no code path that can ever
// return a built-in ability, which is what makes the native-ability leak
// structurally impossible rather than just remembered. No catalog/browse
// route exists any more: Jarvis has no third-party skills ecosystem, and no
// install-from-link route either — no Claude surface supports installing a
// skill that way, so this doesn't either (see CLAUDE.md's Skills research).

app.get('/api/skills/installed', (req, res) => {
  res.json({ skills: listUserSkills() });
});

/**
 * Reads + parses + validates a Skill's skill.toml for the detail page, with
 * the FULL "unknown tool" check — this is the one place that can safely do
 * that (it has capabilities.js's listStepCandidates() available; neither
 * server/skills/index.js nor pipeline.js can import capabilities.js at all,
 * see the root CLAUDE.md's circular-import invariant, so their own
 * validation always skips that one check). Never throws. `{present: false}`
 * when this Skill has no skill.toml at all.
 */
function pipelineInfoFor(name, approved) {
  const raw = readSkillToml(name);
  if (raw === null) return { present: false };
  try {
    const doc = parseToml(raw);
    const { errors } = validatePipeline(doc, { knownToolNames: listStepCandidates().map((t) => t.name) });
    // Per-step `needsConfirm` — server/skills/pipeline.js's own
    // confirmRequiringSteps(), the exact function that decides the
    // wrapper tool's own confirm requirement at run time, so the detail
    // page can never show a step as "needs confirmation" that the real
    // run-time gate disagrees with.
    const confirmIds = new Set(confirmRequiringSteps(doc.steps).map((s) => s.id));
    const steps = (doc.steps || []).map((s) => ({ ...s, needsConfirm: confirmIds.has(s.id) }));
    return { present: true, valid: errors.length === 0, errors, approved, description: doc.description || null, inputs: doc.inputs, steps };
  } catch (err) {
    return { present: true, valid: false, errors: [err?.message || 'Could not parse skill.toml.'], approved, description: null, inputs: [], steps: [] };
  }
}

// Fixed-path routes MUST be registered before '/api/skills/:name' below —
// Express matches route order, and ':name' would otherwise swallow a request
// for a fixed path as if it were a skill's name (confirmed live once
// already, when /catalog was added after :name — see git history).
app.get('/api/skills/:name', (req, res) => {
  const skill = getSkill(req.params.name);
  if (!skill) return res.status(404).json({ ok: false, error: 'Unknown skill.' });
  try {
    const { raw, frontmatter, body } = readSkillMd(req.params.name);
    const pipeline = pipelineInfoFor(req.params.name, skill.pipelineApproved);
    res.json({ ok: true, skill, frontmatter, instructions: body, raw, pipeline });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not read that skill.' });
  }
});

// "Write skill instructions" — the create form matched to Claude's own
// (Skill name / Description / Instructions).
app.post('/api/skills', (req, res) => {
  const { name, description, instructions } = req.body || {};
  try {
    const skill = createSkill({ name, description, instructions }, reservedSkillNames());
    res.json({ ok: true, skill });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not create that skill.' });
  }
});

// Upload — a .zip, .skill, or bare .md, exactly Claude's own three real
// upload formats (verified against a real Claude "Upload skill" dialog —
// see CLAUDE.md's Skills section — not just the earlier text-only docs).
// Which of the three it is gets decided by content, not by trusting a
// claimed filename (see skill-zip.js's header comment for why). Raw body,
// same reasoning as /api/uploads: express.raw() covers a single-file upload
// without a multipart-parser dependency this project doesn't otherwise need.
app.post('/api/skills/upload', express.raw({ type: '*/*', limit: '50mb' }), async (req, res) => {
  try {
    const skill = await installFromUpload(req.body, reservedSkillNames());
    res.json({ ok: true, skill });
  } catch (err) {
    // A real, confirmed leak: this can be raw PowerShell stderr from a
    // failed zip extraction (skills/store/skill-zip.js) — a multi-line
    // "Expand-Archive : ... + CategoryInfo : ..." block, not anything a
    // user should ever see. Logged, never shown.
    console.error('[skills] install failed (shown to user as a plain-language message):', err?.message || err);
    res.status(400).json({ ok: false, error: friendlyMessageFor(err, 'This skill install', 'Could not install that skill — check that the file is a valid .zip, .skill, or .md.') });
  }
});

// Replace — the detail page's "Replace" action: swaps an existing skill's
// whole folder content for a new .zip/.skill/.md, same identity
// (name/enabled state). Same content-sniffed dispatch as Upload above.
app.post('/api/skills/:name/replace', express.raw({ type: '*/*', limit: '50mb' }), async (req, res) => {
  if (!getSkill(req.params.name)) return res.status(404).json({ ok: false, error: 'Unknown skill.' });
  try {
    const skill = await replaceFromUpload(req.params.name, req.body);
    res.json({ ok: true, skill });
  } catch (err) {
    console.error('[skills] replace failed (shown to user as a plain-language message):', err?.message || err);
    res.status(400).json({ ok: false, error: friendlyMessageFor(err, 'This replace', 'Could not replace that skill — check that the file is a valid .zip, .skill, or .md.') });
  }
});

// Download — the detail page's "Download" action: the skill's current
// folder packed back into a .zip, folder-as-root (what Upload expects back).
app.get('/api/skills/:name/download', async (req, res) => {
  const skill = getSkill(req.params.name);
  if (!skill) return res.status(404).json({ ok: false, error: 'Unknown skill.' });
  try {
    const buffer = await buildSkillZip(skillRoot(req.params.name));
    res.setHeader('Content-Type', 'application/zip');
    res.setHeader('Content-Disposition', `attachment; filename="${req.params.name}.zip"`);
    res.send(buffer);
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not build that download.' });
  }
});

app.patch('/api/skills/:name', (req, res) => {
  const { description, instructions, enabled } = req.body || {};
  try {
    if (description !== undefined || instructions !== undefined) {
      updateSkillMd(req.params.name, { description, instructions });
    }
    if (enabled !== undefined) {
      updateSkillState(req.params.name, { enabled });
    }
    res.json({ ok: true, skill: getSkill(req.params.name) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that skill.' });
  }
});

app.delete('/api/skills/:name', (req, res) => {
  try {
    deleteSkill(req.params.name);
    res.json({ ok: true });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not remove that skill.' });
  }
});

app.post('/api/models/test', async (req, res) => {
  const { adapter, model, baseUrl, secret, secretRef } = req.body || {};
  if (!adapter || !model) {
    return res.status(400).json({ ok: false, error: 'Please choose a model type and a model name.' });
  }
  const result = await testModelConnection({ adapter, model, baseUrl, secret, secretRef });
  res.json(result);
});

// The combined "Test and add" entry point: resolves `provider` (the
// five-tile selection — see Provider System Refactor design note in
// CLAUDE.md) to an adapter, tests the connection ONCE (using the first
// selected model), then — only on success — saves the connection and every
// selected model together, so several models sharing one address+key don't
// each duplicate the same saved secret (see registry.js). `adapter` alone
// is still accepted (no `provider`) for anything not yet updated to the new
// flow.
app.post('/api/connections', async (req, res) => {
  const { provider, adapter, baseUrl, label, secret, models, resolved } = req.body || {};
  if (!provider && !adapter) {
    return res.status(400).json({ ok: false, error: 'Please choose a provider.' });
  }
  try {
    const result = await createConnectionWithModels({ provider, adapter, baseUrl, label, secret, models, resolved });
    if (!result.ok) return res.status(400).json(result);
    res.json({
      ok: true,
      connection: publicConnection(result.connection, listModels()),
      added: result.added.map(publicModel),
      failed: result.failed,
      steps: result.steps,
    });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not add that connection.' });
  }
});

// Probes a not-yet-saved Custom address: tries the OpenAI/Anthropic/Gemini
// wire shapes in turn (server/models/probe.js) and reports each attempt in
// `steps` so a failure is explainable rather than one generic sentence —
// this is the direct fix for the OmniRoute-class failure the Provider
// System Refactor was written for (see CLAUDE.md). Used by the Custom
// tile's own "Find models at this address" step; the OpenAI/Anthropic/
// Gemini/Local tiles never call this — their wire shape is already known.
app.post('/api/connections/probe', async (req, res) => {
  const { baseUrl, secret } = req.body || {};
  const result = await probeEndpoint({ baseUrl, secret });
  res.json(result);
});

// Discovers what models a server has. `connectionId` reuses an already-saved
// connection's key instead of re-typing it (used by the "Find models at
// this address" button on an existing connection); otherwise `adapter`/
// `baseUrl`/`secret` describe a not-yet-saved one (the "Add a model" popup
// for a known provider — OpenAI/Anthropic/Gemini/Local. A Custom connection
// uses /api/connections/probe above instead, since it has no adapter yet).
app.post('/api/connections/discover', async (req, res) => {
  const { adapter, baseUrl, secret, connectionId } = req.body || {};
  const result = await discoverModels({ adapter: adapter || 'openai-compatible', baseUrl, secret, connectionId });
  res.json(result);
});

app.patch('/api/connections/:id', (req, res) => {
  try {
    const { label, baseUrl, secret } = req.body || {};
    const connection = updateConnection(req.params.id, { label, baseUrl, secret });
    res.json({ ok: true, connection: publicConnection(connection, listModels()) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that connection.' });
  }
});

app.delete('/api/connections/:id', (req, res) => {
  const result = deleteConnection(req.params.id);
  res.json({ ok: true, ...result });
});

// Adds one or more models under an already-existing connection (no test —
// the connection's address+key was already validated when it was created;
// re-testing per model here would mean N live API calls for N models).
app.post('/api/models', (req, res) => {
  const { connectionId, models } = req.body || {};
  if (!connectionId || !Array.isArray(models) || !models.length) {
    return res.status(400).json({ ok: false, error: 'Pick a connection and at least one model.' });
  }
  try {
    const result = addModels(connectionId, models);
    res.json({ ok: true, added: result.added.map(publicModel), failed: result.failed });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not add those models.' });
  }
});

app.patch('/api/models/:id', (req, res) => {
  try {
    const entry = updateModel(req.params.id, req.body || {});
    res.json({ ok: true, model: publicModel(entry) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that model.' });
  }
});

// Same kind -> availability-state mapping runner.js/ai.js use after a live
// turn — imported from error-kind.js (the same leaf module this file already
// gets classifyError from) rather than each call site carrying its own copy,
// so a manual "Test" click and an automatic turn failure always leave a
// model in the same recorded state.
/** Runs one adapter test call and records the outcome as the model's `availability` — the same bookkeeping a live turn does in runner.js, so a manual "Test" click keeps the models screen's badges accurate between real conversations. */
async function testAndRecord(entry) {
  let result;
  try {
    result = await testModelConnection(entry);
  } catch (err) {
    // testModelConnection() itself throwing (vs. resolving with {ok:false})
    // means something on OUR side broke, not the model — never record that
    // as a ban on the model.
    console.error(`[models] testModelConnection threw for "${entry.id}":`, err);
    return { ok: false, error: 'The test could not be run — try again.' };
  }
  try {
    if (result.ok) {
      markHealthy(entry.id);
      updateModel(entry.id, { availability: { state: 'working', checkedAt: new Date().toISOString(), detail: null, technical: null } });
    } else {
      // Classify the RAW error (result.detail) whenever it exists, not the
      // already-cleaned-up result.error — friendlyMessage() rewrites e.g. a
      // quota error into "This model has hit its usage limit for now...",
      // which no longer contains "quota" or "rate limit" and was being
      // misclassified as generic 'other'/'unreachable' (a 6h ban) instead
      // of 'quota' (30min). registry.js's testModelConnection() only sets
      // `detail` on the non-`friendly` branch (see its own comment), so
      // fall back to `result.error` when `detail` is absent.
      const rawDetail = result.detail || result.error;
      const kind = classifyError({ message: rawDetail });
      markUnhealthy(entry.id, result.error, kind);
      // `detail` on the persisted availability was, before this, the
      // already-friendlied sentence (e.g. "That connection didn't work.") —
      // giving every benched model in the roster the exact same wording
      // with nothing behind it (confirmed live: 22 of the user's own
      // models all read identically). `technical` carries the actual raw
      // provider text, redacted a second time here (registry.js's own
      // testModelConnection() already redacts using whatever `secret` it
      // was directly handed, which testAndRecord's saved-entry callers
      // never have — this resolves the real key via secretRef so a saved
      // model's raw error is redacted just as thoroughly as a not-yet-saved
      // one's).
      const secretValue = entry.secretRef ? getSecret(entry.secretRef) : null;
      const technical = redactSecrets(rawDetail, secretValue ? [secretValue] : []);
      updateModel(entry.id, {
        availability: {
          state: AVAILABILITY_STATE_FOR_KIND[kind] || 'unreachable',
          checkedAt: new Date().toISOString(),
          detail: result.error,
          technical,
        },
      });
    }
  } catch (err) {
    // Availability bookkeeping must never turn a working test result into a
    // failed request — log and move on.
    console.error(`[models] failed to record availability for "${entry.id}":`, err);
  }
  return result;
}

app.post('/api/models/:id/test', async (req, res) => {
  const entry = listModels().find((e) => e.id === req.params.id);
  if (!entry) return res.status(404).json({ ok: false, error: 'Unknown model.' });
  const result = await testAndRecord(entry);
  res.json(result);
});

// A real request budget most connections here run on (both of the user's
// live OpenRouter keys are free-tier, 50 requests/day) — the old unbounded
// `Promise.all` fired every enabled model's test SIMULTANEOUSLY, confirmed
// live to have mass-banned a real roster (all 10 enabled Gemini models
// stamped 'unreachable' inside one 150ms window, 7 of them actually working
// the moment each was retried individually) and to burn roughly half a
// day's free OpenRouter quota in one click. Capped at 3 in flight — the
// screen still refreshes in a reasonable time, but no longer as one
// simultaneous burst.
const RECHECK_CONCURRENCY = 3;

/** Runs `worker` over `items`, at most `limit` in flight at once — a simple pull-based pool, not a library, since this is the only place in the app that needed one. */
async function runPooled(items, worker, limit) {
  let next = 0;
  async function lane() {
    while (next < items.length) {
      const i = next++;
      await worker(items[i], i).catch(() => null);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
}

// Shows the real cost of "Check all models" BEFORE it runs — see
// runPooled()'s own comment for why this exists at all. Read-only: makes no
// model calls, only asks each connection's provider (where one is known to
// support it) how much free quota is left.
app.get('/api/models/recheck/preview', async (req, res) => {
  try {
    const enabled = listModels().filter((e) => e.enabled);
    const notWorking = enabled.filter((e) => e.availability?.state !== 'working');
    const connections = listConnections();
    const byConnection = (
      await Promise.all(
        connections.map(async (c) => {
          const count = enabled.filter((e) => e.connectionId === c.id).length;
          if (!count) return null;
          const status = await quotaStatusFor(c);
          return {
            id: c.id,
            label: c.label,
            count,
            isFreeTier: status?.isFreeTier ?? null,
            remaining: status?.remaining ?? null,
          };
        })
      )
    ).filter(Boolean);
    res.json({ total: enabled.length, notWorking: notWorking.length, byConnection });
  } catch (err) {
    console.error('[models] recheck preview failed:', err);
    res.status(500).json({ ok: false, error: friendlyMessage(err, 'Could not estimate the cost right now.') });
  }
});

// Probes models to refresh availability badges — powers the models screen's
// "Check all models" dialog, so badges can be refreshed on demand instead of
// only updating passively as real conversation turns happen to hit them.
// `scope: 'all'` checks every enabled model (the full cost shown by the
// preview route above); anything else (including no body at all) checks
// only the ones NOT currently marked 'working' — the cheap, default choice,
// since a model already confirmed working needs no re-check to prove it.
app.post('/api/models/recheck', async (req, res) => {
  try {
    const scope = req.body?.scope === 'all' ? 'all' : 'not_working';
    const enabled = listModels().filter((e) => e.enabled);
    const entries = scope === 'all' ? enabled : enabled.filter((e) => e.availability?.state !== 'working');
    await runPooled(entries, (entry) => testAndRecord(entry), RECHECK_CONCURRENCY);
    res.json({ ok: true, models: listModels().map(publicModel) });
  } catch (err) {
    console.error('[models] recheck failed:', err);
    res.status(500).json({ ok: false, error: friendlyMessage(err, 'Could not check models right now.') });
  }
});

app.delete('/api/models/:id', (req, res) => {
  deleteModel(req.params.id);
  res.json({ ok: true });
});

// ---------- preferences (auto-select, balance, manual pin, clarify sensitivity) ----------

app.get('/api/prefs', (req, res) => {
  res.json(getPrefs());
});

app.post('/api/prefs', (req, res) => {
  res.json(setPrefs(req.body || {}));
});

// ---------- external service API keys (Deepgram, a TTS provider, ...) ----------
//
// A generic, user-named alternative to hand-editing .env for the standalone
// service keys the voice layer needs — see external-services.js for the
// storage design (any name the user types, one key per service, an optional
// second field). Still NOT a general-purpose secret store for arbitrary
// refs: models/registry.js's own connection secrets (secretRef values like
// 'conn_...') are managed entirely through /api/models's own routes, never
// through here, so a bug or a stray client call here can't silently corrupt
// an existing model connection's key.
//
// One live "test this key" function per STABLE ref, same {ok, error} shape
// as every model adapter's testConnection(entry) — see stt/deepgram.js's
// testKey() for the pattern. This map only covers refs this app itself
// creates deterministically (Deepgram, via a one-time migration) — a
// generic TTS provider's ref is whatever the USER typed/slugified, which
// this map can't know in advance, so those are resolved dynamically via
// tts/index.js's own testerFor() (matchesRef() dispatch) at the route
// below instead. Extend this map only for a future STABLE-ref service;
// extend tts/index.js's ADAPTERS for a future TTS provider.
const EXTERNAL_SERVICE_TESTERS = {
  deepgram: (key) => stt.testKey(key),
};

app.get('/api/external-services', (req, res) => {
  res.json({ services: externalServices.listServices() });
});

app.post('/api/external-services', (req, res) => {
  const { label, key, extraFieldLabel, extraFieldValue } = req.body || {};
  try {
    // allowUpdate: false — this route is specifically "add something NEW";
    // a collision here (even an exact-name one) must be a clear error, not
    // a silent overwrite of an already-connected service's key through the
    // wrong form. See addOrUpdateService()'s own doc comment.
    res.json({
      ok: true,
      service: externalServices.addOrUpdateService({ label, key, extraFieldLabel, extraFieldValue, allowUpdate: false }),
    });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not add that service.' });
  }
});

app.post('/api/external-services/:ref', (req, res) => {
  const existing = externalServices.getService(req.params.ref);
  if (!existing) return res.status(404).json({ error: 'Unknown service.' });
  const { key, extraFieldLabel, extraFieldValue } = req.body || {};
  try {
    res.json({
      ok: true,
      service: externalServices.addOrUpdateService({ label: existing.label, key, extraFieldLabel, extraFieldValue }),
    });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that service.' });
  }
});

// Clears the key(s) — the row stays, reverting to "not connected" under the same name.
app.delete('/api/external-services/:ref', (req, res) => {
  if (!externalServices.getService(req.params.ref)) return res.status(404).json({ error: 'Unknown service.' });
  externalServices.removeServiceKey(req.params.ref);
  res.json({ ok: true });
});

// Deletes the row entirely — name and all, not just its key.
app.delete('/api/external-services/:ref/full', (req, res) => {
  if (!externalServices.getService(req.params.ref)) return res.status(404).json({ error: 'Unknown service.' });
  externalServices.deleteService(req.params.ref);
  res.json({ ok: true });
});

// Tests a key LIVE against the real service — the value in the request body
// if given (so the UI can test what's in the input box before ever saving
// it, same as a model connection's "Test and add"), otherwise whatever is
// already saved (so "test this key" also works as a plain "is this still
// valid" check later, e.g. after a provider-side rotation).
app.post('/api/external-services/:ref/test', async (req, res) => {
  const service = externalServices.getService(req.params.ref);
  if (!service) return res.status(404).json({ error: 'Unknown service.' });
  // Static map first (Deepgram — a stable ref created by this app's own
  // migration, never user-typed), then tts/index.js's own generic
  // matchesRef() dispatch for TTS providers, whose ref IS whatever the
  // user happened to type/slugify — see tts/elevenlabs.js's header comment
  // for why an exact-string map entry isn't reliable for those.
  const tester = EXTERNAL_SERVICE_TESTERS[req.params.ref] || tts.testerFor(req.params.ref);
  if (!tester) return res.status(501).json({ ok: false, error: 'No live test is available for this service yet.' });
  const value = req.body?.value ? String(req.body.value).trim() : externalServices.getKey(req.params.ref);
  if (!value) return res.status(400).json({ ok: false, error: 'No key to test — paste one or save one first.' });
  try {
    res.json(await tester(value));
  } catch (err) {
    res.status(500).json({ ok: false, error: err?.message || 'The test itself failed unexpectedly.' });
  }
});

// ---------- scheduled tasks ----------

app.get('/api/tasks', (req, res) => {
  res.json({ tasks: listTasks() });
});

app.post('/api/tasks', (req, res) => {
  const body = req.body || {};
  // Validated here, not in task-store.js — that file is a plain leaf store
  // (see its header comment) and hasCapability() lives behind
  // capabilities.js, which task-store.js must never import (the same
  // circular-import risk documented on briefing-config.js: a tool ->
  // task-store.js is a real, safe edge today; task-store.js -> tools/index.js
  // or capabilities.js would not be).
  if (body.action?.type === 'skill' && !hasCapability(body.action.skillName)) {
    return res.status(400).json({ ok: false, error: `Unknown skill: ${body.action?.skillName}` });
  }
  try {
    const task = createTask(body);
    res.json({ ok: true, task });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not create that task.' });
  }
});

app.patch('/api/tasks/:id', (req, res) => {
  const body = req.body || {};
  if (body.action?.type === 'skill' && !hasCapability(body.action.skillName)) {
    return res.status(400).json({ ok: false, error: `Unknown skill: ${body.action?.skillName}` });
  }
  try {
    const task = updateTask(req.params.id, body);
    res.json({ ok: true, task });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that task.' });
  }
});

app.delete('/api/tasks/:id', (req, res) => {
  deleteTask(req.params.id);
  res.json({ ok: true });
});

app.post('/api/tasks/:id/run', async (req, res) => {
  try {
    const result = await runTaskNow(req.params.id);
    res.json({ ok: true, result });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not run that task.' });
  }
});

app.get('/api/task-runs', (req, res) => {
  res.json({ runs: listRuns(req.query.taskId) });
});

// ---------- background Jobs ----------
// Phase 2's own testing surface — Phase 3's work_in_background/check_on_work/
// stop_working_on tools are the real conversational entry point; these
// routes exist so this subsystem is independently curl-able before that
// layer lands, and so a future Jobs screen (Phase 4) has something to call.
// Deliberately a distinct name/route family from the scheduler's own
// /api/tasks above — "task" was already fully taken by scheduled tasks
// before this subsystem existed.

app.get('/api/jobs', (req, res) => {
  const { status } = req.query;
  res.json({ jobs: jobStore.listJobs(status ? { status: String(status).split(',') } : {}) });
});

app.post('/api/jobs', (req, res) => {
  const body = req.body || {};
  if (!body.goal) {
    return res.status(400).json({ ok: false, error: 'A job needs a goal.' });
  }
  try {
    const result = createJobIfCapacity({
      title: body.title,
      goal: body.goal,
      kind: body.kind || 'generic',
      resource: body.resource || null,
      conversationId: body.conversationId || null,
    });
    res.json(result);
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not create that job.' });
  }
});

app.get('/api/jobs/:id', (req, res) => {
  const job = jobStore.getJob(req.params.id);
  if (!job) return res.status(404).json({ ok: false, error: 'Unknown job.' });
  res.json({ job, trace: jobStore.getTrace(job.id), outbox: jobStore.getOutboxForJob(job.id) });
});

app.post('/api/jobs/:id/resume', (req, res) => {
  try {
    resumeOrphan(req.params.id);
    res.json({ ok: true, job: jobStore.getJob(req.params.id) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not resume that job.' });
  }
});

app.post('/api/jobs/:id/restart', (req, res) => {
  try {
    restartOrphan(req.params.id);
    res.json({ ok: true, job: jobStore.getJob(req.params.id) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not restart that job.' });
  }
});

app.post('/api/jobs/:id/discard', (req, res) => {
  try {
    cancelJob(req.params.id);
    res.json({ ok: true, job: jobStore.getJob(req.params.id) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not discard that job.' });
  }
});

app.post('/api/jobs/:id/resume-stuck', (req, res) => {
  try {
    resumeStuckJob(req.params.id, req.body?.guidance || null);
    res.json({ ok: true, job: jobStore.getJob(req.params.id) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not resume that job.' });
  }
});

// ---------- monitoring ("tell me when X happens") ----------
//
// Starting one is always the watch_for skill's job (server/skills/
// watch_for.js — it calls engine.startWatching() itself once it's worked out
// a concrete check from the user's own words), never a route here — there is
// deliberately no "POST /api/monitors" to start one blind. This section only
// covers reading the list and stopping one, plus reacting to a trigger.

app.get('/api/monitors', (req, res) => {
  res.json({ monitors: listMonitors() });
});

app.post('/api/monitors/:id/stop', (req, res) => {
  const monitor = getMonitor(req.params.id);
  if (!monitor) return res.status(404).json({ ok: false, error: 'Unknown monitor.' });
  stopWatching(req.params.id);
  // Same broadcast the voice path (skills/stop_watching.js) sends — any open
  // tab, not just the one whose button was clicked, should see the amber bar
  // clear.
  broadcast({ type: 'monitor_stopped', monitorId: monitor.id, description: monitor.description });
  res.json({ ok: true });
});

// Lets a freshly-loaded/refreshed tab reflect real state immediately (e.g.
// look_at_screen or a screen_looks_like watch was already in progress when
// the page opened) instead of waiting for the next observation_status SSE
// event — same reasoning as /api/control/status and GET /api/monitors.
app.get('/api/observation/status', (req, res) => {
  res.json({ active: isIndicatorActive() });
});

// The one action behind a click on the blue "Jarvis can see your screen"
// badge (desktop badge and in-page dot alike — see control/observation-
// bridge.js). The badge is a single yes/no signal, not a per-watch UI, so
// this stops every currently-active screen_looks_like watch rather than
// asking which one. A click while nothing is actually watching (e.g. mid a
// brief look_at_screen glance) is a harmless no-op — there's nothing to stop
// and nothing here throws for that case.
app.post('/api/observation/stop', (req, res) => {
  const stopped = stopAllVisionWatches();
  // Same broadcast the amber bar's own Stop button sends — a vision watch
  // stopped from the badge should clear that bar too, in any open tab.
  for (const monitor of stopped) {
    broadcast({ type: 'monitor_stopped', monitorId: monitor.id, description: monitor.description });
  }
  res.json({ ok: true, stopped: stopped.length });
});

// Read-only — which sandbox backend is currently active (real WSL isolation,
// or the weaker restricted fallback) and, if it's the fallback, the
// plain-language steps to set up the real thing. Used by a future Sandbox
// settings surface; safe to call any time, changes nothing.
app.get('/api/sandbox/status', async (req, res) => {
  res.json(await sandboxStatus());
});

// Reacts to engine.js's in-process trigger event (separate from the SSE
// broadcast it also sends, which is just for the UI) — this is the ONE place
// a monitor's 'act' follow-up actually runs. Deliberately NOT autoConfirm:
// the user may not be present the moment this fires (that's the whole point
// of asking Jarvis to watch something), so a risky follow-up step must still
// pause and ask rather than silently proceed. It runs on the currently
// active chat session (the same one a live chat turn uses — see
// brain.js's getActiveSessionId(), no longer the fixed string 'main') so
// the result — including a spoken "should I go ahead with X?" if the model
// hit something risky — lands directly in the conversation the user already
// sees, not a side channel.
// runner.js itself never calls broadcast() (confirmed: unlike scheduler.js's
// runOneTurn, which does explicitly broadcast a 'task_run' event after each
// run — same reasoning applies here, so this handler does the same).
monitorEvents.on('triggered', async (monitor) => {
  if (monitor.onTrigger?.mode !== 'act' || !monitor.onTrigger.instruction) return;
  let finalText = '';
  let pausedReason = null;
  try {
    for await (const ev of runTurn(getActiveSessionId(), monitor.onTrigger.instruction, { background: true, source: 'text' })) {
      if (ev.type === 'done') finalText = ev.text;
      else if (ev.type === 'paused') pausedReason = ev.reason;
    }
    broadcast({
      type: 'monitor_action_done',
      monitorId: monitor.id,
      description: monitor.description,
      ok: !pausedReason,
      text: finalText || pausedReason || '',
    });
  } catch (err) {
    broadcast({ type: 'monitor_action_done', monitorId: monitor.id, description: monitor.description, ok: false, text: err?.message || 'The follow-up step failed.' });
  }
});

// ---------- morning briefing ----------

app.get('/api/briefing', (req, res) => {
  res.json(getBriefingConfig());
});

app.post('/api/briefing', (req, res) => {
  res.json(setBriefingConfig(req.body || {}));
});

app.post('/api/briefing/preview', async (req, res) => {
  try {
    const result = await composeBriefing();
    res.json(result);
  } catch (err) {
    res.status(500).json({ ok: false, text: 'Could not put the briefing together right now.', error: err?.message });
  }
});

// ---------- profile / about you ----------

app.get('/api/profile', (req, res) => {
  res.json({ entries: listProfileEntries() });
});

app.post('/api/profile', (req, res) => {
  try {
    const entry = addProfileEntry(req.body?.text);
    res.json({ ok: true, entry });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not save that note.' });
  }
});

app.patch('/api/profile/:id', (req, res) => {
  try {
    const entry = updateProfileEntry(req.params.id, req.body?.text);
    res.json({ ok: true, entry });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not save that note.' });
  }
});

app.get('/api/profile/:id/versions', (req, res) => {
  res.json({ versions: listProfileEntryVersions(req.params.id) });
});

app.delete('/api/profile/:id', (req, res) => {
  deleteProfileEntry(req.params.id);
  res.json({ ok: true });
});

// ---------- memory (server/memory/memory-store.js) ----------
//
// Deliberately thin — every candidate's fate (approve/edit/reject/resolve
// conflict) is a direct, synchronous write with no model call involved, so
// clicking a review-card button costs no quota and feels instant. The
// broader Memory Manager (merge, search, archive/restore, full category
// management) has real functions in memory-store.js but no route here — no
// UI reaches them yet, by design (see root CLAUDE.md's Memory section).

app.get('/api/memories/candidates', (req, res) => {
  res.json({ candidates: listPendingMemoryCandidates() });
});

app.post('/api/memories/candidates/:id/approve', (req, res) => {
  try {
    const memory = approveMemoryCandidate(req.params.id, req.body?.edits || {});
    res.json({ ok: true, memory });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not approve that.' });
  }
});

app.post('/api/memories/candidates/:id/reject', (req, res) => {
  rejectMemoryCandidate(req.params.id);
  res.json({ ok: true });
});

// choice: 'update' (overwrite the conflicting memory), 'keep_both' (approve
// as a separate memory), 'discard' (reject outright) — see
// memory-store.js's resolveConflict() for the full contract.
app.post('/api/memories/candidates/:id/resolve-conflict', (req, res) => {
  try {
    const memory = resolveMemoryConflict(req.params.id, req.body?.choice, req.body?.edits || {});
    res.json({ ok: true, memory });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not resolve that.' });
  }
});

// The Memory screen (public/screens/memory.js) — browse/search/edit/
// archive/delete every saved memory, whether it was approved by hand or
// saved automatically (see server/memory/memory-policy.js's trust dial).
// Registered AFTER the /candidates routes above on purpose: Express matches
// routes in registration order, and a GET /api/memories/:id registered
// first would swallow GET /api/memories/candidates by matching
// `:id = 'candidates'`.
app.get('/api/memories', (req, res) => {
  const { category, query, origin, includeArchived } = req.query;
  res.json({ memories: listMemories({ category, query, origin, includeArchived: includeArchived === 'true' }) });
});

app.get('/api/memories/categories', (req, res) => {
  res.json({ categories: listMemoryCategories() });
});

app.get('/api/memories/:id/versions', (req, res) => {
  res.json({ versions: getMemoryVersionHistory(req.params.id) });
});

app.patch('/api/memories/:id', (req, res) => {
  try {
    // `reason` is optional and only ever sent by the Memory screen's own
    // "Restore this version" button (public/screens/memory.js) — every
    // other caller relies on the default, so a plain edit still reads
    // "Edited on the Memory screen." in the version history.
    const reason = req.body?.reason === 'restore' ? 'Restored an earlier version.' : 'Edited on the Memory screen.';
    const memory = updateMemory(req.params.id, { text: req.body?.text, category: req.body?.category }, reason);
    res.json({ ok: true, memory });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that memory.' });
  }
});

app.post('/api/memories/:id/archive', (req, res) => {
  res.json({ ok: true, memory: archiveMemory(req.params.id) });
});

app.post('/api/memories/:id/restore', (req, res) => {
  res.json({ ok: true, memory: restoreMemory(req.params.id) });
});

app.delete('/api/memories/:id', (req, res) => {
  deleteMemory(req.params.id);
  res.json({ ok: true });
});

// ---------- self-improvement (server/improvement/) ----------
//
// A pending 'rule'/'setting' proposal's approve button goes through
// applyProposal() — the SAME function the auto-apply path calls once
// improvement-policy.js's decide() says 'auto-apply' — so a human clicking
// Approve and Jarvis auto-applying something itself can never produce a
// different-looking result. Every route here is a direct, synchronous
// write with no model call, same "instant, costs no quota" design as the
// memory candidate routes above.

app.get('/api/improvement/status', (req, res) => {
  const prefs = getPrefs();
  res.json({
    enabled: prefs.improvementEnabled,
    trust: prefs.improvementTrust,
    research: prefs.improvementResearch,
    dailyBudgetRemaining: improvementDailyBudgetRemaining(),
    weeklyBudgetRemaining: improvementWeeklyBudgetRemaining(),
    unreviewedOutcomes: listUnreviewedImprovementOutcomes().length,
    pendingProposals: listImprovementProposals({ status: 'pending' }).length,
  });
});

app.get('/api/improvement/proposals', (req, res) => {
  const { status, batchId } = req.query;
  res.json({ proposals: listImprovementProposals({ status: status || undefined, batchId: batchId || undefined }) });
});

app.post('/api/improvement/proposals/:id/approve', (req, res) => {
  try {
    const proposal = getImprovementProposal(req.params.id);
    if (!proposal) return res.status(400).json({ ok: false, error: 'That suggestion no longer exists.' });
    // 'skill'/'code'/'idea'/'conflict' have nothing here for apply.js to
    // apply — approving one of those just marks it acknowledged; the real
    // action (writing an implementation prompt, filing a rule by hand from
    // an idea) happens elsewhere. Only 'rule'/'setting' actually change
    // anything through this route.
    if (proposal.kind === 'rule' || proposal.kind === 'setting') {
      const result = applyImprovementProposal(req.params.id);
      return res.json({ ok: true, applied: result });
    }
    const updated = setImprovementProposalStatus(req.params.id, 'applied');
    res.json({ ok: true, proposal: updated });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not approve that suggestion.' });
  }
});

// Turns an approved 'skill'/'code' idea into a ready-to-paste brief for
// whichever coding assistant the user names (free text — never a fixed
// list, see implementation-prompt.js). This IS the approval action for
// these two kinds — Jarvis never writes the code itself, so generating the
// brief and marking the idea acted-on happen together, one request.
app.post('/api/improvement/proposals/:id/implementation-prompt', async (req, res) => {
  try {
    const result = await generateImplementationPrompt(req.params.id, { target: req.body?.target });
    const proposal = setImprovementProposalStatus(req.params.id, 'applied');
    res.json({ ok: true, ...result, proposal });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not generate a prompt right now.' });
  }
});

app.post('/api/improvement/proposals/:id/reject', (req, res) => {
  try {
    const proposal = setImprovementProposalStatus(req.params.id, 'rejected');
    res.json({ ok: true, proposal });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not reject that suggestion.' });
  }
});

// The "Show rejected" view's own Restore/Delete permanently — same
// archive-style safety net as Rules/Lessons below, applied to a rejected
// suggestion instead of a one-way dismissal.
app.post('/api/improvement/proposals/:id/restore', (req, res) => {
  try {
    const proposal = restoreImprovementProposal(req.params.id);
    res.json({ ok: true, proposal });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not restore that suggestion.' });
  }
});

app.delete('/api/improvement/proposals/:id', (req, res) => {
  deleteImprovementProposal(req.params.id);
  res.json({ ok: true });
});

// includeArchived=true is the "Show archived" toggle's own fetch — an
// archived rule is otherwise hidden from every ordinary list, same as an
// archived memory (see improvement-store.js's listRules()).
app.get('/api/improvement/rules', (req, res) => {
  res.json({ rules: listImprovementRules({ includeArchived: req.query.includeArchived === 'true' }) });
});

app.post('/api/improvement/rules/:id/toggle', (req, res) => {
  try {
    const rule = setImprovementRuleActive(req.params.id, Boolean(req.body?.active));
    res.json({ ok: true, rule });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that rule.' });
  }
});

// Editing a rule's own wording — records a real Change row so the edit
// rides the SAME undo machinery a freshly-applied rule already uses
// (improvement/apply.js's undoChange()), rather than a second, parallel
// history mechanism. before/after both carry the full {text, scope,
// active} shape apply.js already expects for a 'rule' change.
app.patch('/api/improvement/rules/:id', (req, res) => {
  try {
    const before = getImprovementRule(req.params.id);
    if (!before) return res.status(400).json({ ok: false, error: 'That rule no longer exists.' });
    const rule = updateImprovementRuleText(req.params.id, req.body?.text);
    recordImprovementChange({
      kind: 'rule',
      target: rule.id,
      before: { text: before.text, scope: before.scope, active: before.active },
      after: { text: rule.text, scope: rule.scope, active: rule.active },
      reason: 'Edited on the Self-Improvement screen.',
    });
    res.json({ ok: true, rule });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that rule.' });
  }
});

app.post('/api/improvement/rules/:id/archive', (req, res) => {
  res.json({ ok: true, rule: archiveImprovementRule(req.params.id) });
});

app.post('/api/improvement/rules/:id/restore', (req, res) => {
  res.json({ ok: true, rule: restoreImprovementRule(req.params.id) });
});

// Only ever meaningful from the archived view — see apply.js's
// undoChange(), which now answers reason:'target_deleted' honestly for
// any Change row that pointed at a rule removed this way.
app.delete('/api/improvement/rules/:id', (req, res) => {
  deleteImprovementRule(req.params.id);
  res.json({ ok: true });
});

app.get('/api/improvement/lessons', (req, res) => {
  res.json({ lessons: listImprovementLessons({ status: req.query.status || 'active' }) });
});

app.post('/api/improvement/lessons/:id/archive', (req, res) => {
  res.json({ ok: true, lesson: updateImprovementLessonStatus(req.params.id, 'archived') });
});

app.post('/api/improvement/lessons/:id/restore', (req, res) => {
  res.json({ ok: true, lesson: updateImprovementLessonStatus(req.params.id, 'active') });
});

app.delete('/api/improvement/lessons/:id', (req, res) => {
  deleteImprovementLesson(req.params.id);
  res.json({ ok: true });
});

// The screen's detail-view "Evidence (N)" expand — turns a rule/lesson/
// proposal's evidence array (outcome ids) back into real summaries. A
// POST, not a GET-with-query-string, since the id list can be long enough
// to be awkward in a URL and this is a read with a body, not a write.
app.post('/api/improvement/outcomes/lookup', (req, res) => {
  res.json({ outcomes: lookupImprovementOutcomes(req.body?.ids) });
});

app.get('/api/improvement/changes', (req, res) => {
  res.json({ changes: listImprovementChanges({}) });
});

// `force: true` only after the UI has already shown the user a "you
// changed this since — restore anyway?" prompt and they confirmed — see
// improvement/apply.js's undoChange() for the refuse-vs-clobber logic.
// A refusal is a normal 200 response (`ok:false, reason:'changed_since'`),
// not an error status — the caller asked a real question and got a real
// answer, nothing went wrong.
app.post('/api/improvement/changes/:id/undo', (req, res) => {
  try {
    const result = undoImprovementChange(req.params.id, { force: Boolean(req.body?.force) });
    res.json(result);
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not undo that change.' });
  }
});

app.get('/api/improvement/outcomes', (req, res) => {
  res.json({ outcomes: listUnreviewedImprovementOutcomes({ limit: 50 }) });
});

// ---------- attachments ----------
//
// Raw-body upload rather than a multipart form: express.raw() covers it
// without adding a parser dependency (this project deliberately runs on four
// packages). The limit is generous because video is the main heavy case —
// bytes are written straight to data/uploads/ (see uploads.js) and never
// held in memory beyond this handler.
//
// Returns an id, never a path: the id goes back to the browser and comes
// again on the next chat turn, and a filesystem path making that round trip
// would be a gift to anyone who could influence it. uploads.js's getUpload()
// re-validates it either way.
app.post('/api/uploads', (req, res) => {
  try {
    const saved = saveUpload(req.body, req.query?.name);
    res.json({ ok: true, id: saved.id, name: saved.name, size: saved.size });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'That upload failed.' });
  }
});

// ---------- computer-control safety spine ----------
//
// Stage 1 of the app-control build: the always-on-top overlay and the stop
// endpoint. There is no HTTP surface for the safety/blocklist config
// itself — control/safety.js's DEFAULTS are fixed, non-editable "sensible
// hardcoded defaults" (the user's explicit choice — see CLAUDE.md's App
// Control section), read in-process by control/session.js and
// screenshot-store.js. The overlay /show and /hide routes below are the
// manual verification surface for the safety spine (see CLAUDE.md's
// control/ section); the real caller once Stage 2 lands is
// control/session.js, not these routes directly from a screen.

app.get('/api/control/status', (req, res) => {
  // isOverlayActive() also covers the Stage-1 manual overlay/show test
  // route (no real session behind it) — merged with, not overwritten by,
  // getSessionStatus()'s own `active` (only true for a real running
  // session), so either source showing the banner keeps it showing.
  const session = getSessionStatus();
  res.json({
    active: isOverlayActive() || session.active,
    step: currentOverlayStep(),
    awaitingConfirmation: session.awaitingConfirmation || false,
    pendingSummary: session.pendingSummary || null,
  });
});

app.post('/api/control/overlay/show', (req, res) => {
  showOverlay(String(req.body?.step || 'Jarvis is controlling your computer…'));
  res.json({ ok: true });
});

app.post('/api/control/overlay/step', (req, res) => {
  updateStep(String(req.body?.step || ''));
  res.json({ ok: true });
});

app.post('/api/control/overlay/hide', (req, res) => {
  hideOverlay();
  res.json({ ok: true });
});

// The one stop endpoint every path calls — the overlay button, the global
// hotkey, and the in-page Stop button. requestStop() flips a flag
// control/session.js checks between every loop step (and resolves any
// pending risky-action confirmation as "no"), so a genuinely running
// session halts immediately, not just its visual indicator.
app.post('/api/control/stop', (req, res) => {
  requestStop();
  hideOverlay();
  broadcast({ type: 'control_stopped' });
  addNotification({ kind: 'control', level: 'info', title: 'Jarvis stopped controlling your computer.' });
  res.json({ ok: true });
});

// Starts a control session directly, bypassing the control_computer skill's
// own confirm-and-plan gate — used for direct testing/verification, and
// available for a future UI trigger. The normal, user-facing path is asking
// Jarvis in conversation, which goes through the skill (plan shown first,
// approval required) instead.
app.post('/api/control/start', async (req, res) => {
  const goal = String(req.body?.goal || '').trim();
  if (!goal) return res.status(400).json({ ok: false, error: 'No goal given.' });
  try {
    const result = await runControlSession(goal, req.body?.plan || null);
    res.json({ ok: result.status === 'done', ...result });
  } catch (err) {
    res.status(500).json({ ok: false, error: err?.message || 'The control session failed.' });
  }
});

// Answers a pending risky-action confirmation raised mid-session (see
// control/session.js's awaitConfirmation) — the Yes/No chips already wired
// in app.js for skill confirmations reuse this same shape for a control
// session's own in-loop confirmations.
app.post('/api/control/confirm', (req, res) => {
  const result = confirmPendingAction(Boolean(req.body?.approve));
  res.json(result);
});

app.get('/api/control/screenshots', (req, res) => {
  res.json({ screenshots: listScreenshots() });
});

app.get('/api/control/screenshots/:file', (req, res) => {
  const full = screenshotPath(req.params.file);
  if (!full) return res.status(404).json({ ok: false, error: 'Not found.' });
  res.sendFile(full);
});

app.delete('/api/control/screenshots', (req, res) => {
  clearScreenshots();
  res.json({ ok: true });
});

// ---------- App Control connectors: catalog + custom, MCP/API/CLI alike ----------
//
// Official Connectors (the catalog) and Custom Connectors are both `mcp`,
// `api`, or `cli` records managed identically from here on — a catalog entry
// just arrives pre-filled (POST .../catalog/:id/ensure), a Custom Connector
// asks the user which mechanism up front (POST .../custom). Browsing the
// catalog itself never exposes mechanism (see GET .../catalog below).
//
// Every mechanism's real tools, in one uniform shape — `_connector-detail.js`
// renders the exact same list/toggle/badge UI regardless of whether a
// connector is mcp, api, or cli, per the rebuild's "the tools list works
// identically no matter the connector's mechanism" design. `risk` comes from
// connectors/index.js's classifyToolRisk() — the exact same computation that
// decides real runtime confirmation — so the detail page's informational
// "always confirms" badge can never drift out of sync with what actually
// happens when the tool runs.
//
// Deliberately does NOT include `description` in what reaches the browser.
// A real tool's description is documentation written for the model — a
// couple thousand characters of usage guidance and worked JSON examples for
// something like Notion's own tools — not a human-facing label, and the
// connector detail page never renders one (matching Claude Desktop's own
// connector permission screen, per a user-supplied reference: it shows only
// a tool's name and its permission control, no per-tool description). The
// full text still goes into classifyToolRisk() below and, separately and
// unaffected by this, into getToolDeclarations()'s model-facing tool list —
// this function is UI-facing only.
function toolsForConnector(connector) {
  if (connector.type === 'mcp') {
    return (connector.mcpTools || []).map((t) => ({ name: t.name, risk: classifyToolRisk(t.name, t.description) }));
  }
  if (connector.type === 'api') {
    return (connector.config?.operations || []).map((op) => ({ name: op.name, risk: classifyToolRisk(op.name, op.description) }));
  }
  if (connector.type === 'cli') {
    return (connector.config?.commands || []).map((cmd) => ({ name: cmd.name, risk: classifyToolRisk(cmd.name, cmd.description) }));
  }
  return null;
}

// Strips a connector's config down to what the UI should ever see —
// secretRef is an internal storage key, not the secret itself, same
// reasoning as publicModel()'s handling of a model's secretRef above.
function publicConnector(connector) {
  // mcpTools is destructured out and never spread back — `tools` (built
  // above, uniform across all three mechanisms) is what the UI reads now.
  const { config, mcpTools, ...rest } = connector;
  const { secretRef, ...restConfig } = config || {};
  const tools = toolsForConnector(connector);
  // A catalog connector's own iconDataUri field is never written any more
  // (icon-resolver.js's resolveConnectorIcon()/backfillConnectorIcons() are
  // custom-only now — see that file's header) — an already-connected
  // catalog connector (e.g. Notion) still needs to show an icon here, so it
  // reads from the same shared catalog-icon cache the Browse-connectors
  // directory uses, rather than a per-connector value that would just sit
  // there stale/absent forever.
  const iconDataUri = connector.source?.type === 'catalog' ? getCatalogIcon(connector.source.id) : connector.iconDataUri;
  return {
    ...rest,
    ...(tools ? { tools } : {}),
    ...(iconDataUri ? { iconDataUri } : {}),
    config: { ...restConfig, hasSecret: Boolean(secretRef) },
  };
}

app.get('/api/connectors', (req, res) => {
  res.json({ connectors: listConnectors().map(publicConnector) });
});

/** The connector record already backing a given catalog entry, if the user has ever clicked into it before — `null` for a never-touched entry. */
function connectorForCatalogEntry(catalogEntryId) {
  return listConnectors().find((c) => c.source?.type === 'catalog' && c.source.id === catalogEntryId) || null;
}

// The Official Connectors directory — every entry gets one uniform Connect
// button in the UI, no "ready"/"needs setup" label. `connectorId`/`status`
// are only filled in once the user has actually clicked into that entry at
// least once. `connectFlow.kind`/`guide` travel with each entry so the
// detail page can decide up front whether a plain Connect button or the
// guided-setup form is the right thing to show — never attempting (and
// failing) a one-click connect first on an entry already known to need
// manual credentials.
app.get('/api/connectors/catalog', (req, res) => {
  const catalog = listConnectorCatalog().map((entry) => {
    const existing = connectorForCatalogEntry(entry.id);
    return {
      id: entry.id,
      label: entry.label,
      icon: entry.icon,
      description: entry.description,
      connectFlow: { kind: entry.connectFlow.kind, guide: entry.connectFlow.guide || null },
      connectorId: existing?.id || null,
      status: existing?.status?.state || null,
      // From the shared catalog-icon cache (icon-resolver.js), resolved
      // once at startup for every bundled entry regardless of connection
      // status — present whether or not this entry has ever been
      // connected. null only until that first resolve succeeds, in which
      // case the client falls back to its own hand-authored mark
      // (app-control.js's iconForConnector()).
      iconDataUri: getCatalogIcon(entry.id),
    };
  });
  res.json({ catalog });
});

// Ensures the underlying mcp connector record exists for an Official
// Connectors entry — creates it on first click, finds it on any later one.
// Does NOT start OAuth itself: the connector detail page calls this once
// (when the user first opens an entry it's never seen before), then decides
// for itself whether to show a plain Connect button or the guided-setup form
// based on this same entry's `connectFlow.kind`, and only then calls the
// generic `/api/connectors/:id/connect` below.
app.post('/api/connectors/catalog/:catalogId/ensure', (req, res) => {
  const entry = getConnectorCatalogEntry(req.params.catalogId);
  if (!entry) return res.status(404).json({ ok: false, error: "Jarvis doesn't have that connector yet." });
  let connector = connectorForCatalogEntry(entry.id);
  if (!connector) {
    // A Client ID registered once for THIS catalog entry (see
    // catalog-credentials.js) — pre-seeded here so a brand-new connector is
    // already "pre-registered" from its very first Connect click, the same
    // way Claude's own Gmail/Drive connectors never ask because Anthropic
    // registered one client centrally. Merged into connectFlow before
    // saving, not after, so oauth.js's startConnect() needs no awareness
    // this happened — existingFlow.clientId is just already populated.
    const registered = getCatalogClient(entry.id);
    const connectFlow = registered ? { ...entry.connectFlow, clientId: registered.clientId } : entry.connectFlow;
    connector = addConnector({
      type: 'mcp',
      label: entry.label,
      description: entry.description,
      config: { connectFlow },
      source: { type: 'catalog', id: entry.id },
    });
    if (registered) {
      const secret = getCatalogClientSecret(entry.id);
      if (secret) saveSecret(`connclient_${connector.id}`, secret);
    }
  }
  res.json({ ok: true, connectorId: connector.id });
});

// Registers (or replaces) the Client ID/Secret for one catalog entry, shared
// by every connector Jarvis ever creates from it — the one-time equivalent of
// what Anthropic did once, centrally, for Claude's own Gmail/Drive/GitHub/
// Slack connectors. Called by the SAME guided-setup/manual-Client-ID modal
// that already exists (public/screens/_connector-detail.js) — no new screen.
app.post('/api/connectors/catalog/:catalogId/register-client', (req, res) => {
  const entry = getConnectorCatalogEntry(req.params.catalogId);
  if (!entry) return res.status(404).json({ ok: false, error: "Jarvis doesn't have that connector yet." });
  const { clientId, clientSecret } = req.body || {};
  if (!clientId) return res.status(400).json({ ok: false, error: 'A Client ID is required.' });
  if (clientSecret && !clientId) return res.status(400).json({ ok: false, error: 'A Client Secret needs a Client ID to go with it.' });
  saveCatalogClient(entry.id, clientId, clientSecret);
  res.json({ ok: true });
});

app.delete('/api/connectors/catalog/:catalogId/register-client', (req, res) => {
  const entry = getConnectorCatalogEntry(req.params.catalogId);
  if (!entry) return res.status(404).json({ ok: false, error: "Jarvis doesn't have that connector yet." });
  clearCatalogClient(entry.id);
  res.json({ ok: true });
});

// A canonical form of an MCP server URL, for duplicate-detection comparison
// ONLY — never used to rewrite what's actually saved. Scheme and host are
// already lowercased by the WHATWG URL parser itself; this additionally
// strips a trailing slash (so "/mcp" and "/mcp/" match) and drops the query
// string/fragment entirely (so a stray "?src=..." doesn't create a false
// "new" connector for the same real server). Deliberately does NOT
// normalize scheme — http:// and https:// at the same host+path stay
// genuinely different connectors, confirmed with the user, since merging
// them could let a plain-http address silently reuse a real https
// connector's stored token/config. Falls back to the raw trimmed string on
// a URL that fails to parse (matches this route's own existing behavior of
// rejecting an unparseable URL before ever reaching this comparison).
function canonicalUrl(raw) {
  try {
    const u = new URL(raw);
    const pathname = u.pathname.replace(/\/+$/, '') || '/';
    return `${u.protocol}//${u.host}${pathname}`;
  } catch {
    return String(raw || '').trim();
  }
}

// Creates a Custom Connector record — the one place a user picks a
// mechanism directly (MCP server URL / API key / CLI command), since a
// custom connector's mechanism can't be inferred the way an Official
// catalog entry's can. Creation only, no live connect/discovery attempt —
// the detail page the user lands on right after does that (MCP: starts
// OAuth; API: offers spec discovery; CLI: runs --help discovery), exactly
// like an Official entry's own flow.
app.post('/api/connectors/custom', (req, res) => {
  const { type, label, description } = req.body || {};
  if (!label) return res.status(400).json({ ok: false, error: 'A name is required.' });

  if (type === 'mcp') {
    const { url, clientId, clientSecret } = req.body || {};
    if (!url) return res.status(400).json({ ok: false, error: 'A server URL is required.' });
    let parsedUrl;
    try {
      parsedUrl = new URL(url); // throws on garbage input before anything is saved
    } catch {
      return res.status(400).json({ ok: false, error: "That doesn't look like a valid URL." });
    }
    // Only http(s) is a real MCP transport — anything else (file:, ftp:, ...)
    // parses fine but fails much later as an opaque fetch error. Reject at
    // save time instead.
    if (parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') {
      return res.status(400).json({ ok: false, error: 'The server URL must start with http:// or https://.' });
    }
    // A secret without an id has nowhere to attach — silently dropping it
    // (the old behavior) makes the user believe it was saved when it wasn't.
    if (clientSecret && !clientId) {
      return res.status(400).json({ ok: false, error: 'A Client Secret needs a Client ID to go with it.' });
    }

    // Adding the same server twice — the natural response to a connector
    // that looked stuck before Remove existed for an unconfigured one (see
    // CLAUDE.md's reconnection-glitch fix) — reuses the existing record
    // instead of piling up an undeletable duplicate. Scoped to the user's
    // OWN previously-added connectors only: matching against a catalog
    // record here used to silently hand back Notion/Gmail/etc. instead of
    // creating what the user actually asked for (dropping their typed label
    // and description), and could force-rewrite a catalog record verified as
    // needing no auth (`kind: 'none'`) into 'oauth_guided' — breaking a
    // connector that was already working. See CLAUDE.md.
    const canonicalNewUrl = canonicalUrl(url);
    const existing = listConnectors().find(
      (c) => c.type === 'mcp' && c.source?.type === 'user' && canonicalUrl(c.config?.connectFlow?.url) === canonicalNewUrl
    );
    if (existing) {
      const existingKind = existing.config?.connectFlow?.kind;
      if (clientId && !existing.config?.connectFlow?.clientId && existingKind !== 'none') {
        updateConnector(existing.id, { config: { connectFlow: { ...existing.config.connectFlow, kind: 'oauth_guided', clientId } } });
        if (clientSecret) saveSecret(`connclient_${existing.id}`, clientSecret);
      }
      // A real, specific reason this "Add" didn't create anything new — the
      // typed name/description ARE silently discarded below (the existing
      // connector's own label survives), so this is the only place that gets
      // explained anywhere. Reuses the existing toast/bell pipeline
      // (addNotification -> SSE -> public/notifications.js) rather than
      // inventing new UI plumbing for what's already a one-line info notice.
      addNotification({
        kind: 'connector',
        level: 'info',
        title: 'Already connected',
        body: `You already have a connector for this URL, called "${existing.label}" — opening that one instead of creating a new one.`,
        action: { label: 'App Control', section: 'app-control' },
        meta: { connectorId: existing.id },
      });
      return res.json({ ok: true, connectorId: existing.id, label: existing.label, reused: true });
    }

    // Assume oauth_dcr unless the user already supplied a Client ID via
    // Advanced settings — startConnect() tries real Dynamic Client
    // Registration first regardless, and only falls back to asking for
    // manual credentials if the server itself doesn't support it and none
    // were given up front. A custom URL, unlike a catalog entry, is never
    // pre-researched, so which one applies can't be known ahead of time.
    const connectFlow = clientId ? { kind: 'oauth_guided', url, clientId } : { kind: 'oauth_dcr', url };
    const connector = addConnector({ type: 'mcp', label, description, config: { connectFlow }, source: { type: 'user' } });
    if (clientId && clientSecret) saveSecret(`connclient_${connector.id}`, clientSecret);
    return res.json({ ok: true, connectorId: connector.id });
  }

  if (type === 'api') {
    const { baseUrl, authKind, authName, authPrefix, apiKey } = req.body || {};
    if (!baseUrl) return res.status(400).json({ ok: false, error: 'A base web address is required.' });
    try {
      new URL(baseUrl);
    } catch {
      return res.status(400).json({ ok: false, error: "That doesn't look like a valid web address." });
    }
    const connector = addConnector({
      type: 'api',
      label,
      description,
      config: { baseUrl, auth: { kind: authKind || 'bearer', name: authName || undefined, prefix: authPrefix || undefined }, operations: [] },
      source: { type: 'user' },
    });
    // The key is saved server-side under a fresh ref immediately — it never
    // sits in the request body any longer than this one call needs it, and
    // it's never echoed back in any response (publicConnector() strips it).
    if (apiKey) {
      const ref = `conn_${connector.id}`;
      saveSecret(ref, apiKey);
      updateConnector(connector.id, { config: { secretRef: ref } });
    }
    return res.json({ ok: true, connectorId: connector.id });
  }

  if (type === 'cli') {
    const { command, cwd } = req.body || {};
    if (!command) return res.status(400).json({ ok: false, error: 'A command is required.' });
    const connector = addConnector({ type: 'cli', label, description, config: { command, cwd: cwd || undefined, commands: [] }, source: { type: 'user' } });
    return res.json({ ok: true, connectorId: connector.id });
  }

  res.status(400).json({ ok: false, error: 'Choose MCP, API, or CLI for a custom connector.' });
});

// Discovery step for an API connector — parses a real OpenAPI/Swagger
// document into a proposed operations list. Read-only: the user reviews,
// edits, and ticks in the UI, then saves the final set via the generic
// PATCH route below (`{config: {operations}}`) — this route never writes
// anything itself.
app.post('/api/connectors/:id/api/discover', async (req, res) => {
  const connector = getConnector(req.params.id);
  if (!connector || connector.type !== 'api') return res.status(404).json({ ok: false, error: 'Unknown API connector.' });
  const { specUrl } = req.body || {};
  if (!specUrl) return res.status(400).json({ ok: false, error: 'A spec address is required.' });
  try {
    const { baseUrl, operations } = await apiClient.discoverFromSpec(specUrl);
    res.json({ ok: true, baseUrl, operations });
  } catch (err) {
    console.error('[connectors] API spec discovery failed (shown to user as a plain-language message):', err?.message || err);
    res.status(400).json({ ok: false, error: friendlyMessageFor(err, 'That spec', "Couldn't read that document — check the address and that it's a real OpenAPI/Swagger spec.") });
  }
});

// Discovery step for a CLI connector — runs the real `<command> --help` and
// proposes a subcommand list via one model call. Also read-only; the user's
// reviewed/edited/ticked list is saved via the generic PATCH route
// (`{config: {commands}}`), same pattern as the API route above.
app.post('/api/connectors/:id/cli/discover', async (req, res) => {
  const connector = getConnector(req.params.id);
  if (!connector || connector.type !== 'cli') return res.status(404).json({ ok: false, error: 'Unknown CLI connector.' });
  try {
    const { helpText, proposed } = await cliClient.discoverCommands(connector.config.command);
    res.json({ ok: true, helpText, proposed });
  } catch (err) {
    console.error('[connectors] CLI discovery failed (shown to user as a plain-language message):', err?.message || err);
    res.status(400).json({ ok: false, error: friendlyMessageFor(err, 'That command', "Couldn't discover that command's subcommands — check that it's installed and on your PATH.") });
  }
});

// The exact redirect address the server will actually send on every OAuth
// call — the guided-setup form displays THIS instead of computing its own
// from window.location.origin, which can read "localhost" while this always
// says "127.0.0.1", silently breaking a manually-registered app or a
// self-hosted Client ID Metadata Document built from the wrong one (found
// from a real screenshot during the Lovable investigation — see CLAUDE.md).
app.get('/api/connectors/oauth/redirect-uri', (req, res) => {
  res.json({ uri: oauth.redirectUri() });
});

// The Client ID Metadata Document (CIMD, the current MCP spec's primary
// client-registration mechanism — see CLAUDE.md's client-identity.js note)
// an authorization server fetches when Jarvis offers its own address as a
// client_id. Its client_id field must equal the URL it was fetched from —
// when no public address is configured, the local URL fills that role so
// the document is still well-formed and inspectable, even though nothing
// remote can actually reach it (cimdUrl() then returns null and
// oauth.js's obtainClientCredentials() never offers this document to a
// server in the first place; see 2.2/2.3 of the plan this shipped from).
app.get(CLIENT_METADATA_PATH, (req, res) => {
  const id = cimdUrl() || `http://127.0.0.1:${process.env.PORT || 3000}${CLIENT_METADATA_PATH}`;
  res.json(clientMetadataDocument(id));
});

// The one place Jarvis's real, publicly-reachable https address (a tunnel, a
// hosted proxy — never required) is set. Configuring this is what turns CIMD
// on for EVERY MCP connector at once, official or custom alike — no
// per-connector setting exists because the client identity it enables is a
// property of Jarvis itself, not of any one connection. Leaving it unset
// keeps today's behavior exactly: CIMD is never attempted, DCR runs as before.
app.get('/api/connectors/oauth/public-url', (req, res) => {
  res.json({ publicBaseUrl: getPublicBaseUrl(), cimdUrl: cimdUrl() });
});
app.post('/api/connectors/oauth/public-url', (req, res) => {
  const { publicBaseUrl } = req.body || {};
  if (publicBaseUrl) {
    try {
      if (new URL(publicBaseUrl).protocol !== 'https:') throw new Error('not https');
    } catch {
      return res.status(400).json({ ok: false, error: 'That needs to be an https:// web address.' });
    }
  }
  setPublicBaseUrl(publicBaseUrl || null);
  res.json({ ok: true, cimdUrl: cimdUrl() });
});

// The one place any mcp connector's OAuth flow actually starts — used by the
// detail page for an Official entry (after /ensure above), a fresh Custom
// Connector, and a Reconnect on one that's fallen into an error state alike.
// `clientId`/`clientSecret` (optional) are the guided-setup form's fields;
// omit them to attempt Dynamic Client Registration first.
app.post('/api/connectors/:id/connect', async (req, res) => {
  const connector = getConnector(req.params.id);
  if (!connector || connector.type !== 'mcp') return res.status(404).json({ ok: false, error: 'Unknown connector.' });
  const serverUrl = connector.config?.connectFlow?.url;
  if (!serverUrl) return res.status(400).json({ ok: false, error: 'This connector has no server address to connect to.' });

  const { clientId, clientSecret } = req.body || {};
  // A secret typed with no id has nowhere to attach — both the guided-setup
  // and manual-Client-ID modals send {clientId, clientSecret} unconditionally
  // (empty string when a field is blank), so oauth.js's own
  // `manualClientId ? manualClientSecret : storedClientSecret` used to accept
  // this silently and just throw the secret away. Catch it here instead of
  // letting a typed value vanish with no explanation.
  if (clientSecret && !clientId) {
    return res.status(400).json({ ok: false, error: 'A Client Secret needs a Client ID to go with it.' });
  }
  try {
    const outcome = await oauth.startConnect(connector.id, serverUrl, { manualClientId: clientId, manualClientSecret: clientSecret });
    res.json({ ok: true, connectorId: connector.id, ...outcome });
  } catch (err) {
    const message = err?.message || 'Could not start connecting that service.';
    console.error('[connectors] connect failed:', message);
    // A rejected/network DCR failure (see oauth.js — 'unsupported' never
    // throws) is a real, specific reason this attempt didn't work, worth
    // recording the same way the OAuth callback route already does for its
    // own failures — otherwise a page reload after a failed Connect click
    // would show no sign anything had ever gone wrong.
    try {
      recordStatus(connector.id, 'error', message);
    } catch {
      // best-effort — the raw message is still logged above either way
    }
    // NOT run through friendlyMessageFor() — that helper exists to clean up
    // raw provider/SDK error text (JSON blobs, stack internals), but
    // oauth.js's thrown messages are already hand-written, safe,
    // plain-language sentences (including a real provider's own explanation,
    // e.g. Lovable's "restricted to approved partners... or use
    // client_id_metadata_document instead"). Running an already-clean
    // sentence through a 4-category classifier just throws it away whenever
    // it isn't quota/auth/no_access/network — which was happening on every
    // real rejection — and produced a real, confusing bug: this response
    // showed a generic "Could not start connecting" while the exact same
    // message, saved untouched just above, showed correctly on the next
    // page load. Same error, two different displays. Send the real message
    // both places.
    res.status(400).json({ ok: false, error: message });
  }
});

// The redirect target every OAuth provider sends the browser back to — a
// real top-level navigation (the tab the authorize URL opened), so this
// returns a small self-contained HTML page rather than JSON, matching what
// the user is actually doing (finishing a sign-in), not calling an API.
// Broadcasts over SSE so the App Control screen, if it's open, refreshes on
// its own without the user needing to click back into it.
app.get('/api/connectors/oauth/callback', async (req, res) => {
  const outcome = await oauth.handleCallback(req.query || {});
  // A refused/failed sign-in used to leave the connector at 'untested'
  // forever — indistinguishable from never having tried — which stranded
  // the detail page's "Waiting…" button with nothing to react to. Recording
  // the real status here is what lets pollUntilConnected's onError branch
  // fire and the button recover to "Try again".
  if (!outcome.ok && outcome.connectorId) {
    try {
      recordStatus(outcome.connectorId, 'error', outcome.error);
    } catch {
      // Best-effort — the plain HTML page below still tells the user what happened.
    }
  }
  broadcast({ type: 'connector_status', connectorId: outcome.connectorId, ok: outcome.ok, error: outcome.error });
  addNotification({
    kind: 'connector',
    level: outcome.ok ? 'success' : 'error',
    title: outcome.ok ? 'Connector connected.' : 'Connector could not connect.',
    body: outcome.ok ? '' : outcome.error || 'Unknown error.',
    action: { label: 'App Control', section: 'app-control' },
    // Lets the detail view (public/screens/_notification-detail.js) look
    // up and show which connector this actually was, not just a generic
    // "a connector" message.
    meta: outcome.connectorId ? { connectorId: outcome.connectorId } : null,
  });
  const message = outcome.ok ? 'Connected.' : `Could not connect: ${outcome.error}`;
  res.set('Content-Type', 'text/html').send(
    `<!doctype html><html><body style="font:16px system-ui;padding:2em;text-align:center">` +
      `<p>${message}</p><p>You can close this tab.</p>` +
      `<script>setTimeout(function(){ window.close(); }, 1200);</script>` +
      `</body></html>`
  );
});

// Signs a connected mcp connector out without deleting it — the record and
// its tool permissions survive, only the stored token (and the cached MCP
// session id it was talking through) are cleared. Distinct from DELETE
// below (Remove) on purpose: reconnecting after this needs no re-setup,
// where Remove means starting over from scratch. See CLAUDE.md's
// reconnection-glitch fix — this used to not exist, and the button labelled
// "Disconnect" on the detail page actually called DELETE.
app.post('/api/connectors/:id/disconnect', (req, res) => {
  const connector = getConnector(req.params.id);
  if (!connector || connector.type !== 'mcp') return res.status(404).json({ ok: false, error: 'Unknown connector.' });
  oauth.disconnect(connector.id); // deletes the stored token set
  mcpRemoteClient.forgetSession(connector.id); // next connect re-initializes fresh rather than reusing a now-invalid session id
  const updated = updateConnector(connector.id, {
    config: { secretRef: undefined }, // clear the now-dangling pointer rather than leaving it referencing a deleted secret
    status: { state: 'untested', checkedAt: new Date().toISOString(), detail: null },
  });
  res.json({ ok: true, connector: publicConnector(updated) });
});

app.patch('/api/connectors/:id', (req, res) => {
  try {
    const connector = updateConnector(req.params.id, req.body || {});
    res.json({ ok: true, connector: publicConnector(connector) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not update that connector.' });
  }
});

/** Best-effort teardown of whatever a connector's mechanism was actually holding onto — a live stdio process, an OAuth token, a saved API key. Never throws; a connector record disappearing must never be blocked by cleanup of what it pointed at. */
function releaseConnectorResources(connector) {
  if (!connector) return;
  if (connector.type === 'mcp') {
    if (connector.config?.connectFlow?.kind === 'stdio') mcpClient.stopClient(connector.id);
    oauth.disconnect(connector.id); // no-op if there's no OAuth token set to remove
    mcpRemoteClient.forgetSession(connector.id);
    deleteSecret(`connclient_${connector.id}`); // no-op if no manual/DCR client credentials were ever saved for it
  } else if (connector.type === 'api' && connector.config?.secretRef) {
    deleteSecret(connector.config.secretRef);
  }
}

// Removes a connector entirely — for an Official Connector this also means
// its catalog card shows "Connect" again next time (identity lives in
// source.id, not the deleted record), so this serves as both "Disconnect"
// and "Remove" in the UI.
app.delete('/api/connectors/:id', (req, res) => {
  releaseConnectorResources(getConnector(req.params.id));
  deleteConnector(req.params.id);
  res.json({ ok: true });
});

/** Per-mechanism "is this actually usable right now" check — throws a plain-language error when it isn't. mcp also refreshes the real tool list as a side effect (the one mechanism where that's a live, re-fetchable thing); api/cli's operations/commands are a saved, user-reviewed list rather than something fetched fresh each time, so testing them just confirms there's something there to use. */
async function verifyConnectorIsUsable(connector) {
  if (connector.type === 'files') {
    if (!connector.config?.allowedFolders?.length) throw new Error('Add at least one folder first.');
    filesConnector.listFiles(connector.config.allowedFolders[0], connector.config.allowedFolders);
    return;
  }
  if (connector.type === 'browser') {
    await browserConnector.ensureBrowser();
    return;
  }
  if (connector.type === 'mcp') {
    const flow = connector.config?.connectFlow;
    const tools = flow?.kind === 'stdio' ? await mcpClient.listTools(connector.id, flow) : await mcpRemoteClient.listTools(connector.id, flow?.url);
    updateConnector(connector.id, { mcpTools: tools });
    return;
  }
  if (connector.type === 'api') {
    if (!connector.config?.operations?.length) throw new Error('Add at least one endpoint first (discover from a spec, or add one by hand).');
    return;
  }
  if (connector.type === 'cli') {
    if (!connector.config?.commands?.length) throw new Error('Discover and save at least one command first.');
  }
}

// Tests a connector for real and records the outcome — the "live status
// dot" the App Control screen shows next to each one.
app.post('/api/connectors/:id/test', async (req, res) => {
  const connector = getConnector(req.params.id);
  if (!connector) return res.status(404).json({ ok: false, error: 'Unknown connector.' });

  try {
    await verifyConnectorIsUsable(connector);
    let updated = recordStatus(connector.id, 'working', null);
    // A CUSTOM connector's icon is resolved right here — the first moment
    // it's confirmed actually working, and for mcp the exact place
    // listTools() (inside verifyConnectorIsUsable() above) has already
    // populated mcp-remote-client.js's serverInfo cache for this session,
    // so icon-resolver.js's mcp-declared-icon check has something real to
    // read. A catalog connector's icon is deliberately NOT resolved here —
    // it never depends on a live connection at all (icon-resolver.js's
    // resolveCatalogIcons(), run once at startup for every catalog entry
    // regardless of connection status, is what covers that; see
    // publicConnector() below for how an already-connected one still gets
    // it). Guarded on staleness, not just absence, so a real logo change
    // eventually gets picked up automatically rather than only ever being
    // fetched once; never throws (icon-resolver.js is entirely best-
    // effort), so a slow/failed icon lookup can't turn an otherwise-
    // successful test into a failure.
    if (
      updated.source?.type !== 'catalog' &&
      (updated.type === 'mcp' || updated.type === 'api') &&
      (!updated.iconDataUri || isIconStale(updated.iconFetchedAt))
    ) {
      const icon = await resolveConnectorIcon(updated);
      if (icon) updated = updateConnector(connector.id, { iconDataUri: icon, iconFetchedAt: new Date().toISOString() });
    }
    res.json({ ok: true, connector: publicConnector(updated) });
  } catch (err) {
    // recordStatus() keeps the raw detail (internal state only, never
    // rendered directly — see friendly-message.js's header comment on the
    // distinction) while the response's own `error` is the clean sentence
    // _connector-detail.js actually displays.
    console.error(`[connectors] "${connector.label || connector.id}" test failed (shown to user as a plain-language message):`, err?.message || err);
    const updated = recordStatus(connector.id, 'error', err?.message || 'Test failed.');
    res.json({ ok: false, error: friendlyMessageFor(err, 'That connector', "That connector couldn't be verified."), connector: publicConnector(updated) });
  }
});

// ---------- notifications ----------
// The replacement for the old in-layout error banner (see CLAUDE.md/
// handoff.md) — persisted here (data/notifications.json via
// notifications.js) and pushed live over the SSE channel below, so a
// scheduled task that fails overnight is still waiting to be seen next time
// Jarvis is opened, not just flashed once to a tab that wasn't open.
// Fixed paths registered before the /:id route, matching the ordering
// discipline already used for '/api/skills/catalog' vs '/api/skills/:name'
// above (Express matches route registration order, not specificity).

app.get('/api/notifications', (req, res) => {
  res.json({ notifications: listNotifications() });
});

// Client-originated — for a purely browser-side event (e.g. "can't reach the
// Jarvis server") that has no server-side call site to add a notification
// from directly.
app.post('/api/notifications', (req, res) => {
  try {
    const notification = addNotification(req.body || {});
    res.json({ ok: true, notification });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Invalid notification.' });
  }
});

// Each of these broadcasts a lightweight 'notifications_changed' ping (no
// payload — just "something changed, resync") so the header bell's badge
// stays correct even when the change came from the full history screen
// (public/screens/notifications.js), which talks to these routes directly
// rather than through public/notifications.js's own local state. Also
// keeps a second open tab in sync, same reasoning as addNotification()'s
// own broadcast.
app.post('/api/notifications/read', (req, res) => {
  const notifications = markRead(req.body?.ids || []);
  broadcast({ type: 'notifications_changed' });
  res.json({ notifications });
});

app.post('/api/notifications/read-all', (req, res) => {
  const notifications = markAllRead();
  broadcast({ type: 'notifications_changed' });
  res.json({ notifications });
});

app.delete('/api/notifications', (req, res) => {
  clearAllNotifications();
  broadcast({ type: 'notifications_changed' });
  res.json({ ok: true });
});

app.delete('/api/notifications/:id', (req, res) => {
  removeNotification(req.params.id);
  broadcast({ type: 'notifications_changed' });
  res.json({ ok: true });
});

// ---------- server -> browser push ----------

app.get('/api/events', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  });
  res.write(': connected\n\n');
  addClient(res);
  req.on('close', () => removeClient(res));
});

// ---------- chat ----------

app.post('/api/chat', async (req, res) => {
  const message = String(req.body?.message || '').trim();
  const attachmentIds = Array.isArray(req.body?.attachments) ? req.body.attachments : [];
  if (!message && !attachmentIds.length) {
    return res.status(400).json({ error: 'No message provided.' });
  }

  const source = req.body?.source === 'voice' ? 'voice' : 'text';
  const lowConfidence = isLowConfidence(req.body?.confidence, source);

  try {
    let text = message;
    let media;
    let need;
    if (attachmentIds.length) {
      const prepared = await prepareForTurn(attachmentIds, { sessionId: getActiveSessionId() });
      media = prepared.media.length ? prepared.media : undefined;
      need = Object.keys(prepared.need).length ? prepared.need : undefined;
      text = composeMessage(message, prepared) || "I've attached this.";
    }
    const reply = await chat(text, { source, lowConfidence, media, need });
    res.json({ reply });
  } catch (err) {
    if (err?.code === 'NO_API_KEY') {
      return res.status(400).json({ error: 'No model is set up yet.', code: 'NO_API_KEY' });
    }
    if (err?.code === 'PAUSED') {
      return res.status(503).json({ error: err.message, code: 'PAUSED' });
    }
    console.error('[chat] error:', err);
    res.status(500).json({ error: 'I ran into a problem talking to the AI model. Check your internet connection and try again.' });
  }
});

// Streaming version of /api/chat, for the real-time voice/text UI. Uses
// Server-Sent Events (a plain GET + long-lived response) rather than
// WebSockets — it's one-directional (server -> browser), which is all a
// text reply needs, and the browser's built-in EventSource handles
// reconnects for free. GET + query string is required here because
// EventSource can only make GET requests with no custom body.
app.get('/api/chat/stream', async (req, res) => {
  const message = String(req.query.message || '').trim();
  // Attachment ids ride in the query string because EventSource can only make
  // a GET with no body. Ids, never paths — see the /api/uploads note above.
  const attachmentIds = String(req.query.attachments || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  // Sending a photo with no words is a perfectly ordinary thing to do, so an
  // empty message is only an error when there's nothing attached either.
  if (!message && !attachmentIds.length) {
    res.status(400).json({ error: 'No message provided.' });
    return;
  }

  const source = req.query.source === 'voice' ? 'voice' : 'text';
  const confidence = req.query.confidence !== undefined ? Number(req.query.confidence) : undefined;
  const lowConfidence = isLowConfidence(confidence, source);

  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  });

  const send = (event) => res.write(`data: ${JSON.stringify(event)}\n\n`);

  // The client closes its EventSource before opening a new one for its next
  // message (see public/engines/pipeline-engine.js's _send()), and
  // brain.js's chatStream() coordinator already handles that case on its
  // own (a NEW call aborts whatever's running). This covers the other way
  // a connection can go away — the tab closing, a network drop, or an
  // EventSource simply being abandoned with no follow-up message — so the
  // turn stops using API quota and streaming work for a reply nobody is
  // left to receive, rather than running to completion regardless.
  req.on('close', () => {
    try {
      abortActiveTurn(getActiveSessionId());
    } catch (err) {
      console.error('[chat/stream] abort-on-close failed:', err);
    }
  });

  try {
    let text = message;
    let media;
    let need;
    if (attachmentIds.length) {
      const prepared = await prepareForTurn(attachmentIds, { sessionId: getActiveSessionId() });
      media = prepared.media.length ? prepared.media : undefined;
      need = Object.keys(prepared.need).length ? prepared.need : undefined;
      text = composeMessage(message, prepared) || 'I\'ve attached this.';
    }

    for await (const event of chatStream(text, { source, lowConfidence, media, need })) {
      send(event);
    }
  } catch (err) {
    if (err?.code === 'NO_API_KEY') {
      send({ type: 'error', error: 'No model is set up yet.', code: 'NO_API_KEY' });
    } else {
      console.error('[chat/stream] error:', err);
      send({ type: 'error', error: 'I ran into a problem talking to the AI model. Check your internet connection and try again.' });
    }
  } finally {
    res.end();
  }
});

// Reports a barge-in: the duplex voice engine cut Jarvis off mid-reply, and
// this is what was actually spoken before that happened (see
// public/voice/playback.js's getSpokenText()). Retroactively marks the most
// recent assistant message so both chat history and the NEXT turn's model
// context reflect what was truly heard, not the full reply the model
// finished generating server-side regardless — see conversation.js's
// markLastAssistantInterrupted() for the full reasoning and its documented
// race-condition gap. Always 204 (nothing useful to report either way) — a
// no-op here (nothing to mark yet) should never surface as a client error,
// since it always fires right after a barge-in the user already caused
// deliberately.
app.post('/api/chat/interrupt', (req, res) => {
  const spokenText = String(req.body?.spokenText || '');
  try {
    markLastAssistantInterrupted(getActiveSessionId(), spokenText);
  } catch (err) {
    console.error('[chat/interrupt] error:', err);
  }
  res.status(204).end();
});

// ---------- text-to-speech ----------

// Every server-side TTS provider (server/tts/index.js) — for a settings
// picker. Browser speech (client-only, no server component) is never
// listed here — see that file's header comment.
app.get('/api/tts/providers', (req, res) => {
  res.json({ providers: tts.listProviders() });
});

app.post('/api/tts', async (req, res) => {
  const text = String(req.body?.text || '').trim();
  const voice = req.body?.voice;
  const provider = req.body?.provider || undefined;
  if (!text) {
    return res.status(400).json({ error: 'No text provided.' });
  }

  try {
    // Every provider's stream() is an async generator (even a non-streaming
    // one, which yields exactly one chunk — see tts/index.js's header
    // comment) — concatenated here into one response, same simple one-shot
    // contract both audio-player.js and voice/playback.js already rely on.
    // A provider whose API supports real HTTP-level streaming is a future
    // enhancement to this route, not a change to the seam itself.
    const buffers = [];
    let mimeType = 'audio/wav';
    for await (const chunk of tts.stream(text, { voice, provider })) {
      buffers.push(chunk.buffer);
      mimeType = chunk.mimeType || mimeType;
    }
    if (!buffers.length) throw new Error('The TTS provider returned no audio.');
    res.set('Content-Type', mimeType);
    res.send(Buffer.concat(buffers));
  } catch (err) {
    if (err?.code === 'NO_API_KEY') {
      return res.status(400).json({ error: 'No API key is set up for that voice yet.', code: 'NO_API_KEY' });
    }
    console.error('[tts] error:', err);
    res.status(500).json({ error: 'Could not generate speech audio right now.' });
  }
});

// Whether a real-time server-proxied STT provider (Deepgram) is available —
// the duplex engine also learns this from its WebSocket's own 'ready'
// message, but a plain REST check is useful for a settings screen that
// shouldn't have to open a socket just to show a status dot.
app.get('/api/stt/status', (req, res) => {
  res.json({ configured: stt.isConfigured() });
});

// Optional layer-3 turn detection (off by default in the UI) — see
// public/turn-detector.js for why this exists and when it's used.
app.post('/api/turn-check', async (req, res) => {
  const text = String(req.body?.text || '').trim();
  if (!text) return res.json({ complete: true });

  try {
    const complete = await classifyTurnComplete(text);
    res.json({ complete });
  } catch (err) {
    console.error('[turn-check] error:', err);
    res.json({ complete: true }); // fail open
  }
});

// ---------- chat history (server/chat-store.js) ----------

app.get('/api/conversations', (req, res) => {
  const query = typeof req.query.q === 'string' ? req.query.q : '';
  const includeArchived = req.query.archived === '1';
  // getActiveSessionId() must run BEFORE listConversations() — on a fresh
  // install it lazily creates the very first conversation, and object-
  // literal property evaluation order would otherwise call listConversations()
  // first and miss it (found live: an empty list with a real activeId).
  const activeId = getActiveSessionId();
  res.json({ conversations: chatStore.listConversations({ query, includeArchived }), activeId });
});

app.post('/api/conversations', (req, res) => {
  const conv = resetConversation();
  res.json({ conversation: conv });
});

app.get('/api/conversations/:id', (req, res) => {
  if (!chatStore.isConversation(req.params.id)) {
    return res.status(404).json({ error: 'That conversation no longer exists.' });
  }
  const conversation = chatStore.getConversation(req.params.id);
  const messages = chatStore.getMessages(req.params.id);
  res.json({ conversation, messages });
});

app.patch('/api/conversations/:id', (req, res) => {
  if (!chatStore.isConversation(req.params.id)) {
    return res.status(404).json({ error: 'That conversation no longer exists.' });
  }
  try {
    let conversation;
    if (req.body?.title !== undefined) conversation = chatStore.renameConversation(req.params.id, req.body.title);
    if (req.body?.pinned !== undefined) conversation = chatStore.setPinned(req.params.id, Boolean(req.body.pinned));
    if (req.body?.archived !== undefined) conversation = chatStore.setArchived(req.params.id, Boolean(req.body.archived));
    res.json({ conversation: conversation || chatStore.getConversation(req.params.id) });
  } catch (err) {
    res.status(400).json({ error: err?.message || 'Could not update that conversation.' });
  }
});

app.delete('/api/conversations/:id', (req, res) => {
  if (!chatStore.isConversation(req.params.id)) {
    return res.status(404).json({ error: 'That conversation no longer exists.' });
  }
  const wasActive = req.params.id === getActiveSessionId();
  chatStore.deleteConversation(req.params.id);
  // Deleting the conversation you're currently in leaves nothing to be
  // active — start a fresh one so the chat screen always has somewhere to go.
  if (wasActive) resetConversation();
  res.json({ ok: true });
});

app.post('/api/conversations/:id/activate', (req, res) => {
  try {
    const conversation = activateConversation(req.params.id);
    res.json({ conversation });
  } catch (err) {
    res.status(404).json({ error: err?.message || 'That conversation no longer exists.' });
  }
});

// Kept as a plain alias for "start a new chat" — nothing in the UI calls
// this directly any more (POST /api/conversations is what the New Chat
// button uses), but it's a small, harmless surface to leave working.
app.post('/api/reset', (req, res) => {
  const conversation = resetConversation();
  res.json({ ok: true, conversation });
});

const httpServer = app.listen(PORT, HOST, () => {
  console.log(`Jarvis is running at http://${HOST}:${PORT}`);
  // Eagerly resumes the active conversation (rather than waiting for the
  // user's first message) so the Stage 2 "next open" memory checkpoint — see
  // brain.js's getActiveSessionId() — has a chance to run and surface a
  // review card, if there's unreviewed material, before anything is even
  // asked. Fire-and-forget: startup must never block on this.
  getActiveSessionId();
});

// Two WebSocket endpoints ride on this same HTTP server — Engine B (Gemini
// Live) at /api/live, Engine C's (the duplex engine's) speech-to-text half
// at /api/duplex. Both are built with `noServer: true` and handed to ONE
// upgrade dispatcher here, rather than each calling `new
// WebSocketServer({server: httpServer, path: ...})` internally — that
// {server, path} pattern registers its OWN unconditional 'upgrade' listener
// per instance, and Node fires every registered listener for every
// 'upgrade' event regardless of path; `ws`'s own path filter then aborts a
// mismatched request with a 400 — whichever instance registered FIRST wins
// that race for every path meant for a LATER instance. This is exactly
// what broke the duplex engine: live.js's listener (registered first) was
// 400ing every /api/duplex connection attempt before duplex.js's own
// listener ever got a turn, surfacing to the user as a flat "Could not
// reach the Jarvis server" with nothing pointing at the real cause. `ws`'s
// own README documents this exact multi-endpoint pattern (noServer + one
// manual dispatcher) as the correct way to share one HTTP server.
// CSWSH hardening — the same-origin policy does NOT cover WebSocket
// handshakes: any page open in the user's browser, regardless of which site
// served it, can open a `ws://127.0.0.1:PORT/...` connection purely in JS,
// since loopback is reachable from any tab regardless of that tab's own
// origin. Both endpoints below proxy a real, paid third-party key
// (Deepgram/Gemini) — an untrusted page doing this silently could ride the
// user's own quota/cost. Reject any upgrade whose Origin names a page other
// than Jarvis's own — but only when Origin IS present: this app has no
// session/auth system to check instead (single local user, see CLAUDE.md),
// and non-browser callers (this project's own scratch test scripts using
// `ws` directly, per CLAUDE.md's testing guidance) don't set it, so
// requiring it outright would break local testing patterns already in use.
const ALLOWED_WS_ORIGINS = new Set([`http://127.0.0.1:${PORT}`, `http://localhost:${PORT}`]);

const liveWss = createLiveWss();
const duplexWss = createDuplexWss();
httpServer.on('upgrade', (req, socket, head) => {
  const origin = req.headers.origin;
  if (origin && !ALLOWED_WS_ORIGINS.has(origin)) {
    socket.write('HTTP/1.1 403 Forbidden\r\n\r\n');
    socket.destroy();
    return;
  }
  const { pathname } = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (pathname === '/api/live') {
    liveWss.handleUpgrade(req, socket, head, (ws) => liveWss.emit('connection', ws, req));
  } else if (pathname === '/api/duplex') {
    duplexWss.handleUpgrade(req, socket, head, (ws) => duplexWss.emit('connection', ws, req));
  } else {
    socket.destroy();
  }
});

// The scheduler is the first background process in the app — started only
// once the server is actually listening, so its very first tick can already
// broadcast task-run events to any connected browser.
startScheduler();

// Background Jobs' own supervisor — its startup call also runs the one-time
// orphan sweep (any job still 'running' from before this restart), same
// "started only once the server can already broadcast" reasoning as the
// scheduler above.
startOrchestrator();

// Self-Improvement's own background cycle — see server/improvement/CLAUDE.md.
// Started last on purpose: it subscribes to the SAME jobs/job-events.js bus
// startOrchestrator() above already primed, and its first tick sweeps for
// any job orchestrator.js's own recoverOrphans() (called inside
// startOrchestrator()) may have just classified 'orphaned'.
startImprovementCycle();

// Heartbeat + Trigger + Proactive Attention (server/heartbeat/) — started
// after the two above for the same reason improvement's own cycle is: its
// triggers.js subscribes to the same jobs/job-events.js bus
// startOrchestrator() already primed, and its own first tick can see
// whatever startOrchestrator()'s orphan sweep just wrote.
startHeartbeat();

// Resumes any monitor still 'watching' from before the last restart —
// without this a server restart would silently orphan an in-progress watch
// with no timer ever ticking again (the user asked Jarvis to watch for
// something, and it would just... stop, with no indication why).
resumeActiveMonitors();

// The files/browser connector records used to only ever get created as a
// side effect of the (now-removed) App Control settings cards fetching
// them on page load — a fresh install that never visited that page would
// never have either record, and connectors/index.js's getToolDeclarations()
// would silently never find one to register list_files/browser_navigate/etc.
// from. Both abilities are always available regardless of any UI, so both
// singleton records are seeded here, unconditionally, once at boot.
getOrCreateSingleton('files', { label: 'Files' });
getOrCreateSingleton('browser', { label: 'Browser' });

// Catches up any custom connector that existed before automatic icon
// resolution did (or whose first attempt simply found nothing), and
// (separately — see icon-resolver.js's own header for why these two are
// split) resolves every bundled catalog entry's icon regardless of whether
// the user has ever connected it — the gap a real screenshot caught: only
// resolving per-connector left every not-yet-connected catalog entry
// showing its old hand-authored mark in the Browse-connectors directory.
// Both deliberately not awaited: real network calls per entry, must never
// delay the server actually starting up.
backfillConnectorIcons().catch((err) => console.warn('[icons] backfillConnectorIcons() rejected:', err?.message || err));
resolveCatalogIcons().catch((err) => console.warn('[icons] resolveCatalogIcons() rejected:', err?.message || err));

// One-time repair for a connector saved wrong before this fix existed: a
// server that only gates real tool calls (not the handshake) could get
// saved as connectFlow.kind:'none' — "working" forever with an empty tools
// list and no path back to Connect. Re-checks each against what its server
// actually publishes and demotes any that were wrong. Best-effort, never
// awaited — must never delay the server actually starting up.
oauth.recheckNoAuthConnectors().catch((err) => console.warn('[connectors] recheckNoAuthConnectors() rejected:', err?.message || err));

// The bundled Skills catalog/marketplace this guardrail used to check no
// longer exists — see CLAUDE.md's permanent Skills rule. Name collisions are
// now only possible at actual install time (createSkill/installFromUpload/
// replaceFromUpload, all of which take reservedSkillNames() directly), so
// there's nothing left to check at startup.
