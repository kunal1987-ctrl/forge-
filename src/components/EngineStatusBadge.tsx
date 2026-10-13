import React, { useState, useEffect, useCallback, useRef } from 'react';
import { apiClient } from '../services/apiClient';
import type { EngineStatusResponse } from '../types/api';

export interface EngineStatusBadgeProps {
  repoId?: string | null;
  taskId?: string | null;
  className?: string;
  pollIntervalMs?: number;
}

export const EngineStatusBadge: React.FC<EngineStatusBadgeProps> = ({
  repoId,
  taskId,
  className = '',
  pollIntervalMs = 4000,
}) => {
  const [status, setStatus] = useState<EngineStatusResponse | null>(null);
  const [isServerConnected, setIsServerConnected] = useState<boolean>(true);
  const pollTimerRef = useRef<number | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const data = await apiClient.getEngineStatus({
        repoId: repoId || undefined,
        taskId: taskId || undefined,
      });
      setStatus(data);
      setIsServerConnected(data.is_server_connected);
    } catch {
      setIsServerConnected(false);
      setStatus(null);
    }
  }, [repoId, taskId]);

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    void fetchStatus();

    pollTimerRef.current = window.setInterval(() => {
      void fetchStatus();
    }, pollIntervalMs);

    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [fetchStatus, pollIntervalMs]);

  // Case 1: Server offline / unreachable -> Red badge "Disconnected"
  if (!isServerConnected) {
    return (
      <div
        className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200/80 font-mono text-[11px] font-medium transition ${className}`}
        title="Backend API server unreachable"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-rose-500" />
        <span>Disconnected</span>
      </div>
    );
  }

  // Case 2: Workflow actively executing -> Animated green badge "Engine Active"
  if (status?.task_active) {
    return (
      <div
        className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200/80 font-mono text-[11px] font-medium transition ${className}`}
        title="Autonomous task workflow is currently executing"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
        <span>Engine Active</span>
      </div>
    );
  }

  // Case 3: Repository ingested and ready -> Blue/cyan badge "Repository Ready"
  if (status?.repo_connected) {
    return (
      <div
        className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-sky-50 text-sky-700 border border-sky-200/80 font-mono text-[11px] font-medium transition ${className}`}
        title="Repository indexed and ready for autonomous tasks"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-sky-500" />
        <span>Repository Ready</span>
      </div>
    );
  }

  // Case 4: No repository ingested -> Neutral/gray badge "No Repository"
  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-zinc-100 text-zinc-600 border border-zinc-200/80 font-mono text-[11px] font-medium transition ${className}`}
      title="No target repository indexed"
    >
      <span className="w-1.5 h-1.5 rounded-full bg-zinc-400" />
      <span>No Repository</span>
    </div>
  );
};

export default EngineStatusBadge;
