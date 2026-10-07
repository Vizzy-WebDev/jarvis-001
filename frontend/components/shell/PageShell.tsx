'use client';

import { MenuIcon } from '@/components/ui/Icons';
import { STATE_COLOUR, STATE_LABEL, type JarvisState } from '@/lib/jarvis-state';
import type { Page, Tab } from '@/lib/nav';

/**
 * The one shell every page except Home renders into (design 10b).
 *
 * A 64px glass header — the menu in the same top-left place as on Home, a
 * small mark of the core in Jarvis's current colour, the page's name, and what
 * Jarvis is doing right now on the right — with a grouped page's sub-pages as
 * pill tabs hanging below it as an attached tab. The content column scrolls
 * under the header.
 *
 * The page title is the screen's one `<h1>`; a screen never draws its own.
 */
export function PageShell({
  page,
  tab,
  state,
  wide,
  onMenu,
  onTab,
  children,
}: {
  page: Page;
  tab: Tab | null;
  state: JarvisState;
  /** A screen that needs more than the reading column (a gallery, a board). */
  wide?: boolean;
  onMenu: () => void;
  onTab: (tabId: string) => void;
  children: React.ReactNode;
}) {
  const colour = STATE_COLOUR[state];
  const tabs = page.tabs ?? [];
  return (
    <div className="relative flex h-full flex-col" data-testid="page-shell">
      <header
        className="relative z-10 flex h-[var(--shell-header)] shrink-0 items-center gap-3 border-b border-line/[0.16]
                   bg-gradient-to-b from-surface-raised/90 to-surface/85 px-5"
      >
        <button
          type="button"
          aria-label="Menu"
          title="Menu"
          data-testid="menu"
          onClick={onMenu}
          className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] border border-line/[0.22]
                     text-ink-soft transition-colors duration-200 hover:border-line/40 hover:text-white"
        >
          <MenuIcon className="h-[18px] w-[18px]" />
        </button>
        <span
          aria-hidden
          data-testid="shell-mark"
          className="h-7 w-7 shrink-0 rounded-full transition-[background,box-shadow] duration-500"
          style={{
            background: `radial-gradient(circle at 50% 50%, #fff 0, ${colour} 35%, transparent 72%)`,
            boxShadow: `0 0 18px ${colour}`,
          }}
        />
        <span aria-hidden className="h-6 w-px bg-line/20" />
        <h1 className="truncate text-[17px] font-medium text-ink-strong">{page.label}</h1>

        {tabs.length > 0 && (
          <div
            role="tablist"
            aria-label={page.label}
            className="absolute left-1/2 top-[63px] flex max-w-[calc(100vw-24px)] -translate-x-1/2 gap-1 overflow-x-auto
                       rounded-b-[20px] border border-line/[0.16] border-t-surface
                       bg-gradient-to-b from-surface/95 to-surface-raised/90 px-2.5 pb-2.5 pt-1.5 shadow-tab"
          >
            {tabs.map((t) => {
              const on = t.id === tab?.id;
              return (
                <button
                  key={t.id}
                  type="button"
                  role="tab"
                  aria-selected={on}
                  data-testid={`tab-${t.id}`}
                  onClick={() => onTab(t.id)}
                  className={[
                    'h-[34px] shrink-0 whitespace-nowrap rounded-full border px-4 text-[13.5px] transition-colors duration-200',
                    on ? 'border-orb-standby/40 bg-orb-standby/[0.12] text-ink-strong'
                      : 'border-transparent text-ink-muted hover:text-ink-strong',
                  ].join(' ')}
                >
                  {t.label}
                </button>
              );
            })}
          </div>
        )}

        <span
          data-testid="shell-state"
          className="ml-auto shrink-0 font-mono text-[11.5px] tracking-[0.2em] transition-colors duration-500"
          style={{ color: colour }}
        >
          {STATE_LABEL[state]}
        </span>
      </header>

      <div className="scroll-quiet min-h-0 flex-1 overflow-y-auto px-4 pb-20 sm:px-5"
           style={{ paddingTop: tabs.length ? 74 : 28 }}>
        <div className={`mx-auto w-full ${wide ? 'max-w-6xl' : 'max-w-[880px]'}`}>{children}</div>
      </div>
    </div>
  );
}

/** The one-line "what this is for" above a screen that has not been redesigned
 *  yet. A redesigned screen says it in its own layout and drops this. */
export function Blurb({ children }: { children: React.ReactNode }) {
  return <p className="mb-6 text-[13.5px] text-ink-muted">{children}</p>;
}

/** A sub-page that exists in the design but whose screen arrives in a later
 *  stage of the redesign. Says so plainly rather than showing an empty page. */
export function ComingInStage({ what }: { what: string }) {
  return (
    <div className="flex min-h-[40vh] flex-col items-center justify-center gap-3 text-center">
      <span className="font-mono text-[12px] tracking-[0.22em] text-ink-muted">NOT BUILT YET</span>
      <span className="max-w-[440px] text-[15px] leading-relaxed text-ink-soft">{what}</span>
    </div>
  );
}
