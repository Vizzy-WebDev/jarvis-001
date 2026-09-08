# The identical sequence, run against the Python implementation.
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import jarvis.chat_store as cs

out = []
c1 = cs.create_conversation()
out.append(['create', {'title': c1['title'], 'pinned': c1['pinned'], 'archived': c1['archived']}])

cs.append_message(c1['id'], {'role': 'user', 'text': '  Hello   there,  how   are you? '})
out.append(['afterFirstUser', cs.get_conversation(c1['id'])['title']])

c2 = cs.create_conversation()
cs.append_message(c2['id'], {'role': 'user', 'text': 'x' * 200})
out.append(['longTitle', cs.get_conversation(c2['id'])['title']])

c3 = cs.create_conversation()
cs.append_message(c3['id'], {'role': 'assistant', 'text': 'I speak first'})
out.append(['assistantFirst', cs.get_conversation(c3['id'])['title']])

c4 = cs.create_conversation()
cs.append_message(c4['id'], {'role': 'user', 'text': ''})
out.append(['emptyUser', cs.get_conversation(c4['id'])['title']])

cs.append_message(c1['id'], {'role': 'assistant', 'text': 'reply', 'toolCalls': [{'name': 'get_time', 'args': {'tz': 'UTC'}}]})
cs.append_message(c1['id'], {'role': 'tool', 'text': 'result'})
cs.append_message(c1['id'], {'role': 'assistant', 'text': 'plain', 'modelId': None})

msgs = cs.get_messages(c1['id'])
out.append(['messages', [{'role': m['role'], 'text': m.get('text'), 'hasToolCalls': bool(m.get('toolCalls')), 'keys': sorted(m.keys())} for m in msgs]])

cs.update_last_assistant_message(c1['id'], {'interrupted': True, 'spokenText': 'pla'})
after = cs.get_messages(c1['id'])[-1]
out.append(['patched', {'interrupted': after.get('interrupted'), 'spokenText': after.get('spokenText'), 'text': after.get('text')}])

out.append(['removeWrongRole', cs.remove_last_message_if_matches(c1['id'], 'user')])
out.append(['removeRightRole', cs.remove_last_message_if_matches(c1['id'], 'assistant')])

cs.rename_conversation(c1['id'], '  Renamed  ')
cs.set_pinned(c3['id'], True)
cs.set_archived(c4['id'], True)
out.append(['renamed', cs.get_conversation(c1['id'])['title']])

out.append(['list', [{'title': c['title'], 'pinned': c['pinned'], 'messageCount': c.get('messageCount')} for c in cs.list_conversations()]])
out.append(['listArchived', len(cs.list_conversations(include_archived=True))])

out.append(['searchTitle', [c['title'] for c in cs.list_conversations(query='Renamed')]])
out.append(['searchBody', [c['title'] for c in cs.list_conversations(query='I speak first')]])
out.append(['searchTricky', len(cs.list_conversations(query='C++ setup?'))])

out.append(['searchMsgAnd', [r['excerpt'] for r in cs.search_messages('speak first')]])
out.append(['searchMsgOr', [r['role'] for r in cs.search_messages('speak zzzznotpresent')]])
out.append(['searchMsgEmpty', cs.search_messages('   ')])
out.append(['searchMsgTricky', len(cs.search_messages('NOT "quoted" C++'))])
out.append(['searchMsgExclude', len(cs.search_messages('speak first', exclude_conversation_id=c3['id']))])

out.append(['since', [m['seq'] for m in cs.get_messages_since(c1['id'], 1)]])
out.append(['lastUserAtIsSet', bool(cs.get_last_user_message_at(c1['id']))])
out.append(['lastUserAtNone', cs.get_last_user_message_at(c3['id'])])

out.append(['activeInitial', cs.get_active_id()])
cs.set_active_id(c1['id'])
cs.set_active_id(c2['id'])
out.append(['activeAfterUpsert', cs.get_active_id() == c2['id']])

cs.delete_conversation(c2['id'])
out.append(['afterDelete', cs.get_conversation(c2['id'])])
out.append(['isConversation', cs.is_conversation(c1['id']), cs.is_conversation('nope')])

sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False))
