'use client';

import { BellIcon, MenuIcon, ScreenShareIcon, SettingsIcon } from '@/components/ui/Icons';
import { IconButton } from '@/components/ui/IconButton';

/**
 * The top band: the way in on the left, the app's own controls on the right.
 *
 * The hamburger's position is the fixed thing. What sits opposite it is not —
 * so only what acts on JARVIS ITSELF lives here (is it watching my screen, has
 * it told me anything, how is it set up). Starting a new conversation used to
 * be here too and has moved into the conversation panel, which is what it
 * actually acts on.
 *
 * It floats out of the layout flow and the body reserves `--header-reserve` in
 * its own padding instead. That is not a style choice: it is what stops
 * anything up here from pushing the body down and resizing the orb.
 */
export function Header({
  unread,
  sharing,
  settingsOpen,
  onMenu,
  onToggleSharing,
  onNotifications,
  onToggleSettings,
}: {
  unread: number;
  sharing: boolean;
  settingsOpen: boolean;
  onMenu: () => void;
  onToggleSharing: () => void;
  onNotifications: () => void;
  onToggleSettings: () => void;
}) {
  return (
    <header className="absolute inset-x-5 top-0 z-20 flex items-center gap-3 pt-4">
      <IconButton label="Menu" data-testid="menu" onClick={onMenu}>
        <MenuIcon />
      </IconButton>

      <span className="select-none text-[12px] font-semibold uppercase tracking-[0.22em] text-ink-faint">
        Jarvis
      </span>

      {sharing && (
        <span className="flex items-center gap-1.5 rounded-pill bg-state-ok/10 px-2.5 py-1 text-[11px] font-medium text-state-ok">
          <span className="h-1.5 w-1.5 rounded-full bg-state-ok" />
          Watching your screen
        </span>
      )}

      {/* One cluster rather than four loose buttons: grouped on a single quiet
          ground, they read as the app's chrome instead of competing with the
          conversation for attention. */}
      <div className="ml-auto flex items-center gap-0.5 rounded-pill border border-surface-border bg-surface-panel p-0.5 backdrop-blur-xl">
        <IconButton
          label={sharing ? 'Stop sharing your screen' : 'Share your screen'}
          active={sharing}
          onClick={onToggleSharing}
        >
          <ScreenShareIcon />
        </IconButton>

        <div className="relative">
          <IconButton label="Notifications" data-testid="bell" onClick={onNotifications}>
            <BellIcon />
          </IconButton>
          {unread > 0 && (
            <span
              className="pointer-events-none absolute -right-0.5 -top-0.5 min-w-[17px] rounded-pill
                         bg-accent px-1 text-center text-[10px] font-semibold leading-[17px] text-surface"
              aria-label={`${unread} unread`}
            >
              {unread > 99 ? '99+' : unread}
            </span>
          )}
        </div>

        <IconButton
          label="Settings"
          data-testid="settings"
          active={settingsOpen}
          onClick={onToggleSettings}
        >
          <SettingsIcon />
        </IconButton>
      </div>
    </header>
  );
}
