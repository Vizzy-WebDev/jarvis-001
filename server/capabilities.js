// The composition seam for everything a model (or a pipeline, or a task
// action) can call. Three sources merge here, and here only:
//
//   - built-in tools       (server/tools/  — real executable JS, "run_code",
//                            "open_app", "get_weather", ...)
//   - folder Skills        (server/skills/ — SKILL.md instructions, plus a
//                            future skill.toml pipeline; data/skills/<name>/)
//   - connector tools      (server/connectors/ — MCP/API/CLI/browser/files)
//
// This file replaces the merging + confirmation logic that used to live
// inside server/skills/index.js. That file did three unrelated jobs at once
// (load built-in code skills, merge in folder Skills and connector tools,
// own the confirm-and-read-back gate) under one name that also meant "a
// folder of instructions" — which is why built-in abilities kept getting
// treated as if they belonged in a Skills browsing UI. Splitting the loader
// (server/tools/index.js), the folder-Skill store (server/skills/index.js),
// and this seam apart means there is now exactly one function
// (`listStepCandidates()`) that can honestly enumerate "real capabilities a
// pipeline step could call" — built on server/tools/, which structurally
// cannot return a folder Skill or a connector tool, the same way
// listUserSkills() structurally cannot return a built-in.
//
// server/tools/ and server/skills/ must never import this file (or each
// other in that direction) — see CLAUDE.md's circular-import invariant. A
// capability file that needs something only this seam can provide (e.g.
// create_skill.js needing the reserved-name set) receives it via `ctx`,
// injected by invoke() below, never via a top-level import.

import { rawTools, listTools, reservedToolNames } from './tools/index.js';
import { listFolderSkillTools, getFolderSkillTool } from './skills/index.js';
import { getToolDeclarations as getConnectorDeclarations, runConnectorTool } from './connectors/index.js';

// ---------- connector tools, wrapped to the same runnable shape as a built-in ----------
//
// A connector tool (browser/files/mcp/api/cli) reaches the model exactly
// like a built-in or a folder Skill: connectors/index.js already worked out
// its risk tier and confirm requirement, so this is just a thin wrapper
// handing that same shape to the merge below and routing run() to
// runConnectorTool().
function connectorCapabilities() {
  return getConnectorDeclarations().map((d) => ({
    name: d.name,
    description: d.description,
    parameters: d.parameters,
    confirm: d.confirm,
    meta: false,
    kind: 'connector',
    async run(args) {
      try {
        return await runConnectorTool(d.name, args);
      } catch (err) {
        return { ok: false, error: err?.message || 'That connector ran into a problem.' };
      }
    },
  }));
}

/** Every built-in tool as a runnable {name, description, parameters, confirm, meta, kind, run} object — the full internal shape, before it's narrowed for a model or a UI. */
function builtInCapabilities({ includeMeta }) {
  return Array.from(rawTools().values())
    .filter((t) => includeMeta || !t.meta)
    .map((t) => ({ ...t, kind: 'builtin' }));
}

/** Folder Skills as runnable objects, same shape as the above two — see server/skills/index.js's listFolderSkillTools(). */
function folderSkillCapabilities() {
  return listFolderSkillTools();
}

function allCapabilities({ includeMeta }) {
  return [...builtInCapabilities({ includeMeta }), ...folderSkillCapabilities(), ...connectorCapabilities()];
}

