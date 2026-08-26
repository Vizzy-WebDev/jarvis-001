// Skill: changes what the morning briefing includes. Always confirmed
// first, same reasoning as schedule_task — a briefing setting change is
// lasting and easy not to notice was wrong.

// Deliberately imports the config-only module, not scheduler/briefing.js —
// that file pulls in the model runner, which pulls in this skill loader,
// which would be a circular-import deadlock. See briefing-config.js's
// header comment for the full explanation.
import { getBriefingConfig, setBriefingConfig } from '../scheduler/briefing-config.js';

// Weather and headlines are fixed, always-available native abilities with
// no UI of their own any more (see briefing-config.js's header comment) —
// this conversational skill is now the ONLY way to set the weather place or
// turn headlines on/off, matching allow_folder.js's "ask by name in
// conversation" shape for anything that doesn't get a settings screen.
const SECTION_NAMES = ['greeting', 'dateTime', 'tasks', 'goals', 'focus', 'custom'];

export default {
  name: 'configure_briefing',
  // Meta skill: see schedule_task.js's comment — left out of the task/
  // briefing skill picker, still usable in conversation.
  meta: true,
  description:
    'Change what the morning briefing includes — turn sections on or off, set the city used for ' +
    'weather, turn news headlines on/off, or set custom text to include. Use this when the user ' +
    'wants to change their morning briefing.',
  confirm: 'always',
  parameters: {
    type: 'object',
    properties: {
      enable_sections: { type: 'array', items: { type: 'string', enum: SECTION_NAMES }, description: 'Built-in sections to turn on.' },
      disable_sections: { type: 'array', items: { type: 'string', enum: SECTION_NAMES }, description: 'Built-in sections to turn off.' },
      place: { type: 'string', description: 'City/place for a weather source — giving this also turns weather on.' },
      enable_headlines: { type: 'boolean', description: 'Turn the news headlines source on or off.' },
      custom_text: { type: 'string', description: 'Custom text to include, if the custom section is on.' },
    },
    required: [],
  },
  summarize(args) {
    const parts = [];
    if (args.enable_sections?.length) parts.push(`turn on: ${args.enable_sections.join(', ')}`);
    if (args.disable_sections?.length) parts.push(`turn off: ${args.disable_sections.join(', ')}`);
    if (args.place) parts.push(`set weather to ${args.place}`);
    if (args.enable_headlines === true) parts.push('turn on news headlines');
    if (args.enable_headlines === false) parts.push('turn off news headlines');
    if (args.custom_text !== undefined) parts.push('update the custom note');
    return `Update the morning briefing: ${parts.join('; ') || 'no changes given'}.`;
  },
  async run(args) {
    const current = getBriefingConfig();
    const sections = { ...current.sections };
    for (const s of args.enable_sections || []) sections[s] = true;
    for (const s of args.disable_sections || []) sections[s] = false;

    const patch = { sections };
    if (args.custom_text !== undefined) patch.customText = args.custom_text;
    if (args.place) patch.weatherPlace = args.place;
    if (args.enable_headlines !== undefined) patch.headlines = args.enable_headlines;
    const next = setBriefingConfig(patch);

    return { ok: true, sections: next.sections, weatherPlace: next.weatherPlace, headlines: next.headlines };
  },
};
