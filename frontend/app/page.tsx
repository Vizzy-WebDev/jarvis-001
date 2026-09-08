'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { ConversationPanel } from '@/components/conversation/ConversationPanel';
import { attachmentOf, type Turn } from '@/components/conversation/Message';
import { GenericScreen, NotPortedYet } from '@/components/screens/GenericScreen';
import { ModelsScreen } from '@/components/screens/ModelsScreen';
import { NotificationsScreen } from '@/components/screens/NotificationsScreen';
import { TasksScreen } from '@/components/screens/TasksScreen';
import { Drawer } from '@/components/shell/Drawer';
import { Header } from '@/components/shell/Header';
import { SettingsPanel } from '@/components/shell/SettingsPanel';
import { MicButton } from '@/components/stage/MicButton';
import { Orb } from '@/components/stage/Orb';
import { api, ApiRequestError } from '@/lib/api';
import type { Message as StoredMessage } from '@/lib/api-types';
import { streamTurn, type RunningTurn } from '@/lib/chat';
import { PipelineEngine } from '@/lib/voice/pipeline-engine';
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
  const engine = useRef<PipelineEngine | null>(null);

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
      } catch {
        /* a frame we cannot read is not worth acting on */
      }
    };
    return () => source.close();
  }, []);

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
      setListening(false);
      setOrbState('idle');
      setStatus('Type below to talk to Jarvis');
      return;
    }

    const started = new PipelineEngine({ voiceOutput: speakReplies ? voiceId : 'browser' });
    started.on('state', ({ state }) => {
      setOrbState(state as OrbState);
      setStatus(SPOKEN_STATE[state] ?? 'Listening…');
    });
    started.on('transcript', ({ text, final }) => {
      if (final && text) setTurns((current) => [...current, { id: newId(), role: 'user', text }]);
    });
    started.on('chunk', ({ text }) => setTurns((current) => appendToReply(current, text)));
    started.on('restart', () => setTurns((current) => clearReply(current)));
    started.on('done', () => setBusy(false));
    started.on('paused', ({ reason }) => setStatus(reason));
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
  }, [speakReplies, voiceId]);

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
          />
        </aside>
      </div>
    </main>
  );
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
