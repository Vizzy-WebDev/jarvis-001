// The user-facing provider catalog — five tiles (OpenAI, Anthropic, Gemini,
// Local server, Custom), replacing the old "Type" dropdown that asked the
// user to pick a wire protocol (openai-compatible/anthropic/gemini) by name.
// A provider is what the user picks; an adapter (server/adapters/) is the
// wire format underneath it — this file is what keeps those two concepts
// from collapsing back into one string. Deliberately data-only, no imports,
// so nothing here can create a circular-import edge (see CLAUDE.md's
// tools/ invariant — this isn't under server/tools/, but the same
// leaf-module discipline keeps it easy to import from anywhere, including
// server.js and the frontend's providers endpoint).
//
// `kind` classifies WHAT is on the other end, independent of `adapter`
// (the wire format): 'first-party' | 'gateway' | 'local' | null (Custom,
// resolved by probe.js at add time). catalog.js reads this stored value
// instead of re-deriving it from a URL regex — see the Provider System
// Refactor design note in CLAUDE.md for why that distinction matters (an
// OpenRouter-class gateway must stay pessimistic about vision; a local
// server must stay billing:'local' and keyless).

export const PROVIDERS = [
  {
    id: 'openai',
    label: 'OpenAI',
    icon: '🤖',
    iconBg: '#10A37F',
    adapter: 'openai-compatible',
    baseUrl: 'https://api.openai.com/v1',
    urlEditable: false,
    keyRequired: true,
    kind: 'first-party',
    suggestions: ['gpt-5.6-luna'],
    keyHint: 'Paste your OpenAI API key.',
  },
  {
    id: 'anthropic',
    label: 'Anthropic',
    icon: '✳️',
    iconBg: '#D97757',
    adapter: 'anthropic',
    baseUrl: null,
    urlEditable: false,
    keyRequired: true,
    kind: 'first-party',
    suggestions: ['claude-haiku-4-5', 'claude-sonnet-5', 'claude-opus-5'],
    keyHint: 'Paste your Anthropic API key.',
  },
  {
    id: 'gemini',
    label: 'Gemini',
    icon: '✨',
    iconBg: '#4285F4',
    adapter: 'gemini',
    baseUrl: null,
    urlEditable: false,
    keyRequired: true,
    kind: 'first-party',
    suggestions: ['gemini-3.5-flash', 'gemini-3.6-flash', 'gemini-3-pro'],
    keyHint: 'Paste your Gemini API key.',
  },
  {
    id: 'local',
    label: 'Local server',
    icon: '💻',
    iconBg: '#4B5563',
    adapter: 'openai-compatible',
    baseUrl: 'http://localhost:11434/v1',
    urlEditable: true,
    keyRequired: false,
    kind: 'local',
    suggestions: ['llama3.1', 'mistral'],
    keyHint: 'Usually not needed for a local server.',
  },
  {
    id: 'custom',
    label: 'Custom',
    icon: '🔧',
    iconBg: '#6B7280',
    // adapter/baseUrl/kind/keyRequired are all resolved by probe.js once the
    // user runs Test & Add — a Custom row has no fixed shape of its own.
    adapter: null,
    baseUrl: null,
    urlEditable: true,
    keyRequired: null,
    kind: null,
    suggestions: [],
    keyHint: 'Leave blank if the server needs no key — Jarvis will tell you if one is required.',
  },
];

export function getProvider(id) {
  return PROVIDERS.find((p) => p.id === id) || null;
}

// Same host rules the openai-compatible adapter already uses for its
// KEY_REQUIRED_HOSTS check — kept here as a private mirror rather than an
// import, since providers.js must stay import-free (this file is read by
// server.js before the adapters are even relevant to it).
const KNOWN_GATEWAY_HOSTS = /openrouter\.ai|groq\.com|together\.(ai|xyz)/i;

/**
 * Read-time backfill for a connection saved BEFORE this catalog existed
 * (today's real example: the two OpenRouter connections in
 * data/connections.json, saved under adapter:'openai-compatible' with no
 * `provider`/`kind` field at all). Never migrates the file — called from
 * registry.js's hydrate() / server.js's publicConnection() so old rows just
 * display and classify correctly at read time, the same pattern
 * catalog.js's withCapabilityDefaults() already uses.
 *
 * Deliberately narrow: only fills in what's missing, never overrides a
 * connection that already has its own `provider`/`kind` saved.
 */
export function providerForLegacy(adapter, baseUrl) {
  if (adapter === 'anthropic') return { provider: 'anthropic', kind: 'first-party' };
  if (adapter === 'gemini') return { provider: 'gemini', kind: 'first-party' };
  if (adapter === 'openai-compatible') {
    if (!baseUrl || /openai\.com/i.test(baseUrl)) return { provider: 'openai', kind: 'first-party' };
    if (KNOWN_GATEWAY_HOSTS.test(baseUrl)) return { provider: 'custom', kind: 'gateway' };
    return { provider: 'custom', kind: 'local' };
  }
  return { provider: 'custom', kind: null };
}
