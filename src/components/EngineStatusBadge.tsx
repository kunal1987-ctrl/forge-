import React from 'react';

export interface EngineStatusBadgeProps {
  repoId?: string | null;
  taskId?: string | null;
  className?: string;
  pollIntervalMs?: number;
}

/**
 * EngineStatusBadge is deprecated and removed from the UI.
 */
export const EngineStatusBadge: React.FC<EngineStatusBadgeProps> = () => {
  return null;
};

export default EngineStatusBadge;
