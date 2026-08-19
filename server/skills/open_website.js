// Skill: opens a website in the user's default browser.
//
// Safety: we never build a shell command string out of AI-provided text.
// The URL is parsed and validated with the built-in URL class (only
// http/https allowed), then handed to Windows as a single argument via
// `spawn` with an argument array — there is no shell string concatenation
// for anything derived from user/AI input, so there's no injection surface.

import { spawn } from 'node:child_process';

// Friendly shortcuts so "open my email" or "open youtube" work without the
// AI having to guess a URL.
const SHORTCUTS = {
  youtube: 'https://www.youtube.com',
  google: 'https://www.google.com',
  gmail: 'https://mail.google.com',
  email: 'https://mail.google.com',
  github: 'https://github.com',
  whatsapp: 'https://web.whatsapp.com',
  netflix: 'https://www.netflix.com',
  facebook: 'https://www.facebook.com',
  twitter: 'https://x.com',
  x: 'https://x.com',
  instagram: 'https://www.instagram.com',
  reddit: 'https://www.reddit.com',
  amazon: 'https://www.amazon.com',
  wikipedia: 'https://www.wikipedia.org',
  news: 'https://news.google.com',
  weather: 'https://weather.com',
  maps: 'https://maps.google.com',
  spotify: 'https://open.spotify.com',
};

function resolveUrl(input) {
  const key = String(input || '').trim().toLowerCase();
  if (SHORTCUTS[key]) return SHORTCUTS[key];

  let candidate = String(input || '').trim();
  if (!/^https?:\/\//i.test(candidate)) {
    candidate = `https://${candidate}`;
  }

  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  return parsed.toString();
}

export default {
  name: 'open_website',
  description:
    'Open a website in the user\'s default browser. Accepts either a well-known ' +
    'site name (e.g. "youtube", "gmail", "github") or a URL/domain the user mentions.',
  parameters: {
    type: 'object',
    properties: {
      site: {
        type: 'string',
        description: 'The site name, domain, or URL to open, e.g. "youtube" or "example.com".',
      },
    },
    required: ['site'],
  },
  async run({ site }) {
    const url = resolveUrl(site);
    if (!url) {
      return { ok: false, error: `"${site}" isn't a valid website.` };
    }

    // `start` is a built-in command of cmd.exe (not a standalone executable),
    // so we invoke cmd.exe directly with an argument array — no shell:true,
    // no string interpolation of user input into a shell command line.
    // The empty '' argument is required because `start` treats the first
    // quoted argument as a window title.
    spawn('cmd.exe', ['/c', 'start', '', url], { detached: true, stdio: 'ignore' }).unref();

    return { ok: true, opened: url };
  },
};
