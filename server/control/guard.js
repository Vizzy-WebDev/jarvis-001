// The one place that answers two questions for every computer-control
// action: "how risky is this?" and "is this window/process/URL off-limits?".
// Pure logic, no I/O beyond the safety config passed in — directly testable
// with a one-off `node --input-type=module -e "..."` script per CLAUDE.md's
// pattern for pure-logic modules (no server needed).
//
// Risk tiers (per the design plan): 'safe' (look/move/focus/scroll — never
// needs a confirm), 'notable' (click/type/open — visible, but reversible;
// shown live, not gated), 'risky' (delete/send/publish/pay/install, or
// anything that can't be undone — always routed through the existing
// confirm-and-read-back gate in skills/index.js before it's allowed to run).

// Exact action kinds Stage 2's PowerShell bridge (control/agent.ps1) can
// issue, mapped to a tier. Anything not listed here falls through to the
// keyword scan below — e.g. a future connector action like "delete_file" or
// "send_email" that isn't one of these low-level primitives.
const RISK_BY_KIND = {
  windows: 'safe',
  focus: 'safe',
  read_window: 'safe',
  screenshot: 'safe',
  screenshot_burst: 'safe',
  cursor: 'safe',
  idle: 'safe',
  processes: 'safe',
  scroll: 'safe',
  click: 'notable',
  double_click: 'notable',
  right_click: 'notable',
  type: 'notable',
  key: 'notable',
};

// Any action whose kind isn't in the map above is scanned for these — a
// simple, honest keyword match rather than a false sense of precision. This
// deliberately errs toward calling something risky: a missed "notable" is a
// mildly annoying extra confirmation, a missed "risky" is the failure mode
// that actually matters.
const RISKY_KEYWORDS = [
  'delete', 'remove', 'trash', 'uninstall',
  'send', 'email', 'message', 'publish', 'post', 'share', 'tweet',
  'pay', 'purchase', 'buy', 'checkout', 'transfer', 'wire',
  'install', 'download_and_run', 'execute', 'format', 'overwrite',
  // Added after live testing found real connector tools that make an
  // irreversible-ish change but matched none of the above — e.g. Notion's
  // own "update-page" (can overwrite existing content) and "move-pages", and
  // the built-in files connector's write_file/move_file. Confirmed with the
  // user this trades a bit more day-to-day confirmation friction for the
  // "risky always confirms" guarantee actually covering real actions.
  'update', 'write', 'move',
  // 'order' was tried here too (for "place an order") and dropped — it's a
  // common enough word with an innocent meaning ("in sidebar order", i.e.
  // sequence) that it false-positived on a harmless real Notion tool
  // (notion-list-favorite-pages) in testing. purchase/buy/checkout already
  // cover the "spends money" intent without that ambiguity, and since
  // 'risky' can no longer be waived with "Always allow", a bad keyword here
  // is now a permanent annoyance, not a one-time one — worth being choosier.
];

// Splits into whole words (handling hyphens, underscores, AND camelCase
// boundaries — same tokenization idea as _connector-detail.js's groupFor(),
// duplicated rather than shared since that file runs in the browser and this
// one runs in Node). Real bug found in live testing: plain `.includes()`
// substring matching treated "SharePoint" as containing "share" and "in
// sidebar order" as containing "order" — a real MCP server's tool
// descriptions are prose, not short labels, and are full of incidental
// words. Word-tokenized exact matching means "share" only matches the whole
// word "share", never "shared" or "sharepoint".
function words(text) {
  return String(text || '')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .toLowerCase()
    .split(/[^a-z]+/)
    .filter(Boolean);
}

/**
 * 'safe' | 'notable' | 'risky' for one action. `action` is `{kind, ...}` —
 * `label`/`description` (e.g. a model's own stated reasoning for the step,
 * or a connector tool's short description) are also scanned, and checked
 * BEFORE the kind lookup — defense in depth, so a low-level primitive kind
 * like 'key' or 'type' still escalates to 'risky' if what it's actually
 * doing describes something irreversible (e.g. typing "rm -rf" into a
 * terminal, or reasoning that says "delete this permanently"). Without this
 * ordering, RISK_BY_KIND would short-circuit every desktop-control primitive
 * straight to 'safe'/'notable' regardless of intent, since none of Stage 2's
 * own action kinds appear in that map.
 *
 * Callers passing a long, prose-style description (a real MCP tool's own
 * documentation, which can run to a couple thousand characters with worked
 * examples) should pass only its first sentence — see connectors/index.js's
 * shortDescription(). Scanning an entire multi-paragraph doc for keywords
 * finds far more incidental matches than genuine ones.
 */
