/**
 * Every page of the app, in one list.
 *
 * The menu, the hash router and voice navigation (`open_section`) all read
 * from here, so adding a page is one entry here plus one screen, not three
 * places that can disagree about what exists.
 *
 * The menu is eleven rows (design 9a). Five of them are grouped: one page whose
 * sub-pages are pill tabs hanging under the header (design 10b). A route is
 * `#/<page>` or `#/<page>/<tab>`; anything after that belongs to the screen
 * (`#/content/niche/Psychology`), so a refresh stays there and Back leaves it.
 */

export type PageId =
  | 'home'
  | 'chat-history'
  | 'notifications'
  | 'artifacts'
  | 'abilities'
  | 'content'
  | 'routines'
  | 'capabilities'
  | 'you'
  | 'knowledge'
  | 'settings';

export interface Tab {
  id: string;
  label: string;
  /** One line saying what this is for, shown above a screen that has not been
   *  redesigned yet (a redesigned screen says it in its own layout). */
  blurb: string;
}

export interface Page {
  id: PageId;
  label: string;
  /** 24×24 stroke path for the menu tile. */
  icon: string;
  blurb: string;
  tabs?: Tab[];
}

export const PAGES: Page[] = [
  { id: 'home', label: 'Home', blurb: 'Talking to Jarvis.',
    icon: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z' },
  { id: 'chat-history', label: 'Chat History', blurb: 'Every past conversation, searchable.',
    icon: 'M4 5h16v11H8l-4 4z' },
  { id: 'notifications', label: 'Notifications',
    blurb: 'Everything Jarvis has told you, including while you were away.',
    icon: 'M18 15.5V11a6 6 0 1 0-12 0v4.5L4.5 18h15zM10 21h4' },
  { id: 'artifacts', label: 'Artifacts',
    blurb: 'Every file Jarvis has made for you — open it, download it, go back to the chat it came from, or delete it.',
    icon: 'M6 3h8l4 4v14H6zM14 3v4h4' },
  { id: 'abilities', label: 'Abilities', blurb: 'Who and what Jarvis hands work to.',
    icon: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 6.5M18 14a6 6 0 0 1 3.5 6',
    tabs: [
      { id: 'specialists', label: 'Specialists',
        blurb: 'Expert agents Jarvis hands work to — and ones you create yourself.' },
      { id: 'jobs', label: 'Background Jobs',
        blurb: 'Long-running work happening while you talk about something else.' },
    ] },
  { id: 'content', label: 'Content Management',
    blurb: 'Your finished content — added by you, Jarvis or your agents. Review it, schedule it, publish it.',
    icon: 'M12 3l9 5-9 5-9-5zM3 13l9 5 9-5' },
  { id: 'routines', label: 'Routines', blurb: 'Work that runs on a clock.',
    icon: 'M3 5h18v16H3zM3 10h18M8 3v4M16 3v4',
    tabs: [
      { id: 'scheduling', label: 'Scheduling', blurb: 'Work that runs on a clock.' },
      { id: 'briefing', label: 'Morning Briefing', blurb: 'What Jarvis tells you at the start of the day.' },
    ] },
  { id: 'capabilities', label: 'Capabilities', blurb: 'The apps Jarvis is connected to, and the skills it can follow.',
    icon: 'M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z',
    tabs: [
      { id: 'connectors', label: 'Connectors', blurb: 'The apps and services Jarvis is connected to.' },
      { id: 'skills', label: 'Skills', blurb: 'Folders of instructions Jarvis can follow.' },
    ] },
  { id: 'you', label: 'You', blurb: 'Who you are, and what you are working towards.',
    icon: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0',
    tabs: [
      { id: 'profile', label: 'Profile', blurb: 'What Jarvis knows about you.' },
      { id: 'goals', label: 'Goals', blurb: 'What you are working towards.' },
    ] },
  { id: 'knowledge', label: 'Knowledge', blurb: 'What Jarvis remembers, and what it has learned.',
    icon: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
    tabs: [
      { id: 'memory', label: 'Memory',
        blurb: 'Every durable fact Jarvis has saved — editable and undoable.' },
      { id: 'graph', label: 'Knowledge Graph', blurb: 'How what Jarvis knows connects.' },
      { id: 'improvement', label: 'Self-Improvement',
        blurb: 'What Jarvis has learned about its own work.' },
    ] },
  { id: 'settings', label: 'Settings', blurb: 'How Jarvis is set up.',
    icon: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1',
    tabs: [
      { id: 'models', label: 'Models',
        blurb: 'The providers Jarvis connects to, and the models it can think with.' },
      { id: 'voice', label: 'Voice', blurb: 'How Jarvis hears you and speaks back.' },
      { id: 'appearance', label: 'Appearance', blurb: 'Colours and light or dark.' },
      { id: 'quiet-hours', label: 'Quiet Hours', blurb: 'When Jarvis keeps to itself.' },
      { id: 'presence', label: 'Presence and sound', blurb: 'Resting, waking and sound cues.' },
      { id: 'displays', label: 'Displays', blurb: 'A second screen beside the laptop.' },
      { id: 'jobs', label: 'Background jobs', blurb: 'How much runs at once.' },
    ] },
];

export const HOME: Page = PAGES[0]!;

/**
 * The ids pages had before they were grouped. They keep working — a bookmark,
 * a notification's own link, a test and a spoken "open my memory" all still
 * land in the right place — without rewriting the address bar.
 */
export const ALIASES: Record<string, [PageId, string]> = {
  memory: ['knowledge', 'memory'],
  improvement: ['knowledge', 'improvement'],
  models: ['settings', 'models'],
  skills: ['capabilities', 'skills'],
  'app-control': ['capabilities', 'connectors'],
  agents: ['abilities', 'specialists'],
  jobs: ['abilities', 'jobs'],
  tasks: ['routines', 'scheduling'],
  briefing: ['routines', 'briefing'],
  profile: ['you', 'profile'],
};

export interface Route {
  page: Page;
  /** The open tab of a grouped page; null for a page without tabs. */
  tab: Tab | null;
}

export function findPage(id: string | null | undefined): Page {
  return PAGES.find((page) => page.id === id) ?? HOME;
}

/** A hash (without `#/`) as a page and tab. An unknown page is Home, an unknown
 *  tab is the page's first, so a bad link never shows an empty screen. */
export function resolveRoute(path: string): Route {
  const [first = '', second = ''] = path.split('/');
  const alias = ALIASES[first];
  const page = findPage(alias ? alias[0] : first || 'home');
  const wanted = alias ? alias[1] : second;
  const tab = page.tabs ? page.tabs.find((t) => t.id === wanted) ?? page.tabs[0]! : null;
  return { page, tab };
}

/** Where `go(id)` sends the hash: an alias becomes its page/tab. */
export function canonical(id: string): string {
  const alias = ALIASES[id];
  return alias ? `${alias[0]}/${alias[1]}` : id;
}
