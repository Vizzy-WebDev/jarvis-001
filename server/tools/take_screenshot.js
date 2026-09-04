// Skill: takes a screenshot and delivers it into the chat as a real, visible
// image — distinct from look_at_screen.js, which answers a QUESTION about
// what's on screen with a spoken description and never shows the picture
// itself. Use this one when the user actually wants to see/share/send a
// screenshot, not just have it described.
//
// Imports only control/ps-bridge.js + control/screenshot-store.js +
// control/observation-bridge.js + tools/look_at_screen.js's pickWindow()
// (a leaf export, not the loader) — never control/session.js itself, so
// this stays safe to import from anywhere tools/index.js reaches, same
// discipline look_at_screen.js already follows.

import { send as sendCommand } from '../control/ps-bridge.js';
import { saveScreenshot } from '../control/screenshot-store.js';
import { showIndicator, hideIndicator } from '../control/observation-bridge.js';
import { pickWindow } from './look_at_screen.js';

export default {
  name: 'take_screenshot',
  description:
    'Take a screenshot right now and show it to the user directly in the chat, as an actual picture — not a description of it. ' +
    'Use this when the user asks to see, share, send, or save a screenshot ("take a screenshot", "send me a screenshot of this", ' +
    '"grab a picture of my screen"). For answering a QUESTION about what\'s on screen instead, use look_at_screen.',
  parameters: {
    type: 'object',
    properties: {
      target: {
        type: 'string',
        description: 'Optional — which window to capture, by app name or part of its title (e.g. "chrome", "notepad"). Leave out to capture whichever window is currently in front.',
      },
    },
    required: [],
  },
  async run({ target } = {}) {
    let windows = [];
    try {
      ({ windows } = await sendCommand('windows'));
    } catch {
      return { ok: false, error: "I couldn't reach the screen right now." };
    }

    const win = pickWindow(windows, target);
    if (!win && !windows.length) {
      return { ok: false, error: 'Nothing appears to be open on screen right now.' };
    }

    const token = showIndicator('Jarvis is taking a screenshot');
    try {
      let shot;
      try {
        shot = await sendCommand('screenshot', win ? { handle: win.handle } : {});
      } catch (err) {
        return { ok: false, error: err?.message || "I couldn't take a screenshot right now." };
      }

      let file;
      try {
        file = saveScreenshot(shot.base64, { label: win ? win.processName : 'screen' });
      } catch (err) {
        return { ok: false, error: err?.message || "I took the screenshot but couldn't save it." };
      }

      return {
        ok: true,
        note: 'Screenshot captured and shown to the user in the chat.',
        ui_action: { type: 'attachment', kind: 'image', url: `/api/control/screenshots/${file}`, mimeType: shot.mimeType || 'image/png' },
      };
    } finally {
      hideIndicator(token);
    }
  },
};
