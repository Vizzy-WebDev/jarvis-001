// Working out what a piece of content IS, for free, and preparing it for a
// model to actually look at when — and only when — the user says what they
// want.
//
// The split between this file and investigator.js is the load-bearing part
// of the whole rebuild: `identify()` below makes NO model call, ever. It
// answers "what is this" (a title, a rough kind, a length) from cheap
// metadata alone — YouTube's oEmbed endpoint, a page's <title>, a file's
// size on disk. That is what lets Jarvis say "that's a 40-minute video on
// dropshipping — what do you want from it?" and then genuinely wait,
// instead of reading or watching the thing first and asking afterwards.
//
// A leaf module: imports adapters and fs/path, never the skills loader, the
// runner, or investigator.js (investigator.js imports this, not the other
// way around).

import fs from 'node:fs';
import path from 'node:path';
import { getAdapter, getCapabilities } from '../adapters/index.js';
import { fetchArticle, fileKind, mimeTypeFor, inlineAttachment } from '../media.js';

const BROWSER_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36';

// YouTube serves a cookie-consent interstitial instead of the real watch page
// to a request with no consent cookie. These are the standard "consent
// already given" cookies a browser would carry.
const YOUTUBE_HEADERS = {
  'User-Agent': BROWSER_UA,
  'Accept-Language': 'en-US,en;q=0.9',
  Cookie: 'CONSENT=YES+cb.20210328-17-p0.en+FX+000; SOCS=CAI',
};

const TRANSCRIPT_MAX_CHARS = 40000;

