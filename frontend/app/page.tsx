'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { ChatHistoryDrawer } from '@/components/conversation/ChatHistoryDrawer';
import { ConversationPanel } from '@/components/conversation/ConversationPanel';
import { attachmentOf, attachmentsOf, type Turn } from '@/components/conversation/Message';
import { AgentsScreen } from '@/components/screens/AgentsScreen';
import { AppControlScreen } from '@/components/screens/AppControlScreen';
import { BriefingScreen } from '@/components/screens/BriefingScreen';
import { ChatHistoryScreen } from '@/components/screens/ChatHistoryScreen';
import { ContentScreen } from '@/components/screens/ContentScreen';
import { GenericScreen, NotPortedYet } from '@/components/screens/GenericScreen';
import { ImprovementScreen } from '@/components/screens/ImprovementScreen';
import { JobsScreen } from '@/components/screens/JobsScreen';
import { MemoryScreen } from '@/components/screens/MemoryScreen';
import { ModelsScreen } from '@/components/screens/ModelsScreen';
import { NotificationsScreen } from '@/components/screens/NotificationsScreen';
import { ProfileScreen } from '@/components/screens/ProfileScreen';
import { SkillsScreen } from '@/components/screens/SkillsScreen';
import { TasksScreen } from '@/components/screens/TasksScreen';
import { Drawer } from '@/components/shell/Drawer';
import { Header } from '@/components/shell/Header';
import { SettingsPanel } from '@/components/shell/SettingsPanel';
import { MicButton } from '@/components/stage/MicButton';
import { Orb } from '@/components/stage/Orb';
import { MicIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { api, ApiRequestError } from '@/lib/api';
import type { Message as StoredMessage, Monitor } from '@/lib/api-types';
import { cardsFromToolResult, type FileCard } from '@/lib/artifacts';
import { streamTurn, type RunningTurn } from '@/lib/chat';
import { DuplexEngine } from '@/lib/voice/duplex-engine';
import type { VoiceEngine } from '@/lib/voice/engine';
import { PipelineEngine } from '@/lib/voice/pipeline-engine';
import { RealtimeEngine } from '@/lib/voice/realtime-engine';
import { useHashRoute } from '@/lib/useHashRoute';
import { MODELS_CHANGED } from '@/lib/useModels';
import type { OrbState } from '@/lib/orb';

/**
 * The whole interface.
 *
 * Four things about its shape are fixed, and everything else here is a
 * redesign: the hamburger is at the top left and is how every section is
 * reached; the orb is centred on the stage with the mic beneath it and nothing
 * on the page can move or resize it; the conversation floats OVER the right
 * edge of the stage rather than beside it, reserving no column; and the
 * conversation and the composer are one panel, not two.
 *
 * One page rather than a route per screen: the production build is a static
 * export served by FastAPI from a single port, and a hash is what `open_section`
 * speaks, so voice navigation and a click land in the same place.
 */
let nextId = 0;
const newId = () => `t${(nextId += 1)}`;

export default function Home() {
  const [section, go] = useHashRoute();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [speakReplies, setSpeakReplies] = useState(false);
  const [sharing, setSharing] = useState(false);
  /** What Jarvis is watching for. Shown in the shell rather than only on a
   *  screen because a watch is running whether or not anyone is looking at the
   *  list of them. */
  const [watching, setWatching] = useState<Monitor[]>([]);
  const [unread, setUnread] = useState(0);
  const [configured, setConfigured] = useState(true);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [orbState, setOrbState] = useState<OrbState>('idle');
  const [status, setStatus] = useState('Type below to talk to Jarvis');
  const [busy, setBusy] = useState(false);
  /** A specialist the person is talking to directly; null is Jarvis. Mirrored
   *  in a ref because `send` is a stable callback that must read the current one. */
  const [talkingTo, setTalkingToState] = useState<{ id: string; name: string } | null>(null);
  const talkingToRef = useRef<{ id: string; name: string } | null>(null);
  const setTalkingTo = useCallback((next: { id: string; name: string } | null) => {
    talkingToRef.current = next;
    setTalkingToState(next);
  }, []);
  /** Whether a turn this tab started is running — read by the event stream, so a
   *  specialist working in the background for a job never shows up as a note here. */
  const busyRef = useRef(false);
  const [listening, setListening] = useState(false);
  /** Whether the microphone is muted, independent of everything else the
   *  engine is doing — see `toggleMute` for why this is kept as its own
   *  concept rather than folded into stopping/interrupting. */
  const [muted, setMuted] = useState(false);
  const [engineId, setEngineId] = useState('pipeline');
  const [voiceId, setVoiceId] = useState('browser');
  /** Text handed to the composer from elsewhere (e.g. "Create with Jarvis" on
   *  the Skills screen), landing unsent for the person to edit or send. */
  const [composerDraft, setComposerDraft] = useState<string | null>(null);
  const running = useRef<RunningTurn | null>(null);
  const engine = useRef<VoiceEngine | null>(null);
  /**
   * A note that outlives one state change — today, that recognition landed on
   * the browser's own rather than the provider that was picked. A ref, not
   * state: the status handler below is a closure the engine keeps for its whole
   * life, and reading a captured `useState` value there would read the value
   * from the moment the engine was built, forever.
   */
  const sttNotice = useRef<string | null>(null);
  /**
   * Keyed onto `ConversationPanel` below so switching conversations always
   * remounts it — the fix for a real bug: resuming from the chat-history
   * picker updated `turns` on the SAME long-lived `Transcript` instance,
   * whose scroll-pin ref could be stale from whatever conversation was open
   * before, and whose layout had no reason to recompute for a suddenly much
   * longer transcript set all at once. Resuming from the full Chat History
   * page happened to dodge this by accident — it navigates through a
   * different section and back, which unmounts and remounts the whole home
   * layout anyway. This makes both paths behave the same way on purpose,
   * without touching `Transcript`'s own (already correct) scroll logic.
   */
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  /** Read by the event stream, whose effect is set up once. */
  const activeConversationRef = useRef<string | null>(null);
  activeConversationRef.current = activeConversationId;

  // Choosing, connecting or removing a model happens on another screen, or in the
  // composer's own picker. Whether Jarvis can answer is asked of the backend again
  // when that changes, rather than being carried here as a copy that could go stale.
  useEffect(() => {
    const recheck = async () => {
      try {
        const now = await api.status();
        setConfigured(now.configured);
        setStatus((text) =>
          now.configured
            ? (text === 'No model is set up yet' ? 'Type below to talk to Jarvis' : text)
            : (text === 'Type below to talk to Jarvis' ? 'No model is set up yet' : text));
      } catch {
        /* what was last known stays */
      }
    };
    window.addEventListener(MODELS_CHANGED, recheck);
    return () => window.removeEventListener(MODELS_CHANGED, recheck);
  }, []);

  // --- what is already true when the page opens ------------------------------

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [status, notifications, conversations] = await Promise.all([
          api.status(),
          api.notifications.list(50),
          api.conversations.list(),
        ]);
        if (cancelled) return;
        setConfigured(status.configured);
        setUnread(notifications.notifications.filter((n) => !n.read).length);
        if (!status.configured) setStatus('No model is set up yet');

        // The conversation the user had open, so a reload does not look like
        // amnesia. The backend decides which one that is.
        const active = conversations.conversations.find((c) => c.id === conversations.activeId);
        if (active) {
          const detail = await api.conversations.open(active.id);
          if (cancelled) return;
          setTurns(turnsFrom(detail.messages));
          setActiveConversationId(active.id);
        }
      } catch {
        if (!cancelled) setStatus('Jarvis is not answering — is it running?');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const source = new EventSource('/api/events');
    source.onmessage = (raw) => {
      try {
        const event = JSON.parse(raw.data) as { type?: string; payload?: Record<string, unknown> };
        if (event.type === 'notification.stored') setUnread((count) => count + 1);
        // A specialist Jarvis stopped waiting for has finished: its work is shown
        // here as its own reply, in the conversation it was asked from. It also
        // reaches the bell, and Jarvis is told on the next message.
        if (event.type === 'agent.run_finished') {
          const late = event as unknown as { late?: boolean; result?: string; runId: string;
                                             agentName: string; conversationId?: string;
                                             status?: string; error?: string };
          if (late.late && late.conversationId && late.conversationId === activeConversationRef.current) {
            const text = late.status === 'done'
              ? (late.result || `${late.agentName} finished.`)
              : `${late.agentName} couldn’t finish: ${late.error || 'no reason was given.'}`;
            setTurns((current) => [...current, {
              id: `late-${late.runId}`, role: 'assistant', text, speaker: late.agentName,
              failed: late.status !== 'done' && late.status !== 'awaiting_approval' }]);
            return;
          }
        }
        // A specialist working on this tab's own request, shown as it happens:
        // a delegated task can take a minute, and silence reads as stuck.
        if ((event.type === 'agent.run_started' || event.type === 'agent.run_finished')
            && busyRef.current) {
          const run = event as unknown as { runId: string; agentName: string; status?: string;
                                            requestedBy?: string };
          if (run.requestedBy === 'operator') return; // the person's own direct chat
          const noteId = `agent-${run.runId}`;
          if (event.type === 'agent.run_started') {
            setTurns((current) => current.some((turn) => turn.id === noteId) ? current : [
              ...current, { id: noteId, role: 'note', text: `${run.agentName} is working on it…` }]);
          } else {
            const outcome = run.status === 'done' ? 'finished'
              : run.status === 'awaiting_approval' ? 'needs your go-ahead' : 'couldn’t finish';
            setTurns((current) => current.map((turn) => turn.id === noteId
              ? { ...turn, text: `${run.agentName} ${outcome}.` } : turn));
          }
        }
        if (event.type === 'screen.watch') {
          setSharing(Boolean((event.payload as { sharing?: boolean })?.sharing));
        }
        // Broadcast rather than answered to the tab that clicked: a click on the
        // Scheduled Tasks screen and a spoken "stop watching that" must never
        // leave two windows disagreeing about what is still running.
        if (event.type === 'monitor.stopped') {
          const stopped = (event as unknown as { monitorId?: string }).monitorId;
          setWatching((current) => current.filter((monitor) => monitor.id !== stopped));
        }
      } catch {
        /* a frame we cannot read is not worth acting on */
      }
    };
    return () => source.close();
  }, []);

  // Read once on load: a watch started before this tab opened is still running,
  // and the bar has to be right from the first paint rather than only after
  // something happens.
  useEffect(() => {
    api.monitors.list()
      .then((found) => setWatching(found.monitors.filter((m) => m.status === 'watching')))
      .catch(() => undefined);
  }, []);

  async function stopWatching(id: string) {
    setWatching((current) => current.filter((monitor) => monitor.id !== id));
    try {
      await api.monitors.stop(id);
    } catch {
      const found = await api.monitors.list().catch(() => null);
      if (found) setWatching(found.monitors.filter((m) => m.status === 'watching'));
    }
  }

  // --- sending ---------------------------------------------------------------

  const send = useCallback((
    text: string,
    attachments: { id: string; name: string; kind: string }[],
    editOf?: string,
  ) => {
    const asked = text || 'I’ve attached this.';
    const userTurnId = newId();
    const replyId = newId();
    // Shown in the sender's own bubble, not just sent to the model — attached
    // files used to vanish from the transcript entirely once sent. The
    // content route (routes/uploads.py) forces the same download-disposition
    // headers routes/artifacts.py does; browsers still render an <img>/<video>
    // fetched through it inline, same as tool-result attachments already do.
    const sentAttachments = attachments.map((a) => ({
      kind: a.kind, url: `/api/uploads/${a.id}/content`,
    }));
    setTurns((current) => {
      // Edit/Retry: drop the message being redone and everything after it —
      // the server does the same cut (chat_store.truncate_to_before) against
      // the persisted transcript, so the two stay in lockstep. A stale
      // `editOf` (not found — e.g. a second click after it already resolved)
      // is a plain send instead of a no-op, since the id it named is gone.
      const base = editOf
        ? (() => {
            const cut = current.findIndex((turn) => turn.id === editOf);
            return cut === -1 ? current : current.slice(0, cut);
          })()
        : current;
      return [
        ...base,
        { id: userTurnId, role: 'user', text: asked, attachments: sentAttachments },
        { id: replyId, role: 'assistant', text: '', streaming: true,
          speaker: talkingToRef.current?.name },
      ];
    });
    setBusy(true);
    busyRef.current = true;
    setOrbState('thinking');
    setStatus('Thinking…');

    const patch = (change: (turn: Turn) => Turn) =>
      setTurns((current) => current.map((turn) => (turn.id === replyId ? change(turn) : turn)));

    const turn = streamTurn({
      message: text,
      attachments: attachments.map((a) => a.id),
      editOf,
      agent: talkingToRef.current?.id,
      onEvent: (event) => {
        switch (event.type) {
          case 'routed':
            // The user message's real, persisted id — once known, replace the
            // local placeholder so a later Edit/Retry on THIS message (before
            // any reload) references something chat_store actually has.
            if (event.userMessageId) {
              setTurns((current) => current.map((t) =>
                t.id === userTurnId ? { ...t, id: event.userMessageId! } : t));
            }
            break;
          case 'chunk':
            setOrbState('speaking');
            patch((turn) => ({ ...turn, text: turn.text + event.text }));
            break;
          case 'tool_result': {
            setOrbState('tool_running');
            const attachment = attachmentOf(event);
            const produced = attachmentsOf(event);
            if (attachment) {
              patch((turn) => ({
                ...turn,
                attachment: turn.attachment ?? attachment,
                files: [...(turn.files ?? []),
                  ...produced.filter((file) => !(turn.files ?? []).some((f) => f.url === file.url))],
              }));
            }
            // Asked out loud to open a section. The hash router is the same one
            // the drawer drives, so a spoken "open my memory" and a click land
            // in exactly the same place.
            if (event.navigate?.section) go(event.navigate.section);
            break;
          }
          case 'model_switch':
            setTurns((current) => [
              ...current,
              { id: newId(), role: 'note', text: `Switched to ${event.to} — ${event.reason}` },
            ]);
            break;
          case 'approval_required':
            // A control, not a note: the run is stopped until this is answered.
            setTurns((current) => [
              ...current,
              {
                id: newId(),
                role: 'approval',
                approvalId: event.approvalId,
                capability: event.capability,
                text: event.reason || `${event.capability} needs your go-ahead.`,
              },
            ]);
            break;
          case 'interrupted':
            patch((turn) => ({ ...turn, text: event.spokenText, interrupted: true }));
            break;
          case 'error':
            patch((turn) => ({ ...turn, text: event.error, streaming: false, failed: true }));
            break;
          case 'done':
            // Same real-id swap as `routed` above, for the reply's own bubble.
            patch((turn) => ({
              ...turn, text: event.text || turn.text, streaming: false,
              id: event.messageId ?? turn.id,
              speaker: event.agent?.name ?? turn.speaker,
            }));
            break;
          default:
            break;
        }
      },
    });

    running.current = turn;
    turn.done.then(() => {
      patch((turn) => ({ ...turn, streaming: false }));
      running.current = null;
      busyRef.current = false;
      setBusy(false);
      setOrbState('idle');
      setStatus('Type below to talk to Jarvis');
    });
  }, []);

  /** The upload id a sent attachment's content URL was built from — the
   *  inverse of `send()`'s own `/api/uploads/${a.id}/content`. Needed so
   *  Retry/Edit can resend the SAME files, since the composer only ever
   *  hands `send()` ids and `Turn.attachments` only ever keeps the URL. */
  const uploadIdFromAttachment = (url: string): string | null => {
    const match = url.match(/\/api\/uploads\/([^/]+)\/content$/);
    return match?.[1] ?? null;
  };

  const handleEditMessage = useCallback((turn: Turn, newText: string) => {
    if (busy) return;
    const attachments = (turn.attachments ?? [])
      .map((a) => uploadIdFromAttachment(a.url))
      .filter((id): id is string => Boolean(id))
      .map((id) => ({ id, name: '', kind: '' }));
    send(newText, attachments, turn.id);
  }, [busy, send]);

  const handleRetryMessage = useCallback((userTurn: Turn) => {
    if (busy) return;
    const attachments = (userTurn.attachments ?? [])
      .map((a) => uploadIdFromAttachment(a.url))
      .filter((id): id is string => Boolean(id))
      .map((id) => ({ id, name: '', kind: '' }));
    send(userTurn.text, attachments, userTurn.id);
  }, [busy, send]);

  const decide = useCallback(async (approvalId: string, decision: 'allow' | 'deny') => {
    let made: FileCard[] = [];
    try {
      const answered = await api.approvals.decide(approvalId, decision);
      made = answered.attachments ?? [];
    } catch (err) {
      if (err instanceof ApiRequestError) setStatus(err.message);
      return;
    }
    setTurns((current) => current.flatMap((turn) => {
      if (turn.approvalId !== approvalId) return [turn];
      const decided = { ...turn, decided: decision };
      // What the allowed action made shows up right under the question, the
      // same card a reload rebuilds from the saved conversation.
      return made.length
        ? [decided, { id: `${approvalId}-files`, role: 'assistant' as const, text: '', files: made }]
        : [decided];
    }));
  }, []);

  async function newChat() {
    // An already-empty conversation has nothing to leave behind — creating
    // another one on top of it is how repeated clicks used to pile up
    // duplicate empty chats. The backend guards this too (session.py's
    // reset_conversation reuses an empty conversation instead of inserting a
    // new row), but this skips the round trip entirely for the common case.
    if (turns.length === 0) return;
    running.current?.cancel();
    let created;
    try {
      // create() already activates the new conversation server-side
      // (session.py's reset_conversation) — no separate activate() call needed.
      created = await api.conversations.create();
    } catch (err) {
      if (err instanceof ApiRequestError) setStatus(err.message);
      return;
    }
    setTurns([]);
    setActiveConversationId(created.conversation.id);
    setBusy(false);
    setOrbState('idle');
  }

  /**
   * Picking an old conversation back up — from the chat-history slide-out, or
   * from the full Chat History page's own "Pick up where this left off."
   * Activating alone changes what the SERVER thinks is current; without
   * re-fetching and repainting `turns`, the panel would keep showing whatever
   * was already there, which reads as the resume having silently done nothing.
   */
  async function resumeConversation(id: string) {
    running.current?.cancel();
    try {
      await api.conversations.activate(id);
      const detail = await api.conversations.open(id);
      setTurns(turnsFrom(detail.messages));
      setActiveConversationId(id);
    } catch (err) {
      setStatus(err instanceof ApiRequestError ? err.message : 'Could not open that conversation.');
      return;
    }
    setBusy(false);
    setOrbState('idle');
  }

  /**
   * Start or stop listening.
   *
   * The engine is built on demand and torn down completely when it stops: it
   * owns a live microphone, and an engine kept around "just in case" is a
   * microphone kept open for no reason.
   */
  const toggleListening = useCallback(async () => {
    if (engine.current) {
      engine.current.stop();
      engine.current = null;
      sttNotice.current = null;
      setListening(false);
      setMuted(false);  // stop() always begins the next session unmuted too
      setOrbState('idle');
      setStatus('Type below to talk to Jarvis');
      return;
    }

    sttNotice.current = null;
    const started = buildEngine(engineId, speakReplies ? voiceId : 'browser');
    started.on('state', ({ state }) => {
      setOrbState(state as OrbState);
      if (state === 'idle') {
        // The engine reached idle on its own — an auto-hangup after a quiet
        // spell, or an internal stop() from an error path — not from a click on
        // this button. Whatever got us here already released the microphone;
        // mirroring that reset here is what the manual-stop click branch below
        // already does by hand. Without it, the button stays stuck "on" and the
        // next click just re-stops an already-dead engine instead of starting a
        // fresh one.
        engine.current = null;
        sttNotice.current = null;
        setListening(false);
        setMuted(false);
      }
      // A standing note wins while resting, because that is when there is
      // nothing more urgent to say and it is exactly when someone is wondering
      // why the engine they picked feels no different. It never overwrites
      // thinking or speaking.
      const resting = state === 'listening' || state === 'hearing_speech';
      setStatus(resting && sttNotice.current
        ? sttNotice.current
        : SPOKEN_STATE[state] ?? 'Listening…');
    });
    started.on('transcript', ({ text, final }) => {
      if (final && text) setTurns((current) => [...current, { id: newId(), role: 'user', text }]);
    });
    started.on('chunk', ({ text }) => setTurns((current) => appendToReply(current, text)));
    // The real, stored ids — the same swap `send()` does for a typed message, so
    // Edit and Retry on something SAID cut the conversation on the server too.
    started.on('saved', ({ userMessageId, replyMessageId }) => setTurns((current) =>
      withSavedIds(current, userMessageId, replyMessageId)));
    started.on('restart', () => setTurns((current) => clearReply(current)));
    started.on('done', () => setBusy(false));
    // The same Allow / Not now card a typed turn shows: a spoken request for a
    // tool set to "Need approval" used to end with nothing on screen at all.
    started.on('approval', ({ approvalId, capability, reason }) => setTurns((current) => [
      ...current,
      { id: newId(), role: 'approval', approvalId, capability,
        text: reason || `${capability} needs your go-ahead.` },
    ]));
    started.on('paused', ({ reason }) => setStatus(reason));
    // Not an error: the browser's own recognition is a real path, just not the
    // one that was picked. Saying nothing here is how someone ends up wondering
    // why the engine they chose does not feel any different.
    started.on('stt_fallback', () => {
      sttNotice.current = 'Listening — no speech-recognition key is set up, '
        + 'so this is using the browser’s own.';
      setStatus(sttNotice.current);
    });
    // Deliberately not an error: the reply itself is fine, only its audio
    // failed, and clearing what is on screen would be wrong.
    started.on('tts_failure', () =>
      setStatus('That voice could not produce audio — check its key in Settings.'));
    started.on('error', ({ message, turn, fatal }) => {
      // A real, confirmed bug found while testing this change: this used to
      // just forget the engine (null the ref, flip `listening` off) without
      // ever telling it to actually stop — so on a recoverable recognition
      // error (the browser's SpeechRecognition auto-restarts itself after
      // most of them) the UI claimed "not listening" while a live engine kept
      // running underneath, mic still open, quietly retrying forever. stop()
      // is safe to call here regardless of what state the engine is actually
      // in, and its own 'state' → idle transition (handled above) already
      // does the ref/flag reset — this just makes sure that transition
      // genuinely happens instead of being merely claimed.
      //
      // What it must NOT do is stop for a `turn` failure — the model failing to
      // answer says nothing about the microphone, and stopping there is what made
      // a failed spoken turn end as a silent return to Idle with no trace of it.
      // Those show in the transcript instead, where they last, and the engine goes
      // back to listening by itself. Anything untagged still stops, exactly as before.
      if (turn) setTurns((current) => markReplyFailed(current, message));
      if (fatal ?? !turn) engine.current?.stop();
      setStatus(message);
    });

    engine.current = started;
    setListening(true);
    await started.start();
  }, [engineId, speakReplies, voiceId]);

  /**
   * Mute/unmute, kept deliberately separate from `toggleListening` and from
   * interrupting Jarvis. `setMuted()` (every engine already implements this
   * correctly — see `lib/voice/engine.ts`) touches ONLY microphone capture: it
   * never stops, interrupts, or resets whatever Jarvis is currently doing.
   * Nothing here calls `interrupt()` or `stop()`, on purpose — this is the
   * one control that must never do either.
   */
  const toggleMute = useCallback(() => {
    if (!engine.current) return;
    const next = !engine.current.muted;
    engine.current.setMuted(next);
    setMuted(next);
  }, []);

  // Releasing the microphone is not optional cleanup.
  useEffect(() => () => engine.current?.stop(), []);

  async function toggleSharing() {
    const next = !sharing;
    setSharing(next);
    try {
      await fetch(`/api/observation/share/${next ? 'start' : 'stop'}`, { method: 'POST' });
    } catch {
      setSharing(!next); // it did not take; do not claim it did
    }
  }

  // A screen elsewhere handing a message to the assistant: land it in the
  // composer, unsent, and switch to the conversation.
  function startChatWith(draft: string) {
    setComposerDraft(draft);
    go('home');
  }

  // --- a section that is not the assistant ------------------------------------

  if (section.id !== 'home') {
    return (
      <main className="h-screen">
        <Drawer open={drawerOpen} current={section} onClose={() => setDrawerOpen(false)} onNavigate={go} />
        <GenericScreen section={section} onMenu={() => setDrawerOpen(true)}>
          {screenFor(section.id, go, startChatWith, (id) => void resumeConversation(id))
            ?? <NotPortedYet section={section} />}
        </GenericScreen>
      </main>
    );
  }

  return (
    <main className="relative h-screen overflow-hidden">
      <Drawer open={drawerOpen} current={section} onClose={() => setDrawerOpen(false)} onNavigate={go} />

      {/* Rendered here, not inside ConversationPanel: that panel's own
          backdrop-blur-xl creates a containing block for position:fixed
          descendants, which trapped an earlier version of this drawer inside
          the small floating panel instead of the real viewport. */}
      <ChatHistoryDrawer
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        onViewAll={() => {
          setHistoryOpen(false);
          go('chat-history');
        }}
        onResume={(id) => {
          setHistoryOpen(false);
          void resumeConversation(id);
        }}
      />

      <Header
        unread={unread}
        sharing={sharing}
        settingsOpen={settingsOpen}
        onMenu={() => setDrawerOpen(true)}
        onToggleSharing={toggleSharing}
        onNotifications={() => go('notifications')}
        onToggleSettings={() => setSettingsOpen((open) => !open)}
      />

      <SettingsPanel
        open={settingsOpen}
        speakReplies={speakReplies}
        onSpeakReplies={setSpeakReplies}
        engine={engineId}
        onEngine={setEngineId}
        voice={voiceId}
        onVoice={setVoiceId}
        onNavigate={go}
      />

      {/* The body reserves the floated header's height in its own padding, so
          nothing in the header can push the stage or resize the orb. */}
      <div
        className="relative h-full overflow-hidden px-5 pb-5"
        style={{ paddingTop: 'var(--header-reserve)' }}
      >
        {/* The stage: centred, and sized as if the conversation panel did not
            exist. Its gutters are fixed values, never the panel's width. */}
        <section
          data-testid="stage"
          className="relative h-full min-h-0 w-full"
          style={{ paddingLeft: 'var(--rail-gutter)', paddingRight: 'var(--rail-gutter)' }}
        >
          <Orb state={orbState} />

          {/* Out of flow, pinned to the top of the stage: the orb's box is fixed
              and nothing here may move or resize it. */}
          {watching.length > 0 && (
            <div className="pointer-events-none absolute inset-x-0 top-0 flex justify-center">
              <div
                data-testid="watching-bar"
                className="pointer-events-auto flex max-w-[80%] items-center gap-3 rounded-pill
                           border border-state-warn/30 bg-state-warn/10 px-3.5 py-1.5"
              >
                <span className="truncate text-[12px] text-state-warn">
                  Watching for {watching.map((monitor) => monitor.description).join(', ')}
                </span>
                {/* One watch gets a Stop; several get a way to see them, because
                    a single Stop over a list of three would silently pick one. */}
                {watching.length === 1 ? (
                  <button
                    type="button"
                    data-testid="watching-stop"
                    onClick={() => void stopWatching(watching[0]!.id)}
                    className="shrink-0 text-[12px] text-ink-muted hover:text-ink"
                  >
                    Stop
                  </button>
                ) : (
                  <button
                    type="button"
                    data-testid="watching-open"
                    onClick={() => go('tasks')}
                    className="shrink-0 text-[12px] text-ink-muted hover:text-ink"
                  >
                    See all {watching.length}
                  </button>
                )}
              </div>
            </div>
          )}

          {/* The stage's lower band: what Jarvis is doing, and the control for
              it. Reserved space, so the orb's box above is fixed. */}
          <div
            className="absolute inset-x-0 bottom-0 flex flex-col items-center justify-end gap-4 pb-6"
            style={{ height: 'var(--stage-bottom-reserve)' }}
          >
            <p data-testid="status" className="text-[13px] text-ink-faint" aria-live="polite">
              {status}
            </p>
            <div className="flex items-center gap-3">
              <MicButton
                listening={listening}
                hint={listening ? 'Listening — click to stop' : 'Click to talk'}
                onToggle={() => void toggleListening()}
              />
              {/* A real, separate mute — never stops or interrupts Jarvis, only
                  toggles microphone capture (`toggleMute`). Distinct from the
                  mic button above, which ends the whole session. */}
              <IconButton
                label={muted ? 'Unmute the microphone' : 'Mute the microphone'}
                data-testid="mute"
                active={muted}
                disabled={!listening}
                onClick={toggleMute}
              >
                <MicIcon className="h-[18px] w-[18px]" muted={muted} />
              </IconButton>
            </div>
          </div>
        </section>

        {/* The conversation floats over the RIGHT edge, out of flow: it reserves
            no column and never pushes or resizes the stage. It is one panel —
            the composer is the bottom of it, not a second container. */}
        <aside
          data-testid="conversation-rail"
          className="absolute top-1/2 h-[68%] -translate-y-1/2"
          style={{ right: 'var(--rail-gutter)', width: 'var(--rail-width)' }}
        >
          <ConversationPanel
            key={activeConversationId}
            turns={turns}
            notConfigured={!configured}
            busy={busy}
            onSend={send}
            onNewChat={newChat}
            onDecide={decide}
            onEditMessage={handleEditMessage}
            onRetryMessage={handleRetryMessage}
            draftText={composerDraft}
            onDraftConsumed={() => setComposerDraft(null)}
            historyOpen={historyOpen}
            onToggleHistory={() => setHistoryOpen((was) => !was)}
            talkingTo={talkingTo}
            onTalkTo={setTalkingTo}
            // Only one recognition session runs reliably at a time, so the
            // voice engine stands down when the composer's own mic starts.
            onDictationStart={() => {
              if (!engine.current) return;
              void toggleListening();
            }}
            // The other direction of the same rule: starting the main mic
            // stands the composer's own dictation down, via a prop it watches
            // rather than an imperative call — see Composer's own effect.
            voiceEngineActive={listening}
            isSpeaking={() => engine.current?.state === 'speaking'}
          />
        </aside>
      </div>
    </main>
  );
}

