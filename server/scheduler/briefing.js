// Composes the morning briefing: gathers real facts in code first — never
// invented — then asks the currently-selected model to narrate them into a
// short, spoken-style passage, under the same "phrase it yourself, don't
// invent data" guardrail every other tool result already follows (see
// prompt.js). Runs in its own conversation session so a briefing never
// mixes into whatever the user was chatting about.

import { listTasks } from './task-store.js';
import { listEntries as listProfileEntries } from '../profile.js';
import { runSkill } from '../skills/index.js';
import { runTurn, resetConversation } from '../models/runner.js';
import { getBriefingConfig, setBriefingConfig } from './briefing-config.js';

// Re-exported so existing importers (server.js, scheduler.js) don't need to
// care that these live in a separate, dependency-free file — see
// briefing-config.js's header comment for why the split exists.
export { getBriefingConfig, setBriefingConfig };

function timeGreeting() {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

/** Runs one built-in ability directly (not through a model) and returns its data, or null if it's off, failed, or no longer exists — never invented around. */
async function gatherOne(skillName, args, label) {
  const result = await runSkill(skillName, args, { autoConfirm: true, source: 'briefing' });
  // NOT `!result.ok` — get_time returns no `ok` field on success at all,
  // which would make a bare `!result.ok` check silently drop it.
  if (!result || result.ok === false) {
    console.warn(`[briefing] "${skillName}" didn't work:`, result?.error);
    return null;
  }
  const { ok, needs_confirmation, confirm_token, ui_action, error, ...data } = result;
  return { label, data };
}

/**
 * Gathers weather and headlines — fixed, always-available native abilities,
 * not a user-managed list (see briefing-config.js's header comment for why
 * that shape was reverted). No UI controls either; `config.weatherPlace` is
 * set conversationally via configure_briefing.js.
 */
async function gatherLiveInfo(config) {
  const out = [];
  if (config.weatherPlace) {
    const weather = await gatherOne('get_weather', { place: config.weatherPlace }, `Weather in ${config.weatherPlace}`);
    if (weather) out.push(weather);
  }
  if (config.headlines) {
    const headlines = await gatherOne('get_headlines', { count: 4 }, 'News headlines');
    if (headlines) out.push(headlines);
  }
  return out;
}

/** A plain object's data as a short "key: value, key2: value2" string for the narration prompt — arrays joined, empties dropped. */
function formatSourceData(data) {
  return Object.entries(data)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join('; ') : typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(', ');
}

async function gatherFacts(config) {
  const facts = {};
  const now = new Date();

  if (config.sections.greeting) facts.greeting = timeGreeting();

  if (config.sections.dateTime) {
    facts.date = now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
    facts.time = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  }

  if (config.sections.tasks) {
    facts.upcomingTasks = listTasks()
      .filter((t) => t.enabled && t.nextRunAt)
      .sort((a, b) => new Date(a.nextRunAt) - new Date(b.nextRunAt))
      .slice(0, 5)
      .map(
        (t) =>
          `${t.title} (${new Date(t.nextRunAt).toLocaleString('en-US', {
            weekday: 'short',
            hour: 'numeric',
            minute: '2-digit',
          })})`
      );
  }

  facts.liveInfo = await gatherLiveInfo(config);

  if (config.sections.goals) {
    const entries = listProfileEntries();
    if (entries.length) facts.goalsAndNotes = entries.slice(-8).map((e) => e.text);
  }

  if (config.sections.custom && config.customText) {
    facts.custom = config.customText;
  }

  return facts;
}

/** Compares what's enabled against what Jarvis knows, for gentle "want me to add..." suggestions. */
function suggestAdditions(config, facts) {
  const suggestions = [];
  if (!config.weatherPlace) suggestions.push("adding today's weather, if you tell me your city");
  if (!config.headlines) suggestions.push('including a few news headlines each morning');
  if (facts.goalsAndNotes?.length && !config.sections.focus) {
    suggestions.push('suggesting a focus for the day, based on what you\'ve told me about your goals');
  }
  return suggestions;
}

function factsToPrompt(facts, suggestions) {
  const lines = [
    'Compose the morning briefing now, in your own words, as one short spoken passage — a few ' +
      'sentences, not a bulleted list. Use ONLY the facts below; never invent or guess anything not given here.',
    '',
  ];
  if (facts.greeting) lines.push(`Greeting: ${facts.greeting}`);
  if (facts.date) lines.push(`Today: ${facts.date}, ${facts.time}`);
  if (facts.upcomingTasks?.length) lines.push(`Upcoming scheduled items: ${facts.upcomingTasks.join('; ')}`);
  for (const source of facts.liveInfo || []) {
    const formatted = formatSourceData(source.data);
    if (formatted) lines.push(`${source.label}: ${formatted}`);
  }
  if (facts.goalsAndNotes?.length) {
    lines.push(`Things the user has told Jarvis about themselves and their goals: ${facts.goalsAndNotes.join('; ')}`);
  }
  if (facts.custom) lines.push(`Custom note to include: ${facts.custom}`);
  if (facts.upcomingTasks?.length || facts.goalsAndNotes?.length) {
    lines.push(
      'If it genuinely fits, end with one brief suggested focus for the day grounded in the facts above — skip it rather than force one.'
    );
  }
  if (suggestions.length) {
    lines.push(
      '',
      `After the briefing itself, on a new line, briefly suggest ONE of these possible additions the user might want: ${suggestions.join(' / ')}`
    );
  }
  return lines.join('\n');
}

/** Gathers facts and has the model narrate them. `modelId` optionally pins one specific model (falls back through the usual auto-ranked chain if it breaks). Returns {ok, text, facts, modelId, switchedFrom, switchReason}. */
export async function composeBriefing({ modelId } = {}) {
  const config = getBriefingConfig();
  const facts = await gatherFacts(config);
  const suggestions = suggestAdditions(config, facts);
  const prompt = factsToPrompt(facts, suggestions);

  const sessionId = `briefing:${Date.now()}`;
  let text = '';
  let ok = true;
  let usedModelId = null;
  let switchedFrom = null;
  let switchReason = null;

  for await (const ev of runTurn(sessionId, prompt, { background: true, source: 'text', modelId, autoConfirm: true, noTools: true })) {
    if (ev.type === 'model_switch' && !switchedFrom) {
      switchedFrom = ev.from;
      switchReason = ev.reason;
    }
    if (ev.type === 'done') {
      text = ev.text;
      usedModelId = ev.modelId;
    }
    if (ev.type === 'paused') {
      ok = false;
      text = ev.reason;
    }
  }
  resetConversation(sessionId);

  return { ok, text, facts, modelId: usedModelId, switchedFrom, switchReason };
}
