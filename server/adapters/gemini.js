// Gemini adapter — translates the neutral conversation (server/conversation.js)
// to/from Google's wire format. One of three adapters covering every model a
// user can add (see adapters/index.js); this one and adapters/anthropic.js
// are the two that need a dedicated SDK, unlike adapters/openai-compatible.js
// which covers everything OpenAI-shaped (OpenAI itself, Ollama, LM Studio,
// OpenRouter, Groq, ...) through one client.
//
// The `raw.content` round-trip below is the fix for a real gotcha (see
// CLAUDE.md): Gemini tool-calling turns carry a `thought_signature` that
// must be replayed verbatim on the next call, or follow-up calls get
// rejected with a 400. We can't reconstruct that from the neutral fields —
// so whenever a message's raw.adapter is 'gemini', we push its raw.content
// back exactly as Gemini gave it to us, and only fall back to rebuilding a
// plain {role,parts} object for messages that originated on a *different*
// model (which have no thought_signature to preserve in the first place).

import { GoogleGenAI } from '@google/genai';
import { getSecret } from '../config.js';
import { systemInstructionFor } from '../prompt.js';

// What this wire format can do at all — the ceiling for every model saved
// under a Gemini connection. A model's own caps (models/catalog.js) can
// narrow this but never exceed it. Gemini is currently the only adapter
// that implements video/audio understanding and grounded web search; the
// other two declare them false until someone teaches them, and
// models/router.js filters on the result. That's the whole reason Content
// Analysis never says "Gemini" anywhere in its own code.
export const CAPABILITIES = { video: true, audio: true, vision: true, webSearch: true };

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

// Gemini takes images and video the same way — an inline base64 blob, or a
// `fileData` reference for anything the model should fetch itself (this is
// also how a plain YouTube URL is passed through, no download needed on our
// side at all). Genuinely native video support, not a degraded fallback —
// see adapters/anthropic.js and openai-compatible.js for the other two,
// which have no such path and drop video media rather than crash.
function mediaToParts(media) {
  return (media || [])
    .map((item) => {
      if (item.uri) return { fileData: { mimeType: item.mimeType, fileUri: item.uri } };
      if (item.dataBase64) return { inlineData: { mimeType: item.mimeType, data: item.dataBase64 } };
      return null;
    })
    .filter(Boolean);
}

function toContents(messages) {
  const contents = [];
  for (const m of messages) {
    if (m.role === 'user' && (m.text || m.media?.length)) {
      const parts = [...(m.text ? [{ text: m.text }] : []), ...mediaToParts(m.media)];
      contents.push({ role: 'user', parts });
    } else if (m.role === 'assistant') {
      if (m.raw?.adapter === 'gemini' && m.raw.content) {
        contents.push(m.raw.content);
      } else if (m.toolCalls?.length) {
        contents.push({
          role: 'model',
          parts: m.toolCalls.map((c) => ({ functionCall: { name: c.name, args: c.args } })),
        });
      } else if (m.text) {
        contents.push({ role: 'model', parts: [{ text: m.text }] });
      }
    } else if (m.role === 'tool' && m.toolResults?.length) {
      contents.push({
        role: 'user',
        parts: m.toolResults.map((r) => ({ functionResponse: { name: r.name, response: r.result } })),
      });
    }
  }
  return contents;
}

export async function* stream(entry, messages, opts = {}) {
  const apiKey = requireKey(entry);
  const ai = new GoogleGenAI({ apiKey });
  const contents = toContents(messages);
  const tools = opts.tools?.length ? [{ functionDeclarations: opts.tools }] : undefined;

  const genStream = await ai.models.generateContentStream({
    model: entry.model,
    contents,
    config: { tools, systemInstruction: systemInstructionFor(opts) },
  });

  // Each streamed chunk is an incremental delta, not cumulative — parts
  // must be concatenated across all chunks to reconstruct the full turn.
  const turnParts = [];
  const calls = [];
  let text = '';

  for await (const chunk of genStream) {
    const chunkParts = chunk.candidates?.[0]?.content?.parts;
    if (chunkParts) turnParts.push(...chunkParts);
    if (chunk.text) {
      text += chunk.text;
      yield { type: 'chunk', text: chunk.text };
    }
    if (chunk.functionCalls?.length) calls.push(...chunk.functionCalls);
  }

  if (calls.length > 0) {
    yield {
      type: 'call',
      calls: calls.map((c, i) => ({ id: c.id || `call_${i}`, name: c.name, args: c.args })),
      raw: { adapter: 'gemini', content: { role: 'model', parts: turnParts } },
    };
    return;
  }

  yield {
    type: 'final',
    text: text || "Sorry, I didn't quite catch that.",
    raw: { adapter: 'gemini', content: { role: 'model', parts: turnParts.length ? turnParts : [{ text }] } },
  };
}

// ---------- capability extras (see adapters/index.js) ----------

