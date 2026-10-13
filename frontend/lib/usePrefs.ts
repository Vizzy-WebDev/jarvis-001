'use client';

import { useSyncExternalStore } from 'react';

import { api } from './api';
import type { Prefs } from './api-types';

/**
 * The interface's own settings — how Home looks and listens — kept in the same
 * `/api/prefs` file as everything else, so they survive a reload and follow the
 * person to another window. Before this, the voice choices on Home (speak
 * replies, which recogniser, which voice) lived in component state and quietly
 * reset every time the page loaded.
 *
 * One module-level store rather than a context: every reader subscribes to the
 * same snapshot, a write is optimistic (the screen answers at once) and then
 * confirmed by the server's merged copy. Defaults live here, not on the server:
 * a key the server has never seen simply reads as its default.
 */
export interface UiPrefs {
  /** Minutes without a key or pointer before Home rests. 0 = never. */
  restAfterMin: number;
  /** What a resting Home shows (design 1b "Night stand" is `clock`). */
  restLook: 'clock' | 'ember' | 'dim';
  /** How waking plays: the designed ~2.8s sequence, a short one, or none. */
  activation: 'full' | 'quick' | 'instant';
  /** Which of Home's three panels are showing ("Show on Home"). */
  homeShow: { chat: boolean; sys: boolean; jar: boolean };
  wakeWord: boolean;
  wakePhrases: string[];
  pushToTalk: boolean;
  speakReplies: boolean;
  /** The listening engine's id, from `/api/voice/options`. */
  voiceEngine: string;
  /** The speaking voice's id, from `/api/voice/options`. */
  voiceOutput: string;
}

export const DEFAULT_WAKE_PHRASES = ['Hey Jarvis', 'Jarvis', 'OK Jarvis', 'Wake up Jarvis'];

export const UI_DEFAULTS: UiPrefs = {
  restAfterMin: 5,
  restLook: 'clock',
  activation: 'full',
  homeShow: { chat: true, sys: true, jar: true },
  wakeWord: true,
  wakePhrases: DEFAULT_WAKE_PHRASES,
  pushToTalk: false,
  speakReplies: false,
  voiceEngine: 'pipeline',
  voiceOutput: 'browser',
};

export type AllPrefs = Prefs & UiPrefs;

let snapshot: AllPrefs = { ...(UI_DEFAULTS as AllPrefs) };
let loaded = false;
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((listener) => listener());
}

function merge(server: Partial<AllPrefs>) {
  snapshot = { ...UI_DEFAULTS, ...server } as AllPrefs;
  loaded = true;
  emit();
}

/** Read once per page load; every caller shares the one request. */
export function loadPrefs(): Promise<void> {
  if (!loading) {
    loading = api.prefs.get()
      .then((server) => merge(server as Partial<AllPrefs>))
      .catch(() => { loaded = true; emit(); });
  }
  return loading;
}

/** Change some settings now and save them. A failed save puts back what the
 *  server still has, so the screen never claims a setting that did not stick. */
export async function setPrefs(patch: Partial<AllPrefs>): Promise<void> {
  snapshot = { ...snapshot, ...patch };
  emit();
  try {
    merge(await api.prefs.update(patch as Partial<Prefs>) as Partial<AllPrefs>);
  } catch {
    loading = null;
    await loadPrefs();
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (!loaded) void loadPrefs();
  return () => listeners.delete(listener);
}

export function usePrefs(): AllPrefs & { loaded: boolean } {
  const current = useSyncExternalStore(subscribe, () => snapshot, () => snapshot);
  return { ...current, loaded };
}

/** For code outside React (a key handler, an engine callback). */
export function currentPrefs(): AllPrefs {
  return snapshot;
}
