// Pure, dependency-free template resolution for a skill.toml pipeline's
// step arguments — {{inputs.<name>}} and {{steps.<id>.<dot.path>}}. No
// imports from anywhere else in server/, so this is safe for both
// store/skill-toml.js's parse-time validation (finding which step ids a
// pipeline references, to catch an unknown/forward reference before
// anything ever runs) and pipeline.js's actual run-time substitution — same
// regex, same path parsing, used two different ways from two different
// layers that must never import each other's forbidden targets (see the
// root CLAUDE.md's circular-import invariant).
//
// Two rules that matter for safety, not just correctness:
//   - Resolved values are NEVER re-scanned for templates. A tool result
//     that happens to contain literal "{{...}}" text must not become a new
//     template — resolveTemplates() does exactly one substitution pass per
//     source string. String.prototype.replace with a global regex matches
//     against the ORIGINAL string only (the spec resets `lastIndex` to 0 at
//     the start and never re-scans a replacer's return value), and this
//     module never calls itself on an already-resolved value.
//   - An unresolvable reference throws, with a plain-language message
//     naming the exact reference — never silently produces an empty
//     string, which is how these become undebuggable.

const REF_RE = /\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}/g;

function isWholeReference(str) {
  const m = str.match(/^\s*\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}\s*$/);
  return m ? m[1] : null;
}

/**
 * Splits "steps.weather.forecast.today" into
 * {root: 'steps', id: 'weather', path: ['forecast', 'today']}, or
 * "inputs.city" into {root: 'inputs', id: 'city', path: []}, or
 * {root: null, ref} for anything else (an unknown root — always
 * unresolved, e.g. a bare word, or "steps" with no id after it).
 */
export function parseRefPath(ref) {
  const parts = String(ref).split('.');
  if (parts[0] === 'inputs') {
    if (parts.length !== 2) return { root: null, ref };
    return { root: 'inputs', id: parts[1], path: [] };
  }
  if (parts[0] === 'steps') {
    if (parts.length < 2) return { root: null, ref };
    return { root: 'steps', id: parts[1], path: parts.slice(2) };
  }
  return { root: null, ref };
}

/**
 * Every {{...}} reference found anywhere inside `value` (a string, or an
 * array/object holding strings) — for parse-time validation, not
 * resolution. Returns an array of {ref, root, id, path}; duplicates are not
 * removed.
 */
export function extractRefs(value) {
  const out = [];
  function walk(v) {
    if (typeof v === 'string') {
      for (const m of v.matchAll(REF_RE)) {
        out.push({ ref: m[1], ...parseRefPath(m[1]) });
      }
    } else if (Array.isArray(v)) {
      v.forEach(walk);
    } else if (v && typeof v === 'object') {
      Object.values(v).forEach(walk);
    }
  }
  walk(value);
  return out;
}

/**
 * Looks up one reference against `context` ({inputs, steps} — inputs: a
 * plain {name: value} object; steps: a plain {id: {ok, result}} object of
 * every step that has run so far). A step that failed (`ok !== true`) or
 * hasn't run yet counts as not found — a pipeline step can only see a
 * PRIOR, SUCCESSFUL step's data.
 */
function lookupRef(ref, context) {
  const { root, id, path } = parseRefPath(ref);
  if (root === 'inputs') {
    const inputs = context.inputs || {};
    if (!Object.prototype.hasOwnProperty.call(inputs, id)) return { found: false };
    return { found: true, value: inputs[id] };
  }
  if (root === 'steps') {
    const step = (context.steps || {})[id];
    if (!step || step.ok !== true) return { found: false };
    let value = step.result;
    for (const segment of path) {
      if (value === null || typeof value !== 'object' || !(segment in value)) return { found: false };
      value = value[segment];
    }
    return { found: true, value };
  }
  return { found: false };
}

/**
 * Resolves every {{...}} reference inside `value` against `context` (see
 * lookupRef). A string that is ENTIRELY one reference
 * ("{{steps.a.items}}") resolves to the real value with its original type
 * preserved (an array, a number, a whole object); a reference embedded in
 * a larger string is stringified in place. Throws a plain-language Error
 * naming the exact unresolved reference — never silently produces an empty
 * string.
 */
export function resolveTemplates(value, context) {
  if (typeof value === 'string') {
    const whole = isWholeReference(value);
    if (whole !== null) {
      const { found, value: resolved } = lookupRef(whole, context);
      if (!found) {
        throw new Error(`"{{${whole}}}" could not be resolved — check the referenced input/step exists and has already run.`);
      }
      return resolved;
    }
    return value.replace(REF_RE, (_match, ref) => {
      const { found, value: resolved } = lookupRef(ref, context);
      if (!found) {
        throw new Error(`"{{${ref}}}" could not be resolved — check the referenced input/step exists and has already run.`);
      }
      if (resolved === null || resolved === undefined) return '';
      return typeof resolved === 'object' ? JSON.stringify(resolved) : String(resolved);
    });
  }
  if (Array.isArray(value)) return value.map((v) => resolveTemplates(v, context));
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = resolveTemplates(v, context);
    return out;
  }
  return value;
}
