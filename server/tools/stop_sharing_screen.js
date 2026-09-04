// Skill: turns off Screen Sharing — the voice-facing twin of share_screen.js.
// Same underlying state the manual UI toggle uses.

import { stopSharing } from '../control/screen-share-state.js';

export default {
  name: 'stop_sharing_screen',
  description: 'Turn off Screen Sharing. Use this when the user says something like "stop sharing my screen" or "you can stop watching now."',
  parameters: { type: 'object', properties: {}, required: [] },
  async run() {
    stopSharing();
    return { ok: true, note: 'Screen sharing turned off.' };
  },
};