/**
 * Answers a question using Google Search, with real citations — the
 * `webSearch: true` half of this adapter's CAPABILITIES, used by
 * server/research.js.
 *
 * Deliberately a standalone call with `googleSearch` as the ONLY tool and no
 * function declarations alongside it: mixing server-side grounding with our
 * own tool list is exactly the kind of combination that's been restricted on
 * and off across Gemini versions, and research never needs Jarvis's own
 * skills anyway. Costs one request against the key's quota.
 *
 * `groundingMetadata.groundingChunks[].web` is where the sources live; it's
 * absent when the model chose not to search, which is not an error — the
 * answer is still returned, just with an empty source list, and research.js
 * decides what to do about that.
 */
export async function searchGrounded(entry, query) {
  const apiKey = requireKey(entry);
  const ai = new GoogleGenAI({ apiKey });

  const response = await ai.models.generateContent({
    model: entry.model,
    contents: [{ role: 'user', parts: [{ text: String(query || '') }] }],
    config: { tools: [{ googleSearch: {} }] },
  });

  const chunks = response.candidates?.[0]?.groundingMetadata?.groundingChunks || [];
  const sources = [];
  const seen = new Set();
  for (const chunk of chunks) {
    const uri = chunk?.web?.uri;
    if (!uri || seen.has(uri)) continue;
    seen.add(uri);
    sources.push({ title: chunk.web.title || uri, url: uri });
  }

  return { answer: response.text || '', sources };
}

/**
 * Uploads a local file so a model can read it, returning a reference that
 * drops straight into a neutral message's `media` array as `{uri, mimeType}`
 * (see conversation.js) — the `video`/`audio` half of CAPABILITIES.
 *
 * The Files API is asynchronous: a video comes back PROCESSING and is
 * unusable until it flips to ACTIVE, so this polls rather than returning
 * immediately. All three calls (`upload`, `get`, `delete`) were confirmed
 * against the installed SDK's genai.d.ts, not docs — per CLAUDE.md's gotcha
 * about Gemini API shape drift.
 *
 * The returned `cleanup()` deletes the uploaded copy from Google's side.
 * Callers should await it when they're done; files expire on their own after
 * ~48 hours regardless, so a missed cleanup leaks nothing permanent.
 */
export async function uploadFile(entry, filePath, mimeType, { timeoutMs = 5 * 60 * 1000 } = {}) {
  const apiKey = requireKey(entry);
  const ai = new GoogleGenAI({ apiKey });

  let file = await ai.files.upload({ file: filePath, config: mimeType ? { mimeType } : undefined });

  const startedAt = Date.now();
  while (file.state === 'PROCESSING') {
    if (Date.now() - startedAt > timeoutMs) {
      throw new Error('That file took too long to process — it may be too large.');
    }
    await new Promise((r) => setTimeout(r, 2000));
    file = await ai.files.get({ name: file.name });
  }

  if (file.state === 'FAILED') {
    throw new Error(file.error?.message || "That file couldn't be processed.");
  }

  return {
    uri: file.uri,
    mimeType: file.mimeType || mimeType,
    async cleanup() {
      try {
        await ai.files.delete({ name: file.name });
      } catch {
        // Expires on its own within ~48h — never worth failing a finished job over.
      }
    },
  };
}

export async function testConnection(entry) {
  try {
    const apiKey = requireKey(entry);
    const ai = new GoogleGenAI({ apiKey });
    const response = await ai.models.generateContent({
      model: entry.model,
      contents: [{ role: 'user', parts: [{ text: 'Say "ready" and nothing else.' }] }],
    });
    if (!response.text) {
      return { ok: false, error: 'The API responded but with no text — the key may be restricted.' };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: friendlyError(err) };
  }
}

/**
 * Lists the models this key can actually reach, via `ai.models.list()`
 * (confirmed against the installed SDK's genai.d.ts — `ListModelsParameters`
 * / `Pager<Model>` / `Model.{name,displayName,inputTokenLimit,
 * supportedActions}` — not from docs, per the CLAUDE.md gotcha about Gemini
 * API shape drift). `Pager` is async-iterable, so `for await` walks every
 * page without manual pageToken handling. Filtered down to models that can
 * actually chat (`supportedActions` includes `generateContent`) — otherwise
 * embedding-only/tuning-only models would show up as pickable chat models.
 * Throws on failure (bad key, network error, ...) — server/models/registry.js's
 * discoverModels() is what turns that into a friendly {models, error} shape.
 */
export async function listModels(entry) {
  const apiKey = requireKey(entry);
  const ai = new GoogleGenAI({ apiKey });
  const pager = await ai.models.list();

  const out = [];
  for await (const m of pager) {
    if (!m.supportedActions?.includes('generateContent')) continue;
    const rawName = m.name || '';
    const model = rawName.startsWith('models/') ? rawName.slice('models/'.length) : rawName;
    if (!model) continue;
    out.push({
      model,
      label: m.displayName || model,
      contextTokens: typeof m.inputTokenLimit === 'number' ? m.inputTokenLimit : null,
      // Google's API exposes no pricing field at all — catalog.js's
      // inferBilling() fills this in from known-model heuristics instead.
      billing: null,
    });
  }
  return out;
}

export function friendlyError(err) {
  if (err?.code === 'NO_API_KEY') return 'No API key configured.';
  const raw = err?.message || '';
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.error?.message) return parsed.error.message;
  } catch {
    // Not JSON — fall through and use it as-is.
  }
  return raw || 'The key was rejected by Google.';
}
