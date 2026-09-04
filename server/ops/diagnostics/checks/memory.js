// Is Memory actually writing correctly? A real round trip — create, read
// back, delete — not a passive "has anything happened lately" read, which
// can't tell a broken write path apart from a quiet one where nobody's
// taught Jarvis anything new recently.
//
// **Fully synchronous, with no `await` between create and delete, on
// purpose.** `memory-store.js`'s createMemory()/getMemory()/deleteMemory()
// are all synchronous SQLite calls (node:sqlite's DatabaseSync, not a
// promise-based driver) — writing this probe with zero intervening `await`
// means nothing else in the event loop (a live Memory screen render, a
// prompt.js approvedMemoriesText() call feeding a real turn) can ever
// observe the canary row between its creation and its deletion; Node is
// single-threaded and nothing yields control mid-way. This is real, not
// merely likely — confirmed by reading every function in the call chain
// for a stray `await`, not assumed.
//
// A category name that signals "internal, not a real memory" as defense in
// depth, even though the synchronous window above already makes it
// structurally unobservable — a future edit to memory-store.js that
// introduces a genuine async step would find this row visible only for one
// clearly-marked category, never blending into a real one.

import { createMemory, getMemory, deleteMemory } from '../../../memory/memory-store.js';

export const id = 'memory-write-read';
const CANARY_CATEGORY = '__ops_diagnostic_canary__';

export async function probe() {
  const text = `diagnostic canary ${Date.now()}`;
  let created;
  try {
    created = createMemory({ category: CANARY_CATEGORY, text, sourceKind: 'diagnostic', origin: 'auto' });
  } catch (err) {
    return { ok: false, detail: `Memory write failed: ${err?.message || err}` };
  }

  let readBack;
  try {
    readBack = getMemory(created.id);
  } catch (err) {
    // Still try to clean up even though the read failed.
    try {
      deleteMemory(created.id);
    } catch {
      // best-effort — see the outer catch below for the louder failure this masks
    }
    return { ok: false, detail: `Memory read-back failed: ${err?.message || err}` };
  }

  try {
    deleteMemory(created.id);
  } catch (err) {
    return { ok: false, detail: `Memory cleanup (delete) failed — a canary row may have been left behind: ${err?.message || err}` };
  }

  if (!readBack || readBack.text !== text) {
    return { ok: false, detail: 'Memory write succeeded but the read-back did not match what was written.' };
  }
  return { ok: true };
}

// No remedy() — a broken write path isn't something a retry fixes; the
// underlying cause (a corrupt table, a full disk) needs the owner, not an
// automatic patch.
