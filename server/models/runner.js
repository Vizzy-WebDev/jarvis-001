// Executes one conversation turn against an ordered list of model
// candidates, replaying the same neutral transcript if one fails so context
// is never lost. This is what fulfills the "keep talking, tell me it
// switched, and go back to my preference once it's healthy again"
// requirement — models/health.js's cooldown is what makes the recovery
// automatic once the preferred model is working again.

import { getAdapter } from '../adapters/index.js';
import { getToolDeclarations, invoke } from '../capabilities.js';
import * as conversation from '../conversation.js';
import { getModel, updateModel } from './registry.js';
import { profileTask, rankCandidates, explainExclusions } from './router.js';
import { markUnhealthy, markHealthy } from './health.js';
import { classifyError, AVAILABILITY_STATE_FOR_KIND } from './error-kind.js';
import { friendlyMessage } from '../friendly-message.js';
import { getPrefs } from '../prefs.js';
import { redactSecrets } from './redact.js';
import { getSecret } from '../config.js';
// ai.js is the one place the "can this model actually do X" rule lives (it
// gates the adapter ceiling AND the model's own caps). Reused rather than
// reimplemented so a turn carrying an image and a one-off analysis job can
// never disagree about what counts as vision-capable. ai.js does not import
// this file, so there's no cycle.
import { meetsNeed } from '../ai.js';
import { readStyle, clearSession as clearStyleSession, createReactionScanner, stripReactionMarkers } from '../personality.js';
import { noteCorrection } from '../improvement/capture.js';
import { computeTurnSignals } from '../self/self-model.js';
import { recordToolOutcome } from '../self/self-capture.js';
import { recordModelUsage } from '../cost/record.js';
import { appendEntry as appendOpsTrace } from '../ops/ops-trace.js';

// Raised from 5 once installed folder Skills (server/skills-fs.js) arrived —
// calling a Skill consumes one step just to fetch its instructions, leaving
// fewer steps for the tool calls that actually carry them out.
// opts.maxToolSteps lets a specific caller override this (e.g. a future
// control session capping steps tighter or looser); undefined uses
// this default.
const DEFAULT_MAX_TOOL_STEPS = 12;

// A turn that fails on its first candidate doesn't need to walk the ENTIRE
// ranked list before giving up — on a free-tier account most of that list
// is models that failed hours or days ago (see AVAILABILITY_COOLDOWNS_MS in
// router.js), and each one costs a full network round-trip before it's
// crossed off. Confirmed live: 47 of 62 enabled models sitting
// 'unreachable', a top pick that fails walking through all of them cost up
// to 370s of silence — during which the user, reasonably, assumed Jarvis
// had stopped responding. A user told plainly within a few tries that
// nothing is reachable right now is strictly better than one left waiting
// minutes for an answer that was always going to fail anyway; the ranked
// list behind the cap is still there for a genuinely quick retry.
const MAX_FALLBACK_ATTEMPTS = 4;

// No adapter's own SDK sets a request timeout (the OpenAI SDK defaults to
// 10 minutes; the Gemini SDK has none at all) — so a model that accepts the
// connection and then simply never responds was never penalized: the ONLY
// thing that ever noticed was the front-end's own 45s "stuck" watchdog
// (public/engines/voice-engine.js), which just closes the connection and
// tells the user Jarvis "got stuck" — runner.js's own catch block treats
// that closed connection as `opts.signal.aborted` (indistinguishable, before
// this, from the user having sent a NEWER message) and returns silently,
// recording nothing. The hung model kept its 'working' badge and its
// sessionStickyModel lead, so it hung again on the very next turn. Confirmed
// live: one real tool-enabled turn against a model the user's own settings
// showed as 'working' produced zero output for over two minutes.
//
// FIRST_TOKEN_TIMEOUT_MS bounds how long ONE step (one adapter.stream()
// call — a turn can have several, across tool-call round-trips) may go with
// no event at all before it's abandoned. ATTEMPT_TIMEOUT_MS bounds the
// WHOLE attempt against one candidate model (every step combined) — a model
// that keeps trickling tool calls forever must still eventually be cut off.
// Both are real failures now (see runOnEntry's own use of these), not a
// silent, unrecorded return.
const FIRST_TOKEN_TIMEOUT_MS = 20 * 1000;
const ATTEMPT_TIMEOUT_MS = 120 * 1000;

// A single tool call (invoke(), below) has no timeout of its own — a
// genuinely hung tool (a stuck browser automation, a wedged sandbox process)
// could block a turn forever with no chunk/tool_start/tool_result ever
// following. server.js's chat/stream now sends a periodic 'progress'
// heartbeat specifically so the FRONT-END stuck watchdog doesn't fire on a
// long-but-healthy tool call — which makes a real backstop HERE more
// important, not less: without one, a heartbeat would mask a truly hung
// tool instead of catching it. Generous on purpose (browser automation and
// research can legitimately take a couple of minutes) — this is a last
// resort, not a normal-case bound.
const TOOL_CALL_TIMEOUT_MS = 180 * 1000;

// A model (or a gateway proxying one) can enter a degenerate loop and just
// keep repeating the same short phrase — nothing anywhere used to cap this.
// `maxSteps`/DEFAULT_MAX_TOOL_STEPS only bounds tool-call ROUND TRIPS, a
// completely different axis; a single step's own streamed text could run
// forever. Confirmed live: real stored replies up to 287,575 characters,
// one short phrase repeated over 1,200 times in a row, streamed in full to
// both the transcript and TTS. Two independent backstops, checked on every
// step's accumulated text as it streams (see runOnEntry's own use below):
// a bounded repetition scan (cheap regardless of total reply length — it
// only ever looks at the last REPEAT_WINDOW_CHARS) and a hard length
// ceiling as an independent catch-all for a runaway that isn't simple
// repetition. Both are generous — SYSTEM_INSTRUCTION already asks for
// short, spoken-style replies by default — this is a last resort, not a
// normal-case bound.
const REPEAT_WINDOW_CHARS = 600; // how far back the repetition scan looks
const REPEAT_MIN_UNIT_CHARS = 4; // shortest repeated phrase worth flagging
const REPEAT_MAX_UNIT_CHARS = 80; // longest repeated phrase worth flagging
const REPEAT_TRIP_COUNT = 6; // consecutive repeats of the same phrase before tripping
const MAX_REPLY_CHARS = 20000; // hard ceiling, independent of the repetition scan

