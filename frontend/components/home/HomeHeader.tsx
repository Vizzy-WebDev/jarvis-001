'use client';

import { useEffect, useRef, useState } from 'react';

import { STATE_COLOUR, type JarvisState } from '@/lib/jarvis-state';

/**
 * Home's top bar (design: Home v6). The menu at the top left — the one anchor
 * that never moves — the JARVIS wordmark, the live state in a pill at the
 * centre with its own small level meter, and on the right: what Home shows,
 * screen sharing, notifications with the unread count, quick settings, and the
 * date and time.
 */
export function HomeHeader({
  state,
  label,
  visible,
  menuOpen,
  viewOpen,
  viewActive,
  sharing,
  unread,
  bellOpen,
  gearOpen,
  showDate,
  onMenu,
  onView,
  onShare,
  onBell,
  onGear,
  pillMeter,
}: {
  state: JarvisState;
  /** The caps label: STANDBY…, or RESTING / MUTED. */
  label: string;
  /** Faded out while waking up, before its beat in the activation sequence. */
  visible: boolean;
  menuOpen: boolean;
  viewOpen: boolean;
  /** Something is hidden from Home, so the button stays lit. */
  viewActive: boolean;
  sharing: boolean;
  unread: number;
  bellOpen: boolean;
  gearOpen: boolean;
  showDate: boolean;
  onMenu: () => void;
  onView: () => void;
  onShare: () => void;
  onBell: () => void;
  onGear: () => void;
  /** The pill's level meter — drawn by the core's own loop. */
  pillMeter: (canvas: HTMLCanvasElement | null) => void;
}) {
  const colour = STATE_COLOUR[state];
  const now = useNow();
  const plain = 'inline-flex h-10 w-10 items-center justify-center rounded-[10px] border transition-all duration-200';
  const idle = 'border-transparent bg-transparent text-ink-soft hover:bg-line/[0.08] hover:text-white';
  const lit = 'border-line/40 bg-line/[0.14] text-white';
  return (
    <header
      className="absolute left-5 right-5 top-4 z-[6] flex h-11 items-center gap-4 transition-[opacity,transform] duration-700 ease-out"
      style={{ opacity: visible ? 1 : 0, transform: visible ? 'none' : 'translateY(-12px)',
               pointerEvents: visible ? 'auto' : 'none' }}
    >
      <button
        type="button"
        aria-label="Menu"
        title="Menu"
        data-testid="menu"
        onClick={onMenu}
        className={[
          'inline-flex h-[42px] w-[42px] items-center justify-center rounded-[10px] border text-ink-soft transition-all duration-200',
          'hover:border-line/[0.45] hover:text-white',
          menuOpen ? 'border-line/50 bg-line/[0.16]' : 'border-line/[0.22] bg-gradient-to-b from-surface-raised/[0.82] to-surface/[0.82]',
        ].join(' ')}
      >
        <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.75]" strokeLinecap="round" aria-hidden>
          <path d="M4 7h16M4 12h16M4 17h16" />
        </svg>
      </button>
      <span className="select-none text-[22px] font-normal tracking-[0.34em] text-ink-strong">JARVIS</span>

      <div
        data-testid="state-pill"
        className="absolute left-1/2 top-0.5 flex h-10 -translate-x-1/2 items-center gap-3 rounded-full border bg-[rgb(4_6_9/0.7)] px-[18px] transition-[border-color,box-shadow] duration-[600ms]"
        style={{ borderColor: rgba(colour, 0.55), boxShadow: `0 0 24px ${rgba(colour, 0.18)}, inset 0 0 12px ${rgba(colour, 0.18)}` }}
      >
        <span className="h-[9px] w-[9px] rounded-full transition-colors duration-[600ms]"
              style={{ background: colour, boxShadow: `0 0 10px ${colour}` }} />
        <span data-testid="state-label" className="min-w-[96px] font-mono text-[12px] font-medium tracking-[0.22em] transition-colors duration-[600ms]"
              style={{ color: colour }}>
          {label}
        </span>
        <canvas ref={pillMeter} aria-hidden className="h-5 w-16" />
      </div>

      <div className="ml-auto flex items-center gap-1.5">
        <button type="button" title="Show or hide Home elements" aria-label="Show or hide Home elements"
                data-testid="home-view" onClick={onView}
                className={`${plain} ${viewOpen || viewActive ? lit : 'border-transparent text-ink-soft hover:text-white'}`}>
          <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <rect x="3" y="4" width="18" height="16" rx="2.5" /><path d="M15 4v16M3 10h5" />
          </svg>
        </button>
        <button type="button" title={sharing ? 'Stop sharing your screen' : 'Share your screen'}
                aria-label={sharing ? 'Stop sharing your screen' : 'Share your screen'} aria-pressed={sharing}
                data-testid="share" onClick={onShare}
                className={`${plain} ${idle}`}
                style={sharing ? { outline: '1px solid rgba(127,184,230,0.6)', outlineOffset: -1 } : undefined}>
          <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <rect x="3" y="4" width="18" height="13" rx="2" /><path d="M9 21h6M12 17v4" />
          </svg>
        </button>
        <div className="relative flex">
          <button type="button" title="Notifications" aria-label="Notifications" data-testid="bell" onClick={onBell}
                  className={`${plain} ${bellOpen ? lit : idle}`}>
            <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M18 15.5V11a6 6 0 1 0-12 0v4.5L4.5 18h15z" /><path d="M10 21h4" />
            </svg>
          </button>
          {unread > 0 && (
            <span data-testid="bell-count" aria-label={`${unread} unread`}
                  className="pointer-events-none absolute right-0.5 top-[3px] h-4 min-w-4 rounded-full bg-[#ff3b30] px-1 text-center text-[10.5px] font-semibold leading-4 text-white shadow-[0_0_0_2px_#05070a]">
              {unread > 99 ? '99+' : unread}
            </span>
          )}
        </div>
        <button type="button" title="Quick settings" aria-label="Quick settings" data-testid="settings" onClick={onGear}
                className={`${plain} ${gearOpen ? lit : idle}`}>
          <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <circle cx="12" cy="12" r="3" />
            <path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1" />
          </svg>
        </button>
        {showDate && now && (
          <div className="ml-1 flex h-10 items-center gap-2.5 rounded-[10px] border border-line/[0.22] bg-gradient-to-b from-surface-raised/[0.82] to-surface/[0.82] px-3.5 text-[13px] text-ink-soft">
            <svg viewBox="0 0 24 24" className="h-[15px] w-[15px] fill-none stroke-current stroke-[1.7]" strokeLinecap="round" aria-hidden>
              <rect x="3" y="5" width="18" height="16" rx="2" /><path d="M3 10h18M8 3v4M16 3v4" />
            </svg>
            <span>{now.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}</span>
            <span className="text-ink-strong">{now.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}</span>
          </div>
        )}
      </div>
    </header>
  );
}

/** The time, re-read on the minute rather than every second. Null until the
 *  page has mounted: the build pre-renders this page, and a time baked in at
 *  build time would not match the one the browser then shows. */
export function useNow(): Date | null {
  const [now, setNow] = useState<Date | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    setNow(new Date());
    const schedule = () => {
      const d = new Date();
      timer.current = setTimeout(() => { setNow(new Date()); schedule(); }, (60 - d.getSeconds()) * 1000 + 50);
    };
    schedule();
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, []);
  return now;
}

/** A state colour (hex or `rgb(var(--x))`) at an opacity. */
export function rgba(colour: string, alpha: number): string {
  if (colour.startsWith('#')) {
    const n = parseInt(colour.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  }
  return colour.replace(/^rgb\((.*)\)$/, `rgb($1 / ${alpha})`);
}