/**
 * The engine the picker chose, built fresh.
 *
 * The id comes from `/api/voice/options`, which decides what to offer from a
 * capability check rather than from any provider's name — so this maps ids to
 * classes and knows nothing else. An unrecognised id falls back to the engine
 * that works with any model rather than failing: the picker only ever offers
 * what is available, so reaching here with something else means a mismatch, and
 * refusing to listen at all would be the worse of the two answers.
 */
function buildEngine(id: string, voiceOutput: string): VoiceEngine {
  if (id === 'duplex') return new DuplexEngine({ voiceOutput });
  if (id === 'realtime') return new RealtimeEngine();
  // Test-only: lets Playwright prove the real idle/grace timeouts fire without
  // waiting out their real durations. Unset in production, so this is a no-op
  // there — PipelineEngine falls back to its own real constants either way.
  const testTimers = (window as unknown as {
    __jarvisTestVoiceTimers?: { handsFreeIdleMs?: number; followupGraceMs?: number };
  }).__jarvisTestVoiceTimers;
  return new PipelineEngine({ voiceOutput, ...testTimers });
}

/** What the status line says for each engine state. */
const SPOKEN_STATE: Record<string, string> = {
  idle: 'Type below to talk to Jarvis',
  listening: 'Listening…',
  hearing_speech: 'Listening…',
  thinking: 'Thinking…',
  tool_running: 'Working on it…',
  speaking: 'Speaking…',
  interrupted: 'Go on…',
};

