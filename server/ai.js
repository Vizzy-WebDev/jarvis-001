// "Ask a model one question, get one answer back."
//
// Jarvis has two model-driving loops already: models/runner.js (a chat turn,
// with tools and a persistent transcript) and control/session.js (the
// computer-control perceive/act loop). Planning and Content Analysis need
// neither — they need a single, stateless call: here is a prompt, sometimes
// with a video or an image attached, give me back text or a JSON object.
// This is that third shape, and the two features are built on it.
//
// Why not just use runner.js: skills must never import it (CLAUDE.md's
// circular-import invariant — skills/index.js dynamically imports every file
// in server/skills/, so anything reachable from a skill must not lead back
// there). This file imports only adapters and the model registry/router, so
// a skill can safely use it. It follows control/session.js's precedent of
// calling `adapter.stream()` directly with a `systemOverride`.
//
// Capability filtering lives HERE rather than in models/router.js on purpose:
// the router ranks models for *conversation*, where "can this model watch a
// video" is never a question. A job that genuinely needs video (or grounded
// web search) states that requirement at its own call site, and gets an
// honest empty list rather than a model that will fail halfway through.

import { getAdapter, getCapabilities } from './adapters/index.js';
import { rankCandidates, profileTask } from './models/router.js';
import { getModel, updateModel, listModels } from './models/registry.js';
import { markHealthy, markUnhealthy } from './models/health.js';
import { classifyError, AVAILABILITY_STATE_FOR_KIND } from './models/error-kind.js';
import { friendlyMessage } from './friendly-message.js';
import { getPrefs } from './prefs.js';
import { needForType } from './models/task-types.js';

/**
 * True if this model can do everything `need` asks for. A capability is
 * gated twice: the adapter's CAPABILITIES ceiling (what the wire format can
 * express at all) AND the model's own caps (what this particular model can
 * do). Both must allow it — a Gemini-shaped adapter can carry video, but a
 * text-only model behind it still can't watch one.
 */
export function meetsNeed(entry, need) {
  if (!need) return true;
  const ceiling = getCapabilities(entry.adapter);
  for (const key of ['video', 'audio', 'vision', 'webSearch']) {
    if (!need[key]) continue;
    if (!ceiling?.[key]) return false;
    // caps.video/webSearch are backfilled by registry.js at read time, so an
    // older saved model (from before these flags existed) is never wrongly
    // treated as incapable just because its stored record predates them.
    if (entry.caps?.[key] === false) return false;
  }
  return true;
}

/**
 * Models that could run this job, best first. `modelId` puts one specific
 * model at the head (the user's per-job pick), exactly as runner.js's
 * `leadWith` does for a pinned scheduled task — the rest stay behind it as a
 * live fallback chain, so a pick that's out of quota doesn't kill the job.
 *
 * `type` (coding/research/vision/simple-question/general, from
 * models/task-types.js) is optional: a declared type's default `need` bag
 * is used when `need` itself isn't given, and its scoring lean flows through
 * profileTask() below. An explicit `need` always wins over a type's default
 * — declaring a type never takes away a caller's ability to state its own
 * exact requirement.
 */
function candidatesFor({ modelId, need, background, type }) {
  const prefs = getPrefs();
  const task = profileTask({ text: '', background: Boolean(background), type });
  // These are long-form document/analysis jobs, never voice turns — rank for
  // reasoning quality by default. A declared `type` (e.g. 'simple-question')
  // can lean this toward speed instead, via profileTask()'s own handling of
  // `type` above; with no type declared this preserves the original
  // always-reasoning default every existing caller here still relies on.
  if (!type) task.complexity = 'reasoning';
  task.needsTools = false; // a one-off call never calls a skill

  const effectiveNeed = need || needForType(type);
  let ranked = rankCandidates(task, { balance: prefs.balance }).filter((e) => meetsNeed(e, effectiveNeed));

  if (modelId) {
    const lead = getModel(modelId);
    if (lead && lead.enabled && meetsNeed(lead, effectiveNeed)) {
      ranked = [lead, ...ranked.filter((e) => e.id !== lead.id)];
    }
  }
  return ranked;
}

