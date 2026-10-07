'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ConversationPanel } from '@/components/conversation/ConversationPanel';
import type { useAssistant } from '@/components/conversation/useAssistant';
import { createCore, type Core, type CoreLayout, type Presence } from '@/lib/core';
import { jarvisState, STATE_COLOUR, STATE_LABEL } from '@/lib/jarvis-state';
import { setPrefs, usePrefs } from '@/lib/usePrefs';
import { WakeListener, type WakeStatus } from '@/lib/voice/wake-listener';

import { HealthCards, HealthChips, useHealth } from './health';
import { HomeHeader, rgba } from './HomeHeader';
import { BellPopover, ShowOnHome } from './popovers';
import { BootLines, RestOverlay, usePresence } from './presence';
import { HEARD_PHRASE, QuickSettings } from './QuickSettings';
import { VoiceDock } from './VoiceDock';

type Assistant = ReturnType<typeof useAssistant>;
type Breakpoint = 'desk' | 'laptop' | 'tabletP' | 'phone';

/** The status line's resting phrase from the conversation hook. Anything else
 *  it says (an error, "Listening…") is news and is shown as it is. */
const HOOK_IDLE = 'Type below to talk to Jarvis';

/**
 * Home (design Home v6): the core in the centre, the conversation floating on
 * the right, health on the left, the voice dock beneath — and the four anchors
 * that never move: menu top left, core centred, conversation floating right,
 * conversation and composer one panel.
 *
 * Laptop and wider is the full layout; a tablet held upright puts the
 * conversation in a bottom panel with health as two chips; a phone shows the
 * core and voice controls, with the conversation a tap away full screen.
 */