/**
 * True once the tail of `text` looks like a short phrase repeating itself
 * many times in a row — the exact shape of every real runaway reply found
 * live (e.g. "I want to" x1,240, "how many images" x486). Only looks at the
 * last REPEAT_WINDOW_CHARS, so the cost per call stays bounded no matter how
 * long the overall reply has grown — safe to call on every streamed chunk.
 */
function looksLikeRunawayRepetition(text) {
  const recent = text.length > REPEAT_WINDOW_CHARS ? text.slice(-REPEAT_WINDOW_CHARS) : text;
  if (recent.length < REPEAT_MIN_UNIT_CHARS * REPEAT_TRIP_COUNT) return false;
  for (let unit = REPEAT_MIN_UNIT_CHARS; unit <= REPEAT_MAX_UNIT_CHARS; unit++) {
    if (recent.length < unit * REPEAT_TRIP_COUNT) continue;
    const tail = recent.slice(-unit);
    if (!tail.trim()) continue; // an all-whitespace "unit" is meaningless
    let repeats = 1;
    let pos = recent.length - unit;
    while (pos - unit >= 0 && recent.slice(pos - unit, pos) === tail) {
      repeats++;
      pos -= unit;
      if (repeats >= REPEAT_TRIP_COUNT) return true;
    }
  }
  return false;
}

/** Races a tool invoke() against TOOL_CALL_TIMEOUT_MS. Never throws — a timeout resolves to the same `{ok:false, error}` shape a real failed tool call already returns, so every caller downstream (self-model capture, the tool_result event, conversation history) needs no special case for this. */
function invokeWithTimeout(name, args, ctx) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ ok: false, error: 'That took too long and was stopped.' }), TOOL_CALL_TIMEOUT_MS);
  });
  return Promise.race([invoke(name, args, ctx), timeout]).finally(() => clearTimeout(timer));
}

// Which model actually answered last, per session — the "sticky model"
// mechanism (see preferredModelId below). In-memory only, same lifetime as
// conversation.js's session map; cleared on resetConversation() so a new
// chat starts with a clean auto-pick instead of inheriting whatever
// answered the PREVIOUS conversation.
const sessionStickyModel = new Map(); // sessionId -> modelId

// Non-core capability names find_capability.js has ever surfaced in THIS
// conversation, kept for the conversation's lifetime rather than just the
// current turn. Before this existed, runOnEntry's own `unlocked` Set was
// declared fresh on every runTurn() call — so a Skill, connector tool, or
// Planning Partner tool the model found via find_capability on one message
// went invisible again on the very next message, forcing a fresh search
// every single step of what the user experiences as one continuous
// multi-turn task (confirmed against real data: several real Planning
// Partner projects stalled after their first step for exactly this
// reason). Same lifetime/cleanup story as sessionStickyModel above.
const sessionUnlockedTools = new Map(); // sessionId -> Set<string>

function getSessionUnlocked(sessionId) {
  let set = sessionUnlockedTools.get(sessionId);
  if (!set) {
    set = new Set();
    sessionUnlockedTools.set(sessionId, set);
  }
  return set;
}

/** Puts one specific, still-enabled model at the head of the ranked list, keeping the rest behind it as a live fallback chain. An unknown or disabled id is ignored — the ranked list stands alone. */
function leadWith(modelId, ranked) {
  if (!modelId) return ranked;
  const lead = getModel(modelId);
  if (!lead || !lead.enabled) return ranked;
  return [lead, ...ranked.filter((e) => e.id !== lead.id)];
}

/**
 * A task's own pinned model (opts.modelId) outranks the user's pinned voice
 * model, which outranks the user's global manual pick, which outranks the
 * session's own "sticky" model (whichever model last actually answered in
 * THIS conversation — see sessionStickyModel above), which outranks pure
 * auto-ranking. `voiceModelId` only applies to `source: 'voice'` turns — a
 * typed message is unaffected even if a voice model is pinned.
 *
 * The sticky tier only applies when auto-select is on — a manual pick is
 * already an explicit, stronger statement of intent, and letting stickiness
 * override it would silently ignore the user's own choice the moment a
 * different model happened to answer once (e.g. during the manual model's
 * own downtime). This is also the direct fix for "it doesn't feel like one
 * assistant": confirmed live, a single 300-message conversation had been
 * answered by 15 different models, switching 78 times, because every turn
 * re-ranked from scratch with nothing remembering who answered last.
 */
function preferredModelId(prefs, opts, sessionId) {
  if (opts.modelId) return opts.modelId;
  if (opts.source === 'voice' && prefs.voiceModelId) return prefs.voiceModelId;
  if (!prefs.autoSelect && prefs.manualModelId) return prefs.manualModelId;
  if (prefs.autoSelect) return sessionStickyModel.get(sessionId) || null;
  return null;
}

