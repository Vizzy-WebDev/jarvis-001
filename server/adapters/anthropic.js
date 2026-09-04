// Anthropic (Claude) adapter — translates the neutral conversation
// (server/conversation.js) to/from Anthropic's Messages API wire format.
// See adapters/index.js for how the three adapters fit together.

import Anthropic from '@anthropic-ai/sdk';
import { getSecret } from '../config.js';
import { systemInstructionParts } from '../prompt.js';
import { assistantTextOf } from '../conversation.js';

const MAX_TOKENS = 1024;

// What this wire format can do at all (see adapters/index.js). Images yes,
// video no — the Messages API has no video content block, which is why
// toMessages() below drops a video media item instead of translating it.
// `webSearch` is false because Anthropic's server-side web-search tool isn't
// wired up here yet; flipping it to true means adding a `searchGrounded()`
// export, and server/research.js will start using this adapter for research
// automatically the moment it does.
export const CAPABILITIES = { video: false, audio: false, vision: true, webSearch: false };

function resolveKey(entry) {
  if (entry.secretValue !== undefined) return entry.secretValue;
  if (entry.secretRef) return getSecret(entry.secretRef);
  return null;
}

function requireKey(entry) {
  const apiKey = resolveKey(entry);
  if (!apiKey) {
    const err = new Error('No API key configured.');
    err.code = 'NO_API_KEY';
    throw err;
  }
  return apiKey;
}

// `entry.baseUrl` is set only for a Custom connection whose probe (see
// server/models/probe.js) resolved to this adapter's wire shape against a
// non-Anthropic host — real Anthropic accounts never set it. Undefined
// falls through to the SDK's own default (api.anthropic.com).
function client(entry) {
  const apiKey = requireKey(entry);
  return new Anthropic({ apiKey, baseURL: entry.baseUrl || undefined });
}

// Anthropic has no video-input path in this API — a `kind: 'video'` media
// item is dropped rather than translated (see conversation.js's media field
// comment; Gemini is the one adapter with genuine native video support).
// Images translate to a proper content block either way (inline base64 or a
// plain URL source, whichever the item carries).
function mediaToBlocks(media) {
  return (media || [])
    // Images only. Everything below builds an `image` block, so anything that
    // isn't one (video, or a `document` PDF carried as inline bytes) must be
    // dropped rather than mislabelled as an image the API will reject.
    .filter((item) => !item.kind || item.kind === 'image')
    .map((item) => {
      if (item.dataBase64) {
        return { type: 'image', source: { type: 'base64', media_type: item.mimeType, data: item.dataBase64 } };
      }
      if (item.uri) {
        return { type: 'image', source: { type: 'url', url: item.uri } };
      }
      return null;
    })
    .filter(Boolean);
}

function toMessages(messages) {
  const out = [];
  for (const m of messages) {
    if (m.role === 'user' && m.media?.length) {
      out.push({
        role: 'user',
        content: [...(m.text ? [{ type: 'text', text: m.text }] : []), ...mediaToBlocks(m.media)],
      });
    } else if (m.role === 'user' && m.text) {
      out.push({ role: 'user', content: m.text });
    } else if (m.role === 'assistant') {
      // assistantTextOf(), not m.text directly — see openai-compatible.js's
      // identical comment. The raw-content shortcut below is deliberately
      // skipped when interrupted: `raw` is exactly the FULL content
      // Anthropic generated, which is precisely what must NOT be replayed
      // once a barge-in cut it short — falling through to the plain
      // spoken-text branch instead loses the raw round-trip fidelity for
      // that one turn, which is the correct trade: an interrupted turn is
      // already an edited turn, that fidelity only mattered for the
      // untruncated version.
      const spoken = assistantTextOf(m);
      if (!m.interrupted && m.raw?.adapter === 'anthropic' && m.raw.content) {
        out.push({ role: 'assistant', content: m.raw.content });
      } else if (m.toolCalls?.length) {
        out.push({
          role: 'assistant',
          content: [
            ...(spoken ? [{ type: 'text', text: spoken }] : []),
            ...m.toolCalls.map((c) => ({ type: 'tool_use', id: c.id, name: c.name, input: c.args })),
          ],
        });
      } else if (spoken) {
        out.push({ role: 'assistant', content: spoken });
      }
    } else if (m.role === 'tool' && m.toolResults?.length) {
      out.push({
        role: 'user',
        content: m.toolResults.map((r) => ({
          type: 'tool_result',
          tool_use_id: r.id,
          content: JSON.stringify(r.result),
        })),
      });
    }
  }
  return out;
}

// Our skill parameter schemas are already plain JSON Schema objects
// ({type, properties, required}), which is exactly the shape Anthropic's
// input_schema expects — no translation needed.
function toolsForAnthropic(tools) {
  return (tools || []).map((t) => ({ name: t.name, description: t.description, input_schema: t.parameters }));
}