export function HomeScreen({ a, go, menuOpen, onMenu }: {
  a: Assistant;
  go: (id: string) => void;
  menuOpen: boolean;
  onMenu: () => void;
}) {
  const prefs = usePrefs();
  const rootRef = useRef<HTMLDivElement>(null);
  const [bp, setBp] = useState<Breakpoint>('desk');
  const [phoneView, setPhoneView] = useState<'core' | 'chat'>('core');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [pop, setPop] = useState<'bell' | 'gear' | 'view' | null>(null);
  const [chips, setChips] = useState<'sys' | 'jar' | null>(null);
  const [vitalsOpen, setVitalsOpen] = useState(false);
  const [wakeStatus, setWakeStatus] = useState<WakeStatus>({ kind: 'off' });
  const show = prefs.homeShow;

  const speaking = a.orbState === 'speaking';
  const busy = a.busy || a.listening;
  const presence = usePresence({ restAfterMin: prefs.restAfterMin, activation: prefs.activation, busy });
  const resting = presence.presence === 'resting';
  // What the core, the pill and the dock show: waking reads as listening (the
  // core lights in its colour as it comes up), resting as standby.
  const state = presence.presence === 'waking' ? 'listening' : resting ? 'standby' : jarvisState(a.orbState);
  const health = useHealth(!resting || prefs.restLook === 'clock');

  // --- the breakpoints, from the root's own size --------------------------------
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width: w, height: h } = entry!.contentRect;
      setBp(w < 700 ? 'phone' : (w < 1100 && h > w * 1.15) ? 'tabletP' : w < 1400 ? 'laptop' : 'desk');
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // --- the core: one loop, fed from refs so it never re-renders anything ----------
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const coreRef = useRef<Core | null>(null);
  const inputRef = useRef({ state, presence: presence.presence as Presence, layout: 'desk' as CoreLayout });
  inputRef.current = {
    state,
    presence: presence.presence,
    layout: bp === 'phone' ? (phoneView === 'chat' ? 'phoneChat' : 'phone') : bp === 'tabletP' ? 'tabletP' : 'desk',
  };
  const prefsRef = useRef(prefs);
  prefsRef.current = prefs;
  const engineRef = a.engine;
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const core = createCore(canvas, () => {
      const live = engineRef.current as unknown as { getOutputLevel?: () => number; getMicLevel?: () => number } | null;
      const st = inputRef.current.state;
      const level = live
        ? st === 'speaking' ? live.getOutputLevel?.() ?? null : st === 'listening' ? live.getMicLevel?.() ?? null : null
        : null;
      return { ...inputRef.current, restLook: prefsRef.current.restLook, activation: prefsRef.current.activation, level };
    });
    coreRef.current = core;
    // The meters' canvases mount before this effect runs; hand them over now.
    core.attachMeter('wave', meters.current.wave);
    core.attachMeter('pill', meters.current.pill);
    return () => { core.destroy(); coreRef.current = null; };
  }, [engineRef]);
  const meters = useRef<{ wave: HTMLCanvasElement | null; pill: HTMLCanvasElement | null }>({ wave: null, pill: null });
  const waveMeter = useCallback((c: HTMLCanvasElement | null) => {
    meters.current.wave = c;
    coreRef.current?.attachMeter('wave', c);
  }, []);
  const pillMeter = useCallback((c: HTMLCanvasElement | null) => {
    meters.current.pill = c;
    coreRef.current?.attachMeter('pill', c);
  }, []);

  // --- the wake word: listening while no session is, and Home is engaged or resting
  const wakeListener = useRef<WakeListener | null>(null);
  const onWakeRef = useRef<() => void>(() => undefined);
  onWakeRef.current = () => {
    if (resting) presence.wake();
    if (!a.listening) void a.toggleListening();
  };
  useEffect(() => {
    wakeListener.current = new WakeListener({ onWake: () => onWakeRef.current(), onStatus: setWakeStatus });
    return () => wakeListener.current?.stop();
  }, []);
  const hears = prefs.wakeWord && prefs.wakePhrases.some((p) => p.toLowerCase() === HEARD_PHRASE.toLowerCase());
  useEffect(() => {
    const listener = wakeListener.current;
    if (!listener || !prefs.loaded) return;
    if (hears && !a.listening) listener.start();
    else listener.stop();
  }, [hears, a.listening, prefs.loaded]);

  // --- keys: Esc stops Jarvis speaking (or ends a session), F = Just Jarvis, Space = push-to-talk
  const presenceOnly = useCallback(() => {
    const any = show.chat || show.sys || show.jar;
    void setPrefs({ homeShow: { chat: !any, sys: !any, jar: !any } });
    setPop(null);
  }, [show]);
  const keys = useRef({ a, presenceOnly, pop, pushToTalk: prefs.pushToTalk, resting, wake: presence.wake });
  keys.current = { a, presenceOnly, pop, pushToTalk: prefs.pushToTalk, resting, wake: presence.wake };
  const pttHeld = useRef(false);
  useEffect(() => {
    const typing = () => /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName ?? '')
      || (document.activeElement as HTMLElement | null)?.isContentEditable;
    const down = (e: KeyboardEvent) => {
      const k = keys.current;
      if (e.defaultPrevented) return;
      if (k.resting) { k.wake(); return; }
      if (e.key === 'Escape' && !k.pop) {
        const live = k.a.engine.current;
        if (k.a.orbState === 'speaking' || k.a.busy) { k.a.interrupt(); return; }
        if (live && k.a.listening) { void k.a.toggleListening(); }
        return;
      }
      if (typing() || e.metaKey || e.ctrlKey || e.altKey) return;
      if ((e.key === 'f' || e.key === 'F') && !e.repeat) { k.presenceOnly(); return; }
      if (e.code === 'Space' && k.pushToTalk && !e.repeat) {
        e.preventDefault();
        pttHeld.current = true;
        if (!k.a.listening) void k.a.toggleListening();
        else if (k.a.muted) k.a.toggleMute();
      }
    };
    const up = (e: KeyboardEvent) => {
      // Letting go mutes rather than ending the session: what was already said
      // still finishes and is sent; nothing more is heard until Space again.
      if (e.code === 'Space' && pttHeld.current) {
        pttHeld.current = false;
        const k = keys.current;
        if (k.a.listening && !k.a.muted) k.a.toggleMute();
      }
    };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    return () => {
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
    };
  }, []);

  // --- what the status line says --------------------------------------------------
  const greeting = useMemo(() => {
    const h = new Date().getHours();
    const part = h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
    const jobs = health?.jarvis.jobsRunning ?? 0;
    const bits = [jobs ? `${jobs} running` : '', a.unread ? `${a.unread} unread` : ''].filter(Boolean);
    return `${part}, Boss${bits.length ? ` · ${bits.join(' · ')}` : ''}`;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presence.greet]);
  const idleText = !a.configured ? 'No model is set up yet'
    : wakeStatus.kind === 'listening' ? `Ready. Say “${HEARD_PHRASE}”`
      : prefs.pushToTalk ? 'Ready. Hold Space to talk'
        : 'Ready. Click the mic to talk';
  const status = presence.presence === 'waking'
    ? (presence.beat >= 5 ? 'Checking in…' : presence.beat >= 3 ? 'Systems online' : 'Waking up…')
    : presence.greet ? greeting
      : a.listening && a.muted && !a.busy ? 'Muted. Tap the mic to unmute'
        : a.status === 'Speaking…' ? 'Speaking · Esc or Stop to interrupt'
          : a.status && a.status !== HOOK_IDLE && a.status !== 'No model is set up yet' ? a.status
            : idleText;
  const label = resting ? 'RESTING' : a.listening && a.muted && !a.busy ? 'MUTED' : STATE_LABEL[state];
  const colour = STATE_COLOUR[state];
  const glow = rgba(colour, 0.45);
  const micNote = !hears ? 'wake word off' : wakeStatus.kind === 'listening' ? 'mic ready'
    : wakeStatus.kind === 'blocked' ? 'mic blocked' : 'getting ready';

  const on = (n: number) => prefs.restLook === 'dim' || presence.beat >= n;
  const onMic = () => {
    if (!a.listening) void a.toggleListening();
    else a.toggleMute();
  };

  const tabletP = bp === 'tabletP';
  const phone = bp === 'phone';
  const hW = bp === 'laptop' ? 256 : 290;
  const cW = bp === 'laptop' ? 360 : 400;

  const conversation = (
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
      history={{
        open: historyOpen,
        onToggle: () => setHistoryOpen((was) => !was),
        onClose: () => setHistoryOpen(false),
        onViewAll: () => { setHistoryOpen(false); go('chat-history'); },
        onResume: (id) => { setHistoryOpen(false); void a.resumeConversation(id); },
        currentId: a.activeConversationId,
        accent: colour,
      }}
      talkingTo={a.talkingTo}
      onTalkTo={a.setTalkingTo}
      glow={glow}
      // Only one recognition session runs reliably at a time, so the voice
      // engine stands down when the composer's own mic starts — and the
      // composer's dictation stands down when the engine starts.
      onDictationStart={() => { if (a.engine.current) void a.toggleListening(); }}
      voiceEngineActive={a.listening}
      isSpeaking={() => a.engine.current?.state === 'speaking'}
    />
  );

  const popovers = (
    <>
      <BellPopover open={pop === 'bell'} onClose={() => setPop(null)} onNavigate={go} onUnread={a.setUnread} />
      <QuickSettings open={pop === 'gear'} onClose={() => setPop(null)} onNavigate={go} wakeStatus={wakeStatus} />
      <ShowOnHome open={pop === 'view'} onClose={() => setPop(null)} show={show}
                  onShow={(next) => void setPrefs({ homeShow: next })}
                  onRest={() => { setPop(null); presence.rest(); }} onPresenceOnly={presenceOnly} />
    </>
  );

  return (
    <main ref={rootRef} data-testid="home" className="relative h-screen overflow-hidden bg-surface">
      {/* The stage: the whole window, under everything. The core is drawn
          across it, centred on the window — the conversation floats over its
          right edge and reserves no column of it. */}
      <section data-testid="stage" className="absolute inset-0">
        <canvas ref={canvasRef} data-testid="orb-canvas" aria-hidden className="pointer-events-none absolute inset-0 h-full w-full" />
      </section>

      {!phone && (
        <>
          <HomeHeader
            state={state} label={label} visible={on(3)} menuOpen={menuOpen}
            viewOpen={pop === 'view'} viewActive={!show.chat || !show.sys || !show.jar}
            sharing={a.sharing} unread={a.unread} bellOpen={pop === 'bell'} gearOpen={pop === 'gear'}
            showDate={!tabletP}
            onMenu={onMenu}
            onView={() => setPop(pop === 'view' ? null : 'view')}
            onShare={() => void a.toggleSharing()}
            onBell={() => setPop(pop === 'bell' ? null : 'bell')}
            onGear={() => setPop(pop === 'gear' ? null : 'gear')}
            pillMeter={pillMeter}
          />

          {a.watching.length > 0 && <WatchingBar a={a} go={go} />}

          <div>
            <VoiceDock
              state={state} listening={a.listening} muted={a.muted} speaking={speaking}
              status={status} visible={on(2)} width={bp === 'desk' ? 560 : 420} bottom={tabletP ? 446 : 0}
              onMic={onMic} onInterrupt={a.interrupt} waveMeter={waveMeter}
            />
          </div>

          {tabletP ? (
            <HealthChips health={health} showSys={show.sys} showJar={show.jar} open={chips} onOpen={setChips}
                         className="absolute left-5 top-[72px] z-[6] w-[428px] max-w-[calc(100%-40px)]" />
          ) : (
            <HealthCards health={health} showSys={show.sys && on(4)} showJar={show.jar && on(5)} glow={glow} width={hW} />
          )}

          <BootLines beat={presence.beat} show={presence.presence === 'waking' || presence.greet} health={health} micNote={micNote} />

          <aside
            data-testid="conversation-rail"
            className="absolute z-[5] transition-[opacity,transform] duration-[600ms] ease-out"
            style={{
              right: 20,
              left: tabletP ? 20 : undefined,
              top: tabletP ? undefined : 72,
              bottom: 20,
              width: tabletP ? undefined : cW,
              height: tabletP ? 426 : undefined,
              maxHeight: tabletP ? undefined : 760,
              margin: tabletP ? undefined : 'auto 0',
              opacity: show.chat && on(6) ? 1 : 0,
              transform: show.chat && on(6) ? 'none' : 'translateX(28px)',
              pointerEvents: show.chat && on(6) ? 'auto' : 'none',
            }}
          >
            {conversation}
          </aside>
        </>
      )}

      {phone && (
        <PhoneHome a={a} state={state} label={label} status={status} speaking={speaking}
                   view={phoneView} setView={setPhoneView} health={health} show={show}
                   vitalsOpen={vitalsOpen} setVitalsOpen={setVitalsOpen} chips={chips} setChips={setChips}
                   onMenu={onMenu} onBell={() => setPop(pop === 'bell' ? null : 'bell')} onMic={onMic}
                   waveMeter={waveMeter} conversation={conversation} />
      )}

      {popovers}

      <RestOverlay look={prefs.restLook} resting={resting} health={health} unread={a.unread} onWake={presence.wake} />
    </main>
  );
}

