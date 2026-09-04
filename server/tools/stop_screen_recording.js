// Skill: stops the active screen recording and delivers the finished video
// into the chat as a real, playable file — same ui_action:{type:'attachment'}
// delivery pattern take_screenshot.js uses for images, just kind:'video'.

import { stopRecording } from '../control/screen-recorder.js';

export default {
  name: 'stop_screen_recording',
  description: 'Stop the currently running screen recording and show the user the finished video. Use this when the user asks to stop recording or says they\'re done.',
  parameters: { type: 'object', properties: {}, required: [] },
  async run() {
    const result = await stopRecording();
    if (!result.ok) return { ok: false, error: result.error };
    return {
      ok: true,
      note: 'Recording finished and shown to the user in the chat.',
      ui_action: { type: 'attachment', kind: 'video', url: `/api/control/recordings/${result.file}`, mimeType: 'video/mp4' },
    };
  },
};
