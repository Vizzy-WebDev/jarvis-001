// Anthropic (Claude) adapter — translates the neutral conversation
// (server/conversation.js) to/from Anthropic's Messages API wire format.
// See adapters/index.js for how the three adapters fit together.

import Anthropic from '@anthropic-ai/sdk';
import { getSecret } from '../config.js';
import { systemInstructionFor } from '../prompt.js';

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
      if (m.raw?.adapter === 'anthropic' && m.raw.content) {
        out.push({ role: 'assistant', content: m.raw.content });
      } else if (m.toolCalls?.length) {
        out.push({
          role: 'assistant',
          content: [
            ...(m.text ? [{ type: 'text', text: m.text }] : []),
            ...m.toolCalls.map((c) => ({ type: 'tool_use', id: c.id, name: c.name, input: c.args })),
          ],
        });
      } else if (m.text) {
        out.push({ role: 'assistant', content: m.text });
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
  const apiKey = requireKey(entry);
  const client = new Anthropic({ apiKey });
  const tools = toolsForAnthropic(opts.tools);

  const s = client.messages.stream({
    model: entry.model,
    max_tokens: MAX_TOKENS,
    system: systemInstructionFor(opts),
    messages: toMessages(messages),
    tools,
  });

  for await (const event of s) {
    if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
      yield { type: 'chunk', text: event.delta.text };
    }
  }

  // finalMessage() assembles the complete response (full tool_use blocks
  // included) from the events we just streamed — the documented way to get
  // both live deltas and a complete final object from one request.
  const message = await s.finalMessage();
  const toolUses = message.content.filter((b) => b.type === 'tool_use');

  if (message.stop_reason === 'tool_use' && toolUses.length > 0) {
    yield {
      type: 'call',
      calls: toolUses.map((t) => ({ id: t.id, name: t.name, args: t.input })),
      raw: { adapter: 'anthropic', content: message.content },
    };
    return;
  }

  const textBlock = message.content.find((b) => b.type === 'text');
  yield {
    type: 'final',
    text: textBlock?.text || "Sorry, I didn't quite catch that.",
    raw: { adapter: 'anthropic', content: message.content },
  };
}

export async function testConnection(entry) {
  try {
    const apiKey = requireKey(entry);
    const client = new Anthropic({ apiKey });
    const response = await client.messages.create({
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
  const apiKey = requireKey(entry);
  const client = new Anthropic({ apiKey });
  const page = await client.models.list();

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