function buildCandidateList(task, opts = {}, sessionId) {
  const prefs = getPrefs();
  let ranked = rankCandidates(task, { balance: prefs.balance });

  // A turn carrying an attachment can only go to a model that can actually
  // take it. This filter runs BEFORE the manual pick is applied, so even a
  // pinned text-only model is skipped for an image turn rather than being
  // handed bytes it will choke on.
  if (opts.need && Object.keys(opts.need).length) {
    ranked = ranked.filter((e) => meetsNeed(e, opts.need));
  }

  // Manual/task/sticky pick leads; the auto-ranked list behind it is the
  // temporary fallback chain used only if the pick is unavailable or breaks
  // mid-turn. Preference resumes on its own next turn once it's healthy
  // again.
  const withLead = leadWith(preferredModelId(prefs, opts, sessionId), ranked);
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

// Plain-language labels for router.js's explainExclusions() reason keys —
// the persisted-availability-state ones (quota/auth/no_access/busy/
// unreachable/error/unsupported) deliberately use the same vocabulary the
// Model Settings badges show (see public/screens/models.js's
// AVAILABILITY_LABELS), so a spoken/written explanation and what the user
// sees on the screen never disagree about what a state is called.
const EXCLUSION_LABELS = {
  disabled: 'switched off',
  needs_key: 'missing a key',
  recent_failure: 'resting after a recent problem',
  no_tools: "can't use the tools this needs",
  context_too_small: 'too small a context window for this',
  quota: 'hit their usage limit',
  auth: 'have a rejected key',
  no_access: "don't have access on that key",
  busy: 'the provider is temporarily busy',
  unreachable: 'unreachable right now',
  error: 'hit a recent error',
  unsupported: "can't be used for chat at all",
};

function formatRetryEstimate(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return null;
  const minutes = Math.round(ms / 60000);
  if (minutes < 1) return 'less than a minute';
  if (minutes === 1) return 'about a minute';
  if (minutes < 60) return `about ${minutes} minutes`;
  const hours = Math.round(minutes / 60);
  return hours === 1 ? 'about an hour' : `about ${hours} hours`;
}

/**
 * Builds a real "here's why nothing can answer" sentence from router.js's
 * explainExclusions() — named counts and a real retry estimate, replacing
 * the old one-size-fits-all sentence that named none of the actual reasons
 * (see this file's CLAUDE.md-documented history: confirmed live against a
 * roster where 47 of 62 models sat 'unreachable', the generic sentence gave
 * the user nothing to act on beyond "add or fix a model").
 */
function explainNoCandidatesReason(task) {
  const { total, counts, soonestRetryMs } = explainExclusions(task);
  if (!total) {
    return "There are no models set up yet — add one in Model Settings and I'll pick it up automatically.";
  }
  const parts = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([key, n]) => `${n} ${EXCLUSION_LABELS[key] || key}`);
  const retry = formatRetryEstimate(soonestRetryMs);
  return (
    `None of my ${total} models can handle this right now — ${parts.join(', ')}.` +
    (retry
      ? ` The soonest one should be back in ${retry}.`
      : " Add or fix a model in Model Settings and I'll pick it up automatically.")
  );
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
 * Builds the tool list offered to the model this turn/step. `opts.noTools`
 * (a narration-only turn) always wins with an empty list. Otherwise
 * `opts.background === true` (unattended/scheduled runs) drops meta skills
 * (schedule_task, cancel_task, ...) regardless of `allowedTools` — they only
 * make sense in a live conversation. `opts.allowedTools`, when an array,
 * further restricts the list to exactly those names (a scheduled task's own
 * per-task connector allowlist); null/undefined leaves the full list.
 *
 * `unlocked` (a Set<string>, mutated in place across a turn's steps by
 * runOnEntry below) is what find_capability.js surfaces mid-turn — see
 * capabilities.js's getToolDeclarations() for the two-tier split this
 * exists to serve. Declarations otherwise default to the core-only set.
 */
function toolsForTurn(opts, unlocked) {
  if (opts.noTools) return [];
  const declarations = getToolDeclarations({ includeMeta: !opts.background, unlocked });
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
// worth refreshing `checkedAt` occasionally. Success path now uses the same
// discipline (see the call site below) — it used to pass
// `allowStaleRewrite: false`, meaning a model that kept succeeding forever
// still showed the SAME `checkedAt` from whenever it first flipped to
// 'working', so "how recently was this actually confirmed alive" wasn't a
// trustworthy signal (router.js's availability-based ranking, and
// passesAvailabilityCooldown() elsewhere, both lean on `checkedAt` being
// meaningfully fresh).
function shouldRecordAvailability(entry, nextState, allowStaleRewrite) {
  const current = entry?.availability;
  if (!current || current.state !== nextState) return true;
  if (!allowStaleRewrite) return false;
  const checkedAt = current.checkedAt ? Date.parse(current.checkedAt) : 0;
  return !Number.isFinite(checkedAt) || Date.now() - checkedAt > AVAILABILITY_RECHECK_MS;
}

// `technical` (optional) is the raw, redacted provider error text — same
// field, same purpose as server.js's testAndRecord() writes for a manual
// Test click, so an automatic turn failure leaves exactly as much real
// detail behind as a manual one does (see Model Settings' own "Technical
// details" line, which reads this). Before this, every model benched by a
// live turn's own failure carried only the already-friendlied `detail`
// sentence — the same generic wording for every failure of that kind, with
// nothing underneath it to actually diagnose from.
function recordAvailability(modelId, state, detail, { allowStaleRewrite = true, technical = undefined } = {}) {
  try {
    const entry = getModel(modelId);
    if (!entry || !shouldRecordAvailability(entry, state, allowStaleRewrite)) return;
    updateModel(modelId, { availability: { state, checkedAt: new Date().toISOString(), detail, technical } });
  } catch (err) {
    console.error(`[runner] failed to record availability for "${modelId}":`, err);
  }
}

/** Redacts a model's own real secret out of raw adapter error text before it's persisted — mirrors server.js's testAndRecord() (see that function's own comment for why this needs the actual resolved key, not just the generic known-key-shape fallback redactSecrets() applies with no secret at all). */
function technicalDetailFor(entry, err) {
  try {
    const secretValue = entry?.secretRef ? getSecret(entry.secretRef) : null;
    return redactSecrets(err?.message, secretValue ? [secretValue] : []);
  } catch {
    return undefined;
  }
}

/** Runs the tool-calling loop against a single model. Yields chunk/tool events; returns {text}. */
async function* runOnEntry(sessionId, entry, opts) {
  const adapter = getAdapter(entry.adapter);
  const maxSteps = opts.maxToolSteps || DEFAULT_MAX_TOOL_STEPS;
  let finalText = null;
  // Names find_capability.js has surfaced, seeded from two sources: whatever
  // this conversation has EVER unlocked before (sessionUnlockedTools — see
  // its own comment above; this is what makes a find_capability result
  // stick for the rest of the conversation, not just this one message), plus
  // opts.allowedTools if given. Grows across this turn's own steps too
  // (below), and every addition is written back into the session-scoped set
  // so it survives into the NEXT runTurn() call on this session.
  // The allowedTools seeding matters on its own: toolsForTurn's own
  // allowedTools filter only NARROWS whatever getToolDeclarations() already
  // returned, which defaults to core-only without a non-empty `unlocked` —
  // so naming a non-core tool (a connector, or an internal job-only tool
  // like report_job_done) in an explicit allowlist used to have no effect at
  // all, since it never became visible in the first place. An allowlist is
  // itself a clear statement of intent to include exactly those names
  // regardless of core status, so it now pre-unlocks them. Strictly additive
  // versus the old behavior — a caller with no allowedTools (the common
  // case) or one naming only already-core tools sees no change at all.
  const sessionUnlocked = getSessionUnlocked(sessionId);
  const unlocked = new Set([...sessionUnlocked, ...(Array.isArray(opts.allowedTools) ? opts.allowedTools : [])]);
  // Self-Model — every distinct tool name THIS turn has already called, in
  // an earlier step. computeTurnSignals() (server/self/self-model.js) reads
  // this fresh each step below, so a known-failure/no-track-record warning
  // can surface starting the step AFTER a tool's first use within this same
  // turn — see that function's own doc comment for why it can't do better
  // than that with no foreknowledge of what the model is about to call.
  const usedToolNames = new Set();

  // Bounds how long THIS attempt (against this one candidate model, across
  // however many tool-call steps it takes) may run with no response before
  // it's abandoned as a real failure — see FIRST_TOKEN_TIMEOUT_MS/
  // ATTEMPT_TIMEOUT_MS's own comment for why this exists at all. A separate
  // AbortController, not opts.signal directly, so our own timeout and a
  // genuine external cancellation (a newer message superseding this turn —
  // brain.js's per-session coordinator) can be told apart downstream: only
  // `timedOut` below is set by OUR timers, so runTurn's catch can check
  // `opts.signal?.aborted` (untouched by anything in here) to see whether
  // the ORIGINAL signal was the one that fired.
  const attemptController = new AbortController();
  let timedOut = false;
  // Set true only by looksLikeRunawayRepetition()/MAX_REPLY_CHARS below,
  // never by anything else that might abort attemptController — same
  // "tag it plainly so the catch block knows which of OUR triggers this
  // was" discipline `timedOut` already established.
  let runaway = false;
  let runawayReason = '';
  const forwardExternalAbort = () => attemptController.abort();
  if (opts.signal) {
    if (opts.signal.aborted) attemptController.abort();
    else opts.signal.addEventListener('abort', forwardExternalAbort, { once: true });
  }
  const attemptTimer = setTimeout(() => {
    timedOut = true;
    attemptController.abort();
  }, ATTEMPT_TIMEOUT_MS);

  try {
  for (let step = 0; step < maxSteps; step++) {
    // Checked at the top of every step (not just relied on via the adapter
    // call throwing) so a turn superseded by a newer message in the same
    // session (see brain.js's per-session coordinator) stops between steps
    // too, not only while a request is actually in flight. Checks the
    // ORIGINAL opts.signal, not attemptController — a real external
    // supersede must still be recognized as such even if our own timeout
    // happens to have fired in the same instant.
    if (opts.signal?.aborted) {
      throw Object.assign(new Error('Turn superseded by a newer message.'), { aborted: true });
    }
    const tools = toolsForTurn(opts, unlocked);
    const messages = conversation.getMessages(sessionId);
    // Self-Model — cheap SQLite reads only, no model call. Recomputed every
    // step (never cached across the loop) since usedToolNames grows as the
    // turn's own tool calls happen — see root CLAUDE.md's Self-Model
    // section for the trigger design this feeds prompt.js's
    // selfFocusSection() with.
    const selfSignals = computeTurnSignals({
      sessionId,
      correctionDetected: Boolean(opts.correctionDetected),
      usedToolNames: [...usedToolNames],
    });
    // Reasoning integrity's "buffer only the riskiest" half (root
    // CLAUDE.md's Operational Awareness item 1) — the owner's own explicit
    // choice, deliberately the narrowest possible slice: ONLY a step where
    // a REAL matched failure-lesson fired for what this turn already did
    // (selfSignals.knownFailure — which, per self-signals.js's own
    // documented limitation, can only ever be true from the SECOND step of
    // a turn onward, after at least one tool call already happened).
    // Ordinary turns, and even a turn's own FIRST step, stream exactly as
    // before — this never adds latency to the common case.
    const shouldBuffer = Boolean(selfSignals.knownFailure);
    const bufferedChunks = [];
    let text = '';
    let callEvent = null;
    let finalEvent = null;
    // Scoped to this one step's stream — a marker split across a TOOL-CALL
    // boundary would be meaningless anyway, since a new step is a fresh
    // generation, not a continuation of the same text stream. See
    // personality.js's createReactionScanner() for why this exists: the
    // model writes a literal token (e.g. "[[laugh]]") when a real vocal
    // reaction belongs at that point in its reply — this turns that token
    // into a distinct `reaction` event instead of ever letting it reach the
    // transcript or any voice as literal words.
    const reactionScanner = createReactionScanner();

    // Bounds how long THIS step alone may go with no event at all — a
    // separate, shorter budget from the whole-attempt one above, since a
    // hang can happen on any step (including a later tool-round-trip step),
    // not just the very first request. Cleared the instant any event
    // arrives, whatever its type.
    let firstEventSeen = false;
    const firstTokenTimer = setTimeout(() => {
      if (!firstEventSeen) {
        timedOut = true;
        attemptController.abort();
      }
    }, FIRST_TOKEN_TIMEOUT_MS);

    try {
    stepLoop:
    for await (const ev of adapter.stream(entry, messages, { tools, lowConfidence: opts.lowConfidence, gapMs: opts.gapMs, background: opts.background, addressed: opts.addressed, style: opts.style, signal: attemptController.signal, improvementScope: opts.improvementScope, selfSignals })) {
      if (!firstEventSeen) {
        firstEventSeen = true;
        clearTimeout(firstTokenTimer);
      }
      if (ev.type === 'chunk') {
        for (const scanEv of reactionScanner.feed(ev.text)) {
          if (scanEv.type === 'text') {
            text += scanEv.text;
            // Held back, not yielded, only for the narrow buffered slice —
            // see shouldBuffer's own comment above. A reaction event
            // (below) is never delayed by this even on a buffered step: a
            // vocal reaction's own timing is a separate concern from text
            // verification, and desyncing it from the text it reacted to
            // would be its own new bug.
            if (shouldBuffer) bufferedChunks.push(scanEv.text);
            else yield { type: 'chunk', text: scanEv.text };
            // Runaway/repetition guard — see looksLikeRunawayRepetition()'s
            // own header comment. Checked on every chunk but bounded/cheap
            // regardless of total reply length so far (the repetition scan
            // only ever looks at the last REPEAT_WINDOW_CHARS). Aborting
            // here reuses the SAME attemptController the timeouts already
            // use, so whatever's already streamed is cleanly cut off and
            // the catch block below (via `runaway`) benches this model and
            // fails over exactly like a timeout does.
            if (!runaway && (text.length > MAX_REPLY_CHARS || looksLikeRunawayRepetition(text))) {
              runaway = true;
              runawayReason = text.length > MAX_REPLY_CHARS
                ? 'This model kept generating far past a normal reply length.'
                : 'This model got stuck repeating itself.';
              attemptController.abort();
              // Found live, not assumed: aborting attemptController tears
              // down the network connection, but does NOT reliably make
              // every adapter's own `for await` loop actually THROW — the
              // openai SDK's async iterator was observed simply ending
              // cleanly on an aborted stream, same as a real [DONE], which
              // let the adapter fall through to a normal 'final' yield with
              // just the truncated (but still runaway-shaped) text — a
              // "successful" completion that was never really valid, the
              // exact same class of bug Fix 2 exists to prevent. Breaking
              // out HERE and throwing explicitly right after the loop
              // (below) makes this correct regardless of how any given
              // adapter's own abort semantics behave, rather than depending
              // on one.
              break stepLoop;
            }
          } else if (scanEv.type === 'reaction') {
            yield { type: 'reaction', kind: scanEv.kind };
          }
        }
      } else if (ev.type === 'call') {
        callEvent = ev;
      } else if (ev.type === 'final') {
        finalEvent = ev;
      } else if (ev.type === 'usage') {
        // Cost tracking — recorded per step, not just once per turn, so a
        // multi-step tool-calling turn's real total is captured rather than
        // only its last step. See root CLAUDE.md's Cost tracking section.
        recordModelUsage({
          provider: ev.provider || entry.adapter,
          modelId: ev.model || entry.model,
          unitsIn: ev.unitsIn,
          unitsOut: ev.unitsOut,
          cachedIn: ev.cachedIn,
          sessionId,
          background: Boolean(opts.background),
        });
      }
    }
    // The runaway guard's own explicit `break stepLoop` above lands here —
    // never rely on the adapter's own for-await loop having thrown (see
    // that break's own comment for why it doesn't reliably). Thrown here
    // instead, inside the same try block, so it's caught by the catch
    // below exactly like any other failure.
    if (runaway) {
      throw Object.assign(new Error(runawayReason), { runawayOutput: true });
    }
    } catch (err) {
      // Our own deadline fired (attemptController.abort() above), and this
      // was NOT a genuine external supersede — tag it plainly so runTurn's
      // catch treats it as a real failure ("this model never answered") and
      // benches the model, rather than the abort's own generic error text
      // (whatever shape a given SDK happens to throw on a signal abort)
      // getting run through classifyError() and likely landing in 'other'.
      // A genuine external abort (opts.signal.aborted) is left completely
      // untouched here — it still propagates as whatever error it already
      // is, exactly as before this existed.
      if (timedOut && !opts.signal?.aborted) {
        throw Object.assign(new Error('This model took too long to respond.'), { timedOut: true });
      }
      // Same reasoning as the timedOut branch above, for the OTHER trigger
      // that aborts attemptController ourselves — see
      // looksLikeRunawayRepetition()'s own comment. Whatever was already
      // streamed to the caller before this fired is cleaned up the exact
      // same way a timeout's partial output already is (runTurn's own
      // `if (streamed) yield { type: 'restart' }`, unchanged).
      if (runaway && !opts.signal?.aborted) {
        throw Object.assign(new Error(runawayReason), { runawayOutput: true });
      }
      throw err;
    } finally {
      clearTimeout(firstTokenTimer);
    }
    // Anything still held back was never a real marker — just ordinary
    // text that happened to look like the start of one (e.g. "[[" used for
    // emphasis) — flush it as plain text now that the stream is done.
    for (const scanEv of reactionScanner.flush()) {
      text += scanEv.text;
      if (shouldBuffer) bufferedChunks.push(scanEv.text);
      else yield { type: 'chunk', text: scanEv.text };
    }

    // Releasing the buffer, for the narrow slice that was ever held back.
    // A tool-call step (callEvent truthy — this turn isn't over) just
    // releases immediately, in original order, with no check: buffering
    // only matters for a step that's actually about to become the
    // delivered final answer, never an intermediate reasoning step.
    if (shouldBuffer && callEvent) {
      for (const t of bufferedChunks) yield { type: 'chunk', text: t };
    }
    // The real check, only for a step that's both buffered AND about to be
    // the final delivered answer (no callEvent): did the model actually
    // consult the real evidence selfFocusSection() already handed it
    // (check_myself, any call this turn), or is it about to state a
    // conclusion with a known failure pattern in play and never looked?
    // Never withholds the answer either way — flags it, doesn't block it;
    // see this build's own "Open risk" note on why a forced second model
    // round was deliberately NOT built (root CLAUDE.md).
    if (shouldBuffer && !callEvent) {
      const consultedSelfModel = usedToolNames.has('check_myself');
      if (!consultedSelfModel) {
        try {
          appendOpsTrace({
            source: 'verification',
            sourceRef: sessionId,
            phase: 'outcome',
            effect: 'read',
            kind: 'reasoning',
            summary: `A consequential answer followed a known failure pattern (${(selfSignals.matchedScopes || []).join(', ') || 'unspecified scope'}) with no check_myself call this turn to weigh it.`,
            detail: JSON.stringify({ matchedLessons: selfSignals.matchedLessons || [] }),
          });
        } catch (err) {
          console.error('[runner] reasoning-integrity trace failed:', err);
        }
      }
      for (const t of bufferedChunks) yield { type: 'chunk', text: t };
    }

    if (callEvent) {
      conversation.pushAssistantToolCalls(sessionId, callEvent.calls, {
        modelId: entry.id,
        raw: callEvent.raw,
        // stripReactionMarkers() here, not another detection pass — a
        // reaction in this text was already caught and yielded above, via
        // the SAME underlying chunk stream this field is built from
        // (adapter-internal). This only cleans the STORED/echoed text so a
        // raw marker never lands in conversation history or Chat History.
        text: stripReactionMarkers(callEvent.text) || text || undefined,
      });

      const toolResults = [];
      for (const call of callEvent.calls) {
        // `args` rides along so a caller outside this loop (jobs/worker.js's
        // write-ahead trace) can log the intent BEFORE invoke() runs, without
        // this file needing to know Jobs exists — additive field, no existing
        // consumer (public/app.js, the voice engines) destructures anything
        // beyond `type`/`name` off this event today.
        yield { type: 'tool_start', name: call.name, args: call.args };
        // toolsForTurn's allowedTools filter only decides what gets OFFERED
        // to the model — a real model literally can't call a tool it was
        // never given a declaration for, but that's a property of well-
        // behaved function-calling, not something this file enforces. A
        // hallucinated or malformed call naming something outside an
        // explicit allowlist (a scheduled task's own connector restriction,
        // or a background Job's kind-restricted tool set — see
        // jobs/orchestrator.js's KIND_TOOL_NAMES) must not silently execute
        // just because invoke() itself doesn't know about the restriction —
        // confirmed live: a research-kind job's stub test could still reach
        // get_time even though it was never in that kind's allowedTools.
        // report_job_done/report_job_stuck ride in `allowedTools` for every
        // kind precisely so this same check doesn't need a separate carve-out
        // for them.
        const notAllowed = Array.isArray(opts.allowedTools) && !opts.allowedTools.includes(call.name);
        const result = notAllowed
          ? { ok: false, error: 'That capability is not available for this task.' }
          : await invokeWithTimeout(call.name, call.args, {
              sessionId,
              modelId: entry.id,
              lowConfidence: opts.lowConfidence,
              // Unattended runs (scheduled tasks, briefing sources) were
              // consented to once at creation — nobody is present to answer a
              // live read-back, so the confirmation gate is bypassed rather
              // than stalling on a token nobody will ever send back.
              autoConfirm: Boolean(opts.autoConfirm),
              // find_capability.js reads this to keep its own search scoped to
              // non-meta capabilities on a background run, matching what a
              // background run could always reach before the two-tier tool
              // split existed (see that file's comment).
              background: Boolean(opts.background),
              // The third confirm mode (server/jobs/worker.js) — present only
              // for a background Job's own turn; absent (undefined) for every
              // other caller, so capabilities.js's invoke() falls through to
              // its existing token-minting behavior exactly as before for
              // live chat and scheduled tasks alike.
              onEscalate: opts.onEscalate,
              // Self-Model dimension 4 ("what's it doing now, and why") —
              // check_myself.js reports Personality's OWN already-computed
              // decision, never makes one itself; see self-model.js's header
              // invariant.
              style: opts.style,
              // The same-turn confirm-token refusal — see this file's own
              // turnId comment (runTurn()) and capabilities.js's
              // consumePendingToken() for the full reasoning.
              turnId: opts.turnId,
              // Utterance provenance (server/self/self-verify.js) —
              // check_myself.js uses this exact id to link its own snapshot
              // and citation rows back to the real tool_call entry
              // persisted in chat-store.js's messages table, so a later
              // verifyCitation() call can find the real reply that followed
              // this specific call, not just any check_myself call ever.
              toolCallId: call.id,
            });
        // Self-Model capture — zero model calls, a rolling reliability
        // tally plus (only for a notable outcome) one more
        // improvement_outcomes row for Self-Improvement's existing
        // pipeline. Never allowed to affect this turn if it throws — same
        // discipline as noteCorrection() above.
        try {
          recordToolOutcome({ name: call.name, ok: result?.ok !== false, notAllowed, escalated: Boolean(result?.escalated) });
        } catch (err) {
          console.error('[runner] self-model capture failed:', err);
        }
        usedToolNames.add(call.name);
        // find_capability just ran — fold its matches into the set for the
        // NEXT step's tool list (see toolsForTurn's `unlocked` param above).
        // Not reachable on any other tool, since only find_capability.js
        // returns a `matches` array shaped like this.
        if (call.name === 'find_capability' && Array.isArray(result?.matches)) {
          for (const m of result.matches) {
            if (m?.name) {
              unlocked.add(m.name);
              // Persist past this single turn — see sessionUnlockedTools above.
              sessionUnlocked.add(m.name);
            }
          }
        }
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

    // stripReactionMarkers() — same reasoning as callEvent.text above: the
    // reaction itself was already yielded from the live chunk stream; this
    // only keeps a raw marker out of the STORED text (conversation history,
    // Chat History). stripReactionMarkers(undefined) returns undefined
    // unchanged, so this preserves the original `??` fallback to `text`
    // exactly when finalEvent has no text of its own.
    finalText = stripReactionMarkers(finalEvent?.text) ?? (text || "Sorry, I didn't quite catch that.");
    conversation.pushAssistantText(sessionId, finalText, { modelId: entry.id, raw: finalEvent?.raw });
    break;
  }
  } finally {
    clearTimeout(attemptTimer);
    opts.signal?.removeEventListener('abort', forwardExternalAbort);
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
 *   {type:'style_floors', floors, sticky}     — only when personality.js's readStyle()
 *                                                found at least one floor for this turn;
 *                                                a debug-only signal (see public/settings.js's
 *                                                toggle), never something the model itself sees
 *   {type:'chunk', text}
 *   {type:'reaction', kind}                   — a real, non-verbal vocal cue belongs here (currently
 *                                                only kind:'laugh') — see personality.js's
 *                                                createReactionScanner(); the model's own literal
 *                                                token is stripped before it ever reaches this event
 *                                                or any 'chunk' text, so it's never spoken or shown
 *                                                as words. A playback engine queues a real sound clip
 *                                                for it; a consumer that doesn't care (the transcript)
 *                                                just ignores it, same as 'style_floors'.
 *   {type:'tool_start', name} / {type:'tool_result', name, ok}
 *   {type:'model_switch', from, to, reason}   — only when a fallback actually happens
 *   {type:'restart'}                          — clear any partial reply already shown; a fresh one follows
 *   {type:'done', text, modelId}
 *   {type:'paused', reason}                   — nothing could complete the turn; no answer was produced
 */
export async function* runTurn(sessionId, userText, opts = {}) {
  // Read the PREVIOUS last message's timestamp before pushUserText() below
  // adds a new one — this is what lets prompt.js's situationSection() tell
  // the model how long it's actually been since the last exchange (a model
  // otherwise has no clock at all; see the root CLAUDE.md's Model system
  // section for why that mattered). null on the very first turn of a
  // session, or if that message predates createdAt existing on messages.
  const priorMessages = conversation.getMessages(sessionId);
  const lastPrior = priorMessages[priorMessages.length - 1];
  const gapMs = lastPrior?.createdAt ? Date.now() - Date.parse(lastPrior.createdAt) : null;
  // readStyle() is pure/cheap (regex over this turn's own text, personality.js) —
  // computed unconditionally and gated later in prompt.js by background/addressed,
  // same pattern as gapMs above. Skipped when systemOverride is set (the control
  // loop's own instruction is a different task entirely — see prompt.js).
  const style = opts.systemOverride ? undefined : readStyle(sessionId, userText);
  // Self-Improvement correction capture — live conversation only (a
  // scheduled task or a Job worker's own turn was never corrected by
  // anyone; both set opts.background:true, the same signal jobsSection()
  // and the style framework already gate on). No model call, cheap regex —
  // safe to run unconditionally rather than threading a separate opt.
  // Captured (not just fired-and-discarded) so the Self-Model's own
  // 'correction' signal (server/self/self-signals.js) can reuse this exact
  // regex match rather than re-running a second copy of
  // CORRECTION_PATTERNS — see root CLAUDE.md's Self-Model section.
  let correctionDetected = false;
  if (!opts.background) {
    try {
      correctionDetected = Boolean(noteCorrection(sessionId, userText));
    } catch (err) {
      console.error('[runner] self-improvement correction capture failed:', err);
    }
  }
  // One id per runTurn() call, stable across every step/model-switch this
  // ONE turn takes — capabilities.js's consumePendingToken() uses this to
  // refuse a confirm_token redeemed in the SAME turn that minted it, so a
  // model can never complete an entire ask-and-answer confirm round trip
  // with no real human reply in between. Found live, not hypothetical: see
  // capabilities.js's own header comment on consumePendingToken() for the
  // full investigation. Threaded through exactly like gapMs/style/
  // correctionDetected above — one value, computed once, reused by every
  // candidate model this turn tries.
  const turnId = `turn${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const situationOpts = { ...opts, gapMs: Number.isFinite(gapMs) ? gapMs : null, style, correctionDetected, turnId };
  // Debug-only signal (public/settings.js's toggle gates whether the UI shows it) —
  // never read by the model itself, purely for the user to confirm a floor actually
  // fired. Only yielded when something did, so the common case emits nothing extra.
  if (style && Object.values(style.floors).some(Boolean)) {
    yield { type: 'style_floors', floors: style.floors, sticky: style.sticky };
  }

  // `media` rides on the user's own message, so it stays in the transcript
  // and every later turn still sees it — the same way Claude keeps an image
  // in a conversation (see attachments.js for why images aren't digested).
  conversation.pushUserText(sessionId, userText, { media: opts.media });
  const task = profileTask({ text: userText, source: opts.source, background: opts.background, type: opts.type });
  const allCandidates = buildCandidateList(task, opts, sessionId);

  if (allCandidates.length === 0) {
    yield {
      type: 'paused',
      reason: noCapableModelReason(opts.need) || explainNoCandidatesReason(task),
    };
    return;
  }

  // Cap how many models a single turn will walk through on failure — see
  // MAX_FALLBACK_ATTEMPTS's comment. The full ranked list stays available
  // to the NEXT turn (health/availability cooldowns move on independently),
  // this only bounds how long any one turn makes the user wait.
  const candidates = allCandidates.slice(0, MAX_FALLBACK_ATTEMPTS);

  let previousId = null;
  let lastError = null;

  for (const entry of candidates) {
    if (previousId) {
      yield { type: 'model_switch', from: previousId, to: entry.id, reason: lastError || 'the previous model ran into a problem' };
    }

    const gen = runOnEntry(sessionId, entry, situationOpts);
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
      // Either an abort (superseded by a newer message in the same
      // session — brain.js's per-session turn coordinator) or a genuine
      // failure can leave a tool-call message with no matching result, if
      // it happened between pushAssistantToolCalls and pushToolResults
      // inside runOnEntry — clean that up regardless of which one this
      // was. Safe to call unconditionally: a no-op whenever the last
      // message isn't actually an orphaned tool call (the common case,
      // since most stops happen before any tool call this step at all).
      conversation.removeLastOrphanedToolCall(sessionId);
      // Superseded, not a real model failure: no health penalty, and
      // whatever text/tool-calls this step was mid-generating was never
      // pushed (the throw happened before reaching those push calls). Stop
      // silently — the coordinator that aborted us is the one running the
      // merged retry, on its own connection.
      if (opts.signal?.aborted) return;
      failed = true;
      console.error(`[runner] model "${entry.id}" failed:`, err);
      if (err?.timedOut) {
        // runOnEntry's own first-token/attempt deadline fired — a real
        // failure (the model never responded in time), not a supersede, but
        // ALSO not something classifyError() can read anything useful from
        // (an abort's own error text carries no provider-specific signal).
        // Tagged 'transient' -> 'busy': a short cooldown, since a hang is
        // just as likely to be a momentary provider stall as anything
        // permanent, and this is exactly the case that used to leave a
        // hung model's 'working' badge (and its sticky-session lead)
        // untouched forever — see FIRST_TOKEN_TIMEOUT_MS's own comment for
        // the live repro this fixes.
        markUnhealthy(entry.id, 'No response in time.', 'transient');
        recordAvailability(entry.id, 'busy', 'No response in time.', { technical: 'The model never sent a response within the time limit.' });
        lastError = 'That model took too long to respond.';
      } else if (err?.runawayOutput) {
        // runOnEntry's own repetition/length guard fired — same reasoning
        // as the timedOut branch above: not something classifyError() can
        // read a provider-specific signal from, and err.message is already
        // the real, honest reason (see looksLikeRunawayRepetition()'s own
        // comment). 'transient' -> a short cooldown: a model that loops
        // once on one turn isn't necessarily broken for every future turn.
        markUnhealthy(entry.id, err.message, 'transient');
        recordAvailability(entry.id, 'busy', err.message, { technical: err.message });
        lastError = err.message;
      } else {
        const kind = classifyError(err);
        markUnhealthy(entry.id, err?.message || 'error', kind);
        recordAvailability(entry.id, AVAILABILITY_STATE_FOR_KIND[kind] ?? 'unreachable', err?.message || 'error', {
          technical: technicalDetailFor(entry, err),
        });
        lastError = friendlyReason(entry.adapter, err);
      }
      // A model that just failed must not keep leading the NEXT turn's
      // ranking via sessionStickyModel — otherwise a once-good model that
      // starts hanging/erroring keeps winning preferredModelId() forever,
      // since stickiness outranks pure auto-ranking (see that function's
      // own comment).
      if (sessionStickyModel.get(sessionId) === entry.id) sessionStickyModel.delete(sessionId);
      previousId = entry.id;
      if (streamed) yield { type: 'restart' };
    }

    if (!failed && finalResult) {
      markHealthy(entry.id);
      recordAvailability(entry.id, 'working', null);
      // Remember who actually answered — see preferredModelId()'s sticky
      // tier above. Only meaningful when auto-select is on (a manual pick
      // already outranks it), but harmless to always record.
      sessionStickyModel.set(sessionId, entry.id);
      yield { type: 'done', text: finalResult.text, modelId: entry.id };
      return;
    }
  }

  // Every candidate this turn actually tried has, by now, had its own
  // markUnhealthy/recordAvailability call run synchronously (see the catch
  // block above) — so re-running explainExclusions() here reflects the
  // roster INCLUDING what just failed, not a stale pre-turn snapshot.
  // Wording says "a few" rather than "every model I have available" since
  // MAX_FALLBACK_ATTEMPTS means this is no longer literally the whole list —
  // the ranked list itself, and the user's next turn, are unaffected by
  // this turn giving up early.
  yield {
    type: 'paused',
    reason:
      `I tried a few models and none of them could finish that — most recent problem: ` +
      `${lastError || 'unknown error'}. ${explainNoCandidatesReason(task)}`,
  };
}

export function resetConversation(sessionId = 'main') {
  conversation.resetSession(sessionId);
  sessionStickyModel.delete(sessionId);
  sessionUnlockedTools.delete(sessionId);
  clearStyleSession(sessionId);
}
