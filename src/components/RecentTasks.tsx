import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Check,
  Clock,
  ExternalLink,
  GitCommit,
  GitPullRequest,
  Loader2,
  RefreshCw,
  XCircle,
  AlertCircle,
  ArrowRight,
} from 'lucide-react';
import type { TaskHistoryItem } from '../types/api';
import { apiClient } from '../services/apiClient';
import { useTaskContext } from '../context/TaskContext';

export interface RecentTasksProps {
  onNavigate?: (tab: 'workspace' | 'new' | 'activity' | 'review') => void;
  limit?: number;
  repoId?: string;
  className?: string;
}

export const RecentTasks: React.FC<RecentTasksProps> = ({
  onNavigate,
  limit = 10,
  repoId,
  className = '',
}) => {
  const { taskId: activeTaskId, taskStatus, setTaskId } = useTaskContext();
  const [tasks, setTasks] = useState<TaskHistoryItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isMountedRef = useRef(true);

  // Fetch task history from live API endpoint
  const fetchHistory = useCallback(
    async (silent = false) => {
      if (!silent) {
        if (tasks.length === 0) setIsLoading(true);
        else setIsRefreshing(true);
      }
      try {
        setError(null);
        const data = await apiClient.getTaskHistory(limit, repoId);
        if (isMountedRef.current) {
          setTasks(data || []);
        }
      } catch (err: unknown) {
        if (isMountedRef.current) {
          const msg = err instanceof Error ? err.message : 'Failed to load task history';
          setError(msg);
        }
      } finally {
        if (isMountedRef.current) {
          setIsLoading(false);
          setIsRefreshing(false);
        }
      }
    },
    [limit, repoId, tasks.length]
  );

  // Initial load on mount
  useEffect(() => {
    isMountedRef.current = true;
    void fetchHistory(false);
    return () => {
      isMountedRef.current = false;
    };
  }, [fetchHistory]);

  // Re-fetch automatically when global task status transitions (e.g. task completes or fails)
  useEffect(() => {
    if (taskStatus === 'COMPLETED' || taskStatus === 'FAILED' || taskStatus === 'ABORTED') {
      const timer = setTimeout(() => {
        void fetchHistory(true);
      }, 1200);
      return () => clearTimeout(timer);
    }
  }, [taskStatus, fetchHistory]);

  // Periodic polling interval (every 12 seconds) to keep execution statuses live
  useEffect(() => {
    const hasActiveTask =
      taskStatus === 'RUNNING' ||
      tasks.some((t) => t.status === 'RUNNING');

    const intervalMs = hasActiveTask ? 6000 : 15000;
    const interval = setInterval(() => {
      void fetchHistory(true);
    }, intervalMs);

    return () => clearInterval(interval);
  }, [taskStatus, tasks, fetchHistory]);

  const handleSelectTask = (task: TaskHistoryItem) => {
    setTaskId(task.task_id);
    if (!onNavigate) return;

    if (task.status === 'IN_REVIEW') {
      onNavigate('review');
    } else if (task.status === 'RUNNING') {
      onNavigate('activity');
    } else {
      onNavigate('workspace');
    }
  };

  const getPrLabel = (prUrl?: string | null): string => {
    if (!prUrl) return '';
    try {
      const parts = prUrl.split('/');
      const prNum = parts[parts.length - 1];
      return `#PR-${prNum}`;
    } catch {
      return '#PR';
    }
  };

  const getShortRepo = (repoUrl?: string): string => {
    if (!repoUrl) return 'forge';
    try {
      const clean = repoUrl.replace(/\.git$/, '');
      const parts = clean.split('/');
      return parts[parts.length - 1] || 'repo';
    } catch {
      return 'repo';
    }
  };

  return (
    <div className={`pt-2 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-2.5 px-0.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-semibold">
            Recent Tasks
          </span>
          <button
            type="button"
            onClick={() => void fetchHistory(false)}
            disabled={isRefreshing}
            title="Refresh Task History"
            aria-label="Refresh Task History"
            className="text-zinc-400 hover:text-zinc-600 transition-colors p-0.5 rounded focus:outline-none"
          >
            <RefreshCw
              className={`h-3 w-3 ${isRefreshing ? 'animate-spin text-zinc-600' : ''}`}
            />
          </button>
        </div>
        <span
          onClick={() => onNavigate?.('activity')}
          className="font-mono text-xs text-zinc-900 font-medium hover:underline cursor-pointer transition-colors"
        >
          View Live Stream
        </span>
      </div>

      {/* Task List / Skeletons / Empty State */}
      <div className="flex flex-col gap-2">
        {isLoading && tasks.length === 0 ? (
          // Skeleton Loaders
          [1, 2, 3].map((idx) => (
            <div
              key={idx}
              className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-2xs flex items-center justify-between gap-3 animate-pulse"
            >
              <div className="flex items-center gap-3 min-w-0 flex-1">
                <div className="w-8 h-8 rounded-lg bg-zinc-100 shrink-0" />
                <div className="min-w-0 flex-1 space-y-1.5">
                  <div className="h-3.5 bg-zinc-100 rounded w-3/4" />
                  <div className="h-2.5 bg-zinc-100 rounded w-1/3" />
                </div>
              </div>
              <div className="w-16 h-5 rounded-full bg-zinc-100 shrink-0" />
            </div>
          ))
        ) : error && tasks.length === 0 ? (
          // Error State
          <div className="bg-rose-50 border border-rose-200 rounded-xl p-3 text-center">
            <p className="text-xs text-rose-700 font-medium">Failed to load task history</p>
            <button
              type="button"
              onClick={() => void fetchHistory(false)}
              className="mt-1 text-[11px] font-mono text-rose-600 underline hover:text-rose-800"
            >
              Click to retry
            </button>
          </div>
        ) : tasks.length === 0 ? (
          // Empty State
          <div className="bg-white rounded-xl p-6 border border-dashed border-zinc-200 text-center space-y-1.5">
            <GitCommit className="h-6 w-6 text-zinc-300 mx-auto" />
            <p className="text-xs font-medium text-zinc-700">No executed tasks yet</p>
            <p className="text-[11px] text-zinc-400">
              Submit an autonomous task above to start pipeline orchestration.
            </p>
          </div>
        ) : (
          // Real Dynamic Tasks
          tasks.map((task) => {
            const isSelected = activeTaskId === task.task_id;
            const normStatus = (task.status || 'RUNNING').toUpperCase();

            // Status presentation configuration
            const statusConfig = {
              MERGED: {
                badgeBg: 'bg-emerald-50 text-emerald-700 border-emerald-200',
                icon: <Check className="h-3 w-3 text-emerald-600 stroke-[2.5]" />,
                label: 'Merged',
                leadIcon: <GitPullRequest className="h-4 w-4 text-emerald-600" />,
                leadBg: 'bg-emerald-50',
              },
              IN_REVIEW: {
                badgeBg: 'bg-zinc-100 text-zinc-700 border-zinc-200',
                icon: <Clock className="h-3 w-3 text-zinc-600 stroke-[2]" />,
                label: 'In Review',
                leadIcon: <Clock className="h-4 w-4 text-zinc-600" />,
                leadBg: 'bg-zinc-100',
              },
              FAILED: {
                badgeBg: 'bg-rose-50 text-rose-700 border-rose-200',
                icon: <XCircle className="h-3 w-3 text-rose-600 stroke-[2]" />,
                label: 'Failed',
                leadIcon: <AlertCircle className="h-4 w-4 text-rose-600" />,
                leadBg: 'bg-rose-50',
              },
              RUNNING: {
                badgeBg: 'bg-indigo-50 text-indigo-700 border-indigo-200',
                icon: <Loader2 className="h-3 w-3 text-indigo-600 animate-spin" />,
                label: 'Running',
                leadIcon: <RefreshCw className="h-4 w-4 text-indigo-600 animate-spin" />,
                leadBg: 'bg-indigo-50',
              },
            }[normStatus] || {
              badgeBg: 'bg-zinc-100 text-zinc-700 border-zinc-200',
              icon: <Clock className="h-3 w-3 text-zinc-600" />,
              label: normStatus,
              leadIcon: <GitCommit className="h-4 w-4 text-zinc-600" />,
              leadBg: 'bg-zinc-100',
            };

            const prLabel = getPrLabel(task.pr_url);

            return (
              <div
                key={task.task_id}
                onClick={() => handleSelectTask(task)}
                className={`group bg-white rounded-xl p-3 border transition-all duration-150 cursor-pointer flex items-center justify-between gap-3 shadow-2xs hover:shadow hover:border-zinc-300 ${
                  isSelected ? 'border-zinc-900 ring-1 ring-zinc-900/10' : 'border-zinc-200/80'
                }`}
              >
                {/* Task Details */}
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <div
                    className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 transition-colors ${statusConfig.leadBg}`}
                  >
                    {statusConfig.leadIcon}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-zinc-900 truncate group-hover:text-zinc-950 transition-colors">
                      {task.title || 'Autonomous Task Run'}
                    </p>
                    <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                      {task.pr_url ? (
                        <a
                          href={task.pr_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          title="Open Pull Request in GitHub"
                          className="font-mono text-[11px] text-indigo-600 hover:text-indigo-800 hover:underline inline-flex items-center gap-0.5 font-medium"
                        >
                          <span>{prLabel || '#PR'}</span>
                          <ExternalLink className="h-2.5 w-2.5 inline shrink-0" />
                        </a>
                      ) : (
                        <span className="font-mono text-[11px] text-zinc-500">
                          #{task.branch_name ? task.branch_name.replace('forge/', '') : task.task_id.slice(0, 8)}
                        </span>
                      )}
                      <span className="font-mono text-[11px] text-zinc-400">·</span>
                      <span className="font-mono text-[11px] text-zinc-500">
                        {getShortRepo(task.repo_url)}
                      </span>
                      <span className="font-mono text-[11px] text-zinc-400">·</span>
                      <span className="font-mono text-[11px] text-zinc-400">
                        {task.relative_time || 'recently'}
                      </span>
                    </div>
                  </div>
                </div>

                {/* Status Badge & Action */}
                <div className="flex items-center gap-2 shrink-0">
                  <div
                    className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full border text-[11px] font-mono font-medium ${statusConfig.badgeBg}`}
                  >
                    {statusConfig.icon}
                    <span>{statusConfig.label}</span>
                  </div>
                  <ArrowRight className="h-3.5 w-3.5 text-zinc-300 group-hover:text-zinc-600 transition-colors hidden sm:block" />
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export default RecentTasks;