export function classifyActionRisk(action) {
  const kindRaw = String(action?.kind || '');
  const kind = kindRaw.toLowerCase(); // only for the RISK_BY_KIND lookup below
  const proseText = `${action?.label || ''} ${action?.description || ''}`;
  // `kindRaw` is tokenized with its ORIGINAL case, `proseText` pre-lowered
  // before tokenizing — two different text shapes need two different rules:
  //   - `kindRaw` is an identifier (a tool/action name like "updatePet" or
  //     "COMPOSIO_MULTI_EXECUTE_TOOL") — words() needs the real casing to
  //     find a camelCase boundary at all; an earlier version lowercased it
  //     first, which silently made words()'s own camelCase-split a no-op
  //     (nothing uppercase left to find) and let a real risky identifier
  //     slip through unclassified.
  //   - `proseText` (label/description) is natural language, already
  //     space-separated — running the SAME camelCase-split regex over it
  //     finds accidental boundaries inside ordinary capitalized proper nouns
  //     ("SharePoint" -> "Share"+"Point" -> matches the keyword "share"),
  //     which is the exact false-positive class this file was already
  //     rewritten once to avoid. Lowercasing prose before it reaches words()
  //     makes that split step a no-op for prose, same as before, while the
  //     identifier fix above still applies only where it's actually needed.
  const wordSet = new Set([...words(kindRaw), ...words(proseText.toLowerCase())]);
  const lowerText = `${kindRaw} ${proseText}`.toLowerCase();
  const risky = RISKY_KEYWORDS.some((word) =>
    // The one compound keyword (has an underscore of its own) can never
    // survive word-splitting as a single token, so it's checked as a plain
    // substring instead — safe to do only because it's specific enough
    // ("download_and_run") that an incidental match is very unlikely.
    word.includes('_') ? lowerText.includes(word) : wordSet.has(word)
  );
  if (risky) return 'risky';
  if (RISK_BY_KIND[kind]) return RISK_BY_KIND[kind];
  // Unknown and no risky keyword matched — treated as 'notable' rather than
  // 'safe': an action guard.js has never seen before shouldn't get the
  // free pass reserved for the known-harmless primitives above.
  return 'notable';
}

function matchesAny(value, patterns) {
  if (!value) return null;
  const lower = String(value).toLowerCase();
  return patterns.find((p) => p && lower.includes(String(p).toLowerCase())) || null;
}

/**
 * Checks a window/process/URL against the safety config's blocklist.
 * `subject` is `{windowTitle, processName, url}` — any subset; only the
 * fields present are checked. Returns `{blocked: false}` or
 * `{blocked: true, reason, matched}` (`matched` is the pattern that hit, for
 * a clear "why" in the message shown to the user).
 */
export function checkBlocklist(subject, safetyConfig) {
  const titleHit = matchesAny(subject?.windowTitle, safetyConfig?.blockedWindowPatterns || []);
  if (titleHit) {
    return { blocked: true, reason: `the window title matches a blocked pattern ("${titleHit}")`, matched: titleHit };
  }
  const processHit = matchesAny(subject?.processName, safetyConfig?.blockedProcesses || []);
  if (processHit) {
    return { blocked: true, reason: `"${subject.processName}" is on the blocked apps list`, matched: processHit };
  }
  const urlHit = matchesAny(subject?.url, safetyConfig?.blockedUrlPatterns || []);
  if (urlHit) {
    return { blocked: true, reason: `the address matches a blocked pattern ("${urlHit}")`, matched: urlHit };
  }
  return { blocked: false };
}

/** Combines both checks for one action against one on-screen subject — what control/session.js (Stage 2) calls before every act step. Blocklist wins outright (a blocked window is refused regardless of how safe the action itself would otherwise be). */
export function evaluateAction(action, subject, safetyConfig) {
  const blocklist = checkBlocklist(subject, safetyConfig);
  if (blocklist.blocked) {
    return { allowed: false, reason: `I won't act there — ${blocklist.reason}.`, risk: 'blocked' };
  }
  const risk = classifyActionRisk(action);
  return { allowed: true, risk, needsConfirm: risk === 'risky' };
}
