// Security check #1: has a secret/config file changed OUTSIDE anything this
// app itself did through a real route? Detection only, per the owner's own
// explicit scope — no access control, no prevention, nothing here ever
// blocks or reverts anything. Watches the files that actually matter if
// tampered with: .env (every API key), connections.json (model
// connections), external-services.json (TTS/STT keys), connectors.json
// (every connected app's own tokens).
//
// **Never fires on this app's own normal work, including a concurrent
// Claude session editing SOURCE — this only watches the four DATA/secret
// files above, never anything under server/ or public/.** Distinguishes
// "the app itself wrote this" from "something else did" using store.js's
// lastWriteAt()/config.js's envLastWriteTime() — an in-memory, per-process
// record of the exact CONTENT HASH the app itself last wrote, set at the
// ONE real write site each of these files has (writeJson()/writeEnvFile()).
//
// **A time-window version of this (compare against how RECENTLY the app
// wrote it, not WHAT it wrote) was tried first and found genuinely wrong by
// this check's own verification, not just reasoned about**: a legitimate
// write's timestamp stays "recent" long enough to wrongly excuse a LATER,
// unrelated external change landing inside the same window — confirmed
// live: writeJson() a real change, then immediately overwrite the same file
// externally (raw fs, bypassing store.js entirely) — the external change
// went undetected because the legitimate write's timestamp was still fresh.
// Comparing the actual hash the app produced instead means a legitimate
// write only ever explains the EXACT content it actually wrote, no matter
// how much time has passed; re-verified after the fix with the same
// reproduction, now correctly flagged.
//
// Keeps its own hash history via app_state (same pattern
// heartbeat/budget.js already uses), independent of the generic
// diagnostics engine's own per-run bookkeeping — this check owns its
// memory of "what did I see last time," never threaded through
// registry.js's own simple probe()/remedy() contract.

import fs from 'node:fs';
import crypto from 'node:crypto';
import { getDb } from '../../../../db.js';
import { dataFilePath, lastWriteAt } from '../../../../store.js';
import { envFilePath, envLastWriteTime } from '../../../../config.js';

const STATE_KEY = 'ops_diag_config_integrity';

const WATCHED = [
  { name: 'env', path: () => envFilePath(), lastAppWrite: () => envLastWriteTime() },
  { name: 'connections', path: () => dataFilePath('connections'), lastAppWrite: () => lastWriteAt('connections') },
  { name: 'external-services', path: () => dataFilePath('external-services'), lastAppWrite: () => lastWriteAt('external-services') },
  { name: 'connectors', path: () => dataFilePath('connectors'), lastAppWrite: () => lastWriteAt('connectors') },
];

function hashFile(filePath) {
  try {
    const buf = fs.readFileSync(filePath);
    return crypto.createHash('sha256').update(buf).digest('hex');
  } catch {
    return null; // doesn't exist yet — not a finding, just nothing to compare
  }
}

function readState() {
  const row = getDb().prepare('SELECT value FROM app_state WHERE key = ?').get(STATE_KEY);
  if (!row) return { hashes: {} };
  try {
    const parsed = JSON.parse(row.value);
    return parsed?.hashes ? parsed : { hashes: {} };
  } catch {
    return { hashes: {} };
  }
}

function writeState(state) {
  getDb()
    .prepare('INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .run(STATE_KEY, JSON.stringify(state));
}

export const id = 'security-config-integrity';

export async function probe() {
  const state = readState();
  const changed = [];
  const nextHashes = { ...state.hashes };

  for (const w of WATCHED) {
    const filePath = w.path();
    const currentHash = hashFile(filePath);
    const priorHash = Object.prototype.hasOwnProperty.call(state.hashes, w.name) ? state.hashes[w.name] : undefined;
    nextHashes[w.name] = currentHash;

    if (priorHash === undefined) continue; // first time seeing this file — establish baseline, not a finding
    if (currentHash === priorHash) continue; // unchanged (covers both non-null equal hashes and both-missing)

    // Changed. Explained only if the app's own last write to this file
    // produced EXACTLY this content — not merely "recently."
    const appWrite = w.lastAppWrite();
    const explainedByAppItself = appWrite?.hash === currentHash;
    if (!explainedByAppItself) changed.push(w.name);
  }

  writeState({ hashes: nextHashes });

  if (changed.length) {
    return { ok: false, detail: `Changed with no matching in-app write recorded: ${changed.join(', ')}.` };
  }
  return { ok: true };
}

// Deliberately NO remedy() — this build is detection only, per the owner's
// own explicit scope. A possible-tampering finding always escalates
// straight to the owner; nothing here ever reverts or blocks anything.