/** Streamed text lands in the assistant turn already in flight, or starts one. */
function appendToReply(turns: Turn[], text: string): Turn[] {
  const last = turns[turns.length - 1];
  if (last && last.role === 'assistant' && last.streaming) {
    return [...turns.slice(0, -1), { ...last, text: last.text + text }];
  }
  return [...turns, { id: newId(), role: 'assistant', text, streaming: true }];
}

/**
 * A spoken turn's placeholder ids (`t7`) replaced by the ones the server stored.
 * The person's words: the most recent user turn still on a placeholder — a new
 * utterance supersedes the one before it, so `routed` always belongs to the
 * latest. Jarvis's reply: the latest assistant turn, which is also no longer
 * streaming once its `done` has arrived.
 */
function withSavedIds(turns: Turn[], userMessageId?: string, replyMessageId?: string): Turn[] {
  const placeholder = (turn: Turn) => /^t\d+$/.test(turn.id);
  let next = turns;
  if (userMessageId && !turns.some((turn) => turn.id === userMessageId)) {
    const index = findLastIndex(turns, (turn) => turn.role === 'user' && placeholder(turn));
    if (index !== -1) next = next.map((turn, i) => (i === index ? { ...turn, id: userMessageId } : turn));
  }
  if (replyMessageId && !next.some((turn) => turn.id === replyMessageId)) {
    const index = findLastIndex(next, (turn) => turn.role === 'assistant' && placeholder(turn));
    if (index !== -1) {
      next = next.map((turn, i) => (i === index ? { ...turn, id: replyMessageId, streaming: false } : turn));
    }
  }
  return next;
}

