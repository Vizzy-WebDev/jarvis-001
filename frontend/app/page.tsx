'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { ConversationPanel } from '@/components/conversation/ConversationPanel';
import { attachmentOf, type Turn } from '@/components/conversation/Message';
import { BriefingScreen } from '@/components/screens/BriefingScreen';
import { ChatHistoryScreen } from '@/components/screens/ChatHistoryScreen';
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
import { api, ApiRequestError } from '@/lib/api';
import type { Message as StoredMessage, Monitor } from '@/lib/api-types';
import { streamTurn, type RunningTurn } from '@/lib/chat';
import { DuplexEngine } from '@/lib/voice/duplex-engine';
import type { VoiceEngine } from '@/lib/voice/engine';
import { PipelineEngine } from '@/lib/voice/pipeline-engine';
import { RealtimeEngine } from '@/lib/voice/realtime-engine';
import { useHashRoute } from '@/lib/useHashRoute';
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
  const [listening, setListening] = useState(false);
  const [engineId, setEngineId] = useState('pipeline');
  const [voiceId, setVoiceId] = useState('browser');
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
          setTurns(detail.messages.filter(isShown).map(toTurn));
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

  const send = useCallback((text: string, attachments: string[]) => {
    const asked = text || 'I’ve attached this.';
    const replyId = newId();
    setTurns((current) => [
      ...current,
      { id: newId(), role: 'user', text: asked },
      { id: replyId, role: 'assistant', text: '', streaming: true },
    ]);
    setBusy(true);
    setOrbState('thinking');
    setStatus('Thinking…');

    const patch = (change: (turn: Turn) => Turn) =>
      setTurns((current) => current.map((turn) => (turn.id === replyId ? change(turn) : turn)));

    const turn = streamTurn({
      message: text,
      attachments,
      onEvent: (event) => {
        switch (event.type) {
          case 'chunk':
            setOrbState('speaking');
            patch((turn) => ({ ...turn, text: turn.text + event.text }));
            break;
          case 'tool_result': {
            setOrbState('tool_running');
            const attachment = attachmentOf(event);
            if (attachment) patch((turn) => ({ ...turn, attachment }));
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
            patch((turn) => ({ ...turn, text: event.error, streaming: false }));
            break;
          case 'done':
            patch((turn) => ({ ...turn, text: event.text || turn.text, streaming: false }));
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
      setBusy(false);
      setOrbState('idle');
      setStatus('Type below to talk to Jarvis');
    });
  }, []);

  const decide = useCallback(async (approvalId: string, decision: 'allow' | 'deny') => {
    try {
      await api.approvals.decide(approvalId, decision);
    } catch (err) {
      if (err instanceof ApiRequestError) setStatus(err.message);
      return;
    }
    setTurns((current) =>
      current.map((turn) => (turn.approvalId === approvalId ? { ...turn, decided: decision } : turn)));
  }, []);

  async function newChat() {
    running.current?.cancel();
    try {
      const created = await api.conversations.create();
      await api.conversations.activate(created.conversation.id);
    } catch (err) {
      if (err instanceof ApiRequestError) setStatus(err.message);
      return;
    }
    setTurns([]);
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
      setOrbState('idle');
      setStatus('Type below to talk to Jarvis');
      return;
    }

    sttNotice.current = null;
    const started = buildEngine(engineId, speakReplies ? voiceId : 'browser');
    started.on('state', ({ state }) => {
      setOrbState(state as OrbState);
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
    started.on('restart', () => setTurns((current) => clearReply(current)));
    started.on('done', () => setBusy(false));
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
      setStatus('That voice could not produce audio — check its key on Model Settings.'));
    started.on('error', ({ message }) => {
      setStatus(message);
      setListening(false);
      engine.current = null;
    });

    engine.current = started;
    setListening(true);
    await started.start();
  }, [engineId, speakReplies, voiceId]);

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

  // --- a section that is not the assistant ------------------------------------

  if (section.id !== 'home') {
    return (
      <main className="h-screen">
        <Drawer open={drawerOpen} current={section} onClose={() => setDrawerOpen(false)} onNavigate={go} />
        <GenericScreen section={section} onMenu={() => setDrawerOpen(true)}>
          {screenFor(section.id, go) ?? <NotPortedYet section={section} />}
        </GenericScreen>
      </main>
    );
  }

  return (
    <main className="relative h-screen overflow-hidden">
      <Drawer open={drawerOpen} current={section} onClose={() => setDrawerOpen(false)} onNavigate={go} />

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
            <MicButton
              listening={listening}
              hint={listening ? 'Listening — click to stop' : 'Click to talk'}
              onToggle={() => void toggleListening()}
            />
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
            turns={turns}
            notConfigured={!configured}
            busy={busy}
            onSend={send}
            onNewChat={newChat}
            onDecide={decide}
            // Only one recognition session runs reliably at a time, so the
            // voice engine stands down when the composer's own mic starts.
            onDictationStart={() => {
              if (!engine.current) return;
              void toggleListening();
            }}
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
  return new PipelineEngine({ voiceOutput });
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

/** A model switch mid-reply: drop what the failed one said, a fresh one follows. */
function clearReply(turns: Turn[]): Turn[] {
  const last = turns[turns.length - 1];
  if (last && last.role === 'assistant' && last.streaming) return turns.slice(0, -1);
  return turns;
}

/** The screen for a section, or nothing if it is still being ported. One place
 *  rather than a branch inside the render, so adding a screen is one line. */
function screenFor(id: string, go: (id: string) => void): React.ReactNode {
  if (id === 'notifications') return <NotificationsScreen onNavigate={go} />;
  if (id === 'models') return <ModelsScreen />;
  if (id === 'tasks') return <TasksScreen onNavigate={go} />;
  if (id === 'memory') return <MemoryScreen />;
  if (id === 'profile') return <ProfileScreen />;
  if (id === 'improvement') return <ImprovementScreen />;
  if (id === 'jobs') return <JobsScreen />;
  if (id === 'briefing') return <BriefingScreen onNavigate={go} />;
  if (id === 'skills') return <SkillsScreen />;
  if (id === 'chat-history') return <ChatHistoryScreen onNavigate={go} />;
  return null;
}

/** Tool traffic is not conversation. The transcript shows what was said. */
function isShown(message: StoredMessage): boolean {
  return (message.role === 'user' || message.role === 'assistant') && Boolean(message.text);
}

function toTurn(message: StoredMessage): Turn {
  return {
    id: message.id,
    role: message.role === 'user' ? 'user' : 'assistant',
    text: message.text ?? '',
    interrupted: message.interrupted,
  };
}
