// The same sequence of chat-store operations, run against the Node implementation.
import * as cs from '../../../server/chat-store.js';

const out = [];
const c1 = cs.createConversation();
out.push(['create', { title: c1.title, pinned: c1.pinned, archived: c1.archived }]);

// First user message with text should rename "New chat" -> a title from the text.
cs.appendMessage(c1.id, { role: 'user', text: '  Hello   there,  how   are you? ' });
out.push(['afterFirstUser', cs.getConversation(c1.id).title]);

// A very long first message must be truncated with an ellipsis.
const c2 = cs.createConversation();
cs.appendMessage(c2.id, { role: 'user', text: 'x'.repeat(200) });
out.push(['longTitle', cs.getConversation(c2.id).title]);

// A non-user first message must NOT rename it.
const c3 = cs.createConversation();
cs.appendMessage(c3.id, { role: 'assistant', text: 'I speak first' });
out.push(['assistantFirst', cs.getConversation(c3.id).title]);

// Empty text must not rename it either.
const c4 = cs.createConversation();
cs.appendMessage(c4.id, { role: 'user', text: '' });
out.push(['emptyUser', cs.getConversation(c4.id).title]);

// Payload handling: extras ride in payload, a bare role/text row stores NULL.
cs.appendMessage(c1.id, { role: 'assistant', text: 'reply', toolCalls: [{ name: 'get_time', args: { tz: 'UTC' } }] });
cs.appendMessage(c1.id, { role: 'tool', text: 'result' });
cs.appendMessage(c1.id, { role: 'assistant', text: 'plain', modelId: undefined });

const msgs = cs.getMessages(c1.id);
out.push(['messages', msgs.map((m) => ({ role: m.role, text: m.text, hasToolCalls: Boolean(m.toolCalls), keys: Object.keys(m).sort() }))]);

// updateLastAssistantMessage merges into payload.
cs.updateLastAssistantMessage(c1.id, { interrupted: true, spokenText: 'pla' });
const after = cs.getMessages(c1.id).at(-1);
out.push(['patched', { interrupted: after.interrupted, spokenText: after.spokenText, text: after.text }]);

// removeLastMessageIfMatches is a no-op on a role mismatch.
out.push(['removeWrongRole', cs.removeLastMessageIfMatches(c1.id, 'user')]);
out.push(['removeRightRole', cs.removeLastMessageIfMatches(c1.id, 'assistant')]);

// rename / pin / archive
cs.renameConversation(c1.id, '  Renamed  ');
cs.setPinned(c3.id, true);
cs.setArchived(c4.id, true);
out.push(['renamed', cs.getConversation(c1.id).title]);

// Ordering: pinned first, then by updated_at desc. Archived excluded by default.
out.push(['list', cs.listConversations().map((c) => ({ title: c.title, pinned: c.pinned, messageCount: c.messageCount }))]);
out.push(['listArchived', cs.listConversations({ includeArchived: true }).length]);

// Search: title LIKE and FTS over message bodies.
out.push(['searchTitle', cs.listConversations({ query: 'Renamed' }).map((c) => c.title)]);
out.push(['searchBody', cs.listConversations({ query: 'I speak first' }).map((c) => c.title)]);
// A query with FTS-hostile characters must not throw.
out.push(['searchTricky', cs.listConversations({ query: 'C++ setup?' }).length]);

// searchMessages: AND first, OR fallback, and per-term quoting safety.
out.push(['searchMsgAnd', cs.searchMessages('speak first').map((r) => r.excerpt)]);
out.push(['searchMsgOr', cs.searchMessages('speak zzzznotpresent').map((r) => r.role)]);
out.push(['searchMsgEmpty', cs.searchMessages('   ')]);
out.push(['searchMsgTricky', cs.searchMessages('NOT "quoted" C++').length]);
out.push(['searchMsgExclude', cs.searchMessages('speak first', { excludeConversationId: c3.id }).length]);

// getMessagesSince / getLastUserMessageAt
out.push(['since', cs.getMessagesSince(c1.id, 1).map((m) => m.seq)]);
out.push(['lastUserAtIsSet', Boolean(cs.getLastUserMessageAt(c1.id))]);
out.push(['lastUserAtNone', cs.getLastUserMessageAt(c3.id)]);

// active id round-trip
out.push(['activeInitial', cs.getActiveId()]);
cs.setActiveId(c1.id);
cs.setActiveId(c2.id);
out.push(['activeAfterUpsert', cs.getActiveId() === c2.id]);

// delete cascades to messages
cs.deleteConversation(c2.id);
out.push(['afterDelete', cs.getConversation(c2.id)]);
out.push(['isConversation', cs.isConversation(c1.id), cs.isConversation('nope')]);

process.stdout.write(JSON.stringify(out, null, 2));
