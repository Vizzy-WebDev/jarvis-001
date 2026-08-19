// Shared file/media plumbing: what kind of file something is, and turning a
// small file into something a model can see inline.
//
// This file used to also carry Content Analysis's "what did the user hand
// me" classification (YouTube/URL/file/text) and its YouTube-reading code.
// Those moved to server/content/intake.js when Content Analysis was rebuilt
// — they belonged to that one feature, not to every caller of this file.
// What's left here is genuinely shared: the composer's paperclip
// (attachments.js), the read_web_page skill, and the new research.js all use
// fetchArticle/stripHtml or the file-kind helpers, and none of them should
// have to import a content-analysis module to get them.
//
// A leaf module: imports adapters, never the skills loader, the runner, or
// any feature engine.

import fs from 'node:fs';
import path from 'node:path';
import { getCapabilities } from './adapters/index.js';

const ARTICLE_MAX_CHARS = 24000;
// A plain browser User-Agent: several sites serve a stripped-down or
// consent-gated page to anything that announces itself as a bot.
const BROWSER_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36';

export { BROWSER_UA };

const VIDEO_EXTENSIONS = /\.(mp4|mov|mkv|webm|avi|m4v|mpg|mpeg|wmv|3gp|mts|m2ts)$/i;
const AUDIO_EXTENSIONS = /\.(mp3|wav|m4a|aac|ogg|opus|flac|wma|amr|m4b)$/i;
const IMAGE_EXTENSIONS = /\.(png|jpe?g|gif|webp|bmp)$/i;
const DOC_EXTENSIONS = /\.(pdf|txt|md|csv|json|rtf)$/i;

const MIME_BY_EXTENSION = {
  '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.mkv': 'video/x-matroska',
  '.webm': 'video/webm', '.avi': 'video/x-msvideo', '.m4v': 'video/x-m4v',
  '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg', '.wmv': 'video/x-ms-wmv',
  '.3gp': 'video/3gpp', '.mts': 'video/mp2t', '.m2ts': 'video/mp2t',
  '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
  '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.opus': 'audio/opus', '.flac': 'audio/flac',
  '.wma': 'audio/x-ms-wma', '.amr': 'audio/amr', '.m4b': 'audio/mp4',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
  '.pdf': 'application/pdf', '.txt': 'text/plain', '.md': 'text/markdown',
  '.csv': 'text/csv', '.json': 'application/json', '.rtf': 'application/rtf',
};

export function mimeTypeFor(filePath) {
  return MIME_BY_EXTENSION[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
}

/** What sort of file this is, which decides whether a model needs to watch it or just read it. */
export function fileKind(filePath) {
  if (VIDEO_EXTENSIONS.test(filePath)) return 'video';
  if (AUDIO_EXTENSIONS.test(filePath)) return 'audio';
  if (IMAGE_EXTENSIONS.test(filePath)) return 'image';
  if (DOC_EXTENSIONS.test(filePath)) return 'document';
  return 'unknown';
}

// ---------- reading web pages ----------

function decodeEntities(text) {
  return text
    .replace(/&nbsp;/g, ' ')
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCharCode(parseInt(code, 16)))
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

export { decodeEntities };

/** HTML to readable text. Shared by read_web_page, research.js and content/intake.js so there is one implementation rather than several drifting apart. */
export function stripHtml(html) {
  return decodeEntities(
    String(html || '')
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<noscript[\s\S]*?<\/noscript>/gi, ' ')
      // Navigation, headers, footers and sidebars are pure noise in an
      // article — dropping them keeps the model reading the actual content
      // instead of a site's menu, which matters when several sources have to
      // fit in one prompt.
      .replace(/<(nav|header|footer|aside|form|svg)\b[\s\S]*?<\/\1>/gi, ' ')
      .replace(/<!--[\s\S]*?-->/g, ' ')
      .replace(/<\/(p|div|h[1-6]|li|tr|section|article|br)>/gi, '\n')
      .replace(/<[^>]+>/g, ' ')
  )
    .replace(/[ \t ]+/g, ' ')
    .replace(/\n\s*\n\s*\n+/g, '\n\n')
    .trim();
}

function extractTitle(html) {
  const og = /<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']/i.exec(html);
  if (og) return decodeEntities(og[1]).trim();
  const t = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  return t ? decodeEntities(t[1]).trim() : null;
}

/**
 * Fetches a page and returns its readable text. `maxChars` is generous —
 * Content Analysis and research.js both want the whole article, not a
 * taster; read_web_page (a spoken-turn tool) passes its own lower cap.
 */
export async function fetchArticle(url, { maxChars = ARTICLE_MAX_CHARS } = {}) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { ok: false, error: `"${url}" isn't a valid web address.` };
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return { ok: false, error: 'Only http and https links can be read.' };
  }

  try {
    const res = await fetch(parsed.toString(), { headers: { 'User-Agent': BROWSER_UA } });
    if (!res.ok) return { ok: false, error: `That page returned an error (${res.status}).` };

    const contentType = res.headers.get('content-type') || '';
    if (!contentType.includes('text/html') && !contentType.includes('text/plain') && !contentType.includes('xml')) {
      return { ok: false, error: "That link doesn't point at a readable page." };
    }

    const html = await res.text();
    const text = stripHtml(html).slice(0, maxChars);
    if (!text) return { ok: false, error: 'That page had no readable text on it.' };

    return { ok: true, url: parsed.toString(), title: extractTitle(html), text };
  } catch {
    return { ok: false, error: "Couldn't reach that page." };
  }
}

// ---------- inlining a small file for a model to see ----------

// Biggest file we'll base64 into a request body rather than uploading.
// Base64 inflates by 4/3, and Anthropic caps an image at 5MB encoded — so
// ~3.5MB of raw bytes is the largest that's safe across every adapter.
const INLINE_MAX_BYTES = 3.5 * 1024 * 1024;

/**
 * Reads a file straight into a neutral `media` item as base64.
 *
 * This is the path that should be taken for anything small enough, and it
 * needs NO provider upload API at all: every adapter already understands
 * `dataBase64` (adapters/gemini.js's inlineData, anthropic.js's base64 image
 * source, openai-compatible.js's data: URL). Requiring `uploadFile` for an
 * image — which only adapters/gemini.js implements — was why choosing a
 * picture failed instantly with "that model's provider can't take file
 * uploads" even though all three adapters could have displayed it.
 */
export function inlineAttachment(filePath, mimeType, { maxBytes = INLINE_MAX_BYTES } = {}) {
  let stat;
  try {
    stat = fs.statSync(filePath);
  } catch {
    return { ok: false, error: "I can't find that file — check the path is right." };
  }
  if (stat.size > maxBytes) return { ok: false, error: 'too_big', size: stat.size };

  try {
    const kind = fileKind(filePath);
    return {
      ok: true,
      media: [
        {
          kind: kind === 'image' ? 'image' : 'document',
          mimeType: mimeType || mimeTypeFor(filePath),
          dataBase64: fs.readFileSync(filePath).toString('base64'),
        },
      ],
      cleanup: async () => {},
    };
  } catch {
    return { ok: false, error: "I couldn't read that file." };
  }
}

export { getCapabilities };
