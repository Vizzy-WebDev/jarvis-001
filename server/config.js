// Handles reading and writing API keys (for any of the supported AI
// providers) and which provider is currently active, to a local .env file.
// Kept dependency-free on purpose (no "dotenv" package) — it's a tiny
// format, no need to add a dependency just to parse "KEY=value" lines.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Overridable for the same reason as store.js's DATA_DIR — lets a test
// server run against a disposable .env instead of the user's real one.
const ENV_PATH = process.env.JARVIS_ENV_PATH || path.join(__dirname, '..', '.env');

const ENV_KEYS = {
  gemini: 'GEMINI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
  openai: 'OPENAI_API_KEY',
};
const ACTIVE_PROVIDER_VAR = 'JARVIS_ACTIVE_PROVIDER';
const DEFAULT_PROVIDER = 'gemini';

function parseEnvFile(text) {
  const result = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    // Strip surrounding quotes if present.
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    result[key] = value;
  }
  return result;
}

function readEnvFile() {
  if (!fs.existsSync(ENV_PATH)) return {};
  return parseEnvFile(fs.readFileSync(ENV_PATH, 'utf8'));
}

function writeEnvFile(values) {
  const lines = Object.entries(values)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${v}`);
  fs.writeFileSync(ENV_PATH, lines.join('\n') + '\n', 'utf8');
}

/** Returns the saved API key for a provider ('gemini' | 'anthropic' | 'openai'), or null. */
export function getProviderKey(provider) {
  const envVar = ENV_KEYS[provider];
  if (!envVar) return null;
  // Allow an environment variable to override the file, for advanced users.
  if (process.env[envVar]) return process.env[envVar];
  return readEnvFile()[envVar] || null;
}

/** Saves a provider's API key to the local .env file (creates it if needed). */
export function saveProviderKey(provider, key) {
  const envVar = ENV_KEYS[provider];
  if (!envVar) throw new Error(`Unknown provider: ${provider}`);
  const trimmed = String(key || '').trim();
  const values = readEnvFile();
  values[envVar] = trimmed;
  writeEnvFile(values);
  process.env[envVar] = trimmed;
}

/** Which providers currently have a saved, non-empty key. */
export function getConfiguredProviders() {
  return Object.keys(ENV_KEYS).filter((p) => Boolean(getProviderKey(p)));
}

/** Which provider is currently selected to drive the conversation. */
export function getActiveProvider() {
  return readEnvFile()[ACTIVE_PROVIDER_VAR] || DEFAULT_PROVIDER;
}

export function setActiveProvider(provider) {
  if (!ENV_KEYS[provider]) throw new Error(`Unknown provider: ${provider}`);
  const values = readEnvFile();
  values[ACTIVE_PROVIDER_VAR] = provider;
  writeEnvFile(values);
}

// --- Backward-compatible Gemini-specific helpers ---
// Gemini is also used directly for text-to-speech regardless of which
// provider is driving the conversation (that's a separate user choice), so
// these convenience wrappers stay even after the multi-provider work below.

export function getApiKey() {
  return getProviderKey('gemini');
}

export function saveApiKey(key) {
  return saveProviderKey('gemini', key);
}

// --- Generic secrets, for the model registry ---
// Every model a user adds (cloud key, local server token, ...) needs
// somewhere safe to keep its secret. Rather than growing ENV_KEYS forever,
// any model entry can point at a `secretRef` string the registry makes up
// when the model is added (see server/models/registry.js) — stored as
// JARVIS_SECRET_<REF>.
//
// Exception: the three original built-in providers keep using their
// original ENV_KEYS entry (GEMINI_API_KEY etc.) so migrating an existing
// .env doesn't duplicate a key into a second variable — getSecret('gemini')
// and getProviderKey('gemini') read the exact same value.

const SECRET_PREFIX = 'JARVIS_SECRET_';

function secretEnvVar(ref) {
  return `${SECRET_PREFIX}${String(ref || '').toUpperCase()}`;
}

/** Returns the saved secret for a given ref, or null. */
export function getSecret(ref) {
  if (!ref) return null;
  if (ENV_KEYS[ref]) return getProviderKey(ref);
  const envVar = secretEnvVar(ref);
  if (process.env[envVar]) return process.env[envVar];
  return readEnvFile()[envVar] || null;
}

/** Saves a secret under a ref (creates the .env entry if needed). */
export function saveSecret(ref, value) {
  if (!ref) throw new Error('saveSecret needs a ref.');
  if (ENV_KEYS[ref]) return saveProviderKey(ref, value);
  const envVar = secretEnvVar(ref);
  const trimmed = String(value || '').trim();
  const values = readEnvFile();
  values[envVar] = trimmed;
  writeEnvFile(values);
  process.env[envVar] = trimmed;
}

/** Removes a saved secret. Empty values are dropped entirely by writeEnvFile. */
export function deleteSecret(ref) {
  if (!ref) return;
  const envVar = ENV_KEYS[ref] ? ENV_KEYS[ref] : secretEnvVar(ref);
  const values = readEnvFile();
  delete values[envVar];
  writeEnvFile(values);
  delete process.env[envVar];
}
