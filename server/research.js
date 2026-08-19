// Jarvis actually looking things up, rather than answering from memory.
//
// Shared by both rebuilt features: Content Analysis's check_claim/look_it_up
// and the Planning Partner's research_project. Neither is worth anything
// without real, sourced facts, so this lives once, at the top level, rather
// than inside either feature.
//
// Two backends, cheapest first:
//
//   1. The free path — search the web over plain HTTP, fetch the top few
//      results, and spend ONE model call summarizing them with their
//      sources attached. Costs no search quota and no API key.
//   2. Model-native search — hand the question to a model whose adapter
//      declares `webSearch` (Gemini's Google Search grounding today) and let
//      it search for itself. Better on obscure or very recent questions, but
//      it spends one of that key's requests.
//
// The free path runs first and the second only takes over when the first
// comes back thin. That ordering is deliberate on a free tier, where a
// Gemini request is 1/20th of a day's allowance and most questions are
// answered perfectly well by reading three web pages.
//
// A leaf module — imports ai.js, media.js and the adapters, never the skills
// loader or the runner.

import { askModel, pickModel } from './ai.js';
import { getAdapter } from './adapters/index.js';
import { fetchArticle } from './media.js';

const BROWSER_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36';

const MAX_SOURCES = 4;
const PER_SOURCE_CHARS = 5000;
// Below this many readable sources, the free path isn't worth summarizing
// and it's better to spend a model request on a real search.
const MIN_USABLE_SOURCES = 2;

const NEWS_HINTS = /\b(news|latest|today|this week|recent|announced|update|breaking|202\d|price of)\b/i;

// Words that carry no weight in a search box. Kept deliberately short —
// over-stripping loses the meaning, and search engines handle a few filler
// words fine. It's *length* that kills a query, not the odd "the".
const STOP_WORDS = new Set([
  'a', 'an', 'and', 'are', 'as', 'at', 'be', 'but', 'by', 'can', 'do', 'does', 'for', 'from', 'how',
  'i', 'if', 'in', 'into', 'is', 'it', 'its', 'just', 'me', 'my', 'of', 'on', 'or', 'really', 'so',
  'that', 'the', 'their', 'them', 'they', 'this', 'to', 'was', 'what', 'when', 'where', 'which',
  'who', 'why', 'with', 'you', 'your', 'actually', 'genuinely', 'typical', 'typically',
]);

const MAX_QUERY_WORDS = 12;

/**
 * Condenses a question into something worth typing into a search box.
 *
 * This is not cosmetic. Feeding a search engine the full question —
 * "Is this realistic and typical, or is it exaggerated? '...' What actually
 * happens for most people who try this?" — returns literally nothing:
 * measured at 0 results for a 190-character query against 40 results for the
 * same question reduced to keywords. The engines treat a long string as a
 * phrase match that nothing satisfies.
 *
 * The full question is still what the model gets asked; only what goes into
 * the search box is shortened.
 */
export function toSearchQuery(question) {
  const raw = String(question || '').trim();
  if (!raw) return '';

  // A quoted claim inside the question is almost always the real subject —
  // "is this true: '<claim>'" should search for the claim, not the framing.
  const quoted = /["“]([^"”]{12,140})["”]/.exec(raw);
  const subject = quoted ? quoted[1] : raw;

  const words = subject
    .replace(/[^\w\s$%.,-]/g, ' ')
    .split(/\s+/)
    .filter(Boolean);

  // Numbers and money are the most search-worthy parts of a claim, so they
  // are never treated as filler.
  const kept = words.filter((w) => /[\d$%]/.test(w) || !STOP_WORDS.has(w.toLowerCase()));

  const query = (kept.length >= 3 ? kept : words).slice(0, MAX_QUERY_WORDS).join(' ');
  return query.replace(/[.,]+$/, '').trim();
}

// ---------- free search backends ----------

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

/**
 * DuckDuckGo hands back redirect links of the form
 * `//duckduckgo.com/l/?uddg=<url-encoded real target>`. Unwrapping them
 * means the fetch below goes straight to the real page (and the source list
 * shows the user a real domain, not a duckduckgo.com link).
 */
