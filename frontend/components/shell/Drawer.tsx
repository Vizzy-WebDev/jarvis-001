'use client';

import { useEffect } from 'react';

import { CloseIcon, sectionIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { GROUPS, SECTIONS, type Section } from '@/lib/nav';

/**
 * The menu, opening from the left, and the way every section is reached.
 *
 * Built from the SECTIONS registry rather than a hand-written list, so a
 * capability added later appears here, in the router and in voice navigation
 * from one entry. A section whose screen is not ported yet is still listed and
 * says so — hiding it would make the app look smaller than it is.
 *
 * Rows are an icon and a name, tight enough that all twelve are on screen at
 * once — a menu you have to scroll to find something in is a menu that has
 * stopped being a menu. What each section is FOR is said on the section's own
 * screen, where there is room to say it without truncating.
 */
export function Drawer({
  open,
  current,
  onClose,
  onNavigate,
}: {
  open: boolean;
  current: Section;
  onClose: () => void;
  onNavigate: (id: string) => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  return (
    <>
      <div
        className={[
          'fixed inset-0 z-40 bg-surface-overlay backdrop-blur-[2px] transition-opacity duration-200',
          open ? 'opacity-100' : 'pointer-events-none opacity-0',
        ].join(' ')}
        onClick={onClose}
        aria-hidden
      />
      <nav
        aria-label="Sections"
        aria-hidden={!open}
        data-testid="drawer"
        data-open={open ? 'true' : 'false'}
        className={[
          'fixed inset-y-0 left-0 z-50 flex w-[292px] max-w-[86vw] flex-col',
          'border-r border-surface-border bg-surface-raised/95 backdrop-blur-xl shadow-panel',
          'transition-transform duration-200 ease-out',
          open ? 'translate-x-0' : '-translate-x-full',
        ].join(' ')}
      >
        <div className="flex items-start gap-3 px-5 pb-4 pt-5">
          <div>
            <p className="text-[12px] font-semibold uppercase tracking-[0.22em] text-ink-faint">
              Jarvis
            </p>
            <p className="mt-1 text-[13px] text-ink-muted">Everything it can do.</p>
          </div>
          <IconButton
            label="Close menu"
            className="ml-auto -mr-1"
            onClick={onClose}
            tabIndex={open ? 0 : -1}
          >
            <CloseIcon />
          </IconButton>
        </div>

        <div className="scroll-quiet flex-1 overflow-y-auto px-3 pb-6">
          {GROUPS.map((group) => (
            <section key={group} className="mb-5 last:mb-0">
              <h2 className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-ink-faint">
                {group}
              </h2>
              <ul className="space-y-0.5">
                {SECTIONS.filter((section) => section.group === group).map((section) => {
                  const active = section.id === current.id;
                  const Icon = sectionIcon(section.id);
                  return (
                    <li key={section.id}>
                      <button
                        type="button"
                        tabIndex={open ? 0 : -1}
                        data-testid={`nav-${section.id}`}
                        onClick={() => {
                          onNavigate(section.id);
                          onClose();
                        }}
                        aria-current={active ? 'page' : undefined}
                        className={[
                          'group relative flex w-full items-center gap-3 rounded px-3 py-2 text-left',
                          'transition duration-150 ease-out',
                          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60',
                          active ? 'bg-accent/[0.10]' : 'hover:bg-white/[0.05]',
                        ].join(' ')}
                      >
                        {/* The active mark is a bar at the edge, not a filled
                            row: it survives being read at a glance without
                            turning the whole list into blocks of colour. */}
                        <span
                          aria-hidden
                          className={[
                            'absolute left-0 top-1/2 h-5 w-[2px] -translate-y-1/2 rounded-pill bg-accent',
                            'transition-opacity duration-150',
                            active ? 'opacity-100' : 'opacity-0',
                          ].join(' ')}
                        />
                        <Icon
                          className={[
                            'h-[18px] w-[18px] shrink-0 transition-colors duration-150',
                            active ? 'text-accent' : 'text-ink-faint group-hover:text-ink-muted',
                          ].join(' ')}
                        />
                        <span
                          className={`min-w-0 flex-1 truncate text-[14px] ${active ? 'text-ink' : 'text-ink-muted group-hover:text-ink'}`}
                        >
                          {section.label}
                        </span>
                        {!section.ready && (
                          <span className="shrink-0 rounded-pill bg-white/[0.06] px-1.5 py-px text-[9px] font-semibold uppercase tracking-[0.12em] text-ink-faint">
                            soon
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      </nav>
    </>
  );
}
