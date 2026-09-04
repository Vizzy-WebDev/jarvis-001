// Verification / Quality Control (root CLAUDE.md's Operational Awareness
// item 4) — "does a generated file actually open, does generated code
// actually run, does the result actually match what was asked," rather
// than treating "it finished" as the same as "it's correct."
//
// Two tiers, per the owner's own explicit spec: MECHANICAL (this file,
// always, zero model calls — a file opens/parses, code exits 0) and
// SEMANTIC (one budgeted model call — does the result actually answer what
// was asked). No new recovery mechanism on a failure — the SAME
// retry-then-escalate shape Jobs already built (job-policy.js's
// canAutoRetry: one attempt, then escalate) is reused, never duplicated;
// see ops/diagnostics/source.js for the identical pattern applied to
// self-diagnosis.

import fs from 'node:fs';
import path from 'node:path';
import { isOfficeDocument, extractDocument } from '../documents/index.js';
import { askModel } from '../ai.js';

// ---------- mechanical checks (always run, zero model calls) ----------

/**
 * Does a generated FILE actually open/parse? An Office document is
 * re-read through this project's own real, independently-built reader
 * (documents/index.js's extractDocument()) — the exact discipline this
 * build's own writers were verified with (docx.js/xlsx.js in
 * server/artifacts/writers/). Anything else just needs to be non-empty and,
 * for a format with an obvious structural check (JSON, SVG/XML), parse
 * cleanly.
 */
export function verifyFileOpens(filePath) {
  let stat;
  try {
    stat = fs.statSync(filePath);
  } catch (err) {
    return { ok: false, detail: `File does not exist or is unreadable: ${err?.message || err}` };
  }
  if (stat.size === 0) return { ok: false, detail: 'File is empty (0 bytes).' };

  if (isOfficeDocument(filePath)) {
    const result = extractDocument(filePath);
    if (!result.ok) return { ok: false, detail: `Failed to open as a real Office document: ${result.error}` };
    return { ok: true };
  }

  const ext = path.extname(filePath).toLowerCase();
  if (ext === '.json') {
    try {
      JSON.parse(fs.readFileSync(filePath, 'utf8'));
      return { ok: true };
    } catch (err) {
      return { ok: false, detail: `Invalid JSON: ${err?.message || err}` };
    }
  }
  if (ext === '.svg') {
    const text = fs.readFileSync(filePath, 'utf8').trim();
    if (!/^(<\?xml[^>]*\?>\s*)?<svg[\s>]/i.test(text)) {
      return { ok: false, detail: 'Does not look like a well-formed SVG (no <svg> root element found).' };
    }
    return { ok: true };
  }
  // Plain text/markdown/csv/code — non-empty (already checked above) is the
  // whole mechanical check; there's no universal "is this valid text" test
  // beyond that.
  return { ok: true };
}

/** A sandboxed code run's own real exit signal — already computed by sandbox/runner.js, just named consistently with the rest of this file's contract. */
export function verifyCodeRan(sandboxResult) {
  if (!sandboxResult) return { ok: false, detail: 'No sandbox result to check.' };
  if (sandboxResult.timedOut) return { ok: false, detail: 'Timed out before finishing.' };
  if (sandboxResult.exitCode !== 0) return { ok: false, detail: `Exited with code ${sandboxResult.exitCode}${sandboxResult.stderr ? `: ${sandboxResult.stderr.slice(0, 300)}` : ''}` };
  return { ok: true };
}

// ---------- semantic check (budgeted, one model call) ----------

/**
 * Does the result actually answer what was asked? One model call, only —
 * never run automatically alongside every mechanical check (that would be
 * a model call per artifact/job/task on a routinely rate-limited roster —
 * see root CLAUDE.md's Gotchas). Callers decide when this is worth
 * spending; verify.js itself has no budget ledger of its own beyond that
 * judgment call.
 */
export async function verifySemanticMatch({ request, resultSummary, resultText }) {
  const prompt =
    `The user asked for: "${request}"\n\n` +
    `What was actually produced: ${resultSummary}\n\n` +
    (resultText ? `Its content:\n${String(resultText).slice(0, 4000)}\n\n` : '') +
    'Does this genuinely answer what was asked? Reply with ONLY a JSON object: {"matches": true|false, "reason": "one sentence"}.';

  const result = await askModel({ prompt, json: true, background: true });
  if (!result.ok || typeof result.data?.matches !== 'boolean') {
    // No model available, or an unparseable reply — never treated as a
    // pass OR a fail by guessing; the caller gets an honest "couldn't
    // check," the same "silence is the safe failure direction" discipline
    // heartbeat/decision.js already established.
    return { checked: false, matches: null, reason: null };
  }
  return { checked: true, matches: result.data.matches, reason: result.data.reason || null };
}
