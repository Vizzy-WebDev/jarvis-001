'use client';

import { useEffect, useState } from 'react';

import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { api } from '@/lib/api';
import type { CatalogProvider, CatalogVersion, ModelVersion, Support } from '@/lib/api-types';

/**
 * The roster by what the models ARE: provider → family → version.
 *
 * **The thing this view exists to show is the route count.** The same model
 * reached two ways — your own key and a gateway reselling it — is ONE version
 * with two routes, not two unrelated rows. They have separate keys, separate
 * prices and separate rate limits, and the flat list could not say that at all,
 * so a duplicate and a genuine second route looked identical.
 *
 * **A capability has three states and all three are rendered.** "Nobody has
 * asked" is not "it cannot": showing an unestablished capability as a flat no
 * is how a capable model gets quietly hidden, and it is the exact failure the
 * three-state answer exists to prevent.
 *
 * Nothing here is a shipped model list. Every row comes from a model this
 * install actually has.
 */
export function CatalogView({
  reloadKey,
  onOpen,
}: {
  reloadKey: number;
  /** Opens the same detail the by-connection view opens. This is a lens over
   *  those objects, not a separate read-only display — the standing rule is
   *  that if a thing is a thing, it is clickable. */
  onOpen: (deploymentId: string) => void;
}) {
  const [providers, setProviders] = useState<CatalogProvider[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.models.catalog()
      .then((answer) => live && setProviders(answer.providers))
      .catch(() => live && setProviders([]));
    return () => {
      live = false;
    };
  }, [reloadKey]);

  if (providers === null) {
    return <p className="py-10 text-center text-[13px] text-ink-faint">Reading…</p>;
  }
  if (providers.length === 0) {
    return (
      <EmptyState
        title="Nothing to group yet."
        body="Add a model and it will appear here under whoever makes it."
      />
    );
  }

  return (
    <div className="space-y-3" data-testid="catalog-list">
      {providers.map((provider) => (
        <Card key={provider.id} className="p-0" data-testid="catalog-provider">
          <p className="border-b border-surface-border px-4 py-2.5 text-[13px] text-ink-muted">
            {provider.id === 'unknown' ? 'Not recognised' : provider.label}
          </p>
          {provider.families.map((family) => (
            <div key={family.id} className="border-b border-surface-border last:border-b-0">
              <p className="px-4 pt-3 text-[12px] text-ink-faint">{family.label}</p>
              {family.versions.map((version) => (
                <VersionRow
                  key={version.model}
                  version={version}
                  open={open === version.model}
                  onToggle={() => setOpen(open === version.model ? null : version.model)}
                  onOpen={onOpen}
                />
              ))}
            </div>
          ))}
        </Card>
      ))}
    </div>
  );
}

function VersionRow({
  version,
  open,
  onToggle,
  onOpen,
}: {
  version: CatalogVersion;
  open: boolean;
  onToggle: () => void;
  onOpen: (deploymentId: string) => void;
}) {
  const routes = version.deployments;
  return (
    <div className="px-4 py-2" data-testid="catalog-version">
      <button
        type="button"
        onClick={onToggle}
        data-testid={`catalog-open-${version.model}`}
        className="w-full text-left focus-visible:outline-none"
      >
        <span className="block truncate text-[13px] text-ink">{version.label}</span>
        <span className="mt-0.5 block text-[11px] text-ink-faint">
          {routes.length === 1
            ? `via ${routes[0]!.connectionLabel ?? 'one connection'}`
            : `${routes.length} ways to reach it`}
        </span>
      </button>

      {open && (
        <div className="mt-2 border-t border-surface-border pt-2" data-testid="catalog-detail">
          <ul className="mb-2 space-y-1">
            {routes.map((route) => (
              <li key={route.id}>
                <button
                  type="button"
                  data-testid={`catalog-route-${route.id}`}
                  onClick={() => onOpen(route.id)}
                  className="flex w-full items-center justify-between gap-3 rounded px-1 py-1
                             text-left text-[11px] transition hover:bg-white/[0.04]
                             focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                >
                  <span className="min-w-0 truncate text-ink-muted">
                    {route.connectionLabel ?? route.label}
                  </span>
                  <span className="shrink-0 text-ink-faint">
                    {!route.ready ? 'needs a key' : route.enabled ? 'on' : 'off'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <VersionFacts version={version.version} />
        </div>
      )}
    </div>
  );
}

/** What the catalog knows, including what it does not. */
export function VersionFacts({ version }: { version: ModelVersion }) {
  const thinking = version.effort.kind === 'none'
    ? 'none'
    : version.effort.kind === 'unknown'
      ? 'not established'
      : Object.keys(version.effort.native).join(' · ').toLowerCase() || 'yes';

  return (
    <dl className="space-y-1.5 text-[11px]" data-testid="version-facts">
      <Fact term="Model id" value={version.model} />
      {version.contextTokens && (
        <Fact term="Context" value={`${Math.round(version.contextTokens / 1000)}k tokens`} />
      )}
      <Fact term="Thinking" value={thinking} />
      <Fact
        term="Name"
        value={version.pinned === 'yes'
          ? 'a fixed release'
          : version.pinned === 'no'
            ? 'an alias — may change under you'
            : 'not established'}
      />
      <div className="flex justify-between gap-4">
        <dt className="text-ink-faint">Can</dt>
        <dd className="flex min-w-0 flex-wrap justify-end gap-1">
          {Object.entries(version.capabilities).map(([name, support]) => (
            <Capability key={name} name={name} support={support} />
          ))}
        </dd>
      </div>
    </dl>
  );
}

/**
 * All three states, each looking like itself.
 *
 * `unknown` is deliberately not styled as a negative: it means nobody has
 * asked, the model is still offered for that work, and it is allowed to fail
 * honestly — which is the only way anyone finds out.
 */
function Capability({ name, support }: { name: string; support: Support }) {
  const label = name === 'web_search' ? 'search' : name;
  const tone = support === 'yes'
    ? 'bg-state-ok/10 text-state-ok'
    : support === 'no'
      ? 'bg-white/[0.04] text-ink-faint line-through'
      : 'bg-white/[0.04] text-ink-faint';
  return (
    <span
      className={`rounded-pill px-2 py-0.5 text-[10px] ${tone}`}
      title={support === 'unknown' ? 'Nobody has established this either way' : undefined}
    >
      {label}
      {support === 'unknown' && '?'}
    </span>
  );
}

function Fact({ term, value }: { term: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-ink-faint">{term}</dt>
      <dd className="min-w-0 truncate text-ink-muted">{value}</dd>
    </div>
  );
}
