// Executes one conversation turn against an ordered list of model
// candidates, replaying the same neutral transcript if one fails so context
// is never lost. This is what fulfills the "keep talking, tell me it
// switched, and go back to my preference once it's healthy again"
// requirement — models/health.js's cooldown is what makes the recovery
// automatic once the preferred model is working again.

import { getAdapter } from '../adapters/index.js';
import { getToolDeclarations, runSkill } from '../skills/index.js';
import * as conversation from '../conversation.js';
import { getModel, updateModel } from './registry.js';
import { profileTask, rankCandidates } from './router.js';
import { markUnhealthy, markHealthy } from './health.js';
import { classifyError, AVAILABILITY_STATE_FOR_KIND } from './error-kind.js';
import { friendlyMessage } from '../friendly-message.js';
import { getPrefs } from '../prefs.js';
// ai.js is the one place the "can this model actually do X" rule lives (it
// gates the adapter ceiling AND the model's own caps). Reused rather than
// reimplemented so a turn carrying an image and a one-off analysis job can
// never disagree about what counts as vision-capable. ai.js does not import
// this file, so there's no cycle.
import { meetsNeed } from '../ai.js';

// Raised from 5 once installed folder Skills (server/skills-fs.js) arrived —
// calling a Skill consumes one step just to fetch its instructions, leaving
// fewer steps for the tool calls that actually carry them out.
// opts.maxToolSteps lets a specific caller override this (e.g. a future
// control session capping steps tighter or looser); undefined uses
// this default.
const DEFAULT_MAX_TOOL_STEPS = 12;

/** Puts one specific, still-enabled model at the head of the ranked list, keeping the rest behind it as a live fallback chain. An unknown or disabled id is ignored — the ranked list stands alone. */
function leadWith(modelId, ranked) {
  if (!modelId) return ranked;
  const lead = getModel(modelId);
  if (!lead || !lead.enabled) return ranked;
  return [lead, ...ranked.filter((e) => e.id !== lead.id)];
}

/** A task's own pinned model (opts.modelId) outranks the user's global manual pick, which outranks pure auto-ranking. */
function preferredModelId(prefs, opts) {
  if (opts.modelId) return opts.modelId;
  if (!prefs.autoSelect && prefs.manualModelId) return prefs.manualModelId;
  return null;
}

function buildCandidateList(task, opts = {}) {
  const prefs = getPrefs();
  let ranked = rankCandidates(task, { balance: prefs.balance });

  // A turn carrying an attachment can only go to a model that can actually
  // take it. This filter runs BEFORE the manual pick is applied, so even a
  // pinned text-only model is skipped for an image turn rather than being
  // handed bytes it will choke on.
  if (opts.need && Object.keys(opts.need).length) {
    ranked = ranked.filter((e) => meetsNeed(e, opts.need));
  }

  // Manual/task pick leads; the auto-ranked list behind it is the temporary
  // fallback chain used only if the pick is unavailable or breaks mid-turn.
  // Preference resumes on its own next turn once it's healthy again.
  const withLead = leadWith(preferredModelId(prefs, opts), ranked);
  return opts.need && Object.keys(opts.need).length
    ? withLead.filter((e) => meetsNeed(e, opts.need))
    : withLead;
}

/** Why an attachment turn found nothing, said so the user knows what to do about it. */
function noCapableModelReason(need) {
  if (need?.vision) {
    return "None of the models I can reach right now are able to look at images — they're either out of quota, " +
      'switched off, or text-only. A Gemini model can see pictures; you can add or enable one in Model Settings.';
  }
  if (need?.video) {
    return 'None of the models I can reach right now can take a file like that. A Gemini model can; you can add ' +
      'or enable one in Model Settings.';
  }
  return null;
}

/**
 * The reason text shown to the user for a model failure — model_switch's
 * "reason" and the final paused message both go through this. Was: the
 * adapter's own friendlyError(err) (or, on failure, the raw err.message)
 * returned VERBATIM — which is the provider's own wording, not
 * plain-language, and was reaching users as-is (see friendly-message.js's
 * header comment; a real example: quota errors carrying an appended raw
 * JSON blob). The real technical detail is still logged for debugging —
 * just never shown. friendlyMessage() classifies the RAW error (more
 * accurate than classifying an already-adapter-processed string, since the
 * raw shape carries status codes / cause chains the adapter's own
 * friendlyError() has already stripped away).
 */
function friendlyReason(adapterName, err) {
  let detail;
  try {
    detail = getAdapter(adapterName).friendlyError(err);
  } catch {
    detail = err?.message || 'an unexpected error';
  }
  console.error(`[runner] ${adapterName} error (shown to user as a plain-language message): ${detail}`);
  return friendlyMessage(err);
}

