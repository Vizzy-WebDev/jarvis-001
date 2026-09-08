/**
 * Which real sound plays for a reaction.
 *
 * One shared map so the engines cannot drift about what a given kind points at.
 * These are real clips, never text handed to a voice: no synthesis path can
 * reliably produce a genuine non-verbal sound rather than spoken syllables,
 * which is the whole reason this exists as audio at all.
 *
 * **The current clip is a placeholder, not a real laugh** — a short synthesised
 * two-note chirp, built with no dependencies, standing in only to prove the
 * marker → strip → event → queue → playback path works end to end. Replacing it
 * with a real recording is a change to this one line.
 */
export const REACTION_SOUNDS: Record<string, string> = {
  laugh: '/sounds/laugh.wav',
};

export function soundFor(kind: string): string | null {
  return REACTION_SOUNDS[kind] ?? null;
}
