// Skill: tells the current date and/or time from this PC's clock.

export default {
  name: 'get_time',
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
