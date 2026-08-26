// Skill: a handful of current headlines, from Google News' public RSS feed
// (no account, no key). Parsed with a small regex rather than pulling in an
// XML library — RSS <title> tags are simple and consistent enough that a
// full parser would be overkill for "give me a few headlines". Also used
// directly by the morning briefing composer.

const FEED_URL = 'https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en';

function decodeEntities(text) {
  return text
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function extractTitles(xml, limit) {
  const titles = [];
  const re = /<item>[\s\S]*?<title>([\s\S]*?)<\/title>/g;
  let match;
  while ((match = re.exec(xml)) && titles.length < limit) {
    const raw = match[1].replace(/^<!\[CDATA\[|\]\]>$/g, '').trim();
    if (raw) titles.push(decodeEntities(raw));
  }
  return titles;
}

export default {
  name: 'get_headlines',
  description: "Get a few current top news headlines. Use this when the user asks what's in the news, or for a news update.",
  parameters: {
    type: 'object',
    properties: {
      topic: {
        type: 'string',
        description: 'Optional topic to focus on, e.g. "technology" or "sports". Leave out for general top headlines.',
      },
      count: {
        type: 'number',
        description: 'How many headlines to return. Default 5.',
      },
    },
    required: [],
  },
  async run({ topic, count } = {}) {
    const limit = Math.max(1, Math.min(10, Number(count) || 5));
    const url = topic
      ? `https://news.google.com/rss/search?q=${encodeURIComponent(topic)}&hl=en-US&gl=US&ceid=US:en`
      : FEED_URL;

    try {
      const res = await fetch(url);
      if (!res.ok) return { ok: false, error: 'The news service is unavailable right now.' };
      const xml = await res.text();
      const headlines = extractTitles(xml, limit);
      if (!headlines.length) return { ok: false, error: 'No headlines found.' };
      return { ok: true, topic: topic || 'top stories', headlines };
    } catch {
      return { ok: false, error: "Couldn't reach the news service." };
    }
  },
};