function findLastIndex<T>(items: T[], test: (item: T) => boolean): number {
  for (let i = items.length - 1; i >= 0; i -= 1) if (test(items[i]!)) return i;
  return -1;
}

/** A model switch mid-reply: drop what the failed one said, a fresh one follows. */
function clearReply(turns: Turn[]): Turn[] {
  const last = turns[turns.length - 1];
  if (last && last.role === 'assistant' && last.streaming) return turns.slice(0, -1);
  return turns;
}

/**
 * A spoken turn that failed, written into the transcript rather than only into
 * a status line that the engine's own next state change overwrites moments
 * later. Converts the reply already in flight when there is one; otherwise
 * appends a new failed turn, which is the common case — a model that fails
 * before its first chunk never created a bubble to convert.
 */
function markReplyFailed(turns: Turn[], errorText: string): Turn[] {
  const last = turns[turns.length - 1];
  if (last && last.role === 'assistant' && last.streaming) {
    return [...turns.slice(0, -1), { ...last, text: errorText, streaming: false, failed: true }];
  }
  return [...turns, { id: newId(), role: 'assistant', text: errorText, failed: true }];
}

/** The screen for a section, or nothing if it is still being ported. One place
 *  rather than a branch inside the render, so adding a screen is one line. */
function screenFor(
  id: string,
  go: (id: string) => void,
  startChatWith: (draft: string) => void,
  resumeConversation: (id: string) => void,
): React.ReactNode {
  if (id === 'notifications') return <NotificationsScreen onNavigate={go} />;
  if (id === 'models') return <ModelsScreen />;
  if (id === 'tasks') return <TasksScreen onNavigate={go} />;
  if (id === 'memory') return <MemoryScreen />;
  if (id === 'profile') return <ProfileScreen />;
  if (id === 'improvement') return <ImprovementScreen />;
  if (id === 'jobs') return <JobsScreen />;
  if (id === 'briefing') return <BriefingScreen onNavigate={go} />;
  if (id === 'skills') return <SkillsScreen onCreateWithJarvis={startChatWith} />;
  if (id === 'agents') return <AgentsScreen />;
  if (id === 'content') return <ContentScreen onNavigate={go} />;
  if (id === 'chat-history') {
    return <ChatHistoryScreen onNavigate={go} onResumeConversation={resumeConversation} />;
  }
  if (id === 'app-control') return <AppControlScreen />;
  return null;
}

