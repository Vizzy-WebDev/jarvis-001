'use client';

import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import type { Health } from '@/lib/api-types';

/**
 * Home's two health cards — the computer, and Jarvis itself — read from
 * `/api/health` every few seconds while Home is on screen and the tab is
 * visible, and not at all otherwise.
 *
 * Every figure is a real reading. What the server cannot know arrives as null
 * and is shown as "—", never as a zero that reads as idle, instant or free.
 */
export function useHealth(active: boolean): Health | null {
  const [health, setHealth] = useState<Health | null>(null);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const next = await api.health();
          if (!cancelled) setHealth(next);
        } catch {
          /* the last reading stays; a failed poll is not news */
        }
      }
      if (!cancelled) timer = setTimeout(tick, 3000);
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [active]);
  return health;
}

export interface HealthSummary {
  sysOk: boolean;
  sysLabel: string;
  jarOk: boolean;
  jarLabel: string;
  /** Share of Jarvis's own checks that pass, 0..100. */
  jarScore: number;
}

export function summarise(health: Health | null): HealthSummary {
  if (!health) {
    return { sysOk: true, sysLabel: 'Checking…', jarOk: true, jarLabel: 'Checking…', jarScore: 0 };
  }
  const { system: s, jarvis: j } = health;
  const strained = [
    (s.cpuPct ?? 0) >= 90 && 'CPU busy',
    (s.memPct ?? 0) >= 90 && 'Memory low',
    (s.diskPct ?? 0) >= 95 && 'Disk nearly full',
  ].filter(Boolean) as string[];
  const checks = [j.modelReady, ...Array.from({ length: j.connectors.enabled },
    (_, i) => i < j.connectors.working)];
  const passing = checks.filter(Boolean).length;
  return {
    sysOk: strained.length === 0,
    sysLabel: strained[0] ?? 'Healthy',
    jarOk: j.issues.length === 0,
    jarLabel: j.issues.length === 0 ? 'Online' : `${j.issues.length} ${j.issues.length === 1 ? 'issue' : 'issues'}`,
    jarScore: checks.length ? Math.round((passing / checks.length) * 100) : 100,
  };
}

export function bytesRate(bps: number | null): string {
  if (bps == null) return '—';
  if (bps < 1024) return `${Math.round(bps)} B/s`;
  if (bps < 1024 * 1024) return `${Math.round(bps / 1024)} KB/s`;
  return `${(bps / 1024 / 1024).toFixed(1)} MB/s`;
}

/** Network throughput as a ring: there is no "100%" of a network, so the fill
 *  is on a log scale up to 10 MB/s and the label says the real rate. */
export function netFill(bps: number | null): number {
  if (!bps) return 0;
  return Math.max(0, Math.min(100, (Math.log10(bps + 1) / Math.log10(10 * 1024 * 1024)) * 100));
}

