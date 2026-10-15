import React, { useEffect, useRef, useMemo } from 'react';
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
  Radio,
} from 'lucide-react';
import type { TaskHistoryItem } from '../types/api';
import { useTaskContext } from '../context/TaskContext';
import { useTaskHistory } from '../hooks/useTaskHistory';

export interface RecentTasksProps {
  onNavigate?: (tab: 'workspace' | 'new' | 'activity' | 'review') => void;
  limit?: number;
  repoId?: string;
  className?: string;
}

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

const formatExecutionDuration = (
  durationSeconds?: number | null,
  startTime?: string | null,
  endTime?: string | null,
  createdAt?: string | null,
  status?: string
): string => {
  if (typeof durationSeconds === 'number' && durationSeconds > 0) {
    if (durationSeconds < 60) return `${durationSeconds}s`;
    const mins = Math.floor(durationSeconds / 60);
    const secs = durationSeconds % 60;
    return `${mins}m ${secs}s`;
  }
  if (startTime && endTime) {
    const diff = Math.max(
      0,
      Math.floor((new Date(endTime).getTime() - new Date(startTime).getTime()) / 1000)
    );
    if (diff > 0) {
      if (diff < 60) return `${diff}s`;
      const mins = Math.floor(diff / 60);
      const secs = diff % 60;
      return `${mins}m ${secs}s`;
    }
  }
  if (status === 'RUNNING' && (startTime || createdAt)) {
    const refTime = startTime || createdAt;
    const diff = Math.max(
      0,
      Math.floor((Date.now() - new Date(refTime!).getTime()) / 1000)
    );
    if (diff < 60) return `${diff}s`;
    const mins = Math.floor(diff / 60);
    const secs = diff % 60;
    return `${mins}m ${secs}s`;
  }
  return '';
};

/**
 * Memoized individual task row to prevent re-renders when other items update
 */
interface TaskRowProps {
  task: TaskHistoryItem;
  isSelected: boolean;
  onSelect: (task: TaskHistoryItem) => void;
  onNavigateTab: (tab: 'workspace' | 'new' | 'activity' | 'review') => void;
}

const TaskRow: React.FC<TaskRowProps> = React.memo(
  ({ task, isSelected, onSelect, onNavigateTab }) => {
    const normStatus = (task.status || 'UNKNOWN').toUpperCase();

    const statusConfig = useMemo(() => {
      switch (normStatus) {
        case 'MERGED':
          return {
            badgeBg: 'bg-emerald-50 text-emerald-700 border-emerald-200',
            icon: <Check className="h-3 w-3 text-emerald-600 stroke-[2.5]" />,
            label: 'Merged',
            leadIcon: <GitPullRequest className="h-4 w-4 text-emerald-600" />,
            leadBg: 'bg-emerald-50',
          };
        case 'COMPLETED':
          return {
            badgeBg: 'bg-emerald-50 text-emerald-700 border-emerald-200',
            icon: <Check className="h-3 w-3 text-emerald-600 stroke-[2.5]" />,
            label: 'Completed',
            leadIcon: <Check className="h-4 w-4 text-emerald-600 stroke-[2.5]" />,
            leadBg: 'bg-emerald-50',
          };
        case 'IN_REVIEW':
          return {
            badgeBg: 'bg-amber-50 text-amber-700 border-amber-200',
            icon: <Clock className="h-3 w-3 text-amber-600 stroke-[2]" />,
            label: 'In Review',
            leadIcon: <Clock className="h-4 w-4 text-amber-600" />,
            leadBg: 'bg-amber-50',
          };
        case 'FAILED':
          return {
            badgeBg: 'bg-rose-50 text-rose-700 border-rose-200',
            icon: <XCircle className="h-3 w-3 text-rose-600 stroke-[2]" />,
            label: 'Failed',
            leadIcon: <AlertCircle className="h-4 w-4 text-rose-600" />,
            leadBg: 'bg-rose-50',
          };
        case 'ABORTED':
          return {
            badgeBg: 'bg-zinc-100 text-zinc-700 border-zinc-200',
            icon: <XCircle className="h-3 w-3 text-zinc-500" />,
            label: 'Aborted',
            leadIcon: <XCircle className="h-4 w-4 text-zinc-500" />,
            leadBg: 'bg-zinc-100',
          };
        case 'RUNNING':
          return {
            badgeBg: 'bg-indigo-50 text-indigo-700 border-indigo-200',
            icon: <Loader2 className="h-3 w-3 text-indigo-600 animate-spin" />,
            label: 'Running',
            leadIcon: <RefreshCw className="h-4 w-4 text-indigo-600 animate-spin" />,
            leadBg: 'bg-indigo-50',
          };
        default:
          return {
            badgeBg: 'bg-zinc-100 text-zinc-700 border-zinc-200',
            icon: <Clock className="h-3 w-3 text-zinc-600" />,
            label: normStatus,
            leadIcon: <GitCommit className="h-4 w-4 text-zinc-600" />,
            leadBg: 'bg-zinc-100',
          };
      }
    }, [normStatus]);

    const prLabel = getPrLabel(task.pr_url);
    const durationStr = formatExecutionDuration(
      task.duration_seconds,
      task.start_time,
      task.end_time,
      task.created_at,
      normStatus
    );

    return (
      <div
        onClick={() => onSelect(task)}
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
              {/* Live Stream / PR link */}
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
              ) : normStatus === 'RUNNING' ? (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelect(task);
                    onNavigateTab('activity');
                  }}
                  className="font-mono text-[11px] text-indigo-600 hover:text-indigo-800 hover:underline inline-flex items-center gap-1 font-medium"
                  title="View Live Stream"
                >
                  <Radio className="h-2.5 w-2.5 text-indigo-600 animate-pulse" />
                  <span>Live Stream</span>
                </button>
              ) : normStatus === 'IN_REVIEW' ? (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelect(task);
                    onNavigateTab('review');
                  }}
                  className="font-mono text-[11px] text-amber-700 hover:text-amber-900 hover:underline inline-flex items-center gap-0.5 font-medium"
                  title="Review Patch Changes"
                >
                  <span>Review Diff</span>
                  <ArrowRight className="h-2 w-2 inline" />
                </button>
              ) : (
                <span className="font-mono text-[11px] text-zinc-500">
                  #{task.branch_name ? task.branch_name.replace('forge/', '') : task.task_id.slice(0, 8)}
                </span>
              )}

              <span className="font-mono text-[11px] text-zinc-400">·</span>
              <span className="font-mono text-[11px] text-zinc-500">
                {getShortRepo(task.repo_url)}
              </span>

              {durationStr && (
                <>
                  <span className="font-mono text-[11px] text-zinc-400">·</span>
                  <span className="inline-flex items-center gap-0.5 font-mono text-[11px] text-zinc-500">
                    <Clock className="h-2.5 w-2.5 text-zinc-400" />
                    <span>{durationStr}</span>
                  </span>
                </>
              )}

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
  }
);
TaskRow.displayName = 'TaskRow';

