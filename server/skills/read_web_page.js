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

import { fetchArticle } from '../media.js';

const MAX_CHARS = 6000;

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
    if (!result.ok) return { ok: false, error: result.error };
    return { ok: true, url: result.url, title: result.title, text: result.text };
  },
};
