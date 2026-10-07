'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import type { Health } from '@/lib/api-types';
import type { Presence } from '@/lib/core';

import { summarise } from './health';
import { useNow } from './HomeHeader';

/**
 * Engaged ⇄ Resting (design: Presence, Stage 2).
 *
 * Home rests after the chosen minutes with no key, pointer or touch — never
 * while a reply is running or a voice session is open — and wakes on a click
 * anywhere, a key, or the wake word. Waking plays the designed sequence: the
 * header, the mic, each health card and the conversation come back on their
 * own beat (~2.8s; "quick" is a third of that, "instant" skips it).
 *
 * Quiet Hours is a different thing (a schedule, kept in Settings) and does not
 * rest Home.
 */
export function usePresence({ restAfterMin, activation, busy }: {
  restAfterMin: number;
  activation: 'full' | 'quick' | 'instant';
  /** Something is happening that resting must not hide. */
  busy: boolean;
}) {
  const [presence, setPresence] = useState<Presence>('engaged');
  /** How far the wake sequence has got, 0..7 (7 = everything is back). */
  const [beat, setBeat] = useState(7);
  /** The short greeting after waking. */
  const [greet, setGreet] = useState(false);
  const lastAct = useRef(Date.now());
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const presenceRef = useRef(presence);
  presenceRef.current = presence;

  const clear = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  const rest = useCallback(() => {
    if (presenceRef.current === 'resting') return;
    clear();
    setGreet(false);
    setBeat(0);
    setPresence('resting');
  }, []);

  const wake = useCallback(() => {
    lastAct.current = Date.now();
    if (presenceRef.current !== 'resting') return;
    clear();
    const k = activation === 'quick' ? 0.3 : activation === 'instant' ? 0 : 1;
    setPresence('waking');
    setGreet(false);
    setBeat(1);
    const steps: [number, number][] = [[2, 700], [3, 1100], [4, 1450], [5, 1700], [6, 2000], [7, 2800]];
    timers.current = steps.map(([b, t]) => setTimeout(() => {
      setBeat(b);
      if (b === 7) {
        setPresence('engaged');
        setGreet(true);
      }
    }, t * k));
    timers.current.push(setTimeout(() => setGreet(false), 2800 * k + 5200));
  }, [activation]);

  useEffect(() => {
    const onAct = () => { lastAct.current = Date.now(); };
    window.addEventListener('pointermove', onAct, { passive: true });
    window.addEventListener('pointerdown', onAct, { passive: true });
    window.addEventListener('keydown', onAct);
    window.addEventListener('touchstart', onAct, { passive: true });
    const idle = setInterval(() => {
      if (restAfterMin <= 0 || presenceRef.current !== 'engaged') return;
      if (busyRef.current) { lastAct.current = Date.now(); return; }
      if (Date.now() - lastAct.current > restAfterMin * 60_000) rest();
    }, 1000);
    return () => {
      window.removeEventListener('pointermove', onAct);
      window.removeEventListener('pointerdown', onAct);
      window.removeEventListener('keydown', onAct);
      window.removeEventListener('touchstart', onAct);
      clearInterval(idle);
    };
  }, [restAfterMin, rest]);

  useEffect(() => () => clear(), []);

  return { presence, beat, greet, rest, wake, dismissGreet: () => setGreet(false) };
}

