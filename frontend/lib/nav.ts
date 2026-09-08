/**
 * Every section beyond the assistant itself, in one list.
 *
 * The drawer, the hash router and voice navigation (`open_section`) all read
 * from here — the same single source of truth the interface this replaces used,
 * for the same reason: adding a capability should be one entry and one screen,
 * not three places that can disagree about what exists.
 */

export type SectionId =
  | 'home'
  | 'notifications'
  | 'chat-history'
  | 'models'
  | 'skills'
  | 'app-control'
  | 'tasks'
  | 'jobs'
  | 'briefing'
  | 'profile'
  | 'memory'
  | 'improvement';

export interface Section {
  id: SectionId;
  label: string;
  group: string;
  /** What this screen is for, shown while it is still a placeholder. */
  blurb: string;
  /** False until the screen itself is ported — the drawer says so plainly. */
  ready: boolean;
}

export const SECTIONS: Section[] = [
  { id: 'home', label: 'Assistant', group: 'Assistant', ready: true,
    blurb: 'Talking to Jarvis.' },
  { id: 'notifications', label: 'Notifications', group: 'Assistant', ready: true,
    blurb: 'Everything Jarvis has told you, including while you were away.' },
  { id: 'chat-history', label: 'Chat History', group: 'Assistant', ready: false,
    blurb: 'Every past conversation, searchable.' },
  { id: 'models', label: 'Model Settings', group: 'Brain', ready: false,
    blurb: 'The models Jarvis can think with, and the keys they need.' },
  { id: 'skills', label: 'Skills', group: 'Abilities', ready: false,
    blurb: 'Folders of instructions Jarvis can follow.' },
  { id: 'app-control', label: 'App Control', group: 'Abilities', ready: false,
    blurb: 'The apps and services Jarvis is connected to.' },
  { id: 'tasks', label: 'Scheduled Tasks', group: 'Automation', ready: true,
    blurb: 'Work that runs on a clock.' },
  { id: 'jobs', label: 'Background Jobs', group: 'Automation', ready: false,
    blurb: 'Long-running work happening while you talk about something else.' },
  { id: 'briefing', label: 'Morning Briefing', group: 'Automation', ready: false,
    blurb: 'What Jarvis tells you at the start of the day.' },
  { id: 'profile', label: 'Profile & Goals', group: 'About You', ready: false,
    blurb: 'What Jarvis knows about you, and what you are working towards.' },
  { id: 'memory', label: 'Memory', group: 'About You', ready: false,
    blurb: 'Every durable fact Jarvis has saved — editable and undoable.' },
  { id: 'improvement', label: 'Self-Improvement', group: 'About You', ready: false,
    blurb: 'What Jarvis has learned about its own work.' },
];

/** Section order matters; group order follows first appearance. */
export const GROUPS: string[] = SECTIONS.reduce<string[]>((groups, section) => {
  if (!groups.includes(section.group)) groups.push(section.group);
  return groups;
}, []);

/** Home is the fallback for an unknown id — a bad hash shows the assistant
 *  rather than an empty screen. */
export const HOME: Section = SECTIONS[0]!;

export function findSection(id: string | null | undefined): Section {
  return SECTIONS.find((section) => section.id === id) ?? HOME;
}