/** What Jarvis is watching for, pinned under the header: a watch runs whether
 *  or not anyone is looking at the list of them. */
function WatchingBar({ a, go }: { a: Assistant; go: (id: string) => void }) {
  return (
    <div className="pointer-events-none absolute inset-x-0 top-[66px] z-[6] flex justify-center">
      <div data-testid="watching-bar"
           className="pointer-events-auto flex max-w-[60%] items-center gap-3 rounded-full border border-state-warn/30 bg-state-warn/10 px-3.5 py-1.5">
        <span className="truncate text-[12px] text-state-warn">
          Watching for {a.watching.map((monitor) => monitor.description).join(', ')}
        </span>
        {/* One watch gets a Stop; several get a way to see them, because a
            single Stop over a list of three would silently pick one. */}
        {a.watching.length === 1 ? (
          <button type="button" data-testid="watching-stop" onClick={() => void a.stopWatching(a.watching[0]!.id)}
                  className="shrink-0 text-[12px] text-ink-muted hover:text-ink">Stop</button>
        ) : (
          <button type="button" data-testid="watching-open" onClick={() => go('tasks')}
                  className="shrink-0 text-[12px] text-ink-muted hover:text-ink">See all {a.watching.length}</button>
        )}
      </div>
    </div>
  );
}