/** The lines that come up while Jarvis wakes, from real readings. */
export function BootLines({ beat, show, health, micNote }: {
  beat: number;
  show: boolean;
  health: Health | null;
  micNote: string;
}) {
  const sum = summarise(health);
  const lines: [number, string, string][] = [
    [1, 'Core ignition', '#9fb2c6'],
    [2, `Voice link · ${micNote}`, '#9fb2c6'],
    [3, 'Interface online', '#9fb2c6'],
    [4, `System health · ${sum.sysOk ? 'all nominal' : sum.sysLabel.toLowerCase()}`, sum.sysOk ? '#9fb2c6' : '#f5a524'],
    [5, `Jarvis health · ${health?.jarvis.issues[0]?.toLowerCase() ?? 'all good'}`, sum.jarOk ? '#9fb2c6' : '#f5a524'],
    [7, 'Ready', '#6be3a3'],
  ];
  return (
    <div aria-hidden className="pointer-events-none absolute bottom-7 left-7 z-[4] flex flex-col gap-1.5 font-mono text-[11.5px] tracking-[0.06em] transition-opacity duration-[900ms]"
         style={{ opacity: show ? 1 : 0 }}>
      {lines.map(([b, text, colour]) => (
        <span key={text} className="flex items-center gap-2 transition-[opacity,transform] duration-300"
              style={{ color: colour, opacity: beat >= b ? 1 : 0, transform: beat >= b ? 'none' : 'translateX(-8px)' }}>
          <span className="h-[5px] w-[5px] rounded-full" style={{ background: colour }} />{text}
        </span>
      ))}
    </div>
  );
}

/**
 * What a resting Home shows (design 1b "Night stand"): a quiet clock, the date
 * and one health line — or only an ember, or the whole Home dimmed. Clicking
 * anywhere wakes it.
 */
export function RestOverlay({ look, resting, health, unread, onWake }: {
  look: 'clock' | 'ember' | 'dim';
  resting: boolean;
  health: Health | null;
  unread: number;
  onWake: () => void;
}) {
  const now = useNow();
  const sum = summarise(health);
  const jobs = health?.jarvis.jobsRunning ?? 0;
  const second = [jobs ? `${jobs} running` : '', unread ? `${unread} unread` : ''].filter(Boolean).join(' · ');
  const time = now ? now.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '';
  const [clock, ampm] = time.match(/^(.*?)\s?([AaPp]\.?[Mm]\.?)?$/)?.slice(1) ?? [time, ''];
  return (
    <>
      <div aria-hidden className="pointer-events-none absolute inset-0 z-10 bg-[rgb(2_3_5/0.62)] transition-opacity duration-[1200ms]"
           style={{ opacity: resting && look === 'dim' ? 1 : 0 }} />
      {resting && look === 'clock' && now && (
        <div data-testid="rest-clock" className="pointer-events-none absolute inset-x-0 top-[57%] z-[11] flex flex-col items-center gap-2.5">
          <div className="flex items-baseline gap-2">
            <span className="text-[88px] font-light leading-none tracking-[-0.03em] text-ink-soft">{clock}</span>
            {ampm && <span className="text-[20px] text-[#6a7280]">{ampm}</span>}
          </div>
          <span className="text-[17px] text-ink-muted">
            {now.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
          </span>
          <div className="mt-2 flex items-center gap-[18px] text-[13.5px] text-ink-muted">
            <span className="flex items-center gap-[7px]">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: sum.sysOk && sum.jarOk ? '#6be3a3' : '#f5a524' }} />
              {sum.sysOk && sum.jarOk ? 'All systems healthy' : (health?.jarvis.issues[0] ?? sum.sysLabel)}
            </span>
            {second && (
              <span className="flex items-center gap-[7px]">
                <span className="h-1.5 w-1.5 rounded-full bg-[#f5a524]" />{second}
              </span>
            )}
          </div>
        </div>
      )}
      {resting && look === 'ember' && (
        <div className="pointer-events-none absolute inset-x-0 bottom-9 z-[11] flex justify-center">
          <span className="text-[13px] tracking-[0.04em] text-[#6a7280]">Say “Hey Jarvis”, or click anywhere</span>
        </div>
      )}
      {resting && (
        <button type="button" data-testid="wake" title="Wake Jarvis" aria-label="Wake Jarvis" onClick={onWake}
                className="absolute inset-0 z-30 cursor-pointer border-0 bg-transparent" />
      )}
    </>
  );
}
