// Skill: creates a scheduled/repeating task from ordinary conversation
// ("remind me every weekday at 7am to..."). Always confirmed first (see
// skills/index.js's confirmation layer) — a wrong time or day is exactly
// the kind of mishearing that's easy to miss until it's too late.

import { createTask } from '../scheduler/task-store.js';
import { describe } from '../scheduler/recurrence.js';

function buildRecurrence(args) {
  const type = args.type;
  if (type === 'once') {
    if (!args.date || !args.time) return null;
    const at = new Date(`${args.date}T${args.time}:00`);
    if (Number.isNaN(at.getTime())) return null;
    return { type: 'once', at: at.toISOString() };
  }
  if (type === 'daily' || type === 'weekdays') {
    if (!args.time) return null;
    return { type, time: args.time };
  }
  if (type === 'weekly') {
    if (!args.time || !Array.isArray(args.days) || !args.days.length) return null;
    return { type: 'weekly', time: args.time, days: args.days };
  }
  if (type === 'interval') {
    const mins = Number(args.every_minutes);
    if (!mins || mins <= 0) return null;
    return { type: 'interval', everyMs: mins * 60 * 1000 };
  }
  return null;
}

function buildAction(args) {
  if (args.action_type === 'prompt' && args.prompt) return { type: 'prompt', text: args.prompt };
  if (args.action_type === 'briefing') return { type: 'briefing' };
  return { type: 'message', text: args.message || args.title || 'Reminder' };
}

export default {
  name: 'schedule_task',
  // Meta skill: manages Jarvis's own scheduling. Attaching "schedule a
  // task" as the action of a scheduled task would be a confusing loop, so
  // this is left out of the task/briefing skill picker (skills/index.js's
  // listSkills()) even though it's still fully usable in conversation.
  meta: true,
  description:
    'Schedule something to happen later, once or repeating — a reminder, a spoken message, or a ' +
    'task Jarvis should carry out on its own. Use this whenever the user says "remind me", ' +
    '"every day at...", "in an hour...", or similar. Always confirm the exact schedule with the user first.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      title: { type: 'string', description: 'A short label for this task, e.g. "Morning stretch reminder".' },
      type: {
        type: 'string',
        enum: ['once', 'daily', 'weekdays', 'weekly', 'interval'],
        description:
          '"once" for a single occurrence, "daily"/"weekdays" for every day or every weekday, ' +
          '"weekly" for specific days each week, "interval" for every N minutes.',
      },
      date: { type: 'string', description: 'Date for a "once" task, as YYYY-MM-DD.' },
      time: { type: 'string', description: 'Time of day as 24-hour HH:MM, for once/daily/weekdays/weekly.' },
      days: {
        type: 'array',
        items: { type: 'number' },
        description: 'For "weekly": which days, 0=Sunday through 6=Saturday.',
      },
      every_minutes: { type: 'number', description: 'For "interval": how many minutes between runs.' },
      action_type: {
        type: 'string',
        enum: ['message', 'prompt', 'briefing'],
        description:
          '"message" just reminds the user of something spoken; "prompt" has Jarvis actually do/answer ' +
          'something when it fires; "briefing" runs the morning briefing.',
      },
      message: { type: 'string', description: 'What to say, if action_type is "message".' },
      prompt: { type: 'string', description: 'What Jarvis should do/ask itself, if action_type is "prompt".' },
    },
    required: ['title', 'type', 'action_type'],
  },
  summarize(args) {
    const recurrence = buildRecurrence(args);
    const when = recurrence ? describe(recurrence) : 'an unclear schedule — please ask for the date/time again';
    const what =
      args.action_type === 'briefing'
        ? 'run the morning briefing'
        : args.action_type === 'prompt'
          ? `do this: "${args.prompt || ''}"`
          : `remind you: "${args.message || args.title || ''}"`;
    return `Schedule "${args.title}" to ${what}, ${when}.`;
  },
  async run(args) {
    const recurrence = buildRecurrence(args);
    if (!recurrence) {
      return {
        ok: false,
        error: "I couldn't work out a valid schedule from that — please give a specific time (and date, if it's a one-off).",
      };
    }
    const action = buildAction(args);
    const task = createTask({ title: args.title, recurrence, action });
    return { ok: true, taskId: task.id, title: task.title, when: describe(recurrence) };
  },
};