/**
 * A saved conversation as the transcript shows it: what was said, plus the files
 * the tools made along the way. Tool traffic itself is not shown, but a file a
 * tool made is — it is attached to the reply it belongs to, exactly as it
 * appeared live, so reopening a chat never loses its cards. A file made by an
 * action the person allowed later (its turn ended at the question) gets a
 * reply of its own.
 */
function turnsFrom(messages: StoredMessage[]): Turn[] {
  const turns: Turn[] = [];
  let pending: FileCard[] = [];
  let pendingFrom = '';
  const flush = () => {
    if (!pending.length) return;
    turns.push({ id: `${pendingFrom}-files`, role: 'assistant', text: '', files: pending });
    pending = [];
  };
  for (const message of messages) {
    if (message.role === 'tool') {
      for (const entry of message.toolResults ?? []) {
        for (const card of cardsFromToolResult(entry.result)) {
          if (!pending.some((p) => p.url === card.url)) pending.push(card);
        }
      }
      pendingFrom = message.id;
      continue;
    }
    if (!isShown(message)) continue;
    if (message.role === 'assistant' && pending.length) {
      turns.push({ ...toTurn(message), files: pending });
      pending = [];
      continue;
    }
    flush();
    turns.push(toTurn(message));
  }
  flush();
  return turns;
}

/** Tool traffic is not conversation. The transcript shows what was said. */
function isShown(message: StoredMessage): boolean {
  return (message.role === 'user' || message.role === 'assistant') && Boolean(message.text);
}

function toTurn(message: StoredMessage): Turn {
  return {
    id: message.id,
    role: message.role === 'user' ? 'user' : 'assistant',
    text: message.role === 'user' ? withoutModelNote(message.text ?? '') : message.text ?? '',
    interrupted: message.interrupted,
  };
}

/**
 * The server folds a note about attached files into the stored user message
 * (attachments.py's compose_message) — it is for the model, and was never
 * something the person typed, so it is not shown back to them.
 */
function withoutModelNote(text: string): string {
  return text.replace(/\[Note for you, not spoken by the user: [\s\S]*?\](\n\n|$)/, '').trim();
}
