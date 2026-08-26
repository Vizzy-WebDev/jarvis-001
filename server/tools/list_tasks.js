// Skill: lists the user's scheduled tasks/reminders. Read-only, so no
// confirmation is needed — nothing changes.

import { listTasks } from '../scheduler/task-store.js';
import { describe } from '../scheduler/recurrence.js';

export default {
  name: 'list_tasks',
  // Meta skill: see schedule_task.js's comment — left out of the task/
  // briefing skill picker, still usable in conversation.
  meta: true,
  description:
    'List the user\'s scheduled tasks and reminders. Use this when the user asks what\'s scheduled, ' +
    'what reminders they have, or "what\'s on my list".',
  parameters: { type: 'object', properties: {}, required: [] },
  async run() {
    const tasks = listTasks().map((t) => ({
      id: t.id,
      title: t.title,
      enabled: t.enabled,
      schedule: describe(t.recurrence),
      nextRunAt: t.nextRunAt,
    }));
    return { ok: true, tasks };
  },
};
