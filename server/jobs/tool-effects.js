// Classifies a tool call's effect for the write-ahead trace — 'read' (no
// side effect), 'workspace' (a local, visible side effect that's safe to
// just redo), or 'external' (leaves the machine, or takes an action that
// can't be safely repeated) — see server/jobs/job-policy.js's
// classifyRecovery(), which treats ANY 'external' trace row as
// unrecoverable and everything short of 'workspace' as resumable.
//
// A first-pass, hand-classified table, not the final word — root
// CLAUDE.md's Jobs section flags the real effect-based "must the owner
// decide" classifier (reusing control/guard.js's classifyActionRisk shape)
// as arriving properly in Phase 3, once real per-tool risk metadata exists.
// This exists now because worker.js needs SOME effect value for every trace
// row from the moment Jobs can run at all — classifyRecovery has nothing to
// reason about otherwise. Pure data + one function, no imports — same
// leaf discipline as job-policy.js.

// No side effects at all — reading something is never irreversible.
const READ_EFFECT_TOOLS = new Set([
  'get_time', 'get_weather', 'get_headlines', 'look_it_up', 'web_search',
  'read_web_page', 'check_claim', 'examine_content', 'look_at_screen',
  'search_conversations', 'read_skill_file', 'list_tasks', 'review_memories',
]);

// A visible, local side effect, but one that's safe to just redo — running
// the same code/script again, opening the same app/site again, re-recording
// the same project decision, isn't something that compounds or needs undoing.
const WORKSPACE_EFFECT_TOOLS = new Set([
  'run_code', 'analyze_spreadsheet', 'run_skill_script', 'create_skill',
  'approve_skill_pipeline', 'approve_skill_scripts', 'note_project_decision',
  'start_project', 'research_project', 'write_project_plan', 'write_project_prompts',
  'write_build_prompts', 'share_content', 'allow_folder', 'open_website', 'open_app',
  'watch_for', 'stop_watching', 'request_job_split',
]);

// Leaves the machine, or takes a real-world action that can't be safely
// repeated (sending, publishing, purchasing, operating the desktop) — see
// the build spec's own "no sending, publishing, or purchasing" line for a
// Worker's own-authority ceiling. control_computer is named explicitly
// because it can take irreversible actions on the real desktop; every
// connector tool (kind==='connector' — Gmail, Slack, any MCP/API service)
// defaults here too, via `kindByName` below, since connector tool NAMES vary
// per user and can't be hardcoded into this table.
const EXTERNAL_EFFECT_TOOLS = new Set(['control_computer']);

/**
 * `kindByName` (optional Map<name, 'builtin'|'skill'|'connector'>, built by
 * orchestrator.js from capabilities.js's listCapabilities()) is what lets an
 * unrecognized name default correctly: a connector tool defaults to
 * 'external' (safest — it reaches something outside this machine), anything
 * else unrecognized defaults to 'workspace' (the middle tier — matching
 * control/guard.js's own "unknown ⇒ notable, never safe" spirit without
 * being maximally pessimistic about every built-in this table simply
 * hasn't been updated to include yet).
 */
export function classifyToolEffect(name, kindByName) {
  if (READ_EFFECT_TOOLS.has(name)) return 'read';
  if (WORKSPACE_EFFECT_TOOLS.has(name)) return 'workspace';
  if (EXTERNAL_EFFECT_TOOLS.has(name)) return 'external';
  if (kindByName?.get(name) === 'connector') return 'external';
  return 'workspace';
}
