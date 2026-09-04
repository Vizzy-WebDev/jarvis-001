// Skill: fetches a web page and returns its text content, so Jarvis can
// summarize it or answer questions about it in conversation. (Unlike
// web_search, which just opens a results page for the user to read
// themselves.)
//
// The fetching and HTML-stripping now live in server/media.js, shared with
// Content Analysis and the research engine — there were two copies of the
// same stripHtml drifting apart, and media.js's version also drops nav,
// header and footer blocks, so what comes back is the article rather than
// the site's menu.
//
// The character cap stays low here on purpose. This result goes into a
// spoken conversation turn, where a huge wall of text costs tokens and slows
// the reply; Content Analysis asks for far more because it's building a
// digest to keep.
//
// A plain fetch gets nothing useful from a JS-rendered page (a real
// single-page app with no server-rendered HTML) — when that happens, this
// falls back to connectors/browser.js's renderPageHeadless(), a completely
// invisible (no window ever appears) real Chrome render, so "most lookups
// shouldn't pop a browser window" stays true even for those pages. Safe to
// import directly (browser.js is a leaf module — see its own header
// comment; no circular-import risk per root CLAUDE.md's invariant).

import { fetchArticle } from '../media.js';
import { renderPageHeadless } from '../connectors/browser.js';

const MAX_CHARS = 6000;
// Below this, a plain fetch's result is treated as "didn't really get the
// page" (an SPA's near-empty server-rendered shell, a JS-gated paywall
// stub, ...) rather than "the page is just short" — worth a real headless
// render instead of returning a near-nothing answer.
const THIN_RESULT_CHARS = 200;

export default {
  name: 'read_web_page',
  description:
    'Fetch a web page and read its text content, so you can summarize it or answer questions about it. ' +
    'Use this when the user gives you a URL, or after web_search if they want to know what a result actually says.',
  parameters: {
    type: 'object',
    properties: {
      url: { type: 'string', description: 'The full URL to read, e.g. "https://example.com/article".' },
    },
    required: ['url'],
  },
  async run({ url } = {}) {
    const result = await fetchArticle(url, { maxChars: MAX_CHARS });
    if (result.ok && result.text && result.text.trim().length >= THIN_RESULT_CHARS) {
      return { ok: true, url: result.url, title: result.title, text: result.text };
    }
    // Plain fetch either failed outright or came back too thin to be real
    // content — try a real, but still fully invisible, render before giving
    // up. Never pops a window either way.
    try {
      const rendered = await renderPageHeadless(url, { maxChars: MAX_CHARS });
      if (rendered.text && rendered.text.trim().length > 0) {
        return { ok: true, url: rendered.url, title: rendered.title, text: rendered.text };
      }
    } catch {
      // Fall through to whatever the plain fetch already told us — a
      // headless-render failure isn't a better error than the original one.
    }
    if (result.ok) return { ok: true, url: result.url, title: result.title, text: result.text };
    return { ok: false, error: result.error };
  },
};