/**
 * Tool declarations in the shape every model adapter expects — internal
 * fields (confirm, summarize, meta, run, ...) never leak into this.
 * `includeMeta: false` drops meta tools/skills — they only make sense in a
 * live conversation, so an unattended/background turn (a scheduled task's
 * own prompt action) shouldn't be handed them. Default stays `true` so
 * existing callers (live chat, via runner.js) are unaffected.
 *
 * `unlocked` (optional Set<string>, or the literal `true`) additionally
 * includes non-core capabilities — see find_capability.js and runner.js's
 * per-step tool list. A Set includes only those names; `true` includes
 * EVERYTHING (the pre-slimming behavior) — for a caller with no per-turn way
 * to expand its own tool list after the fact, e.g. live.js's Gemini Live
 * session, which sets its tools once at ai.live.connect() time with no
 * per-turn refresh; find_capability's "discover, then call it next step"
 * flow has nothing to attach to there, so it stays opted out of the split
 * entirely rather than being silently capped at the core set with no way to
 * reach anything else. Without `unlocked` at all, only capabilities tagged
 * `core: true` (a built-in tool file's own field — folder Skills and
 * connector tools are never core) are returned, alongside find_capability
 * itself.
 *
 * This two-tier split exists because sending every capability on every turn
 * was measured at 81 declarations / ~160,000 characters (~40k tokens) — on
 * "hello" as much as on anything else. A model reading that much boilerplate
 * before it reads the actual system instruction, on every single turn,
 * produces exactly the flat/ignoring-instructions behavior this was built to
 * fix.
 */
export function getToolDeclarations({ includeMeta = true, unlocked } = {}) {
  const all = allCapabilities({ includeMeta });
  const visible =
    unlocked === true
      ? all
      : unlocked && unlocked.size
        ? all.filter((c) => c.core || unlocked.has(c.name))
        : all.filter((c) => c.core);
  return visible.map((c) => ({
    name: c.name,
    description: c.description,
    parameters: c.confirm ? withConfirmToken(c.parameters) : c.parameters,
  }));
}

/**
 * `confirm_token` was, until this fix, only ever mentioned as PROSE in
 * prompt.js's SYSTEM_INSTRUCTION ("call the tool again with confirm_token
 * set to the value you were given") — no individual capability's own
 * `parameters` schema ever declared it as a real, visible argument, on
 * ANY of the 15+ built-in confirm-gated tools OR a folder Skill's own
 * pipeline-generated confirm (skills/index.js sets `confirm: 'always'`
 * there too). A model that sticks to its own declared function-calling
 * schema — confirmed live to be common on weaker/free-tier models (see
 * root CLAUDE.md's Self-Model section) — had nowhere to actually put the
 * token even after being told to send it back, so a genuine "yes" from the
 * user could silently go nowhere: `consumePendingToken()` correctly found
 * no token, `requiresConfirmation()` fired again, and the model looked
 * stuck in an infinite confirmation loop it had no way out of. Verified
 * live, isolated from any model-behavior question: the confirm/token
 * machinery itself round-trips a real token perfectly once one is actually
 * sent (see server/self/CLAUDE.md's investigation of this exact bug).
 *
 * Fixed HERE, once — the one place every capability's schema is actually
 * built for a model to see — rather than in every individual tool file,
 * which also wouldn't have reached a folder Skill's own generated confirm
 * step. `confirm_token` is added as an OPTIONAL property only (never
 * pushed into `required`) — a first call still shouldn't need it. Never
 * mutates a tool's own shared `parameters` object; `cleanArgs` in `invoke()`
 * below already stripped `confirm_token` out before a capability's `run()`
 * ever sees its args, so this schema addition needs no matching change
 * there.
 */
function withConfirmToken(parameters) {
  const base = parameters && typeof parameters === 'object' ? parameters : { type: 'object', properties: {} };
  return {
    ...base,
    properties: {
      ...(base.properties || {}),
      confirm_token: {
        type: 'string',
        description:
          'Only set this when resending a call after the user has already said yes to a confirmation you asked for — the exact token you were given back then, nothing else.',
      },
    },
  };
}

