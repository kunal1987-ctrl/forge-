import React from 'react';
import type { LogEntry } from '../hooks/useAgentStream';
import type { BackendTaskStatus, TaskStatusResponse } from '../types/api';
import { ExecutionLog } from './ExecutionLog';
import { normalizeTaskStatus, type GlobalTaskStatus } from '../context/TaskContext';

export interface TelemetryFeedProps {
  task?: TaskStatusResponse | null;
  taskStatus?: BackendTaskStatus | string | null;
  logs?: LogEntry[];
  isStreaming?: boolean;
  className?: string;
}

export const TelemetryFeed: React.FC<TelemetryFeedProps> = ({
  task,
  taskStatus,
  logs = [],
  isStreaming = false,
  className = '',
}) => {
  const stringLogs = logs.map((l) => l.message);
  const rawStatus = task?.status || taskStatus || (isStreaming ? 'RUNNING' : undefined);
  const status: GlobalTaskStatus | undefined = rawStatus
    ? normalizeTaskStatus(String(rawStatus))
    : undefined;

  return (
    <ExecutionLog
      logs={stringLogs.length > 0 ? stringLogs : undefined}
      status={status}
      className={className}
      maxHeight="380px"
      title="Live Agent Telemetry Feed"
    />
  );
};

export { ExecutionLog };
export default TelemetryFeed;
