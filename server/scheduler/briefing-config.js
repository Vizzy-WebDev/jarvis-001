// Just the briefing's saved configuration (data/briefing.json) — split out
// of briefing.js on purpose so it has NO dependency on the model runner or
// skills loader. The configure_briefing skill needs this file directly:
// skills/index.js dynamically imports every file in server/skills/,
// including configure_briefing.js, and briefing.js pulls in
// models/runner.js (for composeBriefing) which itself imports
// skills/index.js — a skill reaching back through that chain into the
// loader that's still in the middle of loading it is a real deadlock (a
// dynamic import() of a module still "evaluating" waits for it to finish,
// which here never happens). Keeping config get/set dependency-free avoids
// the cycle entirely; briefing.js still re-exports these two for anyone
// that doesn't care about the distinction.

import { readJson, writeJson } from '../store.js';

const FILE = 'briefing';

const DEFAULT_CONFIG = {
  sections: {
    greeting: true,
    dateTime: true,
    tasks: true,
    goals: true,
    focus: true,
    custom: false,
  },
  customText: '',
  // Weather and headlines are fixed, always-available native abilities —
  // NOT a user-managed list of "sources" any more. That shape (an earlier
  // generation of this file) let the Morning Briefing screen offer
  // Jarvis's own built-in get_weather/get_headlines abilities through the
  // same "add a source" UI used to attach a Skill — a real instance of the
  // native-ability-as-Skill bug (see CLAUDE.md's permanent Skills rule).
  // There is no UI for either any more (matching CLAUDE.md's existing
  // "Desktop control / Browser / Files have no settings screen" precedent)
  // — `weatherPlace` is set conversationally via configure_briefing.js,
  // same as it always required a place before it could ever say anything.
  weatherPlace: '', // '' = weather skipped
  headlines: false,
  // Reserved, disabled by design: a real Google Calendar/Gmail connection is
  // a separate sign-in flow, deliberately not built as part of this
  // upgrade. These slots exist so those sections can drop in later without
  // restructuring the briefing.
  calendar: { enabled: false },
  email: { enabled: false },
};

/**
 * Turns a config saved under either earlier generation of this file into
 * the current shape:
 *  - oldest: fixed `sections.weather`/`sections.headlines` booleans + a
 *    separate `location.place` string.
 *  - middle: an open `sources[]` list of Skill-backed briefing sources
 *    (any Skill, not just weather/headlines) — reverted specifically
 *    because it was the mechanism behind the native-ability leak above.
 * A weather section only carries over if a place was actually saved —
 * unset weather never produced anything before either, so this changes
 * nothing for anyone who never set a location.
 */
function upgrade(saved) {
  if (Array.isArray(saved.sources)) {
    const weatherSrc = saved.sources.find((s) => s?.skillName === 'get_weather' && s.enabled !== false);
    const headlinesSrc = saved.sources.find((s) => s?.skillName === 'get_headlines' && s.enabled !== false);
    const { sources, ...rest } = saved;
    return {
      ...rest,
      weatherPlace: String(weatherSrc?.args?.place || '').trim(),
      headlines: Boolean(headlinesSrc),
    };
  }
  if (saved.sections?.weather !== undefined || saved.sections?.headlines !== undefined || saved.location) {
    const sections = { ...(saved.sections || {}) };
    const weatherPlace = sections.weather ? String(saved.location?.place || '').trim() : '';
    const headlines = Boolean(sections.headlines);
    delete sections.weather;
    delete sections.headlines;
    const upgraded = { ...saved, sections, weatherPlace, headlines };
    delete upgraded.location;
    return upgraded;
  }
  return saved;
}

export function getBriefingConfig() {
  const saved = readJson(FILE, {});
  const base = upgrade(saved);
  return {
    ...DEFAULT_CONFIG,
    ...base,
    sections: { ...DEFAULT_CONFIG.sections, ...(base.sections || {}) },
  };
}

export function setBriefingConfig(patch = {}) {
  const current = getBriefingConfig();
  const next = {
    ...current,
    ...patch,
    sections: { ...current.sections, ...(patch.sections || {}) },
  };
  writeJson(FILE, next);
  return next;
}
