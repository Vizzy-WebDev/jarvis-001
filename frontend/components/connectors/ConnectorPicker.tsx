'use client';

import { useMemo, useRef, useState } from 'react';

import { AppIcon } from '@/components/ui/AppIcon';
import { Button } from '@/components/ui/Button';
import { CloseIcon, PlusIcon, SearchIcon } from '@/components/ui/Icons';
import { inputClass } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Popover } from '@/components/ui/Popover';
import { Toggle } from '@/components/ui/Toggle';
import type { Connector } from '@/lib/api-types';

/**
 * Choosing which connected apps something may use.
 *
 * Shaped like an app picker rather than a list of names, because that is what it
 * is: what is chosen shows as chips carrying each app's own mark, and the
 * dropdown is a row per app — mark, name, what state it is in, and a real
 * switch.
 *
 * **The dropdown is capped.** The original found live that an unbounded list
 * runs off the bottom of the screen once there are more than a handful of
 * connected apps; past the cap, "See more" opens the full list, which is
 * searchable because that is the case where searching starts to matter.
 *
 * Every toggle applies immediately, in both the dropdown and the full list.
 * There is nothing to submit — the caller owns persistence and gets told on
 * every change, exactly as the original's shared picker does.
 */
const INLINE_CAP = 5;

export function ConnectorPicker({
  connectors,
  selected,
  onChange,
  onManage,
}: {
  connectors: Connector[];
  selected: string[];
  onChange: (next: string[]) => void;
  /** Somewhere to go and connect a new app. */
  onManage?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [seeAll, setSeeAll] = useState(false);
  const [query, setQuery] = useState('');
  const anchorRef = useRef<HTMLButtonElement>(null);

  const chosen = useMemo(
    () => selected.map((id) => connectors.find((c) => c.id === id)).filter(Boolean) as Connector[],
    [connectors, selected],
  );

  const matching = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return connectors;
    return connectors.filter((c) => (c.label || c.type).toLowerCase().includes(needle));
  }, [connectors, query]);

  const toggle = (id: string, on: boolean) =>
    onChange(on ? [...selected, id] : selected.filter((entry) => entry !== id));

  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5">
        {chosen.map((connector) => (
          <span
            key={connector.id}
            data-testid="connector-chip"
            className="flex items-center gap-1.5 rounded-pill border border-surface-border
                       bg-white/[0.04] py-1 pl-1 pr-1.5 text-[12px] text-ink"
          >
            <AppIcon label={connector.label} size={20} />
            <span className="max-w-[140px] truncate">{connector.label || connector.type}</span>
            <button
              type="button"
              aria-label={`Remove ${connector.label || connector.type}`}
              onClick={() => toggle(connector.id, false)}
              className="rounded-full p-0.5 text-ink-faint transition hover:bg-white/10 hover:text-ink"
            >
              <CloseIcon className="h-3 w-3" />
            </button>
          </span>
        ))}

        <button
          ref={anchorRef}
          type="button"
          data-testid="add-connector"
          onClick={() => setOpen((was) => !was)}
          className="flex items-center gap-1.5 rounded-pill border border-dashed border-surface-border
                     px-2.5 py-1 text-[12px] text-ink-muted transition duration-150 ease-out
                     hover:border-surface-border-strong hover:text-ink
                     focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
        >
          <PlusIcon className="h-3.5 w-3.5" />
          Add connector
        </button>
      </div>

      <Popover open={open} anchorRef={anchorRef} onClose={() => setOpen(false)} width={312}>
        {connectors.length === 0 ? (
          <p className="px-4 py-4 text-[12px] leading-relaxed text-ink-muted">
            No apps are connected yet. Connect one in App Control and it will show up here.
          </p>
        ) : (
          <div className="scroll-quiet overflow-y-auto py-1">
            {connectors.slice(0, INLINE_CAP).map((connector) => (
              <ConnectorRow
                key={connector.id}
                connector={connector}
                on={selected.includes(connector.id)}
                onToggle={(next) => toggle(connector.id, next)}
              />
            ))}
          </div>
        )}

        <div className="border-t border-surface-border p-1.5">
          {connectors.length > INLINE_CAP && (
            <button
              type="button"
              data-testid="see-more"
              onClick={() => {
                setOpen(false);
                setSeeAll(true);
              }}
              className="w-full rounded px-2.5 py-2 text-left text-[12px] text-accent
                         transition hover:bg-white/[0.05]"
            >
              See more ({connectors.length})
            </button>
          )}
          {onManage && (
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                onManage();
              }}
              className="w-full rounded px-2.5 py-2 text-left text-[12px] text-ink-muted
                         transition hover:bg-white/[0.05] hover:text-ink"
            >
              Manage connectors →
            </button>
          )}
        </div>
      </Popover>

      {/* `nested` because this is opened from inside the task editor's own
          modal: it has to sit above it, and Escape must close only this one. */}
      <Modal
        nested
        open={seeAll}
        title="All connectors"
        onClose={() => {
          setSeeAll(false);
          setQuery('');
        }}
      >
        <label className="relative mb-3 block">
          <SearchIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-faint" />
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search connectors"
            data-testid="connector-search"
            className={`${inputClass} pl-9`}
          />
        </label>

        {matching.length === 0 ? (
          <p className="py-6 text-center text-[13px] text-ink-faint">Nothing matches that.</p>
        ) : (
          <div className="-mx-1">
            {matching.map((connector) => (
              <ConnectorRow
                key={connector.id}
                connector={connector}
                on={selected.includes(connector.id)}
                onToggle={(next) => toggle(connector.id, next)}
              />
            ))}
          </div>
        )}

        {onManage && (
          <div className="mt-3 border-t border-surface-border pt-3">
            <Button
              onClick={() => {
                setSeeAll(false);
                onManage();
              }}
            >
              Connect a new app
            </Button>
          </div>
        )}
      </Modal>
    </>
  );
}

/** One app: its mark, its name, what state it is in, and a switch. Shared by the
 *  dropdown and the full list so there is exactly one row implementation. */
function ConnectorRow({
  connector,
  on,
  onToggle,
}: {
  connector: Connector;
  on: boolean;
  onToggle: (next: boolean) => void;
}) {
  const name = connector.label || connector.type;
  return (
    <div
      data-testid={`connector-row-${connector.id}`}
      className="flex items-center gap-2.5 rounded px-2.5 py-2 transition hover:bg-white/[0.04]"
    >
      <AppIcon label={connector.label} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] text-ink">{name}</span>
        <span className="block truncate text-[11px] text-ink-faint">{describe(connector)}</span>
      </span>
      <Toggle label={name} checked={on} onChange={onToggle} />
    </div>
  );
}

/** What the row says under the name. The connector's own status is already in
 *  the payload, and "not working" is worth knowing BEFORE picking it for
 *  something that runs unattended. */
function describe(connector: Connector): string {
  const state = connector.status?.state;
  if (state === 'ok') return 'Connected';
  if (state === 'error') return connector.status?.detail || 'Not working';
  if (state === 'untested') return 'Not tested yet';
  return connector.type;
}
