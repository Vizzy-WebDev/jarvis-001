// Skill: tells the current date and/or time from this PC's clock.

export default {
  name: 'get_time',
  // One of the ~10 always-on "core" tools — see capabilities.js's
  // getToolDeclarations() and find_capability.js. Sent to the model on
  // every turn instead of being behind the lookup, since it's a common,
  // cheap, conversational ability.
  core: true,
  description:
    "Get the current date and/or time on the user's computer. Use this for " +
    'any question about what time it is, what day/date it is, etc.',
  parameters: {
    type: 'object',
    properties: {},
    required: [],
  },
  async run() {
    const now = new Date();
    return {
      iso: now.toISOString(),
      friendly_time: now.toLocaleTimeString('en-US', {
        hour: 'numeric',
        minute: '2-digit',
      }),
      friendly_date: now.toLocaleDateString('en-US', {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric',
      }),
    };
  },
};
