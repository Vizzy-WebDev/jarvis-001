// Is the SQLite database itself structurally sound? `PRAGMA integrity_check`
// walks every table/index and reports real corruption — the one check here
// that can catch damage no higher-level round trip (memory.js's own canary
// included) would necessarily surface, since a canary only touches ONE
// table and corruption elsewhere could sit silent for a long time.

import { getDb } from '../../../db.js';

export const id = 'database-integrity';

export async function probe() {
  let rows;
  try {
    rows = getDb().prepare('PRAGMA integrity_check').all();
  } catch (err) {
    return { ok: false, detail: `Could not run integrity_check at all: ${err?.message || err}` };
  }
  // A healthy database returns exactly one row: {integrity_check: 'ok'}.
  // Anything else is real, specific corruption detail from SQLite itself.
  const clean = rows.length === 1 && rows[0]?.integrity_check === 'ok';
  if (clean) return { ok: true };
  const problems = rows.map((r) => r.integrity_check).join('; ');
  return { ok: false, detail: `SQLite integrity_check reported problems: ${problems}` };
}

// No remedy() — real corruption isn't something an automatic retry fixes;
// it needs the owner's own decision (restore a backup, accept data loss on
// the affected rows).
