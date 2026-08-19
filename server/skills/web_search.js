// Skill: opens a Google search results page for whatever the user asked about.

import { spawn } from 'node:child_process';

export default {
  name: 'web_search',
  description:
    'Search the web for something and show the user the results. Use this when the ' +
    'user asks you to look something up or search for something online, rather than ' +
    'answering from your own knowledge.',
  parameters: {
    type: 'object',
    properties: {
      query: {
        type: 'string',
        description: 'What to search for, e.g. "weather in Lagos".',
      },
    },
    required: ['query'],
  },
  async run({ query }) {
    const q = String(query || '').trim();
    if (!q) return { ok: false, error: 'No search query given.' };

    const url = `https://www.google.com/search?q=${encodeURIComponent(q)}`;

    // Same safe pattern as open_website.js: URL is built with encodeURIComponent
    // (so it can't contain shell-meaningful characters) and passed as a single
    // argument, never interpolated into a shell string.
    spawn('cmd.exe', ['/c', 'start', '', url], { detached: true, stdio: 'ignore' }).unref();

    return { ok: true, query: q, opened: url };
  },
};