function decodeEntities(text) {
  return String(text || '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&#(\d+);/g, (_, c) => String.fromCharCode(Number(c)))
    .replace(/&#x([0-9a-f]+);/gi, (_, c) => String.fromCharCode(parseInt(c, 16)))
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

// ---------- working out what the user gave us ----------

const YOUTUBE_HOSTS = /^(www\.|m\.|music\.)?(youtube\.com|youtu\.be)$/i;

/** The YouTube video id in a watch/short/youtu.be/embed URL, or null. */
export function youtubeVideoId(url) {
  try {
    const u = new URL(url);
    if (!YOUTUBE_HOSTS.test(u.hostname)) return null;
    if (u.hostname.toLowerCase().endsWith('youtu.be')) {
      return u.pathname.slice(1).split('/')[0] || null;
    }
    const v = u.searchParams.get('v');
    if (v) return v;
    const m = /^\/(shorts|embed|live|v)\/([^/?#]+)/.exec(u.pathname);
    return m ? m[2] : null;
  } catch {
    return null;
  }
}

/**
 * Works out what a piece of user input actually is. Deliberately forgiving —
 * fed by both a text box and a voice transcript, so it must cope with a
 * spoken path, a pasted link with tracking junk on it, or a wall of text
 * with no marker at all.
 *
 * Returns { kind: 'youtube'|'url'|'file'|'text', ... }. Purely syntactic —
 * no network call, no filesystem check.
 */
export function classifySource(input) {
  const raw = String(input || '').trim();
  if (!raw) return { kind: 'text', text: '' };

  const videoId = youtubeVideoId(raw);
  if (videoId) return { kind: 'youtube', url: raw, videoId };

  if (/^https?:\/\//i.test(raw)) {
    try {
      const u = new URL(raw);
      return { kind: 'url', url: u.toString() };
    } catch {
      // Malformed link — treat it as text rather than failing outright.
    }
  }

  // A Windows path (C:\... or \\server\share) or a quoted one, which is what
  // "Copy as path" puts on the clipboard.
  const unquoted = raw.replace(/^"(.*)"$/s, '$1').trim();
  if (/^[a-zA-Z]:[\\/]/.test(unquoted) || unquoted.startsWith('\\\\')) {
    return { kind: 'file', filePath: unquoted };
  }

  return { kind: 'text', text: raw };
}

function humanSize(bytes) {
  if (!bytes) return null;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ---------- YouTube metadata without watching ----------

/** Title and channel from YouTube's public oEmbed endpoint — no key, no scraping. The cheapest possible "what is this" for a video. */
async function fetchYoutubeOEmbed(url) {
  try {
    const res = await fetch(
      `https://www.youtube.com/oembed?url=${encodeURIComponent(url)}&format=json`,
      { headers: { 'User-Agent': BROWSER_UA } }
    );
    if (!res.ok) return null;
    const data = await res.json();
    return { title: data.title || null, author: data.author_name || null, thumbnail: data.thumbnail_url || null };
  } catch {
    return null;
  }
}

/**
 * Extracts the JSON array that follows `"<key>":` in a blob of page source.
 * A regex can't do this — entries contain nested arrays, so a non-greedy
 * match truncates. Count brackets, respecting strings and escapes.
 */
function extractJsonArrayAfter(source, key) {
  const marker = `"${key}":`;
  const at = source.indexOf(marker);
  if (at === -1) return null;

  const start = source.indexOf('[', at);
  if (start === -1) return null;

  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < source.length; i++) {
    const ch = source[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch === '\\') {
      escaped = true;
      continue;
    }
    if (ch === '"') inString = !inString;
    if (inString) continue;
    if (ch === '[') depth++;
    else if (ch === ']') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  return null;
}

/** Pulls the caption text out of a timedtext track (XML `<text>` nodes). */
function parseCaptionXml(xml) {
  const lines = [];
  const re = /<text[^>]*>([\s\S]*?)<\/text>/g;
  let match;
  while ((match = re.exec(xml))) {
    const line = decodeEntities(match[1].replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();
    if (line) lines.push(line);
  }
  return lines.join(' ');
}

/**
 * Everything about a YouTube video that can be had without a model watching
 * it: title, channel, description, and the caption transcript when one is
 * published. Only called when a video-capable model isn't available or
 * didn't work — this is the honest floor, not the default path.
 *
 * IN PRACTICE, EXPECT NO TRANSCRIPT — YouTube currently serves empty caption
 * bodies to anything without a session proof-of-origin token. Title and
 * description come from sturdier sources and do work. That's exactly why
 * watching properly matters when it's available: without it, this is what
 * Jarvis has to go on, and the caller must say so rather than imply more.
 */
export async function fetchYoutubeText(url) {
  const meta = (await fetchYoutubeOEmbed(url)) || {};
  const result = {
    ok: true,
    title: meta.title || null,
    author: meta.author || null,
    thumbnail: meta.thumbnail || null,
    description: null,
    transcript: null,
  };

  let html = '';
  try {
    const withLocale = url.includes('?') ? `${url}&hl=en&gl=US` : `${url}?hl=en&gl=US`;
    const res = await fetch(withLocale, { headers: YOUTUBE_HEADERS });
    if (res.ok) html = await res.text();
  } catch {
    // No watch page — oEmbed data alone still lets the caller say what the video is.
  }

  if (!html) {
    if (!result.title) return { ok: false, error: "Couldn't reach that video." };
    return result;
  }

  const desc = /"shortDescription":"((?:\\.|[^"\\])*)"/.exec(html);
  if (desc) {
    try {
      result.description = JSON.parse(`"${desc[1]}"`);
    } catch {
      result.description = desc[1];
    }
  }

  const tracksJson = extractJsonArrayAfter(html, 'captionTracks');
  if (tracksJson) {
    try {
      const tracks = JSON.parse(tracksJson.replace(/\\u0026/g, '&'));
      const track =
        tracks.find((t) => /^en/i.test(t.languageCode || '') && t.kind !== 'asr') ||
        tracks.find((t) => /^en/i.test(t.languageCode || '')) ||
        tracks[0];
      if (track?.baseUrl) {
        const capRes = await fetch(track.baseUrl, { headers: YOUTUBE_HEADERS });
        if (capRes.ok) {
          const transcript = parseCaptionXml(await capRes.text());
          if (transcript) result.transcript = transcript.slice(0, TRANSCRIPT_MAX_CHARS);
        }
      }
    } catch {
      // Captions unavailable — the caller falls back to title + description.
    }
  }

  return result;
}

// ---------- the free glance ----------

/**
 * Works out what a piece of content IS — no model call, ever. This is the
 * whole point of the split: sharing something must cost nothing until the
 * user says what they want done with it.
 *
 * For a URL, this does fetch the page (needed for even a title) and caches
 * the extracted text on the result as `cachedText` — one fetch serves both
 * "what is this" and, later, the actual reading, rather than fetching twice.
 */
export async function identify(source) {
  if (source.kind === 'youtube') {
    const meta = await fetchYoutubeOEmbed(source.url);
    return {
      title: meta?.title || `YouTube video (${source.videoId})`,
      kind: 'video',
      detail: meta?.author ? `on ${meta.author}` : null,
      thumbnail: meta?.thumbnail || null,
    };
  }

  if (source.kind === 'url') {
    const article = await fetchArticle(source.url);
    if (!article.ok) {
      return { title: source.url, kind: 'article', detail: null, unreachable: article.error };
    }
    return {
      title: article.title || source.url,
      kind: 'article',
      detail: `about ${Math.max(1, Math.round(article.text.length / 1000))}k characters`,
      cachedText: article.text,
    };
  }

  if (source.kind === 'file') {
    const kind = fileKind(source.filePath); // video | audio | image | document | unknown
    let size = null;
    try {
      size = fs.statSync(source.filePath).size;
    } catch {
      return { title: path.basename(source.filePath), kind, detail: null, unreachable: "I can't find that file." };
    }
    return { title: path.basename(source.filePath), kind, detail: humanSize(size) };
  }

  // Pasted/dictated text.
  const text = String(source.text || '');
  const firstLine = text.split(/\r?\n/)[0].trim();
  return {
    title: firstLine ? (firstLine.length > 60 ? `${firstLine.slice(0, 60)}…` : firstLine) : 'Pasted text',
    kind: 'text',
    detail: `${text.length} characters`,
    cachedText: text,
  };
}

/** Plain-language "what this is", for Jarvis to say out loud right after the free glance — before it has read or watched anything. */
export function describe(identity) {
  const bits = [];
  const kindWord = { video: 'a video', article: 'a web article', document: 'a document', image: 'an image', audio: 'an audio file', text: 'some pasted text', unknown: 'a file' };
  bits.push(kindWord[identity.kind] || 'something');
  if (identity.title) bits.push(`— "${identity.title}"`);
  if (identity.detail) bits.push(`(${identity.detail})`);
  return bits.join(' ');
}

/** Plain-language description of how content was actually taken in — said honestly, every time, so a verdict never sounds more informed than it is. */
export function intakeDescription(kind) {
  switch (kind) {
    case 'video':
      return 'I watched the video itself, including what was on screen.';
    case 'audio':
      return 'I listened to the audio.';
    case 'image':
      return 'I looked at the image.';
    case 'captions':
      return "I read the captions — I did NOT see the video, so I don't know what was shown on screen.";
    case 'youtube-text':
      return 'I only had the title and description — I neither watched nor heard it.';
    case 'article':
      return 'I read the article.';
    case 'document':
      return 'I read the document.';
    case 'text':
      return 'I read the text as given.';
    default:
      return 'I looked at what was provided.';
  }
}

// ---------- preparing content for a model to actually examine ----------

/**
 * Builds the `media` array for a model that can genuinely watch/see the
 * source, plus a `cleanup()` for anything uploaded.
 *
 * Two routes, cheapest first:
 *   INLINE   — small images (and PDFs, on an adapter rich enough) ride along
 *              as base64 in the request itself. No upload, works everywhere.
 *   UPLOADED — video, audio and anything over the inline cap go through
 *              `adapter.uploadFile`, which today only Gemini implements.
 *
 * `entry` is the model that will receive it — the uploaded route pins the
 * attachment to that one model's API key, so callers must not let the usual
 * fallback chain hand a dead URI to a different provider (askModel's
 * `only: true`).
 *
 * Returns { ok: true, media, cleanup } or { ok: false, error }.
 */
export async function prepareFor(entry, source) {
  const caps = getCapabilities(entry?.adapter);
  const adapter = getAdapter(entry.adapter);

  if (source.kind === 'youtube') {
    if (!caps.video) return { ok: false, error: 'That model cannot watch video.' };
    return {
      ok: true,
      media: [{ kind: 'video', mimeType: 'video/*', uri: source.url }],
      cleanup: async () => {},
    };
  }

  if (source.kind === 'file') {
    const kind = fileKind(source.filePath);
    const mimeType = mimeTypeFor(source.filePath);
    const isPdf = mimeType === 'application/pdf';

    if (kind === 'video' && !caps.video) return { ok: false, error: 'That model cannot watch video.' };
    if (kind === 'audio' && !caps.audio) return { ok: false, error: 'That model cannot listen to audio.' };
    if (kind === 'image' && !caps.vision) return { ok: false, error: 'That model cannot see images.' };
    if (isPdf && !caps.video) return { ok: false, error: 'That model cannot read a PDF directly.' };

    if (!fs.existsSync(source.filePath)) {
      return { ok: false, error: "I can't find that file — check the path is right." };
    }

    if (kind === 'image' || isPdf) {
      const inline = inlineAttachment(source.filePath, mimeType);
      if (inline.ok) return inline;
      if (inline.error !== 'too_big') return inline;
      // Over the inline cap — fall through to the upload route below.
    }

    if (typeof adapter.uploadFile !== 'function') {
      return {
        ok: false,
        error:
          kind === 'image' || isPdf
            ? 'That file is too big to send directly, and this model\'s provider has nowhere to upload it. A Gemini model can handle files this size.'
            : `Watching ${kind === 'audio' ? 'audio' : 'video'} needs a model that can take file uploads — Gemini can.`,
      };
    }

    try {
      const uploaded = await adapter.uploadFile(entry, source.filePath, mimeType);
      return {
        ok: true,
        media: [{ kind: kind === 'image' ? 'image' : 'video', mimeType: uploaded.mimeType, uri: uploaded.uri }],
        cleanup: uploaded.cleanup,
      };
    } catch (err) {
      return { ok: false, error: err?.message || "That file couldn't be uploaded." };
    }
  }

  return { ok: false, error: 'That kind of source is read as text, not watched.' };
}
