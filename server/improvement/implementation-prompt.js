// Turns an approved 'skill' or 'code' proposal into a ready-to-paste brief
// for whichever coding assistant the user is using — Jarvis never writes or
// edits its own code, full stop; this is the entire mechanism by which an
// idea that needs real code reaches the user instead. `target` is free
// text the user types in on the screen (their own coding assistant's
// name) — deliberately never a hardcoded list, since the user may switch
// tools or use one that was never anticipated.
//
// NEVER reads the repo — the static ARCHITECTURE_BRIEFING below is a
// hand-written, deliberately compact summary of the rules that matter for
// ANY change to this codebase (the circular-import invariant, the manual-
// verification discipline, the module layout), written once and kept here
// rather than the model attempting to inspect the real source tree it has
// no access to from this call. One askModel call, on demand (the user
// clicking "Generate a prompt") — not part of the daily/weekly automatic
// budget, since this only ever runs when the user actually asks for it.

import { askModel } from '../ai.js';
import { getProposal, setProposalImplementation } from './improvement-store.js';

const ARCHITECTURE_BRIEFING = `Jarvis is a local Node.js + Express server with a plain ES-module front end (no build step, no framework). It runs only on 127.0.0.1.

Rules that matter for almost any change:
- server/tools/*.js files (built-in capabilities) must NEVER import server/tools/index.js, server/capabilities.js, server/models/runner.js, server/scheduler/scheduler.js, server/scheduler/briefing.js, or server/control/session.js — directly or transitively. Doing so deadlocks the dynamic tool loader. A "leaf" module (db.js, store.js, prefs.js, a *-store.js file) is always safe to import from a tool.
- There is no automated test suite. Verification is manual: node --check on every changed file, curl against server endpoints, and (for anything visual) an actual browser check. Never claim something works without having actually run it.
- Testing must never touch the real data/ directory, .env file, or port 3000 — use JARVIS_DATA_DIR / JARVIS_ENV_PATH / PORT environment variable overrides to point at a scratch location instead.
- Screens under public/screens/*.js each export one async function render(container) that wipes and rebuilds the container from scratch on every change (container.innerHTML = '' as the first line) — no partial DOM patching. Never use innerHTML with any server- or user-supplied text; use textContent or createElement.
- New API routes in server/server.js follow one pattern throughout the file: reads return a named-collection object like {things: [...]}, writes return {ok: true, ...} or {ok: false, error: "a plain sentence."}, and any literal sub-path (e.g. /api/things/special) must be registered BEFORE a /:id route on the same prefix, or Express's route matching order will swallow it.
- SQLite (Node's built-in node:sqlite) is the storage layer for anything relational; server/db.js is the only module that opens the connection directly, with an ordered, append-only MIGRATIONS array keyed on PRAGMA user_version — a schema change is a new array entry, never an edit to an old one once it has shipped.`;

/**
 * Generates (and saves onto the proposal) a structured implementation
 * brief. `target` is the user's own free-text description of which coding
 * assistant they're using — passed straight into the prompt, never
 * matched against a fixed list.
 */
export async function generateImplementationPrompt(proposalId, { target } = {}) {
  const proposal = getProposal(proposalId);
  if (!proposal) throw new Error('That suggestion no longer exists.');
  if (proposal.kind !== 'skill' && proposal.kind !== 'code') {
    throw new Error('An implementation prompt only makes sense for a Skill idea or a code change.');
  }

  const targetText = String(target || '').trim() || 'a general-purpose AI coding assistant';

  const prompt = [
    `Write a clear, structured, ready-to-paste task brief for ${targetText}, describing the following improvement to a personal ` +
      'assistant application called Jarvis. The brief should stand on its own — whoever receives it may know nothing about this ' +
      'specific idea beyond what you write.',
    '',
    `Title: ${proposal.title}`,
    proposal.rationale ? `Why this came up: ${proposal.rationale}` : '',
    proposal.helpsJarvis ? `How this helps Jarvis: ${proposal.helpsJarvis}` : '',
    proposal.helpsUser ? `How this helps the user: ${proposal.helpsUser}` : '',
    '',
    'Background on the codebase this change would go into:',
    ARCHITECTURE_BRIEFING,
    '',
    'Write the brief with these sections: what to build and why, relevant architecture notes from the background above, ' +
      'a rough shape for the change (without inventing specific file paths you cannot actually know), how to verify it manually ' +
      '(there is no automated test suite), and clear acceptance criteria. Do not claim to know exact file names or line numbers — ' +
      'say plainly that the implementer should locate the relevant code themselves using the guidance above. Write it directly as ' +
      'the brief itself, ready to paste — no preamble like "Here is the brief."',
  ]
    .filter(Boolean)
    .join('\n');

  const result = await askModel({ prompt, background: false });
  if (!result.ok) throw new Error(result.error || 'Could not generate a prompt right now.');

  setProposalImplementation(proposalId, { implementationPrompt: result.text, implementationTarget: targetText });
  return { implementationPrompt: result.text, implementationTarget: targetText };
}
