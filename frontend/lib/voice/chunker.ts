/**
 * Where to break a streaming reply so it can be spoken as it arrives.
 *
 * Extracted because both playback queues need exactly this and the originals
 * carried two copies of the constants and the fallback rule, each with a
 * comment pointing at the other. Pure, so it can be tested directly — and it
 * is the fiddly part: the rest of both queues is plumbing.
 *
 * **A whole sentence is the unit**, except for the FIRST flush of a reply. A
 * long opening sentence would otherwise leave the assistant visibly silent for
 * several seconds while text is already appearing on screen, so the opening
 * clause is allowed to go early — once, then full sentences resume.
 */

const SENTENCE_END = /[^.!?]*[.!?]+(\s+|$)/;
const CLAUSE_END = /[^,;:]*[,;:]+\s+/;

/** Below this a clause is not worth flushing: "Sure," alone sounds choppy and
 *  costs a whole synthesis round trip for almost nothing. */
export const CLAUSE_MIN_CHARS = 24;
/** Last resort for an opening that has no punctuation at all for a while —
 *  it bounds how long first audio can be made to wait either way. */
export const CLAUSE_FALLBACK_CHARS = 90;

export interface Chunked {
  /** Ready to speak, in order. */
  pieces: string[];
  /** What is left over, still waiting for its boundary. */
  rest: string;
  /** True once anything at all has been flushed for this reply. */
  flushed: boolean;
}

/** Take everything speakable out of `buffer`. */
export function takeSpeakable(buffer: string, alreadyFlushed: boolean): Chunked {
  const pieces: string[] = [];
  let rest = buffer;
  let flushed = alreadyFlushed;

  let match = SENTENCE_END.exec(rest);
  while (match && match[0].trim()) {
    pieces.push(match[0].trim());
    rest = rest.slice(match[0].length);
    flushed = true;
    match = SENTENCE_END.exec(rest);
  }

  // The opening-clause fallback, once per reply.
  if (!flushed && rest.trim()) {
    const clause = CLAUSE_END.exec(rest);
    if (clause && clause[0].trim().length >= CLAUSE_MIN_CHARS) {
      pieces.push(clause[0].trim());
      rest = rest.slice(clause[0].length);
      flushed = true;
    } else if (rest.length >= CLAUSE_FALLBACK_CHARS) {
      pieces.push(rest.trim());
      rest = '';
      flushed = true;
    }
  }

  return { pieces, rest, flushed };
}
