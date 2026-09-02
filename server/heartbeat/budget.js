// A small, reusable daily spend ledger for any Heartbeat source that needs
// to bound its own model-call spend — same shape and same `app_state`-backed
// storage as improvement/improvement-store.js's own budget ledger, but
// parameterized by key and limit so a source gets its own independent
// bucket instead of sharing (or duplicating) Self-Improvement's. Leaf
// module: imports only db.js.

import { getDb } from '../db.js';

function todayKey() {
  return new Date().toISOString().slice(0, 10); // 'YYYY-MM-DD'
}

function readState(key) {
  const row = getDb().prepare('SELECT value FROM app_state WHERE key = ?').get(key);
  if (!row) return { day: todayKey(), used: 0 };
  try {
    const parsed = JSON.parse(row.value);
    return parsed && typeof parsed === 'object' ? parsed : { day: todayKey(), used: 0 };
  } catch {
    return { day: todayKey(), used: 0 };
  }
}

function writeState(key, value) {
  getDb()
    .prepare('INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
    .run(key, JSON.stringify(value));
}

/** Atomically consumes one unit of `key`'s daily budget (capped at `limit`) if any remains; returns true if consumed, false if already spent today. Rolls over automatically on a new day. */
export function tryConsumeDailyBudget(key, limit) {
  const today = todayKey();
  const state = readState(key);
  const current = state.day === today ? state : { day: today, used: 0 };
  if (current.used >= limit) return false;
  writeState(key, { day: today, used: current.used + 1 });
  return true;
}

export function dailyBudgetRemaining(key, limit) {
  const state = readState(key);
  if (state.day !== todayKey()) return limit;
  return Math.max(0, limit - state.used);
}