/**
 * Builds the tool list offered to the model this turn. `opts.noTools` (a
 * narration-only turn) always wins with an empty list. Otherwise
 * `opts.background === true` (unattended/scheduled runs) drops meta skills
 * (schedule_task, cancel_task, ...) regardless of `allowedTools` — they only
 * make sense in a live conversation. `opts.allowedTools`, when an array,
 * further restricts the list to exactly those names (a scheduled task's own
 * per-task connector allowlist); null/undefined leaves the full list
 * (current behavior for live chat).
 */
function toolsForTurn(opts) {
  if (opts.noTools) return [];
  const declarations = getToolDeclarations({ includeMeta: !opts.background });
  if (Array.isArray(opts.allowedTools)) {
    return declarations.filter((d) => opts.allowedTools.includes(d.name));
  }
  return declarations;
}

// A repeat failure/success against the same model doesn't need a fresh disk
// write every single turn — only when the state actually changed, or it's
// been long enough that the "checked at" timestamp is stale, is it worth
// persisting. Availability tracking must never crash a user's turn, so every
// call site wraps this in try/catch and swallows.
const AVAILABILITY_RECHECK_MS = 60 * 1000;

// Failure path: re-persist on a state change OR once the last check is
// stale, since the same model can keep failing turn after turn and it's
// worth refreshing `checkedAt` occasionally. Success path (see
// recordAvailability's `allowStaleRewrite: false` caller) only re-persists
// on an actual state change — once a model is marked 'working' there's no
// need to keep rewriting the same state on every successful turn.
function shouldRecordAvailability(entry, nextState, allowStaleRewrite) {
  const current = entry?.availability;
  if (!current || current.state !== nextState) return true;
  if (!allowStaleRewrite) return false;
  const checkedAt = current.checkedAt ? Date.parse(current.checkedAt) : 0;
  return !Number.isFinite(checkedAt) || Date.now() - checkedAt > AVAILABILITY_RECHECK_MS;
}

function recordAvailability(modelId, state, detail, { allowStaleRewrite = true } = {}) {
  try {
    const entry = getModel(modelId);
    if (!entry || !shouldRecordAvailability(entry, state, allowStaleRewrite)) return;
    updateModel(modelId, { availability: { state, checkedAt: new Date().toISOString(), detail } });
  } catch (err) {
    console.error(`[runner] failed to record availability for "${modelId}":`, err);
  }
}

/** Runs the tool-calling loop against a single model. Yields chunk/tool events; returns {text}. */
async function* runOnEntry(sessionId, entry, opts) {
  const tools = toolsForTurn(opts);
  const adapter = getAdapter(entry.adapter);
  const maxSteps = opts.maxToolSteps || DEFAULT_MAX_TOOL_STEPS;
  let finalText = null;

  for (let step = 0; step < maxSteps; step++) {
    const messages = conversation.getMessages(sessionId);
    let text = '';
    let callEvent = null;
    let finalEvent = null;

    for await (const ev of adapter.stream(entry, messages, { tools, lowConfidence: opts.lowConfidence })) {
      if (ev.type === 'chunk') {
        text += ev.text;
        yield { type: 'chunk', text: ev.text };
      } else if (ev.type === 'call') {
        callEvent = ev;
      } else if (ev.type === 'final') {
        finalEvent = ev;
      }
    }

    if (callEvent) {
      conversation.pushAssistantToolCalls(sessionId, callEvent.calls, {
        modelId: entry.id,
        raw: callEvent.raw,
        text: callEvent.text || text || undefined,
      });

      const toolResults = [];
      for (const call of callEvent.calls) {
        yield { type: 'tool_start', name: call.name };
        const result = await runSkill(call.name, call.args, {
          sessionId,
          modelId: entry.id,
          lowConfidence: opts.lowConfidence,
          // Unattended runs (scheduled tasks, briefing sources) were
          // consented to once at creation — nobody is present to answer a
          // live read-back, so the confirmation gate is bypassed rather
          // than stalling on a token nobody will ever send back.
          autoConfirm: Boolean(opts.autoConfirm),
        });
        // ui_action/needs_confirmation/summary/confirm_token are only ever
        // present on skills that set them — JSON.stringify drops the rest.
        yield {
          type: 'tool_result',
          name: call.name,
          ok: result?.ok !== false,
          ui_action: result?.ui_action,
          needs_confirmation: result?.needs_confirmation,
          summary: result?.summary,
        };
        toolResults.push({ id: call.id, name: call.name, result });
      }
      conversation.pushToolResults(sessionId, toolResults);
      continue;
    }

    finalText = finalEvent?.text ?? (text || "Sorry, I didn't quite catch that.");
    conversation.pushAssistantText(sessionId, finalText, { modelId: entry.id, raw: finalEvent?.raw });
    break;
  }

  if (finalText === null) {
    // The loop exhausted maxSteps without the model ever returning a final
    // answer (every step made another tool call). This fallback used to
    // exist only as the generator's RETURN value, never yielded as a
    // 'chunk' — so it never reached the transcript or the speaker, and the
    // turn just stopped with no explanation at all. Yield it like any other
    // reply so the user actually sees/hears why nothing more is coming.
    finalText = "Sorry, I got a bit stuck processing that — could you try again?";
    yield { type: 'chunk', text: finalText };
    conversation.pushAssistantText(sessionId, finalText, { modelId: entry.id });
  }

  return { text: finalText };
}

