import type { Connector } from './api-types';

/**
 * Jarvis's own built-in abilities are not "apps it may use".
 *
 * They are what it can already do, and offering them in a picker would suggest
 * that ticking one adds something. Only real connected apps are pickable.
 *
 * Shared rather than repeated: the scheduled-task editor and the briefing both
 * choose connectors, and two copies of this rule would eventually disagree
 * about what counts as an app.
 *
 * **Enabled is not the same as connected.** A connector that was added but
 * never finished sign-in (`status.state` still `'untested'` or `'error'`) is
 * `enabled: true` by default and does nothing yet — offering it here reads as
 * a real, usable app when it cannot actually do anything. Only `'working'`
 * counts.
 */
const OWN_ABILITIES = new Set(['files', 'browser']);

export function isPickable(connector: Connector): boolean {
  return connector.enabled && connector.status?.state === 'working'
    && !OWN_ABILITIES.has(connector.type);
}