export function uptime(sec: number): string {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

const pct = (v: number | null) => (v == null ? '—' : `${v}%`);
const OK = '#6be3a3';
const WARN = '#f5a524';

function Dot({ colour }: { colour: string }) {
  return <span className="h-[7px] w-[7px] rounded-full" style={{ background: colour, boxShadow: `0 0 8px ${colour}` }} />;
}

function Ring({ value, label, text }: { value: number; label: string; text: string }) {
  const dash = `${((value / 100) * 132).toFixed(1)} 132`;
  return (
    <div className="flex flex-col items-center gap-[5px]">
      <div className="relative h-[50px] w-[50px]">
        <svg viewBox="0 0 50 50" className="h-[50px] w-[50px] -rotate-90" aria-hidden>
          <circle cx="25" cy="25" r="21" fill="none" stroke="rgb(var(--line) / 0.14)" strokeWidth="3" />
          <circle cx="25" cy="25" r="21" fill="none" stroke="#7fb8e6" strokeWidth="3" strokeLinecap="round"
                  strokeDasharray={dash}
                  style={{ transition: 'stroke-dasharray 800ms', filter: 'drop-shadow(0 0 3px rgba(127,184,230,0.6))' }} />
        </svg>
        <span className="absolute inset-0 flex items-center justify-center text-[12px] text-ink-strong">{text}</span>
      </div>
      <span className="font-mono text-[10px] tracking-[0.08em] text-ink-muted">{label}</span>
    </div>
  );
}

const CARD = 'absolute left-5 z-[4] flex flex-col gap-3 rounded-[14px] border border-line/[0.22] px-4 py-3.5 '
  + 'bg-gradient-to-b from-surface-raised/[0.82] to-surface/[0.82] backdrop-blur-[10px] '
  + 'transition-[opacity,transform,top] duration-[600ms] ease-out';

function Highlight() {
  return <span aria-hidden className="pointer-events-none absolute -top-px left-[22px] right-[22px] z-[2] h-px bg-[linear-gradient(90deg,transparent,rgba(170,210,240,0.55),transparent)]" />;
}

/** The two cards at the left of Home (laptop and wider). */
export function HealthCards({ health, showSys, showJar, glow, width }: {
  health: Health | null;
  showSys: boolean;
  showJar: boolean;
  /** The state colour's glow, so the cards breathe with the core. */
  glow: string;
  width: number;
}) {
  const sum = summarise(health);
  const s = health?.system, j = health?.jarvis;
  const shadow = `inset 0 1px 0 rgba(255,255,255,0.04), 0 20px 50px -20px rgba(0,0,0,0.8), 0 0 36px -14px ${glow}`;
  const fade = (on: boolean) => ({
    opacity: on ? 1 : 0,
    transform: on ? 'none' : 'translateX(-28px)',
    pointerEvents: on ? 'auto' as const : 'none' as const,
  });
  return (
    <>
      <aside data-testid="health-system" aria-label="System health" className={CARD}
             style={{ top: 72, width, height: 140, boxShadow: shadow, ...fade(showSys) }}>
        <Highlight />
        <div className="flex items-center gap-2.5">
          <svg viewBox="0 0 24 24" className="h-4 w-4 fill-none stroke-[#9fb2c6] stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M3 12h4l2.5-6 4 12 2.5-6H21" />
          </svg>
          <span className="text-[14px] font-medium text-ink-strong">System Health</span>
          <span className="ml-auto flex items-center gap-1.5 text-[12px]" style={{ color: sum.sysOk ? OK : WARN }}>
            <Dot colour={sum.sysOk ? OK : WARN} />{sum.sysLabel}
          </span>
        </div>
        <div className="grid grid-cols-4 gap-1">
          <Ring value={s?.cpuPct ?? 0} label="CPU" text={pct(s?.cpuPct ?? null)} />
          <Ring value={s?.memPct ?? 0} label="RAM" text={pct(s?.memPct ?? null)} />
          <Ring value={s?.diskPct ?? 0} label="DISK" text={pct(s?.diskPct ?? null)} />
          <Ring value={netFill(s?.netBytesPerSec ?? null)} label="NETWORK"
                text={bytesRate(s?.netBytesPerSec ?? null).replace(' ', '')} />
        </div>
      </aside>

      <aside data-testid="health-jarvis" aria-label="Jarvis health" className={CARD}
             style={{ top: showSys ? 222 : 72, width, paddingBottom: 16, boxShadow: shadow, ...fade(showJar) }}>
        <Highlight />
        <div className="flex items-center gap-2.5">
          <svg viewBox="0 0 24 24" className="h-4 w-4 fill-none stroke-[#9fb2c6] stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M12 21s-7.5-4.6-9.5-9.3C1.2 8.4 3.3 5 6.8 5c2 0 3.4 1.1 4.2 2.4h2C13.8 6.1 15.2 5 17.2 5c3.5 0 5.6 3.4 4.3 6.7C19.5 16.4 12 21 12 21z" />
          </svg>
          <span className="text-[14px] font-medium text-ink-strong">Jarvis Health</span>
          <span className="ml-auto flex items-center gap-1.5 text-[12px]" style={{ color: sum.jarOk ? OK : WARN }}>
            <Dot colour={sum.jarOk ? OK : WARN} />{sum.jarLabel}
          </span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-md bg-line/[0.12]" title={`${sum.jarScore}% of checks passing`}>
          <div className="h-full rounded-md bg-[linear-gradient(90deg,#3f7fae,#8fd0f5)] shadow-[0_0_10px_rgba(143,208,245,0.5)] transition-[width] duration-700"
               style={{ width: `${sum.jarScore}%` }} />
        </div>
        <div className="flex gap-[18px] text-[12.5px]">
          <span className="text-ink-muted">Response <span className="text-ink-strong">
            {j?.responseSec == null ? '—' : `${j.responseSec}s`}</span></span>
          <span className="text-ink-muted">Uptime <span className="text-ink-strong">{j ? uptime(j.uptimeSec) : '—'}</span></span>
        </div>
        <div className="h-px bg-line/[0.12]" />
        <JarvisRows health={health} />
      </aside>
    </>
  );
}

export function JarvisRows({ health, grid }: { health: Health | null; grid?: boolean }) {
  const j = health?.jarvis;
  const c = j?.connectors;
  const rows: [string, string, boolean][] = [
    ['Voice in / out', j ? (j.voice.wakeWord ? 'Ready' : 'Ready · no wake word') : '—', false],
    ['Connectors', c ? (c.enabled ? `${c.working} of ${c.enabled}${c.problems[0] ? ` · ${c.problems[0]}` : ''}` : 'None') : '—',
     Boolean(c?.problems.length)],
    ['Background jobs', j ? (j.jobsRunning ? `${j.jobsRunning} running` : 'None running') : '—', false],
    ['Spend today', j?.spendToday == null ? '—' : `$${j.spendToday.toFixed(2)}`, false],
  ];
  if (j && !j.modelReady) rows.unshift(['Model', 'Not ready', true]);
  return (
    <div className={grid ? 'grid grid-cols-2 gap-x-5 gap-y-2.5 text-[13px]' : 'flex flex-col gap-2 text-[12.5px]'}>
      {rows.map(([label, value, warn]) => (
        <div key={label} className="flex justify-between gap-3">
          <span className="text-ink-muted">{label}</span>
          <span className="truncate" style={{ color: warn ? WARN : 'rgb(var(--ink-strong))' }}>{value}</span>
        </div>
      ))}
    </div>
  );
}

/** The two chips that replace the cards on a tablet held upright and on a
 *  phone; tapping one opens its detail below. */
export function HealthChips({ health, showSys, showJar, open, onOpen, className = '' }: {
  health: Health | null;
  showSys: boolean;
  showJar: boolean;
  open: 'sys' | 'jar' | null;
  onOpen: (which: 'sys' | 'jar' | null) => void;
  className?: string;
}) {
  const sum = summarise(health);
  const s = health?.system;
  const chip = (which: 'sys' | 'jar', label: string, ok: boolean, state: string) => (
    <button type="button" data-testid={`health-chip-${which}`} onClick={() => onOpen(open === which ? null : which)}
            className="flex h-11 flex-1 items-center gap-2 rounded-xl border bg-gradient-to-b from-surface-raised/90 to-surface/90 px-3 text-[13px] text-ink-strong"
            style={{ borderColor: open === which ? 'rgb(var(--line) / 0.5)' : 'rgb(var(--line) / 0.22)' }}>
      <Dot colour={ok ? OK : WARN} />{label}
      <span className="ml-auto text-[12px]" style={{ color: ok ? OK : WARN }}>{state}</span>
    </button>
  );
  const bar = (label: string, value: number | null, fill: number, text?: string) => (
    <div className="flex flex-col gap-1.5">
      <div className="flex justify-between text-[12px]">
        <span className="font-mono text-ink-muted">{label}</span><span>{text ?? pct(value)}</span>
      </div>
      <div className="h-[3px] rounded-[3px] bg-line/[0.14]">
        <div className="h-full rounded-[3px] bg-[#7fb8e6] transition-[width] duration-700" style={{ width: `${fill}%` }} />
      </div>
    </div>
  );
  const panel = 'rounded-[14px] border border-line/[0.22] bg-gradient-to-b from-surface-raised/[0.96] to-surface/[0.96] px-4 py-3.5 shadow-[0_20px_50px_-16px_rgba(0,0,0,0.9)]';
  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      <div className="flex gap-2">
        {showSys && chip('sys', 'System Health', sum.sysOk, sum.sysLabel)}
        {showJar && chip('jar', 'Jarvis Health', sum.jarOk, sum.jarLabel)}
      </div>
      {open === 'sys' && (
        <div className={`${panel} grid grid-cols-4 gap-3.5`}>
          {bar('CPU', s?.cpuPct ?? null, s?.cpuPct ?? 0)}
          {bar('RAM', s?.memPct ?? null, s?.memPct ?? 0)}
          {bar('DISK', s?.diskPct ?? null, s?.diskPct ?? 0)}
          {bar('NET', null, netFill(s?.netBytesPerSec ?? null), bytesRate(s?.netBytesPerSec ?? null))}
        </div>
      )}
      {open === 'jar' && <div className={panel}><JarvisRows health={health} grid /></div>}
    </div>
  );
}
