// Skill: starts recording the whole real desktop to a proper video file.
// Paired with stop_screen_recording.js, same start/stop shape as
// watch_for.js/stop_watching.js. Runs as an independent background process
// (server/control/screen-recorder.js), unrelated to the control loop's own
// perceive/act cycle, so a recording keeps going through an active control
// session exactly as intended — start it, then ask Jarvis to go do the task,
// then stop it when done.

import { startRecording } from '../control/screen-recorder.js';

export default {
  name: 'start_screen_recording',
  description:
    'Start recording the whole screen as a real video, right now. Use this when the user asks to record their screen, capture a video of ' +
    'something happening, or record while a task is performed. Stays running until stop_screen_recording is called — including through a ' +
    'computer-control task, if one is asked for next.',
  parameters: { type: 'object', properties: {}, required: [] },
  async run() {
    const result = await startRecording();
    if (!result.ok) return { ok: false, error: result.error };
    return { ok: true, note: 'Recording started — say when to stop, or ask for something else and stop it later.' };
  },
};
