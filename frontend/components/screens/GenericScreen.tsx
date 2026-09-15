'use client';

import { EmptyState } from '@/components/ui/EmptyState';
import { MenuIcon, sectionIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';
import { PageHeader } from '@/components/ui/PageHeader';
import type { Section } from '@/lib/nav';

/**
 * The one shell every section renders into.
 *
 * Its own hamburger in the same place as the assistant's, so the menu is
 * reachable from anywhere without going back first. Everything below that is
 * the same three-part pattern on every screen — title, the controls that act on
 * the whole screen, then the content in one measured column — which is what
 * keeps twelve different screens feeling like one product.
 */
export function GenericScreen({
  section,
  onMenu,
  controls,
  children,
}: {
  section: Section;
  onMenu: () => void;
  controls?: React.ReactNode;
  children?: React.ReactNode;
}) {
  const Icon = sectionIcon(section.id);
  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center gap-3 px-5 pt-4">
        <IconButton label="Menu" data-testid="menu" onClick={onMenu}>
          <MenuIcon />
        </IconButton>
        <span className="flex items-center gap-2 text-[12px] font-semibold uppercase tracking-[0.22em] text-ink-faint">
          <Icon className="h-4 w-4" />
          {section.group}
        </span>
      </header>

      <div className="scroll-quiet flex-1 overflow-y-auto px-5 pb-10 pt-6">
        <div className="mx-auto w-full max-w-3xl">
          <PageHeader title={section.label} blurb={section.blurb}>
            {controls}
          </PageHeader>
          {children}
        </div>
      </div>
    </div>
  );
}

/** A section whose screen has not been ported yet. It says what is missing
 *  rather than showing an empty page — or, worse, hiding the entry entirely. */
export function NotPortedYet({ section }: { section: Section }) {
  return (
    <EmptyState
      title={`${section.label} isn’t on screen yet.`}
      body="The engine behind it is built and tested — this is the part of the move that puts a face on it. It arrives in a later wave."
    />
  );
}
