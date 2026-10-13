'use client';

import { useCallback, useEffect, useState } from 'react';

import { canonical, resolveRoute, type Route } from './nav';

/**
 * `#/knowledge/memory` selects a page and its tab. A hash rather than a path
 * because the production build is a static export served by FastAPI from one
 * port: a real route per screen would need a file per screen on disk and a
 * server willing to rewrite unknown paths, and a hash needs neither. It is also
 * what `open_section` speaks, so voice navigation and a click land in the same
 * place.
 */
export function useHashRoute(): [Route, (id: string) => void] {
  const [path, setPath] = useState<string>('');

  useEffect(() => {
    const read = () => setPath(window.location.hash.replace(/^#\/?/, '').trim());
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);

  const go = useCallback((next: string) => {
    window.location.hash = next === 'home' ? '#/' : `#/${canonical(next)}`;
  }, []);

  return [resolveRoute(path), go];
}
