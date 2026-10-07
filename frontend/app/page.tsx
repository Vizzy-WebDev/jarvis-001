'use client';

import dynamic from 'next/dynamic';
import { useEffect, useMemo, useState } from 'react';

import { ChatHistoryDrawer } from '@/components/conversation/ChatHistoryDrawer';
import { ConversationPanel } from '@/components/conversation/ConversationPanel';
import { useAssistant } from '@/components/conversation/useAssistant';
import { AskJarvis } from '@/components/shell/AskJarvis';
import { Drawer } from '@/components/shell/Drawer';
import { Header } from '@/components/shell/Header';
import { Blurb, ComingInStage, PageShell } from '@/components/shell/PageShell';
import { SettingsPanel } from '@/components/shell/SettingsPanel';
import { MicButton } from '@/components/stage/MicButton';
import { Orb } from '@/components/stage/Orb';
import { MicIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { jarvisState } from '@/lib/jarvis-state';
import type { Route } from '@/lib/nav';
import { useHashRoute } from '@/lib/useHashRoute';

/**
 * The whole interface.
 *
 * Four things about its shape are fixed: the menu button is at the top left
 * and is how every page is reached; the orb is centred on the stage with the
 * mic beneath it and nothing on the page can move or resize it; the
 * conversation floats OVER the right edge of the stage rather than beside it,
 * reserving no column; and the conversation and the composer are one panel.
 *
 * One page rather than a route per screen: the production build is a static
 * export served by FastAPI from a single port, and a hash is what `open_section`
 * speaks, so voice navigation and a click land in the same place. Each screen
 * is its own chunk, fetched the first time it is opened (and quietly ahead of
 * time once the app is idle), so Home never waits for code it is not showing.
 */

const loaders = {
  artifacts: () => import('@/components/screens/ArtifactsScreen').then((m) => m.ArtifactsScreen),
  notifications: () => import('@/components/screens/NotificationsScreen').then((m) => m.NotificationsScreen),
  models: () => import('@/components/screens/ModelsScreen').then((m) => m.ModelsScreen),
  tasks: () => import('@/components/screens/TasksScreen').then((m) => m.TasksScreen),
  memory: () => import('@/components/screens/MemoryScreen').then((m) => m.MemoryScreen),
  profile: () => import('@/components/screens/ProfileScreen').then((m) => m.ProfileScreen),
  improvement: () => import('@/components/screens/ImprovementScreen').then((m) => m.ImprovementScreen),
  jobs: () => import('@/components/screens/JobsScreen').then((m) => m.JobsScreen),
  briefing: () => import('@/components/screens/BriefingScreen').then((m) => m.BriefingScreen),
  skills: () => import('@/components/screens/SkillsScreen').then((m) => m.SkillsScreen),
  agents: () => import('@/components/screens/AgentsScreen').then((m) => m.AgentsScreen),
  content: () => import('@/components/screens/ContentScreen').then((m) => m.ContentScreen),
  chatHistory: () => import('@/components/screens/ChatHistoryScreen').then((m) => m.ChatHistoryScreen),
  connectors: () => import('@/components/screens/AppControlScreen').then((m) => m.AppControlScreen),
};

const loading = () => (
  <p className="py-16 text-center text-[13px] text-ink-faint" data-testid="screen-loading">Loading…</p>
);
const ArtifactsScreen = dynamic(loaders.artifacts, { loading });
const NotificationsScreen = dynamic(loaders.notifications, { loading });
const ModelsScreen = dynamic(loaders.models, { loading });
const TasksScreen = dynamic(loaders.tasks, { loading });
const MemoryScreen = dynamic(loaders.memory, { loading });
const ProfileScreen = dynamic(loaders.profile, { loading });
const ImprovementScreen = dynamic(loaders.improvement, { loading });
const JobsScreen = dynamic(loaders.jobs, { loading });
const BriefingScreen = dynamic(loaders.briefing, { loading });
const SkillsScreen = dynamic(loaders.skills, { loading });
const AgentsScreen = dynamic(loaders.agents, { loading });
const ContentScreen = dynamic(loaders.content, { loading });
const ChatHistoryScreen = dynamic(loaders.chatHistory, { loading });
const AppControlScreen = dynamic(loaders.connectors, { loading });

/** Pages that need more than the reading column. */
const WIDE = new Set(['artifacts', 'content']);

export default function Home() {
  const [route, go] = useHashRoute();
  const onHome = route.page.id === 'home';
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [askOpen, setAskOpen] = useState(false);
  const a = useAssistant(go, onHome);

  // Every screen's code, fetched while nothing else is happening, so the first
  // visit to a page is as quick as the second.
  useEffect(() => {
    const idle = (fn: () => void) =>
      (typeof window.requestIdleCallback === 'function'
        ? window.requestIdleCallback(fn, { timeout: 4000 })
        : setTimeout(fn, 1500));
    const timer = window.setTimeout(() => idle(() => {
      for (const load of Object.values(loaders)) void load().catch(() => undefined);
    }), 2500);
    return () => window.clearTimeout(timer);
  }, []);

  // The screen is built once per page/tab, not on every streamed word of a
  // reply: the conversation updates this component many times a second, and a
  // screen that re-rendered with it would make typing and scrolling stutter.
  const { startChatWith, resumeConversation, openArtifactInChat } = a;
  const pageKey = `${route.page.id}/${route.tab?.id ?? ''}`;
  // However the page changed — a menu row, a spoken "open my memory", a link
  // in a notification, Back — the menu is not left open over the new page.
  useEffect(() => setDrawerOpen(false), [pageKey]);
  const screen = useMemo(
    () => screenFor(route, go, startChatWith,
                    (id) => void resumeConversation(id),
                    (conversationId, artifactId) => void openArtifactInChat(conversationId, artifactId)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pageKey],
  );

  const conversation = (docked: boolean) => (
    <ConversationPanel
      key={a.activeConversationId}
      turns={a.turns}
      notConfigured={!a.configured}
      busy={a.busy}
      onSend={a.send}
      onNewChat={a.newChat}
      onDecide={a.decide}
      onEditMessage={a.handleEditMessage}
      onRetryMessage={a.handleRetryMessage}
      draftText={a.composerDraft}
      onDraftConsumed={() => a.setComposerDraft(null)}
      historyOpen={docked ? undefined : historyOpen}
      onToggleHistory={docked ? undefined : () => setHistoryOpen((was) => !was)}
      talkingTo={a.talkingTo}
      onTalkTo={docked ? undefined : a.setTalkingTo}
      onCollapse={docked ? () => setAskOpen(false) : undefined}
      // Only one recognition session runs reliably at a time, so the
      // voice engine stands down when the composer's own mic starts.
      onDictationStart={() => {
        if (!a.engine.current) return;
        void a.toggleListening();
      }}
      // The other direction of the same rule: starting the main mic
      // stands the composer's own dictation down, via a prop it watches
      // rather than an imperative call — see Composer's own effect.
      voiceEngineActive={a.listening}
      isSpeaking={() => a.engine.current?.state === 'speaking'}
    />
  );

  const drawer = (
    <Drawer
      open={drawerOpen}
      current={route.page.id}
      unread={a.unread}
      onClose={() => setDrawerOpen(false)}
      onNavigate={go}
    />
  );

  // --- a page that is not Home ------------------------------------------------

  if (!onHome) {
    return (
      <main className="h-screen">
        {drawer}
        <PageShell
          page={route.page}
          tab={route.tab}
          state={jarvisState(a.orbState)}
          wide={WIDE.has(route.page.id)}
          onMenu={() => setDrawerOpen(true)}
          onTab={(tabId) => go(`${route.page.id}/${tabId}`)}
        >
          {screen}
        </PageShell>
        <AskJarvis open={askOpen} onToggle={() => setAskOpen((was) => !was)}>
          {conversation(true)}
        </AskJarvis>
      </main>
    );
  }

  return (
    <main className="relative h-screen overflow-hidden">
      {drawer}

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
          void a.resumeConversation(id);
        }}
      />

      <Header
        unread={a.unread}
        sharing={a.sharing}
        settingsOpen={settingsOpen}
        onMenu={() => setDrawerOpen(true)}
        onToggleSharing={a.toggleSharing}
        onNotifications={() => go('notifications')}
        onToggleSettings={() => setSettingsOpen((open) => !open)}
      />

      <SettingsPanel
        open={settingsOpen}
        speakReplies={a.speakReplies}
        onSpeakReplies={a.setSpeakReplies}
        engine={a.engineId}
        onEngine={a.setEngineId}
        voice={a.voiceId}
        onVoice={a.setVoiceId}
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
          <Orb state={a.orbState} />

          {/* Out of flow, pinned to the top of the stage: the orb's box is fixed
              and nothing here may move or resize it. */}
          {a.watching.length > 0 && (
            <div className="pointer-events-none absolute inset-x-0 top-0 flex justify-center">
              <div
                data-testid="watching-bar"
                className="pointer-events-auto flex max-w-[80%] items-center gap-3 rounded-pill
                           border border-state-warn/30 bg-state-warn/10 px-3.5 py-1.5"
              >
                <span className="truncate text-[12px] text-state-warn">
                  Watching for {a.watching.map((monitor) => monitor.description).join(', ')}
                </span>
                {/* One watch gets a Stop; several get a way to see them, because
                    a single Stop over a list of three would silently pick one. */}
                {a.watching.length === 1 ? (
                  <button
                    type="button"
                    data-testid="watching-stop"
                    onClick={() => void a.stopWatching(a.watching[0]!.id)}
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
                    See all {a.watching.length}
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
              {a.status}
            </p>
            <div className="flex items-center gap-3">
              <MicButton
                listening={a.listening}
                hint={a.listening ? 'Listening — click to stop' : 'Click to talk'}
                onToggle={() => void a.toggleListening()}
              />
              {/* A real, separate mute — never stops or interrupts Jarvis, only
                  toggles microphone capture (`toggleMute`). Distinct from the
                  mic button above, which ends the whole session. */}
              <IconButton
                label={a.muted ? 'Unmute the microphone' : 'Mute the microphone'}
                data-testid="mute"
                active={a.muted}
                disabled={!a.listening}
                onClick={a.toggleMute}
              >
                <MicIcon className="h-[18px] w-[18px]" muted={a.muted} />
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
          {conversation(false)}
        </aside>
      </div>
    </main>
  );
}

/** The screen for a page and tab. One place rather than a branch inside the
 *  render, so adding a screen is one line. A screen not yet redesigned gets the
 *  one-line blurb that used to sit under its title. */
function screenFor(
  route: Route,
  go: (id: string) => void,
  startChatWith: (draft: string) => void,
  resumeConversation: (id: string) => void,
  openArtifactInChat: (conversationId: string, artifactId: string) => void,
): React.ReactNode {
  const { page, tab } = route;
  const blurb = <Blurb>{tab?.blurb ?? page.blurb}</Blurb>;
  const old = (node: React.ReactNode) => <>{blurb}{node}</>;
  switch (`${page.id}/${tab?.id ?? ''}`) {
    case 'artifacts/': return old(<ArtifactsScreen onNavigate={go} onOpenInChat={openArtifactInChat} />);
    case 'notifications/': return old(<NotificationsScreen onNavigate={go} />);
    case 'chat-history/':
      return old(<ChatHistoryScreen onNavigate={go} onResumeConversation={resumeConversation} />);
    case 'content/': return old(<ContentScreen onNavigate={go} />);
    case 'abilities/specialists': return old(<AgentsScreen />);
    case 'abilities/jobs': return old(<JobsScreen />);
    case 'routines/scheduling': return old(<TasksScreen onNavigate={go} />);
    case 'routines/briefing': return old(<BriefingScreen onNavigate={go} />);
    case 'capabilities/connectors': return old(<AppControlScreen />);
    case 'capabilities/skills': return old(<SkillsScreen onCreateWithJarvis={startChatWith} />);
    case 'you/profile': return old(<ProfileScreen />);
    case 'you/goals':
      return <ComingInStage what="Goals — what you are working towards, with milestones and progress — arrive with the redesign of You." />;
    case 'knowledge/memory': return old(<MemoryScreen />);
    case 'knowledge/graph':
      return <ComingInStage what="The Knowledge Graph — a map of how what Jarvis knows connects — arrives with the redesign of Knowledge." />;
    case 'knowledge/improvement': return old(<ImprovementScreen />);
    case 'settings/models': return old(<ModelsScreen />);
    default:
      return <ComingInStage what={`${tab?.label ?? page.label} arrives with the redesign of Settings. Until then, voice choices are under the gear on Home.`} />;
  }
}
