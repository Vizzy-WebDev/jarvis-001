// Parses skill.toml — a Skill folder's OPTIONAL fixed, ordered pipeline, an
// alternative/addition to SKILL.md's free-form instructions. A folder with
// both runs the pipeline and appends SKILL.md's body as context; a folder
// with only skill.toml has no separate instructions at all.
//
// This is a hand-rolled SUBSET of real TOML, not a general parser — the
// project runs on 5 dependencies and has twice already refused a 6th (the
// hand-rolled YAML subset in skill-files.js's parseSkillMd(), the
// PowerShell shell-out for zip in skill-zip.js). TOML is meaningfully
// harder to hand-parse than that YAML subset (arrays-of-tables, multi-line
// strings, typed scalars), so the grammar here is deliberately narrowed to
// EXACTLY what a pipeline file needs — three header shapes only:
//
//   [[inputs]]      an array-of-tables entry (zero-argument Skill if omitted)
//   [[steps]]       an array-of-tables entry — one fixed pipeline step
//   [steps.args]    the most-recently-opened [[steps]] entry's argument table
//
// Plus one exception: a bare `description = "..."` line is allowed BEFORE
// the first header, for a pipeline-only Skill (skill.toml with no SKILL.md
// at all) — otherwise there is no source at all for the description a tool
// declaration needs (SKILL.md's frontmatter is the description source when
// it exists, exactly as today; this is only reached for a Skill that has no
// SKILL.md — see server/skills/index.js's folderSkillToTool()).
//
// Any other header, any dotted key, any inline table, any date, any
// hex/octal/binary number is a NAMED, loud parse error — the opposite of
// parseSkillMd()'s tolerance-by-design. That asymmetry is deliberate: a
// mis-parsed SKILL.md frontmatter field merely displays wrong; a
// mis-parsed pipeline EXECUTES, so silently accepting an unsupported
// construct here would mean silently running something other than what was
// written.
//
// Dependency-free leaf module — same discipline as skill-files.js (only
// server/skills/pipeline-template.js, an equally pure/leaf sibling, is
// imported, for the {{...}} reference-extraction validatePipeline() needs).
// Must NEVER import capabilities.js, tools/index.js, or skills/index.js —
// see the root CLAUDE.md's circular-import invariant. This is why
// validatePipeline()'s tool-name check takes `knownToolNames` as a plain
// injected argument rather than calling capabilities.js's
// listStepCandidates() itself: capabilities.js -> skills/index.js ->
// (a folder's skill.toml, loaded via this file) -> capabilities.js would be
// exactly the same class of deadlock the tools/index.js invariant exists to
// prevent, just one hop further out. The caller that already has safe
// access to capabilities.js (server.js's route layer) passes the list in.

import { extractRefs } from '../pipeline-template.js';

const MAX_STEPS = 20;
const VALID_INPUT_TYPES = new Set(['string', 'number', 'boolean']);
const BARE_KEY_CHAR = /[A-Za-z0-9_-]/;

/**
 * Parses skill.toml source text into {inputs: [], steps: []} — plain data,
 * no validation beyond what the grammar itself enforces (an unsupported
 * construct throws here; a structurally-wrong-but-grammatical pipeline,
 * like two steps sharing an id, is caught by validatePipeline() below).
 * Throws a plain-language `Error` naming the line number on anything it
 * can't parse.
 */