export async function* stream(entry, messages, opts = {}) {
  const c = client(entry);
  const tools = toolsForAnthropic(opts.tools);

  // Two text blocks, not one plain string: `cache_control` on the stable
  // block marks "everything up to and including this is a cache
  // breakpoint" — a turn whose memories/pending-jobs haven't changed since
  // the last one reuses Anthropic's cached prefix (lower latency, lower
  // cost) instead of a full re-prefill on every single turn. The volatile
  // block (current time, "N minutes ago", low-confidence note) rides
  // AFTER the breakpoint with no cache_control of its own, so it never
  // needs to match byte-for-byte for the cached part to still hit — see
  // prompt.js's systemInstructionParts().
  const { stable, volatile } = systemInstructionParts(opts);
  const system = volatile
    ? [
        { type: 'text', text: stable, cache_control: { type: 'ephemeral' } },
        { type: 'text', text: volatile },
      ]
    : [{ type: 'text', text: stable, cache_control: { type: 'ephemeral' } }];

  const s = c.messages.stream(
    {
      model: entry.model,
      max_tokens: MAX_TOKENS,
      system,
      messages: toMessages(messages),
      tools,
    },
    { signal: opts.signal }
  );

  for await (const event of s) {
    if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
      yield { type: 'chunk', text: event.delta.text };
    }
  }

  // finalMessage() assembles the complete response (full tool_use blocks
  // included) from the events we just streamed — the documented way to get
  // both live deltas and a complete final object from one request.
  const message = await s.finalMessage();

  // message.usage was sitting on this same object the whole time and was
  // never read — see root CLAUDE.md's Cost tracking section. Emitted before
  // either terminal branch below so a tool-calling step's usage is captured
  // too, not just a turn's final answer. cache_read_input_tokens is what
  // makes the cache_control breakpoint above's actual payoff measurable for
  // the first time.
  if (message.usage) {
    yield {
      type: 'usage',
      unitKind: 'tokens',
      unitsIn: message.usage.input_tokens ?? null,
      unitsOut: message.usage.output_tokens ?? null,
      cachedIn: message.usage.cache_read_input_tokens ?? null,
      cacheWriteIn: message.usage.cache_creation_input_tokens ?? null,
      provider: 'anthropic',
      model: entry.model,
    };
  }

  const toolUses = message.content.filter((b) => b.type === 'tool_use');

  if (message.stop_reason === 'tool_use' && toolUses.length > 0) {
    yield {
      type: 'call',
      calls: toolUses.map((t) => ({ id: t.id, name: t.name, args: t.input })),
      raw: { adapter: 'anthropic', content: message.content },
    };
    return;
  }

  // Join ALL text blocks, not just the first — a reply can carry more than
  // one (e.g. text before and after a thinking/tool-adjacent block), and
  // `.find()` here used to silently drop everything after the first one:
  // the UI (which streams every text_delta above) would show the full
  // reply, but the transcript, the `done` event, and Chat History would
  // all persist only its opening fragment — invisible until a model switch
  // dropped the raw round-trip that was papering over it.
  const text = message.content
    .filter((b) => b.type === 'text')
    .map((b) => b.text)
    .join('');
  // A genuinely empty final response is a real failure, not a fake success —
  // let runner.js's existing failover machinery handle it (mark this model
  // unhealthy, try the next candidate) exactly the way a timeout already
  // does, instead of yielding a placeholder reply that gets shown/spoken as
  // if it were real and marks this model healthy. See
  // openai-compatible.js's own stream() for the same fix and its reasoning.
  if (!text) {
    const err = new Error('The model returned an empty response — try again later.');
    err.code = 'EMPTY_RESPONSE';
    throw err;
  }

  yield {
    type: 'final',
    text,
    raw: { adapter: 'anthropic', content: message.content },
  };
}

export async function testConnection(entry) {
  try {
    const c = client(entry);
    const response = await c.messages.create({
      model: entry.model,
      max_tokens: 10,
      messages: [{ role: 'user', content: 'Say "ready" and nothing else.' }],
    });
    const textBlock = response.content.find((b) => b.type === 'text');
    if (!textBlock?.text) {
      return { ok: false, error: 'The API responded but with no text — the key may be restricted.' };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: friendlyError(err) };
  }
}

/**
 * Lists the models this key can actually reach, via `client.models.list()`
 * (confirmed against the installed SDK's resources/models.d.ts —
 * `list(): PagePromise<ModelInfosPage, ModelInfo>` and `ModelInfo.{id,
 * display_name, max_input_tokens, capabilities}`). `PagePromise` is
 * async-iterable, so `for await` walks every page without manual cursor
 * handling. Throws on failure (bad key, network error, ...) —
 * server/models/registry.js's discoverModels() is what turns that into a
 * friendly {models, error} shape.
 */
export async function listModels(entry) {
  const c = client(entry);
  const page = await c.models.list();

  const out = [];
  for await (const m of page) {
    out.push({
      model: m.id,
      label: m.display_name || m.id,
      contextTokens: typeof m.max_input_tokens === 'number' ? m.max_input_tokens : null,
      // Anthropic's API exposes no pricing field at all — catalog.js's
      // inferBilling() fills this in from known-model heuristics instead.
      billing: null,
    });
  }
  return out;
}

export function friendlyError(err) {
  if (err?.code === 'NO_API_KEY') return 'No API key configured.';
  const nested = err?.error?.error?.message;
  if (nested) return nested;
  return err?.message || 'The key was rejected by Anthropic.';
}
