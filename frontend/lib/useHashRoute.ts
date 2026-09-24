'use client';

import { useEffect, useState } from 'react';

import { findSection, type Section } from './nav';

/**
 * `#/models` selects a section. A hash rather than a path because the
 * production build is a static export served by FastAPI from one port: a real
 * route per screen would need a file per screen on disk and a server willing to
 * rewrite unknown paths, and a hash needs neither. It is also what
 * `open_section` speaks, so voice navigation and a click land in the same place.
 */
export function useHashRoute(): [Section, (id: string) => void] {
  const [id, setId] = useState<string>('home');

  useEffect(() => {
    const read = () => {
      // The first segment is the section; a screen may keep its own place in
      // the rest (`#/content/niche/Psychology`), so refresh and Back return there.
      const raw = window.location.hash.replace(/^#\/?/, '').trim().split('/')[0];
      setId(raw || 'home');
    };
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);

  const go = (next: string) => {
    window.location.hash = next === 'home' ? '#/' : `#/${next}`;
  };

  return [findSection(id), go];
}
