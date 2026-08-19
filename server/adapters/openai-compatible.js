// The "any model" adapter — anything that speaks OpenAI's chat-completions
// wire format goes through here: OpenAI itself, and any local or third-party
// server that mimics it (Ollama, LM Studio, OpenRouter, Groq, Together, ...)
// just by pointing `baseUrl` somewhere else. This is what makes "give Jarvis
// a local address" work with zero new code per model — the installed
// `openai` SDK already accepts a custom baseURL.

import OpenAI from 'openai';
import { getSecret } from '../config.js';
import { systemInstructionFor } from '../prompt.js';

// Local/self-hosted servers (Ollama, LM Studio, ...) generally don't need a
// real API key, but the SDK requires a non-empty string. Hosts we know
// actually enforce a key are listed here; anything else with a custom
// baseUrl is assumed to be a local/no-auth server.
// Exported so catalog.js's isLocal check can reuse the exact same rule
// instead of drifting out of sync with a second copy.
export const KEY_REQUIRED_HOSTS = /openai\.com|openrouter\.ai|groq\.com|together\.(ai|xyz)/i;

// What this wire format can do at all (see adapters/index.js). Chat
// Completions carries images (image_url parts) but has no video part type,
// so video media is dropped rather than translated. Note this is the
// *ceiling* for the whole format — a local text-only Ollama model behind it
// is narrowed further by its own caps.vision (models/catalog.js).
// `webSearch` stays false: some hosts behind this adapter do offer it
// (OpenRouter's :online models, for one), but it isn't wired up, and
// claiming a capability that isn't implemented is worse than not having it.
export const CAPABILITIES = { video: false, audio: false, vision: true, webSearch: false };

function resolveKey(entry) {
  if (entry.secretValue !== undefined) return entry.secretValue;
  if (entry.secretRef) return getSecret(entry.secretRef);
  return null;
}

function requireKeyIfNeeded(entry) {
  const needsKey = !entry.baseUrl || KEY_REQUIRED_HOSTS.test(entry.baseUrl);
  if (needsKey && !resolveKey(entry)) {
    const err = new Error('No API key configured.');
    err.code = 'NO_API_KEY';
    throw err;
  }
}

function client(entry) {
  const apiKey = resolveKey(entry) || 'not-needed';
  return new OpenAI({ apiKey, baseURL: entry.baseUrl || undefined });
}

function toolsForOpenAI(tools) {
  return (tools || []).map((t) => ({
    type: 'function',
    function: { name: t.name, description: t.description, parameters: t.parameters },
  }));
}

// Same reasoning as adapters/anthropic.js's mediaToBlocks: no video-input
// path in the OpenAI chat-completions format used here, so `kind: 'video'`
// is dropped rather than degraded into sampled frames (that would need a
// video decoder — real work, out of scope for what Stage 2 actually uses,
// which is screenshots). Images use the standard image_url content part,
// either a data: URI for inline bytes or the item's own URL.
function mediaToParts(media) {
  return (media || [])
    // Images only — the image_url part below is the format's one media path,
    // so video and `document` PDFs are dropped rather than mislabelled.
    .filter((item) => !item.kind || item.kind === 'image')
    .map((item) => {
      const url = item.dataBase64 ? `data:${item.mimeType};base64,${item.dataBase64}` : item.uri;
      return url ? { type: 'image_url', image_url: { url } } : null;
    })
    .filter(Boolean);
}

function toMessages(messages, opts) {
  const out = [{ role: 'system', content: systemInstructionFor(opts) }];
  for (const m of messages) {
    if (m.role === 'user' && m.media?.length) {
      out.push({
        role: 'user',
        content: [...(m.text ? [{ type: 'text', text: m.text }] : []), ...mediaToParts(m.media)],
      });
    } else if (m.role === 'user' && m.text) {
      out.push({ role: 'user', content: m.text });
    } else if (m.role === 'assistant') {
      if (m.toolCalls?.length) {
        out.push({
          role: 'assistant',
          content: m.text || null,
          tool_calls: m.toolCalls.map((c) => ({
            id: c.id,
            type: 'function',
            function: { name: c.name, arguments: JSON.stringify(c.args || {}) },
          })),
        });
      } else if (m.text) {
        out.push({ role: 'assistant', content: m.text });
      }
    } else if (m.role === 'tool' && m.toolResults?.length) {
      for (const r of m.toolResults) {
        out.push({ role: 'tool', tool_call_id: r.id, content: JSON.stringify(r.result) });
      }
    }
  }
  return out;
}