// Very small, deliberately conservative stemmer — normalizes a handful of
// common verb/plural endings (typing/typed/types -> typ) so a query using
// one word form matches a description using another. Applied to both the
// query and the haystack, so the match is symmetric either direction.
// Confirmed live gap this closes: "type text into notepad" scored 0 against
// control_computer.js's description, which says "typing" — a real word,
// just not the one the user said. Not a real stemmer (no dictionary, no
// irregular forms) — just enough to stop the most common verb-form miss.
function stem(word) {
  if (word.length > 5 && word.endsWith('ing')) return word.slice(0, -3);
  if (word.length > 4 && word.endsWith('ed')) return word.slice(0, -2);
  if (word.length > 4 && word.endsWith('es')) return word.slice(0, -2);
  if (word.length > 3 && word.endsWith('s') && !word.endsWith('ss')) return word.slice(0, -1);
  return word;
}

/**
 * Simple keyword-overlap search over every NON-core capability (built-in
 * tools, folder Skills, connector tools alike) — what find_capability.js
 * calls, via ctx injection (it cannot import this module directly; see this
 * file's header comment and create_skill.js's identical need for
 * reservedSkillNames). No embeddings, no extra model call — this only has to
 * beat "not knowing the tool exists at all", and a name+description overlap
 * score is cheap and good enough for that. `excludeNames` additionally
 * drops anything already unlocked this turn, so repeated searches don't
 * keep re-surfacing what the model can already call.
 */
