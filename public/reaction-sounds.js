// Maps a 'reaction' event's `kind` (see server/personality.js's
// REACTION_MARKERS and runner.js) to the real, static audio clip that
// plays for it. One shared file so pipeline-engine.js and duplex-engine.js
// can't drift out of sync on which clip a given kind actually points to.
//
// These are real, pre-recorded/generated sound clips — never TTS-spoken
// text (see this session's "Real Vocal Laughter" plan for why: no current
// TTS path can reliably produce a genuine non-verbal sound rather than
// spoken syllables).
//
// PLACEHOLDER, NOT A REAL LAUGH: laugh.wav is a short synthesized two-note
// chirp, generated with zero external dependencies, standing in only to
// prove the marker -> strip -> event -> queue -> playback pipeline actually
// works end-to-end (see the plan's own reasoning for building this way
// while the real ElevenLabs generation was blocked on account quota).
// Replace this ONE file with a genuine laugh clip once sourced — nothing
// else in the pipeline needs to change.
export const REACTION_SOUNDS = {
  laugh: '/sounds/laugh.wav',
};