export function parseToml(text) {
  const src = String(text ?? '');
  let pos = 0;
  let line = 1;

  const peek = (offset = 0) => src[pos + offset];
  const eof = () => pos >= src.length;
  function advance() {
    const c = src[pos++];
    if (c === '\n') line++;
    return c;
  }
  function err(msg) {
    throw new Error(`skill.toml, line ${line}: ${msg}`);
  }

  function skipLineWhitespace() {
    while (!eof() && (peek() === ' ' || peek() === '\t' || peek() === '\r')) advance();
  }

  // Whitespace/comments only — newlines skipped too when acrossNewlines is
  // true (used inside an array, which may legally span several lines).
  function skipWhitespaceAndComments(acrossNewlines) {
    while (!eof()) {
      const c = peek();
      if (c === ' ' || c === '\t' || c === '\r') { advance(); continue; }
      if (c === '\n') { if (acrossNewlines) { advance(); continue; } break; }
      if (c === '#') { while (!eof() && peek() !== '\n') advance(); continue; }
      break;
    }
  }

  function expectNewlineOrEof() {
    skipLineWhitespace();
    if (eof()) return;
    if (peek() === '#') { while (!eof() && peek() !== '\n') advance(); }
    if (eof()) return;
    if (peek() !== '\n') err(`Unexpected text after a value: "${src.slice(pos, pos + 20).split('\n')[0]}".`);
    advance();
  }

  function skipBlankAndCommentLines() {
    while (!eof()) {
      skipLineWhitespace();
      if (eof()) break;
      if (peek() === '\n') { advance(); continue; }
      if (peek() === '#') { while (!eof() && peek() !== '\n') advance(); if (!eof()) advance(); continue; }
      break;
    }
  }

  function readBareKey() {
    if (peek() === '"' || peek() === "'") err("Quoted keys aren't supported in a skill.toml pipeline — use a plain bare key.");
    const start = pos;
    while (!eof() && BARE_KEY_CHAR.test(peek())) advance();
    if (pos === start) err(`Expected a key name, found "${eof() ? 'end of file' : peek()}".`);
    return src.slice(start, pos);
  }

  function readBareKeyPath() {
    const segments = [readBareKey()];
    skipLineWhitespace();
    while (peek() === '.') {
      advance();
      skipLineWhitespace();
      segments.push(readBareKey());
      skipLineWhitespace();
    }
    return segments;
  }

  function parseBasicString() {
    advance(); // opening "
    let out = '';
    for (;;) {
      if (eof()) err('Unterminated string (missing closing ").');
      const c = advance();
      if (c === '"') break;
      if (c === '\n') err('A basic string ("...") cannot contain a literal newline — use a multi-line string ("""...""") instead.');
      if (c === '\\') {
        if (eof()) err('Unterminated escape sequence.');
        const e = advance();
        if (e === 'n') out += '\n';
        else if (e === 't') out += '\t';
        else if (e === '"') out += '"';
        else if (e === '\\') out += '\\';
        else if (e === 'r') out += '\r';
        else err(`Unsupported escape sequence "\\${e}" — only \\n, \\t, \\", \\\\, \\r are supported.`);
        continue;
      }
      out += c;
    }
    return out;
  }

  function parseMultilineBasicString() {
    pos += 3; // opening """ — none of these three chars is a newline
    if (peek() === '\r') advance();
    if (peek() === '\n') advance(); // a newline right after the opener is trimmed, per TOML
    let out = '';
    for (;;) {
      if (eof()) err('Unterminated multi-line string (missing closing """).');
      if (src.startsWith('"""', pos)) { pos += 3; break; }
      const c = advance();
      if (c === '\\') {
        if (eof()) err('Unterminated escape sequence.');
        const e = advance();
        if (e === 'n') out += '\n';
        else if (e === 't') out += '\t';
        else if (e === '"') out += '"';
        else if (e === '\\') out += '\\';
        else if (e === 'r') out += '\r';
        else if (e === '\n') { while (!eof() && /[ \t\r\n]/.test(peek())) advance(); } // line-ending backslash
        else err(`Unsupported escape sequence "\\${e}" — only \\n, \\t, \\", \\\\, \\r are supported.`);
        continue;
      }
      out += c;
    }
    return out;
  }

  function parseLiteralString() {
    if (src.startsWith("'''", pos)) {
      err("Multi-line literal strings ('''...''') aren't supported — use a multi-line basic string (\"\"\"...\"\"\") instead.");
    }
    advance(); // opening '
    let out = '';
    for (;;) {
      if (eof()) err("Unterminated string (missing closing ').");
      const c = advance();
      if (c === "'") break;
      if (c === '\n') err("A literal string ('...') cannot contain a literal newline.");
      out += c;
    }
    return out;
  }

  function parseArray() {
    advance(); // '['
    const items = [];
    skipWhitespaceAndComments(true);
    while (peek() !== ']') {
      if (eof()) err('Unterminated array (missing closing ]).');
      items.push(parseValue());
      skipWhitespaceAndComments(true);
      if (peek() === ',') { advance(); skipWhitespaceAndComments(true); continue; }
      break;
    }
    if (peek() !== ']') err('Expected "," or "]" in array.');
    advance();
    return items;
  }

  function parseBareValue() {
    const start = pos;
    while (!eof() && !/[\s,\]#]/.test(peek())) advance();
    const raw = src.slice(start, pos);
    if (raw === '') err('Expected a value.');
    if (raw === 'true') return true;
    if (raw === 'false') return false;
    if (/^[+-]?0[xob]/i.test(raw)) err(`Hex/octal/binary numbers aren't supported (found "${raw}") — use a plain decimal number.`);
    if (/^\d{4}-\d{2}-\d{2}/.test(raw) || /^\d{2}:\d{2}:\d{2}/.test(raw)) {
      err(`Dates/times aren't supported (found "${raw}") — use a quoted string instead, e.g. "${raw}".`);
    }
    if (/^[+-]?\d+$/.test(raw)) return parseInt(raw, 10);
    if (/^[+-]?\d+\.\d+([eE][+-]?\d+)?$/.test(raw) || /^[+-]?\d+[eE][+-]?\d+$/.test(raw)) return parseFloat(raw);
    err(`Couldn't parse the value "${raw}" — supported value types are strings, numbers, booleans, and arrays.`);
  }

  function parseValue() {
    const c = peek();
    if (c === '"') return src.startsWith('"""', pos) ? parseMultilineBasicString() : parseBasicString();
    if (c === "'") return parseLiteralString();
    if (c === '[') return parseArray();
    if (c === '{') err("Inline tables ({ ... }) aren't supported in a skill.toml pipeline — use a [table] section instead.");
    if (c === undefined || c === '\n' || c === '#') err('Expected a value.');
    return parseBareValue();
  }

  // `doc` itself is the fallback target for any bare key = value line seen
  // BEFORE the first header in the file — the one deliberate top-level
  // exception to "every key belongs to a table". This is what lets a
  // pipeline-only Skill (skill.toml with no SKILL.md at all) declare
  // `description = "..."` up front, since without SKILL.md's frontmatter
  // there is otherwise no source for the description a tool declaration
  // needs. Only `description` is ever read back out of it (see
  // server/skills/index.js's folderSkillToTool()); any other stray
  // top-level key is harmless, unread data — same tolerance
  // parseSkillMd() already has for an unrecognized frontmatter key.
  const doc = { inputs: [], steps: [] };
  let currentInput = null;
  let currentStep = null;
  let currentStepArgs = null;

  function currentTarget() {
    if (currentStepArgs) return currentStepArgs;
    if (currentStep) return currentStep;
    if (currentInput) return currentInput;
    return doc;
  }

  function parseHeaderLine() {
    advance(); // '['
    let isArray = false;
    if (peek() === '[') { isArray = true; advance(); }
    skipLineWhitespace();
    const segments = readBareKeyPath();
    skipLineWhitespace();
    if (isArray) {
      if (peek() !== ']' || peek(1) !== ']') err('Expected "]]" to close an array-of-tables header.');
      advance(); advance();
    } else {
      if (peek() !== ']') err('Expected "]" to close a table header.');
      advance();
    }
    expectNewlineOrEof();

    const path = segments.join('.');
    if (isArray) {
      if (path === 'inputs') {
        currentInput = {}; doc.inputs.push(currentInput);
        currentStep = null; currentStepArgs = null;
      } else if (path === 'steps') {
        currentStep = {}; doc.steps.push(currentStep);
        currentInput = null; currentStepArgs = null;
      } else {
        err(`Unknown array-of-tables "[[${path}]]" — a skill.toml pipeline only supports [[inputs]] and [[steps]].`);
      }
    } else if (path === 'steps.args') {
      if (!currentStep) err('"[steps.args]" appeared before any [[steps]] entry — every step needs [[steps]] first.');
      currentStepArgs = currentStep.args = currentStep.args || {};
      currentInput = null;
    } else {
      err(`Unknown table "[${path}]" — a skill.toml pipeline only supports [[inputs]], [[steps]], and [steps.args].`);
    }
  }

  function parseKeyValueLine() {
    const key = readBareKey();
    skipLineWhitespace();
    if (peek() === '.') err(`Dotted keys aren't supported in a skill.toml pipeline (found "${key}.…") — use a nested [table] section instead.`);
    if (peek() !== '=') err(`Expected "=" after key "${key}".`);
    advance();
    skipLineWhitespace();
    const value = parseValue();
    expectNewlineOrEof();

    currentTarget()[key] = value; // always non-null — see currentTarget()'s own comment
  }

  while (!eof()) {
    skipBlankAndCommentLines();
    if (eof()) break;
    if (peek() === '[') parseHeaderLine();
    else parseKeyValueLine();
  }

  return doc;
}

/**
 * Structural + reference validation for a parsed skill.toml pipeline
 * (parseToml()'s output). Returns {errors: string[]} — never throws, so a
 * broken pipeline can be reported in plain language (the Skills detail
 * page's "why this pipeline can't run yet") rather than crashing whatever's
 * displaying it.
 *
 * `knownToolNames` (optional, an array/Set of tool names) checks a `tool`
 * step's target actually exists — omit it to skip just that one check. See
 * this file's header comment for why this function doesn't fetch that list
 * itself.
 */
export function validatePipeline(doc, { knownToolNames } = {}) {
  const errors = [];
  const knownTools = knownToolNames ? new Set(knownToolNames) : null;
  const inputs = Array.isArray(doc?.inputs) ? doc.inputs : [];
  const steps = Array.isArray(doc?.steps) ? doc.steps : [];

  if (steps.length > MAX_STEPS) {
    errors.push(`This pipeline has ${steps.length} steps — the limit is ${MAX_STEPS}.`);
  }

  const inputNames = new Set();
  inputs.forEach((input, i) => {
    const label = `Input ${i + 1}`;
    if (typeof input.name !== 'string' || !input.name.trim()) {
      errors.push(`${label}: missing "name".`);
      return;
    }
    if (inputNames.has(input.name)) errors.push(`${label}: duplicate input name "${input.name}".`);
    inputNames.add(input.name);
    if (input.type !== undefined && !VALID_INPUT_TYPES.has(input.type)) {
      errors.push(`Input "${input.name}": unsupported type "${input.type}" — use string, number, or boolean.`);
    }
  });

  const stepIndexById = new Map();
  steps.forEach((step, i) => {
    const label = typeof step?.id === 'string' && step.id ? `Step "${step.id}"` : `Step ${i + 1}`;
    if (typeof step.id !== 'string' || !step.id.trim()) {
      errors.push(`${label}: missing "id".`);
    } else if (stepIndexById.has(step.id)) {
      errors.push(`${label}: duplicate id "${step.id}" (already used by an earlier step).`);
    } else {
      stepIndexById.set(step.id, i);
    }

    const hasTool = typeof step.tool === 'string' && step.tool.trim();
    const hasPrompt = typeof step.prompt === 'string' && step.prompt.trim();
    if (hasTool && hasPrompt) {
      errors.push(`${label}: has both "tool" and "prompt" set — a step can only be one kind.`);
    } else if (!hasTool && !hasPrompt) {
      errors.push(`${label}: has neither "tool" nor "prompt" — every step must be one or the other.`);
    } else if (hasTool && knownTools && !knownTools.has(step.tool)) {
      errors.push(`${label}: unknown tool "${step.tool}".`);
    }
  });

  steps.forEach((step, i) => {
    const label = typeof step?.id === 'string' && step.id ? `Step "${step.id}"` : `Step ${i + 1}`;
    const refs = [
      ...extractRefs(step.args || {}),
      ...(typeof step.prompt === 'string' ? extractRefs(step.prompt) : []),
    ];
    for (const { ref, root, id } of refs) {
      if (root === 'inputs') {
        if (!inputNames.has(id)) errors.push(`${label}: references "{{${ref}}}" but no input named "${id}" is declared in [[inputs]].`);
      } else if (root === 'steps') {
        if (!stepIndexById.has(id)) errors.push(`${label}: references "{{${ref}}}" but no earlier step has id "${id}".`);
        else if (stepIndexById.get(id) >= i) errors.push(`${label}: references "{{${ref}}}" but step "${id}" hasn't run yet — a step can only reference an EARLIER step.`);
      } else {
        errors.push(`${label}: "{{${ref}}}" isn't a valid reference — use inputs.<name> or steps.<id>.<path>.`);
      }
    }
  });

  return { errors };
}
