// Screen Sharing's own persistent on/off state — deliberately separate from
// observation-bridge.js's per-call token ref-counting (look_at_screen's
// one-off glance, a screen_looks_like monitor's ongoing watch). Those two
// are transient: the badge lights for exactly as long as one specific call
// is actively capturing. Screen Sharing is a real MODE the user turns on and
// leaves on — the manual toggle in the UI and a voice instruction
// ("share my screen with me" / "stop sharing") both read and write this same
// flag, so either one is always in sync with the other, per the user's own
// requirement that the toggle "works alongside voice commands, not instead
// of them."
//
// Turning sharing on does NOT itself trigger a spoken description — nothing
// was asked yet. It only arms the indicator and tells the model (via
// prompt.js's volatile section, gated on isSharing()) that the next time the
// user asks something about their screen, it can just answer — no need to
// hear "look at my screen" first. This matches the project's existing
// "never speak unprompted" discipline (Heartbeat, Monitoring).

import { showIndicator, hideIndicator } from './observation-bridge.js';
import { broadcast } from '../events.js';

let sharing = false;
let indicatorToken = null;

export function isSharing() {
  return sharing;
}

export function startSharing() {
  if (sharing) return { ok: true, alreadyOn: true };
  sharing = true;
  indicatorToken = showIndicator('Screen sharing is on');
  broadcast({ type: 'screen_sharing_status', sharing: true });
  return { ok: true };
}

export function stopSharing() {
  if (!sharing) return { ok: true, alreadyOff: true };
  sharing = false;
  if (indicatorToken !== null) {
    hideIndicator(indicatorToken);
    indicatorToken = null;
  }
  broadcast({ type: 'screen_sharing_status', sharing: false });
  return { ok: true };
}