function unwrapDuckDuckGoUrl(href) {
  if (!href) return null;
  let url = href.startsWith('//') ? `https:${href}` : href;
  try {
    const parsed = new URL(url);
    const target = parsed.searchParams.get('uddg');
    if (target) return target;
    if (/duckduckgo\.com/i.test(parsed.hostname) && parsed.pathname !== '/html/') return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

function isUsefulResultUrl(url) {
  if (!url || !/^https?:\/\//i.test(url)) return false;
  // Search/aggregator pages and video players aren't readable articles — the
  // fetch would succeed and return navigation chrome.
  return !/duckduckgo\.com|google\.[a-z.]+\/search|bing\.com\/search|youtube\.com\/watch|\.pdf($|\?)/i.test(url);
}

/** Search results from DuckDuckGo's no-JavaScript endpoints. Two of them, because the HTML one intermittently serves an anti-bot page and the lite one usually still answers. */
async function duckDuckGoSearch(query) {
  const endpoints = [
    `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`,
    `https://lite.duckduckgo.com/lite/?q=${encodeURIComponent(query)}`,
  ];

  for (const endpoint of endpoints) {
    try {
      const res = await fetch(endpoint, {
        headers: { 'User-Agent': BROWSER_UA, 'Accept-Language': 'en-US,en;q=0.9' },
      });
      if (!res.ok) continue;
      const html = await res.text();

      const out = [];
      const seen = new Set();
      // Keyed on the `uddg=` redirect parameter rather than a CSS class.
      // Both endpoints wrap every real result link that way, but they
      // disagree on everything else: /html/ uses class="result__a" with the
      // class before href, /lite/ uses class='result-link' (single quotes)
      // after it. Matching on the redirect is the one signal that holds for
      // both — and it ignores DuckDuckGo's own nav links, which don't have it.
      const re = /<a\s+[^>]*href="((?:https?:)?\/\/[^"]*uddg=[^"]+)"[^>]*>([\s\S]*?)<\/a>/gi;
      let match;
      while ((match = re.exec(html)) && out.length < MAX_SOURCES * 2) {
        const url = unwrapDuckDuckGoUrl(decodeEntities(match[1]));
        if (!isUsefulResultUrl(url) || seen.has(url)) continue;
        seen.add(url);
        out.push({ title: decodeEntities(match[2].replace(/<[^>]+>/g, '')).trim() || url, url });
      }
      if (out.length) return out;
    } catch {
      // Try the next endpoint.
    }
  }
  return [];
}

/** Google News' public RSS feed — the same keyless source get_headlines.js already relies on. Only used for questions that look like they're about current events. */
async function newsSearch(query) {
  try {
    const res = await fetch(
      `https://news.google.com/rss/search?q=${encodeURIComponent(query)}&hl=en-US&gl=US&ceid=US:en`,
      { headers: { 'User-Agent': BROWSER_UA } }
    );
    if (!res.ok) return [];
    const xml = await res.text();

    const out = [];
    const re = /<item>[\s\S]*?<title>([\s\S]*?)<\/title>[\s\S]*?<link>([\s\S]*?)<\/link>/g;
    let match;
    while ((match = re.exec(xml)) && out.length < MAX_SOURCES) {
      const title = decodeEntities(match[1].replace(/^<!\[CDATA\[|\]\]>$/g, '')).trim();
      const url = decodeEntities(match[2]).trim();
      if (title && isUsefulResultUrl(url)) out.push({ title, url });
    }
    return out;
  } catch {
    return [];
  }
}

/** Wikipedia's search API — a sturdy, never-blocked backstop for factual/background questions, which is exactly what most "is this claim real" checks come down to. */
async function wikipediaSearch(query) {
  try {
    const url =
      'https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&origin=*' +
      `&srlimit=2&srsearch=${encodeURIComponent(query)}`;
    const res = await fetch(url, { headers: { 'User-Agent': BROWSER_UA } });
    if (!res.ok) return [];
    const data = await res.json();
    return (data?.query?.search || []).map((hit) => ({
      title: `${hit.title} — Wikipedia`,
      url: `https://en.wikipedia.org/wiki/${encodeURIComponent(String(hit.title).replace(/ /g, '_'))}`,
    }));
  } catch {
    return [];
  }
}

/** Candidate pages for a question, from every free source that answers, deduplicated by URL. */
async function freeSearch(query) {
  const jobs = [duckDuckGoSearch(query), wikipediaSearch(query)];
  if (NEWS_HINTS.test(query)) jobs.push(newsSearch(query));

  const settled = await Promise.all(jobs.map((p) => p.catch(() => [])));
  const seen = new Set();
  const out = [];
  // Interleaved rather than concatenated, so one backend returning ten
  // results can't crowd the others out of the top few that actually get read.
  const lists = settled.filter((l) => l.length);
  for (let i = 0; i < MAX_SOURCES * 2; i++) {
    for (const list of lists) {
      const item = list[i];
      if (!item || seen.has(item.url)) continue;
      seen.add(item.url);
      out.push(item);
    }
  }
  return out;
}

// ---------- the two research paths ----------

async function researchViaWeb(query, { modelId, searchQuery } = {}) {
  // What gets typed into the search box is not the question itself — see
  // toSearchQuery() for why that distinction matters so much.
  const terms = searchQuery || toSearchQuery(query);
  const candidates = await freeSearch(terms);
  if (!candidates.length) return { ok: false, error: 'No search results came back.' };

  // Fetch more than needed, in parallel, and keep whichever actually read —
  // a fair share of pages block scripted requests or return a cookie wall.
  const fetched = await Promise.all(
    candidates.slice(0, MAX_SOURCES + 2).map(async (c) => {
      const article = await fetchArticle(c.url, { maxChars: PER_SOURCE_CHARS });
      return article.ok ? { title: article.title || c.title, url: c.url, text: article.text } : null;
    })
  );

  const usable = fetched.filter(Boolean).slice(0, MAX_SOURCES);
  if (usable.length < MIN_USABLE_SOURCES) {
    return { ok: false, error: 'Search results came back but none of them could be read.', thin: true };
  }

  const context = usable
    .map((s, i) => `SOURCE ${i + 1}: ${s.title}\nURL: ${s.url}\n${s.text}`)
    .join('\n\n---\n\n');

  const result = await askModel({
    modelId,
    background: true,
    system:
      'You are a careful researcher. Answer only from the sources given to you. ' +
      'Where they disagree, say so. Where they do not cover something, say that plainly ' +
      'rather than filling the gap from your own knowledge. Cite sources as [1], [2] and so on. ' +
      'Be specific about numbers, dates and conditions.',
    prompt:
      `Question: ${query}\n\n` +
      `Answer it using only these sources.\n\n${context}`,
  });

  if (!result.ok) return { ok: false, error: result.error, kind: result.kind };

  return {
    ok: true,
    answer: result.text,
    sources: usable.map(({ title, url }) => ({ title, url })),
    via: 'web',
    modelLabel: result.modelLabel,
  };
}

async function researchViaModelSearch(query, { modelId } = {}) {
  const entry = pickModel({ modelId, need: { webSearch: true } });
  if (!entry) return { ok: false, error: 'No model that can search the web is available.' };

  try {
    const { answer, sources } = await getAdapter(entry.adapter).searchGrounded(entry, query);
    if (!answer) return { ok: false, error: 'The search came back empty.' };
    return { ok: true, answer, sources: sources || [], via: 'model-search', modelLabel: entry.label };
  } catch (err) {
    let error;
    try {
      error = getAdapter(entry.adapter).friendlyError(err);
    } catch {
      error = err?.message || 'the search failed';
    }
    return { ok: false, error };
  }
}

/**
 * Looks something up and returns a sourced answer.
 *
 * Returns { ok: true, answer, sources: [{title, url}], via, modelLabel }
 *      or { ok: false, error }
 *
 * `via` matters to the caller: 'web' means Jarvis read pages itself, and
 * 'model-search' means the model searched. Both are shown to the user, since
 * an answer's trustworthiness depends on where it came from.
 *
 * `searchQuery` overrides what gets typed into the search box, for a caller
 * that already knows the concise subject (a claim check knows the claim).
 * Left out, it's derived from the question — see toSearchQuery().
 */
export async function research(query, { modelId, preferModelSearch = false, searchQuery } = {}) {
  const q = String(query || '').trim();
  if (!q) return { ok: false, error: 'Nothing to look up.' };

  if (preferModelSearch) {
    const grounded = await researchViaModelSearch(q, { modelId });
    if (grounded.ok) return grounded;
  }

  const web = await researchViaWeb(q, { modelId, searchQuery });
  if (web.ok) return web;

  // The free path found nothing readable — now it's worth spending a request
  // on a model that can search properly.
  if (!preferModelSearch) {
    const grounded = await researchViaModelSearch(q, { modelId });
    if (grounded.ok) return grounded;
    return { ok: false, error: `${web.error} And no model that can search the web was available either.` };
  }

  return web;
}

/** Runs several lookups at once (a project researching cost, tooling and competitors together). Failures come back as `{ok:false}` in place rather than sinking the batch. */
export async function researchAll(queries, options = {}) {
  return Promise.all(queries.map((q) => research(q, options).catch((err) => ({ ok: false, error: err?.message }))));
}
