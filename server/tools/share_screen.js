// Skill: turns on Screen Sharing as a persistent mode — separate from
// look_at_screen (a one-off glance) and a screen_looks_like monitor (an
// ongoing watch for a specific condition). Once on, the user can ask about
// what's on their screen at any point without saying "look at my screen"
// first — see prompt.js's volatile section (gated on isSharing()) and
// control/screen-share-state.js's own header comment for the full design.
//
// Same underlying state the manual UI toggle uses (control/screen-share-
// state.js), so a voice instruction and the toggle are always in sync —
// per the user's own requirement.

import { startSharing } from '../control/screen-share-state.js';

export default {
  name: 'share_screen',
  description:
    'Turn on Screen Sharing — a persistent mode where the user can ask about what\'s on their screen at any point without saying "look at my screen" ' +
    'first each time. Use this when the user says something like "share my screen with me" or "keep an eye on my screen while I work" with no specific ' +
    'condition attached. If they want you to watch for one specific thing and act when it happens, use watch_for instead.',
  parameters: { type: 'object', properties: {}, required: [] },
  async run() {
    startSharing();
    return { ok: true, note: 'Screen sharing turned on.' };
  },
};
