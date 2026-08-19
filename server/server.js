// Jarvis's local server: serves the browser UI and its API endpoints.
// Deliberately bound to 127.0.0.1 only — it is not reachable from other
// devices on the network.

import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { chat, chatStream, resetConversation, getActiveSessionId, activateConversation } from './brain.js';
import * as chatStore from './chat-store.js';
// Direct runner.js import (not through brain.js) so a monitor's follow-up
// can run WITHOUT autoConfirm — a risky action must still pause and ask,
// even though nobody may be watching the moment it fires (see the monitor
// trigger handler below). scheduler.js's own runOneTurn is the precedent
// for a server-side (not server/skills/) file importing runner.js directly.
import { runTurn } from './models/runner.js';
import { synthesizeSpeech, AVAILABLE_VOICES } from './tts.js';
import { classifyTurnComplete } from './turn-check.js';
import { isLowConfidence } from './clarify.js';
import { attachLiveServer } from './live.js';
import { addClient, removeClient, broadcast } from './events.js';
import { addNotification, listNotifications, markRead, markAllRead, removeNotification, clearAll as clearAllNotifications } from './notifications.js';
import { ADAPTER_NAMES } from './adapters/index.js';
import { SUGGESTIONS } from './models/catalog.js';
import { getHealthStatus, markHealthy, markUnhealthy } from './models/health.js';
import { classifyError, AVAILABILITY_STATE_FOR_KIND } from './models/error-kind.js';
import { friendlyMessage, friendlyMessageFor } from './friendly-message.js';
import { listSkills, hasSkill, reservedSkillNames } from './skills/index.js';
import {
  listUserSkills,
  getSkill,
  readSkillMd,
  createSkill,
  updateSkillMd,
  updateSkillState,
  deleteSkill,
  skillRoot,
} from './skills/store/skill-files.js';
import { installFromUpload, replaceFromUpload, buildSkillZip } from './skills/store/skill-zip.js';
import {
  listModels,
  listConnections,
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
import { saveSecret, deleteSecret } from './config.js';
import { listEntries as listProfileEntries, addEntry as addProfileEntry, deleteEntry as deleteProfileEntry } from './profile.js';
import {
  listPendingCandidates as listPendingMemoryCandidates,
  approveCandidate as approveMemoryCandidate,
  rejectCandidate as rejectMemoryCandidate,
  resolveConflict as resolveMemoryConflict,
} from './memory/memory-store.js';
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

const ADAPTER_LABELS = {
  gemini: 'Google (Gemini)',
  anthropic: 'Anthropic (Claude)',
  'openai-compatible': 'OpenAI-compatible (OpenAI, Ollama, LM Studio, OpenRouter, Groq, ...)',
};

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

function publicConnection(conn, models) {
  const { secretRef, ...rest } = conn;
  return { ...rest, hasSecret: Boolean(secretRef), modelCount: models.filter((m) => m.connectionId === conn.id).length };
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

app.get('/api/models/adapters', (req, res) => {
  res.json({
    adapters: ADAPTER_NAMES.map((name) => ({ name, label: ADAPTER_LABELS[name] || name })),
    suggestions: SUGGESTIONS,
  });
});

app.get('/api/skills', (req, res) => {
  res.json({ skills: listSkills({ includeMeta: req.query.all === '1' }) });
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

// Fixed-path routes MUST be registered before '/api/skills/:name' below —
// Express matches route order, and ':name' would otherwise swallow a request
// for a fixed path as if it were a skill's name (confirmed live once
// already, when /catalog was added after :name — see git history).
app.get('/api/skills/:name', (req, res) => {
  const skill = getSkill(req.params.name);
  if (!skill) return res.status(404).json({ ok: false, error: 'Unknown skill.' });
  try {
    const { raw, frontmatter, body } = readSkillMd(req.params.name);
    res.json({ ok: true, skill, frontmatter, instructions: body, raw });
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

// The combined "Test and add" entry point: tests the connection ONCE (using
// the first selected model), then — only on success — saves the connection
// and every selected model together, so several models sharing one
// address+key don't each duplicate the same saved secret (see registry.js).
app.post('/api/connections', async (req, res) => {
  const { adapter, baseUrl, label, secret, models } = req.body || {};
  if (!adapter) {
    return res.status(400).json({ ok: false, error: 'Please choose a model type.' });
  }
  try {
    const result = await createConnectionWithModels({ adapter, baseUrl, label, secret, models });
    if (!result.ok) return res.status(400).json(result);
    res.json({
      ok: true,
      connection: publicConnection(result.connection, listModels()),
      added: result.added.map(publicModel),
      failed: result.failed,
    });
  } catch (err) {
    res.status(400).json({ ok: false, error: err?.message || 'Could not add that connection.' });
  }
});

// Discovers what models a server has. `connectionId` reuses an already-saved
// connection's key instead of re-typing it (used by the "Find models at
// this address" button on an existing connection); otherwise `adapter`/
// `baseUrl`/`secret` describe a not-yet-saved one (the "Add a model" popup).
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
  const result = await testModelConnection(entry);
  try {
    if (result.ok) {
      markHealthy(entry.id);
      updateModel(entry.id, { availability: { state: 'working', checkedAt: new Date().toISOString(), detail: null } });
    } else {
      const kind = classifyError({ message: result.error });
      markUnhealthy(entry.id, result.error, kind);
      updateModel(entry.id, {
        availability: { state: AVAILABILITY_STATE_FOR_KIND[kind] || 'unreachable', checkedAt: new Date().toISOString(), detail: result.error },
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

// Probes every enabled model at once — powers the models screen's "Check all
// models" button, so availability badges can be refreshed on demand instead
// of only updating passively as real conversation turns happen to hit them.
app.post('/api/models/recheck', async (req, res) => {
  try {
    const entries = listModels().filter((e) => e.enabled);
    await Promise.all(entries.map((entry) => testAndRecord(entry).catch(() => null)));
    res.json({ ok: true, models: listModels().map(publicModel) });
  } catch (err) {
    console.error('[models] recheck-all failed:', err);
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

// ---------- scheduled tasks ----------

app.get('/api/tasks', (req, res) => {
  res.json({ tasks: listTasks() });
});

app.post('/api/tasks', (req, res) => {
  const body = req.body || {};
  // Validated here, not in task-store.js — that file is a plain leaf store
  // (see its header comment) and hasSkill() lives behind the skills loader,
  // which task-store.js must never import (the same circular-import risk
  // documented on briefing-config.js: a skill -> task-store.js is a real,
  // safe edge today; task-store.js -> skills/index.js would not be).
  if (body.action?.type === 'skill' && !hasSkill(body.action.skillName)) {
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
  if (body.action?.type === 'skill' && !hasSkill(body.action.skillName)) {
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
    const existing = listConnectors().find(
      (c) => c.type === 'mcp' && c.source?.type === 'user' && c.config?.connectFlow?.url === url
    );
    if (existing) {
      const existingKind = existing.config?.connectFlow?.kind;
      if (clientId && !existing.config?.connectFlow?.clientId && existingKind !== 'none') {
        updateConnector(existing.id, { config: { connectFlow: { ...existing.config.connectFlow, kind: 'oauth_guided', clientId } } });
        if (clientSecret) saveSecret(`connclient_${existing.id}`, clientSecret);
      }
      return res.json({ ok: true, connectorId: existing.id, label: existing.label });
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

// ---------- text-to-speech ----------

app.get('/api/voices', (req, res) => {
  res.json({ voices: AVAILABLE_VOICES });
});

app.post('/api/tts', async (req, res) => {
  const text = String(req.body?.text || '').trim();
  const voice = req.body?.voice;
  if (!text) {
    return res.status(400).json({ error: 'No text provided.' });
  }

  try {
    const wavBuffer = await synthesizeSpeech(text, { voice });
    res.set('Content-Type', 'audio/wav');
    res.send(wavBuffer);
  } catch (err) {
    if (err?.code === 'NO_API_KEY') {
      return res.status(400).json({ error: 'No Gemini API key is set up yet.', code: 'NO_API_KEY' });
    }
    console.error('[tts] error:', err);
    res.status(500).json({ error: 'Could not generate speech audio right now.' });
  }
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

// Engine B (Gemini Live) rides on the same HTTP server, upgraded to a
// WebSocket at /api/live — no separate port needed.
attachLiveServer(httpServer);

// The scheduler is the first background process in the app — started only
// once the server is actually listening, so its very first tick can already
// broadcast task-run events to any connected browser.
startScheduler();

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
