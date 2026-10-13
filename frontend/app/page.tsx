'use client';

import dynamic from 'next/dynamic';
import { useEffect, useMemo, useState } from 'react';

import { ConversationPanel } from '@/components/conversation/ConversationPanel';
import { useAssistant } from '@/components/conversation/useAssistant';
import { HomeScreen } from '@/components/home/HomeScreen';
import { AskJarvis } from '@/components/shell/AskJarvis';
import { Drawer } from '@/components/shell/Drawer';
import { Blurb, ComingInStage, PageShell } from '@/components/shell/PageShell';
import { ServiceKeys } from '@/components/shell/ServiceKeys';
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

  // The "Ask Jarvis" dock on every other page: the same conversation as Home.
  const docked = (
    <ConversationPanel
      key={a.activeConversationId}
      turns={a.turns}
      notConfigured={!a.configured}
      busy={a.busy}
      onSend={a.send}
      onNewChat={a.newChat}
      onDecide={a.decide}
      editing={a.editing}
      onStartEdit={(turn) => a.setEditing(a.editing?.id === turn.id ? null : turn)}
      onCancelEdit={() => a.setEditing(null)}
      onRetryMessage={a.handleRetryMessage}
      draftText={a.composerDraft}
      onDraftConsumed={() => a.setComposerDraft(null)}
      talkingTo={a.talkingTo}
      onCollapse={() => setAskOpen(false)}
      // Only one recognition session runs reliably at a time, so the voice
      // engine stands down when the composer's own mic starts.
      onDictationStart={() => { if (a.engine.current) void a.toggleListening(); }}
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
          {docked}
        </AskJarvis>
      </main>
    );
  }

  return (
    <>
      {drawer}
      <HomeScreen a={a} go={go} menuOpen={drawerOpen} onMenu={() => setDrawerOpen(true)} />
    </>
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
    // The speech-service keys used to sit in Home's gear panel; the design puts
    // them under Settings → Voice. The full Voice screen comes with Settings.
    case 'settings/voice': return old(<ServiceKeys />);
    default:
      return <ComingInStage what={`${tab?.label ?? page.label} arrives with the redesign of Settings. Until then, voice choices are under the gear on Home.`} />;
  }
}
