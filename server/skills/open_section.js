// Skill: opens a section of the Jarvis app for the user by voice (e.g.
// "open schedule"). The actual navigation happens in the browser — this
// just hands back a `ui_action` the front-end acts on (see app.js's
// handling of tool_result events). Quick and fully reversible, so it only
// needs confirming when the audio was genuinely unclear.

export default {
  name: 'open_section',
  // Meta skill: navigating the browser is meaningless when nobody's
  // watching an unattended run — left out of the task/briefing skill
  // picker (see schedule_task.js's comment), still usable in conversation.
  meta: true,
  description:
    'Open a section of the Jarvis app for the user, such as Model Settings, Skills, Connectors, Scheduled ' +
    'Tasks, Morning Briefing, Notifications, or Profile & Goals. Use this when the user says ' +
    '"open...", "show me...", or "go to..." a section. Planning and shared content are NOT sections — they ' +
    'happen here in the conversation, so never try to open a page for them.',
  confirm: 'ifUnclear',
  parameters: {
    type: 'object',
    properties: {
      section: {
        type: 'string',
        // Must stay in step with public/nav.js's SECTIONS — that list drives
        // the drawer and the router, and a section named here but missing
        // there navigates the browser nowhere.
        enum: ['home', 'notifications', 'models', 'skills', 'app-control', 'tasks', 'briefing', 'profile'],
        description: 'Which section to open.',
      },
    },
    required: ['section'],
  },
  summarize(args) {
    return `Open the ${args.section} section.`;
  },
  async run(args) {
    return { ok: true, ui_action: { type: 'navigate', section: args.section } };
  },
};