/** Home on a phone (design: phone frames): the core with its voice controls,
 *  health behind the vitals button, and the conversation full screen a tap away. */
function PhoneHome({ a, state, label, status, speaking, view, setView, health, show, vitalsOpen, setVitalsOpen,
  chips, setChips, onMenu, onBell, onMic, waveMeter, conversation }: {
  a: Assistant;
  state: ReturnType<typeof jarvisState>;
  label: string;
  status: string;
  speaking: boolean;
  view: 'core' | 'chat';
  setView: (v: 'core' | 'chat') => void;
  health: ReturnType<typeof useHealth>;
  show: { chat: boolean; sys: boolean; jar: boolean };
  vitalsOpen: boolean;
  setVitalsOpen: (open: boolean) => void;
  chips: 'sys' | 'jar' | null;
  setChips: (which: 'sys' | 'jar' | null) => void;
  onMenu: () => void;
  onBell: () => void;
  onMic: () => void;
  waveMeter: (c: HTMLCanvasElement | null) => void;
  conversation: React.ReactNode;
}) {
  const colour = STATE_COLOUR[state];
  const issues = health ? health.jarvis.issues.length : 0;
  return (
    <>
      <header className="absolute left-3 right-3 top-2.5 z-[7] flex h-12 items-center gap-1.5">
        <button type="button" aria-label="Menu" title="Menu" data-testid="menu" onClick={onMenu}
                className="inline-flex h-[42px] w-[42px] items-center justify-center rounded-[10px] border border-line/[0.22] bg-gradient-to-b from-surface-raised/[0.82] to-surface/[0.82] text-ink-soft">
          <svg viewBox="0 0 24 24" aria-hidden className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.75]" strokeLinecap="round"><path d="M4 7h16M4 12h16M4 17h16" /></svg>
        </button>
        {view === 'core' ? (
          <div className="absolute left-1/2 top-[5px] flex h-[38px] -translate-x-1/2 items-center gap-[9px] rounded-full border bg-[rgb(4_6_9/0.7)] px-4"
               style={{ borderColor: rgba(colour, 0.55), boxShadow: `0 0 20px ${rgba(colour, 0.18)}` }}>
            <span className="h-2 w-2 rounded-full" style={{ background: colour, boxShadow: `0 0 10px ${colour}` }} />
            <span className="font-mono text-[11px] font-medium tracking-[0.2em]" style={{ color: colour }}>{label}</span>
          </div>
        ) : (
          <button type="button" title="Back to Jarvis" aria-label="Back to Jarvis" onClick={() => setView('core')}
                  className="absolute left-1/2 top-0 -ml-[26px] h-[52px] w-[52px] rounded-full border bg-transparent"
                  style={{ borderColor: rgba(colour, 0.55), boxShadow: `0 0 18px ${rgba(colour, 0.18)}` }} />
        )}
        <div className="ml-auto flex items-center gap-0.5">
          <button type="button" title="System and Jarvis health" aria-label="System and Jarvis health"
                  onClick={() => setVitalsOpen(!vitalsOpen)}
                  className="relative inline-flex h-[42px] w-[42px] items-center justify-center rounded-[10px] border text-ink-soft"
                  style={{ borderColor: vitalsOpen ? 'rgb(var(--line) / 0.4)' : 'transparent',
                           background: vitalsOpen ? 'rgb(var(--line) / 0.14)' : 'transparent' }}>
            <svg viewBox="0 0 24 24" aria-hidden className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12h4l2.5-6 4 12 2.5-6H21" /></svg>
            {issues > 0 && <span className="absolute right-2 top-[9px] h-[7px] w-[7px] rounded-full bg-[#f5a524] shadow-[0_0_0_2px_#05070a]" />}
          </button>
          <div className="relative flex">
            <button type="button" title="Notifications" aria-label="Notifications" data-testid="bell" onClick={onBell}
                    className="inline-flex h-[42px] w-[42px] items-center justify-center rounded-[10px] text-ink-soft">
              <svg viewBox="0 0 24 24" aria-hidden className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round"><path d="M18 15.5V11a6 6 0 1 0-12 0v4.5L4.5 18h15z" /><path d="M10 21h4" /></svg>
            </button>
            {a.unread > 0 && (
              <span className="pointer-events-none absolute right-[3px] top-1 h-4 min-w-4 rounded-full bg-[#ff3b30] px-1 text-center text-[10.5px] font-semibold leading-4 text-white shadow-[0_0_0_2px_#05070a]">
                {a.unread > 99 ? '99+' : a.unread}
              </span>
            )}
          </div>
        </div>
      </header>

      {vitalsOpen && (
        <HealthChips health={health} showSys={show.sys} showJar={show.jar} open={chips} onOpen={setChips}
                     className="absolute left-3 right-3 top-[66px] z-[9]" />
      )}

      {view === 'core' ? (
        <div className="absolute inset-x-0 bottom-0 z-[4] flex flex-col items-center gap-3.5 px-3.5 pb-[18px]">
          <p data-testid="status" aria-live="polite" className="m-0 text-center text-[14px] text-ink-soft">{status}</p>
          {speaking && (
            <button type="button" data-testid="interrupt" onClick={a.interrupt}
                    className="flex h-11 items-center gap-2 rounded-full border-[1.5px] border-[rgb(255_59_48/0.65)] bg-[rgb(255_59_48/0.12)] px-[18px] text-[13.5px] text-[#ff8a82]">
              <svg viewBox="0 0 24 24" aria-hidden className="h-4 w-4 fill-none stroke-current stroke-[1.8]" strokeLinecap="round"><path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" /><path d="M3 21 21 3" /></svg>
              Stop
            </button>
          )}
          <div className="relative flex h-24 w-full max-w-[360px] items-center justify-between">
            <canvas ref={waveMeter} aria-hidden className="pointer-events-none absolute inset-0 h-full w-full" />
            <button type="button" title="Type instead" aria-label="Type instead" onClick={() => setView('chat')}
                    className="relative inline-flex h-[46px] w-[46px] items-center justify-center rounded-full border border-line/[0.22] bg-[rgb(7_10_15/0.85)] text-ink-soft">
              <svg viewBox="0 0 24 24" aria-hidden className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round"><rect x="2.5" y="6" width="19" height="12" rx="2.5" /><path d="M6.5 10h1M10.5 10h1M14.5 10h1M8 14h8" /></svg>
            </button>
            <button type="button" data-testid="mic" aria-pressed={a.listening} onClick={onMic}
                    title={!a.listening ? 'Start talking' : a.muted ? 'Unmute' : 'Mute'}
                    aria-label={!a.listening ? 'Start talking' : a.muted ? 'Unmute' : 'Mute'}
                    className="relative inline-flex h-[76px] w-[76px] items-center justify-center rounded-full border-2 text-white"
                    style={{ borderColor: colour, background: `radial-gradient(circle at 50% 50%, #0a0d12 55%, ${rgba(colour, 0.18)})`,
                             boxShadow: `0 0 0 6px ${rgba(colour, 0.07)}, 0 0 36px ${rgba(colour, 0.45)}` }}>
              <svg viewBox="0 0 24 24" aria-hidden className="h-[26px] w-[26px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round">
                <rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
                <path d="M4 4l16 16" style={{ opacity: a.muted ? 1 : 0 }} />
              </svg>
            </button>
            <button type="button" title="Mute" aria-label={a.muted ? 'Unmute the microphone' : 'Mute the microphone'}
                    data-testid="mute" disabled={!a.listening} onClick={a.toggleMute}
                    className="relative inline-flex h-[46px] w-[46px] items-center justify-center rounded-full border border-line/[0.22] bg-[rgb(7_10_15/0.85)] text-ink-soft disabled:opacity-40">
              <svg viewBox="0 0 24 24" aria-hidden className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round"><path d="M15 9.3V6a3 3 0 0 0-5.9-.7M9 9v2a3 3 0 0 0 5.1 2.1M19 11a7 7 0 0 1-1.1 3.8M5 11a7 7 0 0 0 11.4 5.4M12 18v3M4 4l16 16" /></svg>
            </button>
          </div>
          <button type="button" onClick={() => setView('chat')}
                  className="flex w-full max-w-[420px] items-center gap-3 rounded-2xl border border-line/[0.22] bg-gradient-to-b from-surface-raised/[0.88] to-surface/[0.88] px-3.5 py-3 text-left">
            <span aria-hidden className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[radial-gradient(circle_at_35%_30%,#ff6a5f,#b3140c)] text-[13px] font-semibold text-white">J</span>
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-[13.5px] text-ink-strong">
                {[health?.jarvis.jobsRunning ? `${health.jarvis.jobsRunning} running` : '', a.unread ? `${a.unread} unread` : '']
                  .filter(Boolean).join(' · ') || 'Conversation'}
              </span>
              <span className="text-[12px] text-ink-muted">Tap to open the chat</span>
            </span>
            <svg viewBox="0 0 24 24" aria-hidden className="h-4 w-4 fill-none stroke-ink-muted stroke-2" strokeLinecap="round"><path d="m9 6 6 6-6 6" /></svg>
          </button>
        </div>
      ) : (
        <aside data-testid="conversation-rail" className="absolute inset-x-2 bottom-2 top-[66px] z-[5]">
          {conversation}
        </aside>
      )}
    </>
  );
}