/**
 * Internal unmemoized component
 */
const RecentTasksComponent: React.FC<RecentTasksProps> = ({
  onNavigate,
  limit = 10,
  repoId,
  className = '',
}) => {
  const { taskId: activeTaskId, setTaskId } = useTaskContext();

  // Stable hook consumption with SWR caching, in-flight deduplication, and ref-guarded silent revalidations
  const { tasks, isLoading, isRefreshing, isSkeletonTimedOut, refetch } = useTaskHistory({
    limit,
    repoId,
  });

  // Keep navigation callback stable in a ref to avoid invalidating memoization when parents re-render
  const onNavigateRef = useRef(onNavigate);
  useEffect(() => {
    onNavigateRef.current = onNavigate;
  }, [onNavigate]);

  const handleSelectTask = (task: TaskHistoryItem) => {
    setTaskId(task.task_id);
    const nav = onNavigateRef.current;
    if (!nav) return;

    if (task.status === 'IN_REVIEW') {
      nav('review');
    } else if (task.status === 'RUNNING') {
      nav('activity');
    } else {
      nav('workspace');
    }
  };

  const handleNavigateTab = (tab: 'workspace' | 'new' | 'activity' | 'review') => {
    onNavigateRef.current?.(tab);
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
            onClick={() => void refetch(false)}
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
        <button
          type="button"
          onClick={() => handleNavigateTab('activity')}
          className="font-mono text-xs text-zinc-900 font-medium hover:underline cursor-pointer transition-colors bg-transparent border-none p-0"
        >
          View Live Stream
        </button>
      </div>

      {/* Task List / Skeletons / Clean Empty State */}
      <div className="flex flex-col gap-2">
        {isLoading && tasks.length === 0 && !isSkeletonTimedOut ? (
          // Subtle, animated skeleton rows for maximum of 3 seconds
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
        ) : tasks.length === 0 ? (
          // Clean Resilient Empty State
          <div className="bg-white rounded-xl p-6 border border-dashed border-zinc-200 text-center space-y-1.5">
            <GitCommit className="h-6 w-6 text-zinc-300 mx-auto" />
            <p className="text-xs font-medium text-zinc-700">No executed tasks yet</p>
            <p className="text-[11px] text-zinc-400">
              Submit an autonomous task above to start pipeline orchestration.
            </p>
          </div>
        ) : (
          // Real Dynamic Tasks with real-time badges, duration, and working navigation links
          tasks.map((task) => (
            <TaskRow
              key={task.task_id}
              task={task}
              isSelected={activeTaskId === task.task_id}
              onSelect={handleSelectTask}
              onNavigateTab={handleNavigateTab}
            />
          ))
        )}
      </div>
    </div>
  );
};

/**
 * PILLAR 2: PREVENTING UNNECESSARY PARENT RERENDERS
 * Wrap the RecentTasks component in React.memo with a stable props comparison,
 * isolating it from scroll-driven layout shifts and re-renders in parent components.
 */
export const RecentTasks = React.memo<RecentTasksProps>(
  RecentTasksComponent,
  (prevProps, nextProps) => {
    return (
      prevProps.limit === nextProps.limit &&
      prevProps.repoId === nextProps.repoId &&
      prevProps.className === nextProps.className
      // onNavigate function identity is ignored because it's safely anchored in onNavigateRef
    );
  }
);
RecentTasks.displayName = 'RecentTasks';

export default RecentTasks;
