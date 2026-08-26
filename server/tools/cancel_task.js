// Skill: cancels (or pauses) a scheduled task by name. Always confirmed
// first — deleting the wrong reminder from a fuzzy name match is exactly
// the kind of lasting, hard-to-notice mistake the confirmation layer exists
// to catch.

import { listTasks, deleteTask, updateTask } from '../scheduler/task-store.js';
import { describe } from '../scheduler/recurrence.js';

function findTask(args) {
  const tasks = listTasks();
  if (args.task_id) return tasks.find((t) => t.id === args.task_id) || null;
  if (args.title) {
    const needle = args.title.toLowerCase();
    return (
      tasks.find((t) => t.title.toLowerCase() === needle) ||
      tasks.find((t) => t.title.toLowerCase().includes(needle)) ||
      null
    );
  }
  return null;
}

export default {
  name: 'cancel_task',
  // Meta skill: see schedule_task.js's comment — left out of the task/
  // briefing skill picker, still usable in conversation.
  meta: true,
  description:
    'Cancel (delete) or pause a scheduled task/reminder by name. Use this when the user asks to ' +
    'cancel, remove, stop, or turn off a reminder or scheduled task.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      title: { type: 'string', description: "The task's title, or a close match to it, as the user described it." },
      task_id: { type: 'string', description: 'The exact task id, if already known from list_tasks.' },
      pause_only: { type: 'boolean', description: 'If true, just turn it off instead of deleting it entirely.' },
    },
    required: [],
  },
  summarize(args) {
    const task = findTask(args);
    if (!task) return `Cancel a task matching "${args.title || args.task_id}" — but I couldn't find one; please double-check.`;
    const when = describe(task.recurrence);
    return args.pause_only ? `Turn off "${task.title}" (${when}).` : `Delete "${task.title}" (${when}) entirely.`;
  },
  async run(args) {
    const task = findTask(args);
    if (!task) return { ok: false, error: `I couldn't find a scheduled task matching "${args.title || args.task_id}".` };
    if (args.pause_only) {
      updateTask(task.id, { enabled: false });
      return { ok: true, title: task.title, action: 'paused' };
    }
    deleteTask(task.id);
    return { ok: true, title: task.title, action: 'deleted' };
  },
};
