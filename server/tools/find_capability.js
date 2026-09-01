// Skill: looks up one of Jarvis's less-common abilities by what the user
// actually wants — schedule a reminder, control the computer, check a
// spreadsheet, search Notion, anything not already in the small always-on
// set (see capabilities.js's getToolDeclarations()). Calling this makes the
// matching abilities callable for the REST of this turn — the model calls
// find_capability first, then calls the ability it found in a later step.
//
// Exists because sending all ~70 non-core capabilities on every single turn
// (measured: ~150,000 characters, on "hello" as much as on anything else)
// was the single largest cause of flat, instruction-ignoring voice replies —
// see the root CLAUDE.md's Model system section. This tool, plus the ~10
// core ones, is what most turns see instead.
//
// Cannot import capabilities.js directly — that file's getToolDeclarations()
// is what decides whether THIS file is even in the list a model sees, and a
// top-level import back would deadlock server/tools/index.js's dynamic-
// import loader (see CLAUDE.md's circular-import invariant, and
// create_skill.js for the identical pattern with reservedSkillNames). The
// actual search function arrives via ctx, injected by capabilities.js's
// invoke().

export default {
  name: 'find_capability',
  core: true,
  // Deliberately NOT meta — a scheduled/background run (opts.background)
  // gets includeMeta:false, which built-in tools' own `meta` filter honors
  // BEFORE the core/unlocked split even applies (see capabilities.js's
  // getToolDeclarations()). Marking this meta would silently strip it from
  // every background run right alongside the real meta tools, leaving a
  // scheduled task able to reach only the non-meta core set (6 tools) with
  // no way to discover run_code/read_web_page/analyze_spreadsheet/etc. —
  // tools it could already reach in full before this change. `run()` below
  // still respects ctx.background itself, so a background run's search
  // stays scoped to non-meta capabilities, same as the old unfiltered list
  // always was.
  description:
    'Look up one of your less common abilities by what the user wants — things like scheduling a reminder, ' +
    'controlling the computer, reading a spreadsheet, checking a claim, or anything connected through Notion ' +
    'or another app. Only use this when something the user asked for clearly needs an ability beyond the ' +
    'everyday ones already available to you (time, weather, opening things, searching, remembering). Describe ' +
    'what\'s needed in a few plain words; matching abilities become callable right away, in this same turn.',
  parameters: {
    type: 'object',
    properties: {
      intent: {
        type: 'string',
        description: 'A few words describing the ability needed, e.g. "schedule a reminder" or "read a spreadsheet".',
      },
    },
    required: ['intent'],
  },
  async run({ intent } = {}, ctx = {}) {
    const query = String(intent || '').trim();
    if (!query) return { ok: false, error: 'No intent given.' };
    if (typeof ctx.searchCapabilities !== 'function') {
      return { ok: false, error: 'Capability search is unavailable right now.' };
    }
    // A background/scheduled run never sees meta tools (schedule_task,
    // remember_about_me, ...) — matches what it could always reach before
    // this file existed (runner.js's toolsForTurn() called
    // getToolDeclarations({includeMeta: !opts.background})).
    const matches = ctx.searchCapabilities(query, { includeMeta: !ctx.background });
    if (!matches.length) {
      return { ok: true, matches: [], note: "Nothing matched — this may not be something you're able to do." };
    }
    return { ok: true, matches: matches.map((m) => ({ name: m.name, kind: m.kind, description: m.description })) };
  },
};
