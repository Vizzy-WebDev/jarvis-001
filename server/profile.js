// The "About You" store — short notes Jarvis has been told about the
// user's work and goals. Feeds the morning briefing's personalization and
// its proactive suggestions (see scheduler/briefing.js).
//
// Stage 2: this is now a thin adapter over server/memory/memory-store.js,
// fixed to the 'About You' category — data/profile.json (and this file's
// own direct file I/O) is gone; the old entries were migrated once, in
// db.js's migration 2. Every exported function here keeps its EXACT
// original signature/shape on purpose, so server.js's /api/profile routes
// and public/screens/profile.js (and briefing.js's listEntries() call)
// needed zero changes — this file's job is entirely to be that seam.
//
// Kept separate from remember_about_me.js calling memory-store directly
// (rather than going through this file) because this module now exists
// only for the manual "Add a note" flow on the Profile & Goals screen —
// see remember_about_me.js's own header comment.

import { listMemories, createMemory, deleteMemory } from './memory/memory-store.js';

const CATEGORY = 'About You';

function toEntry(memory) {
  return { id: memory.id, text: memory.text, addedAt: memory.createdAt };
}

// memory-store.js's listMemories() returns newest-first (by design, for a
// general browse view); the original data/profile.json shape was
// oldest-first (a plain appended array), and public/screens/profile.js
// still does its OWN `.reverse()` on top of this expecting that — sorting
// back to oldest-first here, rather than touching that screen, is what
// keeps its display order unchanged.
export function listEntries() {
  return listMemories({ category: CATEGORY })
    .map(toEntry)
    .sort((a, b) => a.addedAt.localeCompare(b.addedAt));
}

export function addEntry(text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) throw new Error('No text given.');
  return toEntry(createMemory({ category: CATEGORY, text: trimmed, sourceKind: 'chat', sourceRef: null }));
}

export function deleteEntry(id) {
  deleteMemory(id);
}
