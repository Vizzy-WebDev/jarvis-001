'use client';

/**
 * Jarvis's own browser, as it appears in the chat: a live picture of the window he is
 * using, and a way to take it over.
 *
 * Ported in spirit from OpenMuse's browser card (MIT): the card shows what the agent is
 * looking at and offers "Take control", which opens the console in its own tab — to sign
 * in, solve a CAPTCHA or approve a payment, none of which Jarvis does for the person.
 *
 * One card per window, however many steps ran: the picture is live, so a card for each
 * step would only repeat it (`Message.tsx` keeps one). It looks only while it is on screen
 * and the tab is in front — a window nobody is looking at is not photographed — and it
 * stops for good once the window is closed, so a card in an old chat costs one request.
 *
 * The picture is outside content served as a download, so it is fetched and drawn from a
 * local object URL, never pointed at directly.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { api, ApiRequestError } from '@/lib/api';
import type { BrowserImport, BrowserSessionInfo } from '@/lib/api-types';

type State = 'loading' | 'live' | 'closed' | 'gone' | 'unavailable';

const POLL_MS = 2500;

export interface BrowserCardInfo {
  url: string;
  sessionId?: string;
  name?: string;
  pageUrl?: string;
}

/** The session id out of `/api/browsers/<id>/console`, for a card that only has the address. */
function sessionIdOf(card: BrowserCardInfo): string | null {
  if (card.sessionId) return card.sessionId;
  const match = card.url.match(/^\/api\/browsers\/([0-9a-f-]{36})\//i);
  return match ? match[1] ?? null : null;
}

function siteOf(address: string | undefined): string {
  if (!address || address === 'about:blank') return 'a blank page';
  try {
    return new URL(address).hostname.replace(/^www\./, '');
  } catch {
    return address;
  }
}

export function BrowserCard({ card }: { card: BrowserCardInfo }) {
  const sessionId = sessionIdOf(card);
  const rootRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [state, setState] = useState<State>('loading');
  const [info, setInfo] = useState<BrowserSessionInfo | null>(null);
  const [shot, setShot] = useState<string | null>(null);
  const [problem, setProblem] = useState('');
  const [busy, setBusy] = useState(false);
  const [round, setRound] = useState(0);
  const [kept, setKept] = useState<BrowserImport['files']>([]);

  // Only look while it is on screen.
  useEffect(() => {
    const node = rootRef.current;
    if (!node || typeof IntersectionObserver === 'undefined') {
      setVisible(true);
      return undefined;
    }
    const watcher = new IntersectionObserver((entries) => setVisible(entries.some((e) => e.isIntersecting)));
    watcher.observe(node);
    return () => watcher.disconnect();
  }, []);

  // Release the last picture when the card goes away.
  const lastShot = useRef<string | null>(null);
  useEffect(() => () => { if (lastShot.current) URL.revokeObjectURL(lastShot.current); }, []);

  useEffect(() => {
    if (!sessionId || !visible) return undefined;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function look() {
      if (cancelled || !sessionId) return;
      if (document.hidden) { timer = setTimeout(look, POLL_MS); return; }
      try {
        const response = await fetch(api.browsers.previewUrl(sessionId), { cache: 'no-store' });
        if (cancelled) return;
        if (response.status === 409) {
          setState('closed');
          api.browsers.get(sessionId).then((found) => !cancelled && setInfo(found)).catch(() => undefined);
          return;                                   // closed: nothing more to watch
        }
        if (response.status === 404) { setState('gone'); return; }
        if (response.status === 503) {                      // the page is busy arriving: keep looking
          timer = setTimeout(look, POLL_MS);
          return;
        }
        if (!response.ok) throw new Error(`preview ${response.status}`);
        const next = URL.createObjectURL(await response.blob());
        if (cancelled) { URL.revokeObjectURL(next); return; }
        if (lastShot.current) URL.revokeObjectURL(lastShot.current);
        lastShot.current = next;
        setShot(next);
        setState('live');
        setProblem('');
        api.browsers.get(sessionId).then((found) => !cancelled && setInfo(found)).catch(() => undefined);
      } catch {
        if (!cancelled) setState((was) => (was === 'loading' ? 'unavailable' : was));
      }
      if (!cancelled) timer = setTimeout(look, POLL_MS);
    }
    void look();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [sessionId, visible, round]);

  const reopen = useCallback(async () => {
    if (!sessionId) return;
    setBusy(true);
    setProblem('');
    try {
      await api.browsers.open(sessionId);
      setState('loading');
      setRound((n) => n + 1);                       // starts looking again
    } catch (err) {
      setProblem(err instanceof ApiRequestError ? err.message : 'It would not open again.');
    } finally {
      setBusy(false);
    }
  }, [sessionId]);

  const close = useCallback(async () => {
    if (!sessionId) return;
    setBusy(true);
    try {
      await api.browsers.close(sessionId);
      setState('closed');
    } catch (err) {
      setProblem(err instanceof ApiRequestError ? err.message : 'It would not close.');
    } finally {
      setBusy(false);
    }
  }, [sessionId]);

  const keepPdfs = useCallback(async () => {
    if (!sessionId) return;
    setBusy(true);
    setProblem('');
    try {
      const done = await api.browsers.importDownloads(sessionId);
      setKept(done.files);
      setInfo((was) => (was ? { ...was, downloads: 0 } : was));
      if (!done.files.length && done.failures[0]) setProblem(done.failures[0].message);
    } catch (err) {
      setProblem(err instanceof ApiRequestError ? err.message : 'The PDF could not be kept.');
    } finally {
      setBusy(false);
    }
  }, [sessionId]);

  if (!sessionId) return null;

  const waiting = info?.downloads ?? 0;
  const where = info?.url || card.pageUrl;
  const title = info?.title || card.name || siteOf(where);
  const live = state === 'live';
  const label = live ? 'Live' : state === 'closed' ? 'Closed' : state === 'gone' ? 'Gone'
    : state === 'unavailable' ? 'Not answering' : 'Looking…';

  return (
    <div ref={rootRef} data-testid="browser-card" data-state={state} data-session={sessionId}
         className="mb-2 w-[340px] max-w-full overflow-hidden rounded-lg border border-surface-border bg-white/[0.03]">
      <div className="flex items-center gap-2 px-3 pt-2.5">
        <span aria-hidden
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-accent/25 bg-accent/10 text-accent">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8"
               strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18" />
          </svg>
        </span>
        <span className="min-w-0 flex-1">
          <span data-testid="browser-card-title" title={title}
                className="block truncate text-[13px] font-medium text-ink-strong">{title}</span>
          <span data-testid="browser-card-site" className="block truncate text-[11.5px] text-ink-muted">
            {siteOf(where)}
          </span>
        </span>
        <span data-testid="browser-card-status"
              className={`shrink-0 rounded-full px-2 py-0.5 text-[10.5px] ${live ? 'bg-accent/15 text-accent' : 'bg-white/[0.06] text-ink-muted'}`}>
          {label}
        </span>
      </div>

      <div className="mx-3 mt-2 overflow-hidden rounded-md bg-black/20" style={{ aspectRatio: '16 / 10' }}>
        {shot ? (
          // eslint-disable-next-line @next/next/no-img-element -- a blob from the live window; no optimiser applies
          <img src={shot} alt={`What Jarvis's browser is showing: ${title}`} data-testid="browser-card-shot"
               className={`block h-full w-full object-cover object-top ${live ? '' : 'opacity-50'}`} />
        ) : (
          <div className="flex h-full items-center justify-center px-4 text-center text-[12px] text-ink-muted">
            {state === 'closed' ? 'This window is closed. Opening it again keeps everything you were signed in to.'
              : state === 'gone' ? 'This browser window no longer exists.'
              : state === 'unavailable' ? 'The browser is not answering right now.'
              : 'Looking at the window…'}
          </div>
        )}
      </div>

      {problem && <p role="alert" className="mx-3 mt-2 text-[12px] text-red-300">{problem}</p>}

      {waiting > 0 && (
        <div data-testid="browser-card-pdf" className="mx-3 mt-2 flex items-center gap-2 text-[12px] text-ink-soft">
          <span>{waiting === 1 ? 'A PDF was downloaded.' : `${waiting} PDFs were downloaded.`}</span>
          <button type="button" onClick={keepPdfs} disabled={busy} data-testid="browser-card-keep-pdf"
                  className="ml-auto rounded-md border border-accent/40 px-2.5 py-1 text-accent hover:bg-accent/10 disabled:opacity-50">
            Keep {waiting === 1 ? 'it' : 'them'}
          </button>
        </div>
      )}
      {kept.length > 0 && (
        <p data-testid="browser-card-kept" className="mx-3 mt-2 text-[12px] text-ink-soft">
          Kept {kept.map((file) => file.name).join(', ')} —{' '}
          <a href={`#/artifacts/${kept[0]?.id ?? ''}`} className="text-accent underline-offset-2 hover:underline">
            open it in Artifacts
          </a>
        </p>
      )}

      <div className="flex items-center gap-2 px-3 py-2.5">
        {state === 'closed' ? (
          <button type="button" onClick={reopen} disabled={busy} data-testid="browser-card-reopen"
                  className="rounded-md border border-accent/40 px-2.5 py-1 text-[12px] text-accent hover:bg-accent/10 disabled:opacity-50">
            Open it again
          </button>
        ) : (
          <a href={api.browsers.consoleUrl(sessionId)} target="_blank" rel="noopener noreferrer"
             data-testid="browser-card-take-control"
             className="rounded-md border border-accent/40 px-2.5 py-1 text-[12px] text-accent hover:bg-accent/10">
            Take control
          </a>
        )}
        {live && (
          <button type="button" onClick={close} disabled={busy} data-testid="browser-card-close"
                  className="ml-auto text-[12px] text-ink-muted underline-offset-2 hover:text-ink hover:underline disabled:opacity-50">
            Close window
          </button>
        )}
      </div>
    </div>
  );
}
