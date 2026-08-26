// Skill: lets Jarvis reach into every past conversation, not just the one
// currently open — "what did we decide about the invoice thing?", "you said
// something about this last week". Before this tool existed there was no
// path from a live turn to server/chat-store.js's SQLite store at all, even
// though the FULL transcript of every conversation has always been sitting
// there, indexed for full-text search (see db.js's messages_fts table) —
// only the human, through the Chat History screen, could ever search it.
// This exposes that same search to the model.
//
// chat-store.js imports only db.js (a leaf module itself), so this import
// does NOT cross the circular-import invariant in root CLAUDE.md — same
// precedent as look_it_up.js importing research.js.
//
// Read-only over the user's own local data — no `confirm`, same reasoning
// as review_memories.js (nothing changes just by looking).

import { searchMessages } from '../chat-store.js';
import { toSearchQuery } from '../research.js';

const MAX_RESULTS = 8;

/** "March 14" / "Jan 3, 2025" — coarse on purpose, matching the level of precision a person would actually say out loud. */
function friendlyDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const opts = d.getFullYear() === now.getFullYear() ? { month: 'long', day: 'numeric' } : { month: 'long', day: 'numeric', year: 'numeric' };
  return d.toLocaleDateString('en-US', opts);
}

export default {
  name: 'search_conversations',
  meta: true,
  description:
    'Search everything the user has ever said across ALL past conversations, not just the current one. ' +
    'Use this when they refer to something from an earlier conversation ("what did we decide about...", ' +
    '"you said last week...") instead of saying you don\'t remember. Never use it for anything already in the current conversation.',
  parameters: {
    type: 'object',
    properties: {
      query: {
        type: 'string',
        description: 'A few keywords for the subject — not a full question. E.g. "invoice automation stripe", not "what did we decide about the invoice thing".',
      },
    },
    required: ['query'],
  },
  async run(args, ctx) {
    // A model sometimes hands over a whole question despite the
    // instruction above — toSearchQuery() (research.js) already solves
    // exactly this reduction for web search, so reuse it here rather than
    // duplicating the same keyword-extraction logic a second time.
    const looksLikeQuestion = /\s/.test(args.query || '') && (args.query || '').split(/\s+/).length > 6;
    const query = looksLikeQuestion ? toSearchQuery(args.query) || args.query : args.query;

    const results = searchMessages(query, { limit: MAX_RESULTS, excludeConversationId: ctx?.sessionId });

    if (!results.length) {
      return {
        ok: true,
        count: 0,
        spoken_hint: 'Nothing in past conversations matched — say plainly that you searched and found nothing, do not guess or invent an answer.',
      };
    }

    return {
      ok: true,
      count: results.length,
      results: results.map((r) => ({
        conversationId: r.conversationId,
        title: r.title,
        date: friendlyDate(r.createdAt),
        who: r.role === 'user' ? 'the user said' : 'Jarvis said',
        excerpt: r.excerpt,
      })),
      // A button to open the single best match, next to the spoken reply —
      // never switches anything on its own (app.js renders it as a click
      // target only; see its ui_action handler). Only the top hit, not
      // every result: "which old conversation was this" almost always has
      // one dominant answer, and one button reads as an offer, several
      // reads as a list to work through.
      ui_action: { type: 'open_conversation', conversationId: results[0].conversationId, title: results[0].title },
      spoken_hint:
        'Answer from these excerpts. Say WHEN each relevant one was said (use the date given) rather than stating it as true right now — an old answer may no longer hold. ' +
        'A button to open the best-matching conversation is already shown alongside your reply — mention you can open it if that would help, but only actually open it if they say yes.',
    };
  },
};
