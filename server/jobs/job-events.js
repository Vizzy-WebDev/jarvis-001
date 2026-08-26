// In-process event bus for Jobs — the same two-channel split
// server/monitor/engine.js's monitorEvents uses (see that file's header
// comment) and for the identical reason: server/tools/work_in_background.js,
// check_on_work.js, and stop_working_on.js must reach the orchestrator
// without importing it directly (nothing under server/tools/ may import
// anything that transitively reaches models/runner.js — root CLAUDE.md's
// circular-import invariant), and only server.js may import
// jobs/orchestrator.js to subscribe it.
//
// The job-store row is the durable truth, not this emitter — a missed event
// only costs latency until the next supervisor tick picks the row up, never
// correctness.

import { EventEmitter } from 'node:events';

export const jobEvents = new EventEmitter();
