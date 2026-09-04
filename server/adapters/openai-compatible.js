// The "any model" adapter — anything that speaks OpenAI's chat-completions
// wire format goes through here: OpenAI itself, and any local or third-party
// server that mimics it (Ollama, LM Studio, OpenRouter, Groq, Together, ...)
// just by pointing `baseUrl` somewhere else. This is what makes "give Jarvis
// a local address" work with zero new code per model — the installed
// `openai` SDK already accepts a custom baseURL.

import OpenAI from 'openai';
import { getSecret } from '../config.js';
import { systemInstructionFor } from '../prompt.js';
import { assistantTextOf } from '../conversation.js';

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

// `entry.keyRequired`, when set, is the stored fact from the provider
// catalog / probe (server/models/providers.js, probe.js) — a saved
// connection's own classification, which is more accurate than re-guessing
// from the host every call. Only a boolean is honored; undefined (any
// connection saved before this field existed) falls through to the
// original host-regex guess unchanged, so nothing already saved changes
// behavior.
function requireKeyIfNeeded(entry) {
  const needsKey = typeof entry.keyRequired === 'boolean' ? entry.keyRequired : !entry.baseUrl || KEY_REQUIRED_HOSTS.test(entry.baseUrl);
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

// A short JSON blob shaped like {"error":{"message":"...",...}} sometimes
// arrives as ordinary chat-completion content instead of a real HTTP error —
// confirmed live against the user's own gateway (an aggregator whose own
// upstream pool had failed returned a 200 OK carrying this as the message
// body: `{"error":{"message":"[429]: ... Rate limit exceeded...","type":
// "rate_limit_error","code":"rate_limit_exceeded"}}`). Undetected, that text
// streamed straight to the transcript and TTS as if it were a real reply,
// and the turn was recorded as a healthy success — see stream()'s own
// buffering below for how this is caught. Detected narrowly: only text that
// actually parses as this exact shape counts, so a model legitimately asked
// to produce JSON is never misread.
function errorPayloadMessage(text) {
  const trimmed = (text || '').trim();
  if (!trimmed.startsWith('{')) return null;
  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  const message = parsed?.error?.message;
  return typeof message === 'string' && message ? message : null;
}

// How long a reply that STARTS with '{' is held back (not yet yielded as
// chunks) before giving up on it being an error payload and streaming
// normally from then on — real error blobs seen in practice are well under
// this length; a model legitimately asked to produce longer JSON just
// resumes streaming live past this point instead of appearing all at once.
const ERROR_PAYLOAD_HOLD_CHARS = 500;

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
      // assistantTextOf(), not m.text directly — if this turn was
      // interrupted by a barge-in (see conversation.js), the model should
      // believe it only said what was actually heard, not everything it
      // happened to finish generating after being cut off.
      const spoken = assistantTextOf(m);
      if (m.toolCalls?.length) {
        out.push({
          role: 'assistant',
          content: spoken || null,
          tool_calls: m.toolCalls.map((c) => ({
            id: c.id,
            type: 'function',
            function: { name: c.name, arguments: JSON.stringify(c.args || {}) },
          })),
        });
      } else if (spoken) {
        out.push({ role: 'assistant', content: spoken });
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

  const resp = await c.chat.completions.create(
    {
      model: entry.model,
      messages: toMessages(messages, opts),
      tools,
      stream: true,
      // Without this, an OpenAI-shaped stream emits no usage data at all —
      // it isn't merely discarded the way the other two adapters' usage
      // used to be, it's never requested. See root CLAUDE.md's Cost
      // tracking section. A backend that doesn't recognize the option is
      // expected to ignore an unknown field per the OpenAI-compatible
      // convention every other caller here already relies on; if a given
      // backend never sends the usage chunk anyway, usageMetadata below
      // simply stays null and no usage event is emitted — never fabricated.
      stream_options: { include_usage: true },
    },
    { signal: opts.signal }
  );

  let text = '';
  // Tool calls stream as incremental deltas keyed by index — name and
  // arguments both arrive in pieces that must be concatenated until the
  // stream ends.
  const toolCallsByIndex = new Map();
  let usage = null;

  // See errorPayloadMessage()/ERROR_PAYLOAD_HOLD_CHARS above. `yieldedLength`
  // is how much of `text` has already been sent out as a chunk; `holding` is
  // true only while the reply so far could still plausibly be converging
  // into a short error-shaped JSON blob; `holdDecided` becomes true the
  // moment that's settled either way (first real character isn't '{', or
  // it's grown past the hold length) so the decision is never re-litigated.
  let yieldedLength = 0;
  let holding = false;
  let holdDecided = false;

  /** Releases whatever's been accumulated-but-not-yet-yielded, in place. */
  function* releaseHeld() {
    const toYield = text.slice(yieldedLength);
    if (toYield) yield { type: 'chunk', text: toYield };
    yieldedLength = text.length;
  }

  for await (const chunk of resp) {
    // The final usage chunk (when the backend sends one) carries an EMPTY
    // choices array — checked before the `!delta` skip below, or it would
    // silently fall through unread, the exact bug this comment exists to
    // prevent.
    if (chunk.usage) usage = chunk.usage;

    const delta = chunk.choices?.[0]?.delta;
    if (!delta) continue;

    if (delta.content) {
      text += delta.content;

      if (!holdDecided) {
        if (text.trimStart().startsWith('{')) {
          holding = true;
        } else if (text.trim() !== '') {
          // First real character isn't '{' — this reply will never hold.
          holdDecided = true;
        }
        // else: still all whitespace so far — wait for more before deciding.
      }
      if (holding && text.length - yieldedLength >= ERROR_PAYLOAD_HOLD_CHARS) {
        // Held long enough without looking like a short error blob — give up holding.
        holding = false;
        holdDecided = true;
      }

      if (!holding) yield* releaseHeld();
    }

    if (delta.tool_calls) {
      // A real tool call means this was never an error payload (a model
      // can't legitimately emit both) — release anything still held, in its
      // original position, before recording the tool-call delta.
      if (holding) {
        holding = false;
        holdDecided = true;
        yield* releaseHeld();
      }
      for (const tc of delta.tool_calls) {
        const existing = toolCallsByIndex.get(tc.index) || { id: '', name: '', argsText: '' };
        if (tc.id) existing.id = tc.id;
        if (tc.function?.name) existing.name += tc.function.name;
        if (tc.function?.arguments) existing.argsText += tc.function.arguments;
        toolCallsByIndex.set(tc.index, existing);
      }
    }
  }

  // Stream ended still holding — this is the real decision point for a
  // short reply delivered all at once rather than trickled in over many
  // chunks. A genuine error payload throws here instead of ever reaching
  // the browser; anything else (including a model legitimately asked for
  // short JSON) is released exactly as it would have been without holding.
  if (holding) {
    const message = errorPayloadMessage(text);
    if (message) {
      const err = new Error(message);
      err.code = 'UPSTREAM_ERROR_PAYLOAD';
      throw err;
    }
    yield* releaseHeld();
  }

  if (usage) {
    yield {
      type: 'usage',
      unitKind: 'tokens',
      unitsIn: usage.prompt_tokens ?? null,
      unitsOut: usage.completion_tokens ?? null,
      cachedIn: usage.prompt_tokens_details?.cached_tokens ?? null,
      provider: entry.provider || 'openai-compatible',
      model: entry.model,
    };
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

  // A genuinely empty final response is a real failure, not a fake success —
  // let runner.js's existing failover machinery handle it (mark this model
  // unhealthy, try the next candidate) exactly the way a timeout already
  // does, instead of yielding a placeholder reply that gets shown/spoken as
  // if it were real and marks this model healthy. Worded to naturally match
  // friendly-message.js's own 'transient' text pattern ("try again later")
  // since an empty response from an otherwise-reachable model is more often
  // a momentary hiccup than a permanent problem.
  if (!text) {
    const err = new Error('The model returned an empty response — try again later.');
    err.code = 'EMPTY_RESPONSE';
    throw err;
  }

  yield { type: 'final', text };
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
    // A host requireKeyIfNeeded() assumed was keyless (no keyRequired fact
    // stored yet, host not in KEY_REQUIRED_HOSTS) can still reject an
    // unauthenticated request — this is the exact OmniRoute-class failure
    // that used to surface as "That API key isn't valid" for a key the user
    // never even had a field to type. Named correctly only when no key was
    // actually sent; a real, wrong key on a 401 still goes through
    // friendlyError()'s normal path below.
    if (err?.status === 401 && !resolveKey(entry)) {
      // `friendly: true` tells registry.js's testModelConnection() this
      // message is already final English, not raw provider text — without
      // it, that layer's own friendlyMessage(result.error, ...) re-runs
      // classifyError() on THIS string, finds no auth-pattern match for
      // "needs an API key" (only "invalid key" text matches), and silently
      // replaces it with the generic "That connection didn't work." —
      // confirmed live against a stub gateway before adding this flag.
      return { ok: false, error: 'That server was reached but needs an API key.', friendly: true };
    }
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