/**
 * Runs one turn in `sessionId`. `opts` (all optional): `source`
 * ('voice'|'text'), `background` (cost over latency in auto-ranking),
 * `lowConfidence` (voice-clarity gate), `modelId` (pin one specific model
 * for this turn — outranks the user's global manual pick, still falls back
 * through the auto-ranked list if it breaks), `autoConfirm` (bypass the
 * interactive confirm-and-wait gate — for unattended/scheduled runs where
 * nobody is present to answer it), `noTools` (no tool list exposed to the
 * model — for a narration-only turn), `allowedTools` (array of skill names
 * to restrict the tool list to — e.g. a scheduled task's own connector
 * allowlist; null/undefined means no restriction). Yields, in order:
 *   {type:'chunk', text}
 *   {type:'tool_start', name} / {type:'tool_result', name, ok}
 *   {type:'model_switch', from, to, reason}   — only when a fallback actually happens
 *   {type:'restart'}                          — clear any partial reply already shown; a fresh one follows
 *   {type:'done', text, modelId}
 *   {type:'paused', reason}                   — nothing could complete the turn; no answer was produced
 */
export async function* runTurn(sessionId, userText, opts = {}) {
  // `media` rides on the user's own message, so it stays in the transcript
  // and every later turn still sees it — the same way Claude keeps an image
  // in a conversation (see attachments.js for why images aren't digested).
  conversation.pushUserText(sessionId, userText, { media: opts.media });
  const task = profileTask({ text: userText, source: opts.source, background: opts.background, type: opts.type });
  const candidates = buildCandidateList(task, opts);

  if (candidates.length === 0) {
    yield {
      type: 'paused',
      reason:
        noCapableModelReason(opts.need) ||
        "None of the models I have set up right now can handle this — they're either unavailable, " +
          "missing a key, or not built for this kind of request. Add or fix a model in Model Settings " +
          'and I\'ll pick it up automatically.',
    };
    return;
  }

  let previousId = null;
  let lastError = null;

  for (const entry of candidates) {
    if (previousId) {
      yield { type: 'model_switch', from: previousId, to: entry.id, reason: lastError || 'the previous model ran into a problem' };
    }

    const gen = runOnEntry(sessionId, entry, opts);
    let streamed = false;
    let finalResult = null;
    let failed = false;

    try {
      while (true) {
        const { value, done } = await gen.next();
        if (done) {
          finalResult = value;
          break;
        }
        if (value.type === 'chunk') streamed = true;
        yield value;
      }
    } catch (err) {
      failed = true;
      console.error(`[runner] model "${entry.id}" failed:`, err);
      const kind = classifyError(err);
      markUnhealthy(entry.id, err?.message || 'error', kind);
      recordAvailability(entry.id, AVAILABILITY_STATE_FOR_KIND[kind] ?? 'unreachable', err?.message || 'error');
      lastError = friendlyReason(entry.adapter, err);
      previousId = entry.id;
      if (streamed) yield { type: 'restart' };
    }

    if (!failed && finalResult) {
      markHealthy(entry.id);
      recordAvailability(entry.id, 'working', null, { allowStaleRewrite: false });
      yield { type: 'done', text: finalResult.text, modelId: entry.id };
      return;
    }
  }

  // Dropped the old trailing "Check Model Settings, or try again in a
  // moment." — lastError is now always one of friendlyMessage()'s clean
  // category sentences (see friendlyReason() above), and three of its four
  // real categories already end with their own specific next step ("...
  // switch models in Model Settings" etc.); appending the same generic
  // advice again read redundant.
  yield {
    type: 'paused',
    reason: `I tried every model I have available and none of them could finish that. Most recent problem: ${lastError || 'unknown error'}`,
  };
}

export function resetConversation(sessionId = 'main') {
  conversation.resetSession(sessionId);
}
