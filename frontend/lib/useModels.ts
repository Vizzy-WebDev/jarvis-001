'use client';

import { useCallback, useEffect, useState } from 'react';

import { api } from './api';
import type { ModelsOverview } from './api-types';

/**
 * Fired whenever a connection, a model list or the selection changes, from
 * whichever component changed it.
 *
 * The model picker in the composer, the Model Settings screen and the page's own
 * "is anything set up" state all read the same backend truth and are on screen at
 * different times, so nothing holds a copy that could drift: each refetches when
 * told something changed, rather than one component reaching into another.
 */
export const MODELS_CHANGED = 'jarvis:models-changed';

export function announceModelsChanged(): void {
  window.dispatchEvent(new Event(MODELS_CHANGED));
}

export function useModels() {
  const [overview, setOverview] = useState<ModelsOverview | null>(null);
  const [failed, setFailed] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setOverview(await api.models.overview());
      setFailed(false);
    } catch {
      // Keep what was last known: a moment's failure to ask is not a reason to
      // blank a screen the person is in the middle of using.
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onChanged = () => void refresh();
    window.addEventListener(MODELS_CHANGED, onChanged);
    return () => window.removeEventListener(MODELS_CHANGED, onChanged);
  }, [refresh]);

  return { overview, failed, refresh };
}

/** How a provider-reported effort level reads to a person. */
export function effortLabel(level: string): string {
  if (level === 'xhigh') return 'Extra high';
  return level.charAt(0).toUpperCase() + level.slice(1);
}