export function searchCapabilities(query, { includeMeta = true, limit = 8, excludeNames } = {}) {
  const q = String(query || '').toLowerCase();
  // length > 2 drops stopwords ("a", "to", "of", "is", ...) — without this,
  // a 1-2 letter query word matches almost every description as a bare
  // substring (e.g. "a" is inside the vast majority of English sentences),
  // which drowned out the words that actually carried meaning. Found live:
  // searching "schedule a reminder tomorrow" ranked schedule_task behind
  // unrelated tools that happened to share more short, meaningless words.
  const queryWords = q.split(/[^a-z0-9]+/).filter((w) => w.length > 2);
  if (!queryWords.length) return [];
  const queryStems = queryWords.map(stem);

  const candidates = allCapabilities({ includeMeta }).filter(
    (c) => !c.core && c.name !== 'find_capability' && !excludeNames?.has(c.name)
  );

  const scored = candidates
    .map((c) => {
      const haystack = `${c.name.replace(/_/g, ' ')} ${c.description || ''}`.toLowerCase();
      const haystackStems = new Set(haystack.split(/[^a-z0-9]+/).filter(Boolean).map(stem));
      // Word-boundary match, not a bare substring — otherwise a query word
      // like "an" or "or" matches as a fragment inside unrelated longer
      // words (e.g. "an" inside "channel"), the same false-positive class
      // the length filter above addresses, just for longer words. An exact
      // match scores a full point; a stemmed-only match (the query's verb
      // form differs from the description's) scores half, so a literal
      // match still outranks a looser one on a tie.
      let score = 0;
      for (let i = 0; i < queryWords.length; i++) {
        const w = queryWords[i];
        if (new RegExp(`\\b${w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(haystack)) score += 1;
        else if (haystackStems.has(queryStems[i])) score += 0.5;
      }
      return { c, score };
    })
    .filter((s) => s.score > 0)
    .sort((a, b) => b.score - a.score);

  // Capped, not the full raw description — confirmed live source of the
  // connector-hallucination problem: one connector's own cached MCP tool
  // description ran to 4,500+ characters and, ~1,500 characters in,
  // asserted (in that service's own voice) which apps ITS account has
  // connected — text with nothing to do with what the tool actually does,
  // that a search result would otherwise hand the model verbatim before
  // the tool is ever even unlocked. A plain length cap (not a
  // first-sentence cut — some real descriptions carry load-bearing schema
  // guidance past the first sentence) keeps enough for the model to judge
  // relevance without carrying arbitrarily long, unvetted text from a
  // remote service. The FULL description is still exactly what reaches the
  // model once a tool is actually unlocked and declared via
  // getToolDeclarations() below — this cap applies only to the discovery
  // step, where the full text was never needed anyway.
  const SEARCH_DESCRIPTION_CAP = 300;
  return scored.slice(0, limit).map(({ c }) => {
    const full = c.description || '';
    const description = full.length > SEARCH_DESCRIPTION_CAP ? `${full.slice(0, SEARCH_DESCRIPTION_CAP)}…` : full;
    return { name: c.name, kind: c.kind, description, parameters: c.parameters };
  });
}

/**
 * Every runnable capability — built-in tool, folder Skill, or connector tool
 * — tagged with `kind: 'builtin' | 'skill' | 'connector'`. A general-purpose
 * enumeration (used by the task/briefing action pickers, where offering a
 * built-in ability alongside a user Skill is legitimate — see
 * server/tools/schedule_task.js). **It is NOT safe for a Skills-only UI to
 * filter this down by `kind` and call it done** — use `listUserSkills()`
 * (./skills/store/skill-files.js) directly for that, per CLAUDE.md's
 * permanent Skills rule: a Skills screen must be built on a source that
 * cannot structurally return a native ability, not on a filtered version of
 * this.
 */
export function listCapabilities({ includeMeta = false } = {}) {
  // `internal` (e.g. server/tools/report_job_done.js) is a DIFFERENT axis
  // from `meta`: an internal tool must still reach a background Job's own
  // model turn (which runs with background:true, and includeMeta:false
  // already strips every meta:true tool before allowedTools is even
  // considered — see runner.js's toolsForTurn/builtInCapabilities), so it
  // can never be marked meta itself. It's excluded here instead — this is
  // the task/briefing action picker, and "report the job done" is never a
  // sensible standalone action for either of those to select.
  return allCapabilities({ includeMeta })
    .filter((c) => !c.internal)
    .map((c) => ({
      name: c.name,
      description: c.description,
      parameters: c.parameters,
      meta: Boolean(c.meta),
      kind: c.kind,
    }));
}

/**
 * Built-in tools only, non-meta, non-internal — the honest enumeration a
 * pipeline step picker (skill.toml's `tool = "..."`) is built on.
 * Structurally cannot return a folder Skill (no pipeline-calls-pipeline), a
 * meta tool (schedule a task, remember something — those only make sense in
 * live conversation, never as one fixed step of an automatically-run
 * sequence), or an internal job-only tool (report_job_done/report_job_stuck
 * mean nothing as a standalone pipeline step). Reads rawTools() directly
 * rather than tools/index.js's own listTools() — that function's fixed
 * output shape doesn't carry `internal` through, and duplicating its
 * meta-filter here is simpler than teaching it a second custom field only
 * this one caller needs.
 */
export function listStepCandidates() {
  return Array.from(rawTools().values())
    .filter((t) => !t.meta && !t.internal)
    .map((t) => ({
      name: t.name,
      description: t.description,
      parameters: t.parameters,
    }));
}

export function hasCapability(name) {
  if (rawTools().has(name)) return true;
  if (getFolderSkillTool(name)) return true;
  return connectorCapabilities().some((c) => c.name === name);
}

// ---------- managing installed Skills (Skills screen) ----------
//
// This file doesn't do any of the actual file I/O for install/edit/remove —
// server.js's routes call server/skills/store/skill-files.js and
// skill-zip.js directly for that. What lives here is just the one thing
// that store can't know on its own: which tool names are already reserved
// by a built-in tool or an active connector, so a new Skill's name never
// collides with one. (Folder-Skill-to-folder-Skill collisions are checked
// separately, inside skill-files.js's own uniqueName(), against the
// filesystem directly.)

/** Every reserved tool name a new Skill must not collide with — every built-in tool, every active connector tool. */
export function reservedSkillNames() {
  return new Set([...reservedToolNames(), ...connectorCapabilities().map((c) => c.name)]);
}

// ---------- confirmation layer ----------
//
// Moved verbatim from the old server/skills/index.js (not rewritten) — see
// consumePendingToken()'s doc comment for the real bug this shape fixes: an
// earlier version compared the resent arguments against the originals
// byte-for-byte and broke for any complex/nested argument shape, producing
// what looked like an infinite confirmation loop. The token itself (random,
// single-use, capability-scoped, 5-minute TTL) is the whole proof of
// consent now; nothing about the resent args is trusted or required.

const pending = new Map(); // token -> { name, args, expiresAt, mintedTurnId }
const CONFIRM_TTL_MS = 5 * 60 * 1000;

function makeToken() {
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

function requiresConfirmation(capability, ctx) {
  // Unattended runs (scheduled tasks, briefing sources) were consented to
  // once at creation time — there is nobody present to answer a live
  // read-back, so the gate is bypassed entirely rather than stalling the
  // run on a confirm_token nobody will ever send back. Interactive chat
  // (pipeline/live engines) never sets this, so the normal read-back flow
  // there is unaffected.
  if (ctx?.autoConfirm) return false;
  if (!capability.confirm || capability.confirm === 'never') return false;
  if (capability.confirm === 'ifUnclear') return Boolean(ctx?.lowConfidence);
  return true; // 'always'
}

/**
 * The pending record for a valid, unexpired token belonging to THIS
 * capability, or null. Consumes the token either way (one-time use) once
 * it's looked up at all — a token is meant to represent one specific "yes",
 * not a reusable key. Deliberately does NOT compare the resent arguments
 * against what was originally asked about — see this module's header
 * comment.
 *
 * **A token may only be redeemed in a LATER turn than the one that minted
 * it — never the same one.** Found live, not hypothetical: a confirm-gated
 * tool used to be structurally incapable of completing at all in one turn
 * (no tool schema ever declared `confirm_token` — see getToolDeclarations()'s
 * withConfirmToken()), which accidentally acted as a safety net. Fixing
 * that schema gap removed the accident and exposed the real one underneath
 * it: with `confirm_token` now a real, callable argument, a model told
 * "skip asking me" could mint the token and immediately resend it in the
 * SAME turn, completing the entire ask-and-answer round trip with no real
 * human reply in between — confirmed live on a real free-tier model, real
 * production data (server/tools/CLAUDE.md's "Voice-clarity confirmation"
 * section records the investigation). The whole point of asking is a genuine pause for a
 * genuine separate reply; nothing enforced that pause actually span two
 * different turns. This is that enforcement. Only applied when BOTH sides
 * carry a real `ctx.turnId` — a caller outside the per-turn system (neither
 * side sets one) falls back to the original, turn-unaware behavior rather
 * than being silently, incorrectly blocked.
 */
function consumePendingToken(capability, rawArgs, ctx) {
  const token = rawArgs?.confirm_token;
  if (!token) return null;
  const record = pending.get(token);
  if (!record) return null;
  pending.delete(token); // one-time use either way
  if (record.name !== capability.name) return null;
  if (Date.now() > record.expiresAt) return null;
  if (record.mintedTurnId && ctx?.turnId && record.mintedTurnId === ctx.turnId) {
    // Same turn that minted it — treated exactly like an invalid token:
    // the caller falls through to minting a FRESH one and asking again,
    // this time for real, in whatever turn actually replies.
    return null;
  }
  return record;
}

/** Finds a runnable capability by name — a built-in tool first, then a currently-enabled folder Skill, then a connector tool (a disabled/removed one of either is treated as unknown, same as if it never existed). */
function findCapability(name) {
  if (rawTools().has(name)) return rawTools().get(name);
  const folderTool = getFolderSkillTool(name);
  if (folderTool) return folderTool;
  return connectorCapabilities().find((c) => c.name === name) || null;
}

/**
 * Invokes a named capability with the given arguments. `ctx` (optional)
 * carries {sessionId, modelId, lowConfidence, autoConfirm, turnId} — `turnId`
 * (models/runner.js's own per-runTurn()-call id) is what lets
 * consumePendingToken() refuse a token redeemed in the same turn that
 * minted it; a caller that never sets it just gets the original,
 * turn-unaware behavior — plus
 * `reservedSkillNames`, `searchCapabilities`, and `listCapabilities` (this
 * module's own functions, injected below) — a tool file under server/tools/
 * can't import any of them from this module directly (that's the seam that
 * composes it; see CLAUDE.md's circular-import invariant), so this is how
 * create_skill.js, find_capability.js, and check_myself.js (dimension 1's
 * live capability count — see server/self/CLAUDE.md) get them without
 * deadlocking the dynamic-import loader at startup. Never throws.
 */
export async function invoke(name, args, ctx = {}) {
  const capability = findCapability(name);
  if (!capability) {
    return { ok: false, error: `Unknown skill: ${name}` };
  }

  const cleanArgs = { ...(args || {}) };
  delete cleanArgs.confirm_token;

  try {
    if (requiresConfirmation(capability, ctx)) {
      const confirmed = consumePendingToken(capability, args || {}, ctx);
      if (confirmed) {
        // Run with the ORIGINAL args captured when confirmation was asked
        // for, not whatever the model just resent — see
        // consumePendingToken()'s doc comment for why.
        return await capability.run(confirmed.args, { ...ctx, reservedSkillNames, searchCapabilities, invoke, listCapabilities });
      }
      // The third confirm mode, alongside the interactive read-back below
      // and ctx.autoConfirm above: a background Job (server/jobs/worker.js)
      // is neither interactive (nobody is present to answer a live prompt)
      // nor pre-consented (unlike a scheduled task, nobody agreed to this
      // specific action up front) — ctx.onEscalate parks the decision and
      // sends it upward instead of minting a confirm_token nobody will ever
      // resend. No token is minted on this path at all: a background job
      // may wait hours, far past CONFIRM_TTL_MS, and the durable record of
      // consent is the job's own outbox entry, not a short-lived token.
      if (typeof ctx.onEscalate === 'function') {
        const summary = typeof capability.summarize === 'function' ? await capability.summarize(cleanArgs) : capability.description;
        await ctx.onEscalate({ name, args: cleanArgs, summary });
        return { ok: false, escalated: true, error: 'Waiting on the owner to decide. Stop and report back — do not ask again this turn.' };
      }
      const token = makeToken();
      pending.set(token, { name: capability.name, args: cleanArgs, expiresAt: Date.now() + CONFIRM_TTL_MS, mintedTurnId: ctx?.turnId || null });
      // `await` here works whether summarize() is sync (every existing
      // capability) or async (control_computer.js generates its plan via a
      // real model call before it has anything to read back) — awaiting a
      // plain string is a no-op, so this changes nothing for callers that
      // were already sync.
      const summary = typeof capability.summarize === 'function' ? await capability.summarize(cleanArgs) : capability.description;
      return { ok: true, needs_confirmation: true, summary, confirm_token: token };
    }

    // `invoke` is injected into ctx alongside reservedSkillNames, for the
    // same reason: a capability that itself needs to dispatch further calls
    // (server/skills/index.js's folderSkillToTool(), for a skill.toml
    // pipeline's tool-kind steps — see server/skills/pipeline.js) cannot
    // import this module (capabilities.js -> skills/index.js ->
    // pipeline.js -> capabilities.js would be exactly the deadlock the
    // circular-import invariant forbids) — so it receives this same
    // function back through ctx instead.
    return await capability.run(cleanArgs, { ...ctx, reservedSkillNames, searchCapabilities, invoke, listCapabilities });
  } catch (err) {
    console.error(`[capabilities] "${name}" threw:`, err);
    return { ok: false, error: `Something went wrong running ${name}.` };
  }
}
