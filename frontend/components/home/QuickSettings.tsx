'use client';

import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import type { VoiceOptions } from '@/lib/api-types';
import { DEFAULT_WAKE_PHRASES, setPrefs, usePrefs } from '@/lib/usePrefs';
import type { WakeStatus } from '@/lib/voice/wake-listener';

import { HomePopover } from './popovers';

/** The one phrase the on-device wake model can actually hear. */
export const HEARD_PHRASE = 'Hey Jarvis';

/**
 * Quick settings (design 1f), from the gear on Home: how Jarvis listens and
 * speaks right now, with "All settings →" for the rest.
 *
 * Every voice offered here comes from `/api/voice/options`, which decides what
 * is available from real state (a connected model, a configured key) — never
 * from a provider name this code knows. An unavailable one says why.
 *
 * Wake phrases (1d): the list is the person's to edit. The on-device model hears
 * "Hey Jarvis" only, so any other phrase is kept but marked "not heard yet" —
 * the editor never claims a phrase works when nothing is listening for it.
 */
export function QuickSettings({ open, onClose, onNavigate, wakeStatus }: {
  open: boolean;
  onClose: () => void;
  onNavigate: (section: string) => void;
  wakeStatus: WakeStatus;
}) {
  const prefs = usePrefs();
  const [options, setOptions] = useState<VoiceOptions | null>(null);
  const [view, setView] = useState<'main' | 'phrases'>('main');
  const [select, setSelect] = useState<'stt' | 'tts' | null>(null);
  const [draft, setDraft] = useState('');

  useEffect(() => {
    if (!open) {
      setView('main');
      setSelect(null);
      return;
    }
    api.voice.options().then(setOptions).catch(() => setOptions(null));
  }, [open]);

  const phrases = prefs.wakePhrases;
  const engines = options?.engines ?? [];
  const voices = options?.voices ?? [];
  const engine = engines.find((e) => e.id === prefs.voiceEngine);
  const voice = voices.find((v) => v.id === prefs.voiceOutput);
  const quiet = prefs.quietHours ?? { enabled: false, start: '23:00', end: '08:00' };

  const wakeHint = !prefs.wakeWord ? `${HEARD_PHRASE} · off`
    : wakeStatus.kind === 'listening' ? `${HEARD_PHRASE} · listening`
      : wakeStatus.kind === 'preparing' ? 'Getting ready…'
        : wakeStatus.kind === 'blocked' ? 'Microphone blocked'
          : wakeStatus.kind === 'unavailable' ? 'Not available'
          : HEARD_PHRASE;

  const addPhrase = () => {
    const v = draft.trim();
    setDraft('');
    if (!v || phrases.some((p) => p.toLowerCase() === v.toLowerCase())) return;
    void setPrefs({ wakePhrases: [...phrases, v] });
  };
  const same = phrases.length === DEFAULT_WAKE_PHRASES.length && DEFAULT_WAKE_PHRASES.every((p) => phrases.includes(p));

  const toggleRow = (key: 'wakeWord' | 'pushToTalk' | 'speakReplies', label: string, hint: React.ReactNode, icon: string) => (
    <div role="switch" aria-checked={prefs[key]} tabIndex={0} data-testid={`quick-${key}`}
         onClick={() => void setPrefs({ [key]: !prefs[key] })}
         onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void setPrefs({ [key]: !prefs[key] }); } }}
         className="flex min-h-11 cursor-pointer items-center gap-[11px] outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
      <svg viewBox="0 0 24 24" aria-hidden className="h-[18px] w-[18px] shrink-0 fill-none stroke-[1.7]" strokeLinecap="round" strokeLinejoin="round"
           style={{ stroke: prefs[key] ? '#9fd0f2' : '#7d8a9a' }}>
        <path d={icon} />
      </svg>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-[13.5px] text-ink-strong">{label}</span>
        <span className="flex items-center gap-1.5 text-[11.5px] text-ink-muted">{hint}</span>
      </span>
      <Switch on={prefs[key]} />
    </div>
  );

  const dropdown = (key: 'stt' | 'tts', label: string, value: string, items: { id: string; label: string; ok: boolean; why?: string | null }[],
                    pick: (id: string) => void, testId: string) => (
    <div className="relative flex min-h-[46px] items-center gap-2.5">
      <span className="flex-1 text-[13.5px] text-ink-soft">{label}</span>
      <button type="button" data-testid={`${testId}-select`} onClick={() => setSelect(select === key ? null : key)}
              className="flex h-[34px] min-w-[170px] max-w-[190px] items-center gap-2 rounded-[9px] border bg-[rgb(8_12_18/0.85)] pl-3 pr-2.5 text-left text-[13px] text-ink-strong hover:border-line/[0.45]"
              style={{ borderColor: select === key ? 'rgb(var(--line) / 0.45)' : 'rgb(var(--line) / 0.22)' }}>
        <span className="flex-1 truncate">{value}</span>
        <svg viewBox="0 0 24 24" aria-hidden className={`h-3.5 w-3.5 fill-none stroke-current stroke-2 text-ink-muted transition-transform ${select === key ? 'rotate-180' : ''}`} strokeLinecap="round">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {select === key && (
        <div data-testid={`${testId}-options`}
             className="absolute right-0 top-[42px] z-10 flex w-[230px] flex-col gap-px rounded-[11px] border border-line/[0.28] bg-gradient-to-b from-[rgb(15_22_31/0.99)] to-[rgb(8_11_16/0.99)] p-[5px] shadow-[0_20px_50px_-16px_rgba(0,0,0,0.9)]">
          {items.map((item) => (
            <button key={item.id} type="button" disabled={!item.ok} data-testid={`${testId === 'listening' ? 'engine' : 'voice'}-${item.id}`}
                    onClick={() => { pick(item.id); setSelect(null); }}
                    className="flex flex-col gap-0.5 rounded-lg px-2.5 py-2 text-left text-[13px] text-ink-strong hover:bg-line/[0.12] disabled:cursor-not-allowed disabled:opacity-55 disabled:hover:bg-transparent"
                    style={{ background: item.label === value ? 'rgb(var(--line) / 0.1)' : undefined }}>
              <span className="flex items-center gap-2">
                <span className="flex-1">{item.label}</span>
                {item.label === value && (
                  <svg viewBox="0 0 24 24" aria-hidden className="h-3.5 w-3.5 fill-none stroke-[#9fd0f2] stroke-2" strokeLinecap="round"><path d="m5 12 5 5 9-10" /></svg>
                )}
              </span>
              {!item.ok && item.why && <span className="text-[11px] leading-snug text-ink-muted">{item.why}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <HomePopover open={open} onClose={onClose} width={380} testId="settings-panel" className="max-h-[calc(100%-86px)] overflow-y-auto px-3.5 pt-1.5">
      {view === 'main' ? (
        <div className="flex flex-col">
          <span className="pb-1 pt-2.5 font-mono text-[10.5px] tracking-[0.18em] text-ink-muted">LISTENING</span>
          {toggleRow('wakeWord', 'Wake word', <>
            {wakeHint} ·
            <button type="button" data-testid="edit-phrases"
                    onClick={(e) => { e.stopPropagation(); setView('phrases'); }}
                    className="text-[11.5px] text-[#9fd0f2] hover:text-white">Edit</button>
          </>, 'M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4')}
          {toggleRow('pushToTalk', 'Push-to-talk', prefs.pushToTalk ? 'Hold Space to talk · on' : 'Hold Space to talk',
            'M8 9h8a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1zM10 9V4M10 14h4')}
          {toggleRow('speakReplies', 'Speak replies out loud',
            voice ? `Uses ${voice.label}` : 'Uses the browser’s own voice',
            'M4 10v4h3l5 4V6L7 10zM16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11')}
          <div className="my-1.5 h-px bg-line/[0.14]" />
          <span className="pb-1 pt-2.5 font-mono text-[10.5px] tracking-[0.18em] text-ink-muted">VOICE PROVIDERS</span>
          {dropdown('stt', 'Listening (speech to text)', engine?.label ?? 'Default · browser',
            engines.map((e) => ({ id: e.id, label: e.label, ok: e.available, why: e.reason })),
            (id) => void setPrefs({ voiceEngine: id }), 'listening')}
          {dropdown('tts', 'Speaking provider', voice?.label ?? 'Default · browser',
            voices.map((v) => ({ id: v.id, label: v.label, ok: true })),
            (id) => void setPrefs({ voiceOutput: id }), 'speaking')}
          <span className="pb-2 text-[11.5px] leading-normal text-ink-muted">
            {voice && voice.needsKey
              ? `${voice.label}’s voice and model are set in Settings, so there is nothing more to pick here.`
              : 'More voices appear here once their key is added in Settings → Voice.'}
          </span>
          <div className="mb-1.5 mt-0.5 h-px bg-line/[0.14]" />
          <div role="switch" aria-checked={quiet.enabled} tabIndex={0} data-testid="quick-quiet"
               onClick={() => void setPrefs({ quietHours: { ...quiet, enabled: !quiet.enabled } })}
               onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void setPrefs({ quietHours: { ...quiet, enabled: !quiet.enabled } }); } }}
               className="flex min-h-[46px] cursor-pointer items-center gap-[11px] outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
            <span className="flex flex-1 flex-col">
              <span className="text-[13.5px] text-ink-strong">Quiet Hours</span>
              <span className="text-[11.5px] text-ink-muted">{quiet.start} to {quiet.end} · {quiet.enabled ? 'on' : 'off'}</span>
            </span>
            <Switch on={quiet.enabled} />
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3 pb-3 pt-2" data-testid="wake-phrases">
          <div className="flex items-center gap-2">
            <button type="button" title="Back" aria-label="Back" onClick={() => setView('main')}
                    className="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-ink-soft hover:bg-line/[0.12]">
              <svg viewBox="0 0 24 24" aria-hidden className="h-4 w-4 fill-none stroke-current stroke-2" strokeLinecap="round"><path d="m15 6-6 6 6 6" /></svg>
            </button>
            <span className="text-[14px] font-medium text-ink-strong">Wake phrases</span>
          </div>
          <span className="text-[12.5px] leading-normal text-ink-muted">
            Jarvis listens for “{HEARD_PHRASE}” on this computer — the audio never leaves it.
            Other phrases are kept here, and marked until Jarvis can hear them.
          </span>
          <div className="flex flex-wrap gap-1.5">
            {phrases.map((p) => {
              const heard = p.toLowerCase() === HEARD_PHRASE.toLowerCase();
              return (
                <span key={p} data-testid="wake-phrase"
                      className="flex items-center gap-1.5 rounded-full border py-1.5 pl-[11px] pr-1.5 text-[13px]"
                      style={heard
                        ? { borderColor: 'rgba(127,184,230,0.35)', background: 'rgba(127,184,230,0.1)', color: '#cfe6f7' }
                        : { borderColor: 'rgb(var(--line) / 0.22)', color: 'rgb(var(--ink-muted))' }}
                      title={heard ? 'Heard on this computer' : 'Not heard yet'}>
                  {heard && (
                    <svg viewBox="0 0 24 24" aria-hidden className="h-3 w-3 fill-none stroke-current stroke-2" strokeLinecap="round"><path d="m5 12 5 5 9-10" /></svg>
                  )}
                  {p}
                  {!heard && <span className="text-[10.5px] text-ink-faint">not heard yet</span>}
                  <button type="button" title="Remove" aria-label={`Remove ${p}`}
                          onClick={() => void setPrefs({ wakePhrases: phrases.filter((x) => x !== p) })}
                          className="flex h-5 w-5 items-center justify-center rounded-full text-ink-muted hover:bg-line/20 hover:text-white">
                    <svg viewBox="0 0 24 24" aria-hidden className="h-3 w-3 fill-none stroke-current stroke-2" strokeLinecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg>
                  </button>
                </span>
              );
            })}
          </div>
          {!phrases.some((p) => p.toLowerCase() === HEARD_PHRASE.toLowerCase()) && (
            <span className="text-[12px] leading-normal text-state-warn">
              Without “{HEARD_PHRASE}” in the list, the wake word can’t hear you.
            </span>
          )}
          <div className="flex gap-2">
            <input value={draft} onChange={(e) => setDraft(e.target.value)} data-testid="wake-phrase-input"
                   onKeyDown={(e) => { if (e.key === 'Enter') addPhrase(); }}
                   placeholder="Add a phrase, e.g. Computer"
                   className="h-[38px] min-w-0 flex-1 rounded-[10px] border border-line/[0.22] bg-[rgb(8_12_18/0.85)] px-3 text-[13.5px] text-ink-strong outline-none focus:border-line/[0.55]" />
            <button type="button" onClick={addPhrase}
                    className="h-[38px] rounded-[10px] border border-line/[0.35] bg-[rgb(63_127_174/0.35)] px-4 text-[13.5px] text-ink-strong">
              Add
            </button>
          </div>
          {!same && (
            <button type="button" onClick={() => void setPrefs({ wakePhrases: DEFAULT_WAKE_PHRASES })}
                    className="self-start py-0.5 text-[12.5px] text-[#9fd0f2] hover:text-white">
              Restore defaults
            </button>
          )}
        </div>
      )}
      <div className="sticky bottom-0 -mx-3.5 mt-1.5 border-t border-line/[0.14] bg-[rgb(8_11_16/0.98)] p-2">
        <button type="button" data-testid="all-settings" onClick={() => { onClose(); onNavigate('settings/voice'); }}
                className="min-h-[38px] w-full rounded-[10px] border border-line/[0.28] bg-line/[0.06] text-[13.5px] text-ink-strong hover:bg-line/[0.14]">
          All settings →
        </button>
      </div>
    </HomePopover>
  );
}

function Switch({ on }: { on: boolean }) {
  return (
    <span aria-hidden className="relative h-5 w-[34px] shrink-0 rounded-full transition-colors duration-200"
          style={{ background: on ? '#3f7fae' : 'rgb(var(--line) / 0.22)' }}>
      <span className="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-[left] duration-200" style={{ left: on ? 16 : 2 }} />
    </span>
  );
}
