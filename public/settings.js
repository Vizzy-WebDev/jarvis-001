// Tiny localStorage-backed store for browser-only preferences (things that
// don't need the server, like "speak replies out loud"). Server-side
// settings — which AI provider, which voice engine — get their own endpoint
// once Phase 2/3 introduce them; this file stays focused on UI prefs.

const STORAGE_KEY = 'jarvis.settings';

const DEFAULTS = {
  speakReplies: true,
  micMode: 'conversation', // 'conversation' | 'push'
  voiceOutput: 'gemini', // 'gemini' | 'browser'
  geminiVoice: 'Kore',
  useAiTurnCheck: false,
  // 'pipeline' works with any AI model (Gemini/Claude/OpenAI), ~0.8-1.5s to
  // first words. 'live' is Gemini-only, ~0.3-0.6s and interruptible
  // mid-word, but locks voice mode to Gemini. Default to pipeline since
  // it's the more thoroughly tested path.
  voiceEngine: 'pipeline', // 'pipeline' | 'live'
};

function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : { ...DEFAULTS };
  } catch {
    return { ...DEFAULTS };
  }
}

let settings = load();

export function getSetting(key) {
  return settings[key];
}

export function setSetting(key, value) {
  settings = { ...settings, [key]: value };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // localStorage unavailable (e.g. private mode quirks) — setting still
    // applies for this page load, just won't persist. Not worth bothering
    // the user about.
  }
}