/** Availability bookkeeping, mirroring runner.js — a job failing on quota should grey the model out in Model Settings just like a chat turn does. Never allowed to break the job itself. */
function recordAvailability(modelId, state, detail) {
  try {
    updateModel(modelId, { availability: { state, checkedAt: new Date().toISOString(), detail } });
  } catch (err) {
    console.error(`[ai] could not record availability for "${modelId}":`, err);
  }
}

/**
 * Pulls a JSON object or array out of a model's reply. Models wrap JSON in
 * ``` fences, prefix it with "Here's the JSON:", or add a trailing note,
 * however firmly you ask them not to — so this is deliberately forgiving
 * rather than a strict JSON.parse of the whole reply. Doing it this way
 * means every adapter works identically with no per-provider structured
 * output support.
 */
export function extractJson(text) {
  const raw = String(text || '').trim();
  if (!raw) return null;

  const unfenced = raw.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '').trim();
  try {
    return JSON.parse(unfenced);
  } catch {
    // Fall through to scanning for the first balanced object/array.
  }

  const start = unfenced.search(/[[{]/);
  if (start === -1) return null;
  const open = unfenced[start];
  const close = open === '{' ? '}' : ']';

  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < unfenced.length; i++) {
    const ch = unfenced[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch === '\\') {
      escaped = true;
      continue;
    }
    if (ch === '"') inString = !inString;
    if (inString) continue;
    if (ch === open) depth++;
    else if (ch === close) {
      depth--;
      if (depth === 0) {
        try {
          return JSON.parse(unfenced.slice(start, i + 1));
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

const JSON_DIRECTIVE =
  '\n\nReply with valid JSON only. No explanation before or after it, no markdown code fences.';

/**
 * Runs one prompt against the best available model, falling through to the
 * next one if it fails.
 *
 * Options:
 *   prompt      the user-side text (required)
 *   system      system instruction for this job (replaces the spoken-reply
 *               one entirely, via prompt.js's systemOverride)
 *   media       [{kind, mimeType, uri|dataBase64}] — attachments, in the
 *               same neutral shape conversation.js already uses
 *   json        true to ask for and parse a JSON reply (result.data)
 *   modelId     a specific model to try first; others stay as fallback
 *   only        true to use `modelId` alone with no fallback — required when
 *               `media` holds an uploaded file, since the upload lives
 *               against that model's own API key and a different provider
 *               would just get a URI it can't read
 *   need        {video, audio, vision, webSearch} — hard requirements
 *   type        coding/research/vision/simple-question/general (models/task-types.js) —
 *               optional; seeds `need`'s default and leans scoring when `need` isn't given explicitly
 *   background  rank for cost over latency (unattended work)
 *
 * Returns { ok: true, text, data?, modelId, modelLabel }
 *      or { ok: false, error, kind }   — `error` is already plain language
 */
export async function askModel({ prompt, system, media, json = false, modelId, only = false, need, type, background = false } = {}) {
  const text = String(prompt || '').trim();
  if (!text) return { ok: false, error: 'Nothing was asked.', kind: 'other' };

  const candidates = only
    ? [getModel(modelId)].filter((e) => e && e.enabled)
    : candidatesFor({ modelId, need, background, type });
  if (!candidates.length) {
    return { ok: false, error: noCandidateReason(need || needForType(type)), kind: 'no_model' };
  }

  const messages = [{ role: 'user', text: json ? text + JSON_DIRECTIVE : text, media }];
  let lastError = null;
  let lastKind = 'other';

  for (const entry of candidates) {
    try {
      const adapter = getAdapter(entry.adapter);
      let out = '';
      for await (const ev of adapter.stream(entry, messages, { systemOverride: system, tools: [] })) {
        if (ev.type === 'chunk') out += ev.text;
        else if (ev.type === 'final' && !out) out = ev.text || '';
      }

      out = out.trim();
      if (!out) throw new Error('The model replied with nothing at all.');

      if (json) {
        const data = extractJson(out);
        if (data === null) {
          // Not a transport failure — this model is working, it just didn't
          // follow the format. Try the next one rather than marking this one
          // unhealthy (that would wrongly lock it out of chat for minutes).
          lastError = 'The model did not reply in the format this needed.';
          lastKind = 'format';
          continue;
        }
        markHealthy(entry.id);
        return { ok: true, text: out, data, modelId: entry.id, modelLabel: entry.label };
      }

      markHealthy(entry.id);
      return { ok: true, text: out, modelId: entry.id, modelLabel: entry.label };
    } catch (err) {
      const kind = classifyError(err);
      markUnhealthy(entry.id, err?.message || 'error', kind);
      recordAvailability(entry.id, AVAILABILITY_STATE_FOR_KIND[kind] ?? 'unreachable', err?.message || 'error');
      lastKind = kind;
      // The real, technical detail is still logged for debugging — see
      // friendly-message.js's header comment for why it's never shown to
      // the user directly any more. lastError is now always one of
      // friendlyMessage()'s clean category sentences (classified from the
      // RAW err, not the adapter's own already-processed string, for
      // better accuracy).
      const detail = (() => {
        try {
          return getAdapter(entry.adapter).friendlyError(err);
        } catch {
          return err?.message || 'an unexpected error';
        }
      })();
      console.error(`[ai] model "${entry.id}" failed (shown to user as a plain-language message): ${detail}`);
      lastError = friendlyMessage(err);
    }
  }

  return { ok: false, error: friendlyFailure(lastError, lastKind), kind: lastKind };
}

/** Why nothing qualified — said in a way that tells the user what to actually do about it. */
function noCandidateReason(need) {
  if (need?.video) {
    const anyVideoModel = listModels().some((e) => getCapabilities(e.adapter)?.video && e.caps?.video !== false);
    return anyVideoModel
      ? "None of your models that can watch video are available right now — they're out of quota, switched off, or unreachable."
      : 'None of your models can watch video. Google\'s Gemini models can; you can add one in Model Settings.';
  }
  if (need?.webSearch) {
    return 'None of your models can search the web themselves right now.';
  }
  return "None of your models are available right now — they're either out of quota, switched off, or missing a key. Check Model Settings.";
}

function friendlyFailure(error, kind) {
  // 'format' isn't one of classifyError()'s real categories — it's set
  // locally (above) when a model replies but not in the JSON shape asked
  // for, so friendlyMessage() has no sentence for it; kept bespoke here.
  if (kind === 'format') {
    return 'The models I tried could not produce this in the format it needs — a stronger model would handle it better.';
  }
  // Every other kind: `error` is already one of friendlyMessage()'s clean
  // category sentences (see the catch block above) — used directly rather
  // than wrapped in another sentence around it, unlike before this fix
  // (which wrapped a raw provider string, e.g. "Every model I tried has
  // hit its usage limit. <raw quota JSON blob>").
  return error || "I couldn't get this done and I'm not sure why.";
}

/** True if at least one model could run a job with these requirements — for telling the user up front instead of after a wasted wait. */
export function canRun(need) {
  return candidatesFor({ need }).length > 0;
}

/**
 * Every model that could run this job, best first. Needed when something
 * must happen *before* the call — uploading or inlining a file, in media.js's
 * case — so the caller can't just hand a prompt to askModel and let it
 * choose.
 *
 * Returning the whole list, not just the winner, is the point: preparing an
 * attachment can fail for the top-ranked model specifically (it can't take
 * that file kind) while the one behind it would have been fine. Callers walk
 * the list until one accepts the attachment.
 */
export function pickModels({ modelId, need, type, background = false } = {}) {
  return candidatesFor({ modelId, need, type, background });
}

/** The single best model for a job, or null. Prefer pickModels() when a failure to prepare should fall through instead of ending the job. */
export function pickModel({ modelId, need, type, background = false } = {}) {
  return pickModels({ modelId, need, type, background })[0] || null;
}

/** Why nothing qualified, for a caller that used pickModel() and got null. */
export function whyNoModel(need) {
  return noCandidateReason(need);
}
