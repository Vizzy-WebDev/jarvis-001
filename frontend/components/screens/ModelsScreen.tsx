'use client';

import { EmptyState } from '@/components/ui/EmptyState';

/**
 * Model Settings — for AI provider models only. The AI model system is being
 * rebuilt, so there is nothing to configure here yet.
 */
export function ModelsScreen() {
  return (
    <EmptyState
      title="Model settings are being rebuilt."
      body="Jarvis has no AI model connected right now, so it can't answer yet. Adding models will come back with the new model system."
    />
  );
}
