'use client';

import { appMark } from '@/lib/app-icons';

/**
 * One app's mark, at a fixed size, on its own tile.
 *
 * A tile rather than a bare glyph: the marks are white-on-brand-colour, and a
 * list of them only reads as a list of APPS when each sits on its own coloured
 * ground at the same size.
 */
export function AppIcon({
  label,
  iconDataUri,
  size = 28,
}: {
  label: string | null | undefined;
  iconDataUri?: string | null;
  size?: number;
}) {
  const mark = appMark({ label, iconDataUri });
  return (
    <span
      aria-hidden
      className="flex shrink-0 items-center justify-center overflow-hidden rounded"
      style={{ width: size, height: size, background: mark.background }}
    >
      {mark.src ? (
        // eslint-disable-next-line @next/next/no-img-element -- a data URI, and a static export has no optimiser
        <img src={mark.src} alt="" width={size} height={size} className="h-full w-full object-contain" />
      ) : mark.path ? (
        <svg viewBox="0 0 24 24" width={size * 0.6} height={size * 0.6}>
          <path d={mark.path} fill="#fff" />
        </svg>
      ) : (
        <span
          className="font-semibold text-white"
          style={{ fontSize: size * 0.44, lineHeight: 1 }}
        >
          {mark.letter}
        </span>
      )}
    </span>
  );
}