export async function* stream(entry, messages, opts = {}) {
  requireKeyIfNeeded(entry);
  const c = client(entry);
  const tools = opts.tools?.length ? toolsForOpenAI(opts.tools) : undefined;

  const resp = await c.chat.completions.create({
    model: entry.model,
    messages: toMessages(messages, opts),
    tools,
    stream: true,
  });

  let text = '';
  // Tool calls stream as incremental deltas keyed by index — name and
  // arguments both arrive in pieces that must be concatenated until the
  // stream ends.
  const toolCallsByIndex = new Map();

  for await (const chunk of resp) {
    const delta = chunk.choices?.[0]?.delta;
    if (!delta) continue;

    if (delta.content) {
      text += delta.content;
      yield { type: 'chunk', text: delta.content };
    }

    if (delta.tool_calls) {
      for (const tc of delta.tool_calls) {
        const existing = toolCallsByIndex.get(tc.index) || { id: '', name: '', argsText: '' };
        if (tc.id) existing.id = tc.id;
        if (tc.function?.name) existing.name += tc.function.name;
        if (tc.function?.arguments) existing.argsText += tc.function.arguments;
        toolCallsByIndex.set(tc.index, existing);
      }
    }
  }

  if (toolCallsByIndex.size > 0) {
    const calls = Array.from(toolCallsByIndex.values()).map((tc) => {
      let args = {};
      try {
        args = JSON.parse(tc.argsText || '{}');
      } catch {
        // Leave args empty rather than crash on a malformed call.
      }
      return { id: tc.id, name: tc.name, args };
    });
    yield { type: 'call', calls, text: text || undefined };
    return;
  }

  yield { type: 'final', text: text || "Sorry, I didn't quite catch that." };
}

export async function testConnection(entry) {
  try {
    requireKeyIfNeeded(entry);
    const c = client(entry);
    const response = await c.chat.completions.create({
      model: entry.model,
      messages: [{ role: 'user', content: 'Say "ready" and nothing else.' }],
    });
    const text = response.choices[0]?.message?.content;
    if (!text) {
      return { ok: false, error: 'The server responded but with no text — check the model name.' };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: friendlyError(err) };
  }
}

// OpenRouter's real GET /v1/models response includes a
// `pricing: {prompt, completion, image, request}` object of numeric-string
// fields even though the `openai` SDK's typed `Model` interface only
// declares id/created/object/owned_by — unknown JSON properties survive on
// the parsed object at runtime because the SDK doesn't strip them. A local
// Ollama/LM Studio server has no such field at all, hence the `null` case.
function inferBillingFromPricing(m) {
  if (typeof m.id === 'string' && m.id.endsWith(':free')) return 'free';
  const pricing = m.pricing;
  if (!pricing || typeof pricing !== 'object') return null;
  const values = Object.values(pricing)
    .map((v) => Number(v))
    .filter((n) => Number.isFinite(n));
  if (!values.length) return null;
  if (values.every((n) => n === 0)) return 'free';
  if (values.some((n) => n > 0)) return 'paid';
  return null;
}

/**
 * Asks the server what models it has (works against Ollama, LM Studio, and
 * any host implementing GET /v1/models). Throws on failure rather than
 * swallowing the error — server/models/registry.js's discoverModels() is
 * the layer that catches it and turns it into a friendly {models, error}
 * shape, so callers can tell "unreachable address" apart from "reachable,
 * genuinely has zero models" instead of seeing an empty list either way.
 */
export async function listModels(entry) {
  const c = client(entry);
  const list = await c.models.list();
  return (list.data || [])
    .map((m) => ({
      model: m.id,
      label: m.name || m.id,
      // `context_length` isn't in the SDK's typed Model interface but
      // OpenRouter-style hosts really do send it — read it off the raw
      // object rather than trusting the type.
      contextTokens: typeof m.context_length === 'number' ? m.context_length : null,
      billing: inferBillingFromPricing(m),
    }))
    .sort((a, b) => a.model.localeCompare(b.model));
}

// The SDK wraps a refused local connection as `TypeError: fetch failed`
// with `err.message` reduced to a generic "Connection error." — the actual
// ECONNREFUSED is buried a couple of `.cause` links down (confirmed against
// a real refused connection: err.cause.cause.code === 'ECONNREFUSED').
function isConnectionRefused(err) {
  let current = err;
  for (let i = 0; i < 5 && current; i++) {
    if (current.code === 'ECONNREFUSED') return true;
    current = current.cause;
  }
  return false;
}

export function friendlyError(err) {
  if (err?.code === 'NO_API_KEY') return 'No API key configured.';
  const nested = err?.error?.message;
  if (nested) return nested;
  if (isConnectionRefused(err)) return "Couldn't reach that address — is the local server running?";
  return err?.message || 'The request was rejected.';
}
