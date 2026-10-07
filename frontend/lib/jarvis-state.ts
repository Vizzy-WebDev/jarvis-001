/** What the voice engines (and a typed turn) report Jarvis is doing — the
 *  finer states the five below are folded from. */
export type OrbState =
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'hearing_speech'
  | 'tool_running'
  | 'interrupted';

/**
 * What Jarvis is doing, as the person sees it everywhere: the five fixed states
 * of the design (Standby, Listening, Thinking, Processing, Speaking) and their
 * colours. The engines report finer states; this is the one place they are
 * folded into the five, so the core, the header label and the menu mark never
 * disagree.
 */
export type JarvisState = 'standby' | 'listening' | 'thinking' | 'processing' | 'speaking';

export function jarvisState(orb: OrbState): JarvisState {
  switch (orb) {
    case 'listening':
    case 'hearing_speech':
    case 'interrupted':
      return 'listening';
    case 'thinking':
      return 'thinking';
    case 'tool_running':
      return 'processing';
    case 'speaking':
      return 'speaking';
    default:
      return 'standby';
  }
}

/** The colour of each state. Standby is a variable because light mode deepens
 *  it; the others are fixed and never themeable. */
export const STATE_COLOUR: Record<JarvisState, string> = {
  standby: 'rgb(var(--orb-standby))',
  listening: '#22d3ee',
  thinking: '#a78bfa',
  processing: '#f5a524',
  speaking: '#ff3b30',
};

export const STATE_LABEL: Record<JarvisState, string> = {
  standby: 'STANDBY',
  listening: 'LISTENING',
  thinking: 'THINKING',
  processing: 'PROCESSING',
  speaking: 'SPEAKING',
};
