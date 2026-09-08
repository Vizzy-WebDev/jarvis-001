'use client';

import { useEffect, useRef, useState } from 'react';

import { createOrb, type OrbState } from '@/lib/orb';

/**
 * The orb, in its own box.
 *
 * That box is the whole stage minus what the mic reserves at the bottom, and it
 * is taken out of flow: nothing below it can shrink it, and it never moves off
 * centre. `pointer-events-none` because it overlays the mic's own growth room —
 * clicks belong to whatever is underneath.
 */
export function Orb({ state, getLevel }: { state: OrbState; getLevel?: () => number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const orbRef = useRef<ReturnType<typeof createOrb> | null>(null);
  const levelRef = useRef(getLevel);
  const [fallbackState, setFallbackState] = useState<OrbState | null>(null);

  levelRef.current = getLevel;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const orb = createOrb(canvas, {
      getLevel: () => levelRef.current?.() ?? 0,
      onFallback: (next) => setFallbackState(next),
    });
    orbRef.current = orb;
    if (!orb.rendering) setFallbackState('idle');
    orb.start();
    return () => {
      orb.destroy();
      orbRef.current = null;
    };
  }, []);

  useEffect(() => {
    orbRef.current?.setState(state);
  }, [state]);

  return (
    <div
      className="pointer-events-none absolute inset-x-0 top-0 flex items-center justify-center"
      style={{ height: 'calc(100% - var(--stage-bottom-reserve))' }}
    >
      <canvas
        ref={canvasRef}
        data-testid="orb-canvas"
        aria-hidden
        className={`orb-canvas ${fallbackState ? 'hidden' : ''}`}
      />
      {fallbackState && <div className="orb-fallback" data-state={fallbackState} aria-hidden />}
    </div>
  );
}
