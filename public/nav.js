// The single source of truth for every section beyond the main assistant
// screen — the drawer menu, the hash router, and voice navigation
// (server/skills/open_section.js) all read from this list. To add a future
// capability: one entry here, one file in screens/. Grouped for growth, not
// a flat list — see CLAUDE.md's navigation design notes.

export const SECTIONS = [
  { id: 'home', label: 'Assistant', group: 'Assistant', screen: 'app-screen' },
  // Also reachable from the header bell (see public/notifications.js) —
  // that's a shortcut to this same section, not a second entry point;
  // still listed here so the drawer, #/notifications, and voice nav
  // ("open notifications") all work too.
  { id: 'notifications', label: 'Notifications', group: 'Assistant', module: './screens/notifications.js' },
  { id: 'chat-history', label: 'Chat History', group: 'Assistant', module: './screens/chat-history.js' },
  { id: 'models', label: 'Model Settings', group: 'Brain', module: './screens/models.js' },
  { id: 'skills', label: 'Skills', group: 'Abilities', module: './screens/skills.js' },
  { id: 'app-control', label: 'App Control', group: 'Abilities', module: './screens/app-control.js' },
  // The Planning Partner and Content Analysis deliberately have NO entry
  // here. They used to be a page each, and a page each is exactly what kept
  // them apart — separate screens meant separate stores and separate
  // memories, so a video you shared could never inform a plan you were
  // working on. Both live entirely in conversation now and write into the
  // one transcript (see server/skills/, server/attachments.js). Their
  // engines live under server/projects/ and server/content/; there is still
  // no screen for either — what went away is the doors, not the abilities.
  { id: 'tasks', label: 'Scheduled Tasks', group: 'Automation', module: './screens/tasks.js' },
  { id: 'briefing', label: 'Morning Briefing', group: 'Automation', module: './screens/briefing.js' },
  { id: 'profile', label: 'Profile & Goals', group: 'About You', module: './screens/profile.js' },
];

export function findSection(id) {
  return SECTIONS.find((s) => s.id === id) || SECTIONS[0];
}
