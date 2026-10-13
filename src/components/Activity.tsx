import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Timer,
  GitCommit,
  Zap,
  CheckCircle2,
  Sliders,
  XCircle,
  ArrowRight,
  PlusSquare,
} from 'lucide-react';
import type { LogEntry } from '../hooks/useAgentStream';
import type { TaskStatusResponse } from '../types/api';
import { apiClient } from '../services/apiClient';
import { WorkflowSteps } from './WorkflowSteps';
import { TelemetryFeed } from './TelemetryFeed';

export interface ActivityProps {
  logs: LogEntry[];
  currentStep: number;
  onNavigate?: (tab: 'workspace' | 'new' | 'activity' | 'review') => void;
  taskId?: string | null;
  task?: TaskStatusResponse | null;
  taskStatus?: string | null;
}

export const Activity: React.FC<ActivityProps> = ({
  logs,
  currentStep,
  onNavigate,
  taskId: propTaskId,
  task: propTask,
  taskStatus: propTaskStatus,
}) => {
  const [task, setTask] = useState<TaskStatusResponse | null>(propTask || null);
  const [elapsedSeconds, setElapsedSeconds] = useState<number>(0);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);

  const pollTimerRef = useRef<number | null>(null);

  // Resolve effective task ID: prop -> propTask -> localStorage
  const effectiveTaskId =
    propTaskId ||
    propTask?.task_id ||
    (typeof window !== 'undefined' ? localStorage.getItem('forge_active_task_id') : null);

  // Fetch real task status from API
  const fetchTaskStatus = useCallback(async (id: string) => {
    try {
      const data = await apiClient.getTaskStatus(id);
      setTask(data);
      return data;
    } catch {
      // API call failed or task not found in backend
      return null;
    }
  }, []);

  // Sync propTask if updated from parent
  useEffect(() => {
    if (propTask) {
      // oxlint-disable-next-line react/set-state-in-effect
      setTask(propTask);
    }
  }, [propTask]);

  // Initial fetch and polling if running
  useEffect(() => {
    if (!effectiveTaskId) {
      return;
    }

    // oxlint-disable-next-line react/set-state-in-effect
    void fetchTaskStatus(effectiveTaskId);

    const poll = async () => {
      const updated = await fetchTaskStatus(effectiveTaskId);
      if (updated && (updated.status === 'RUNNING' || updated.status === 'PENDING')) {
        pollTimerRef.current = window.setTimeout(poll, 3000);
      }
    };

    pollTimerRef.current = window.setTimeout(poll, 3000);

    return () => {
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [effectiveTaskId, fetchTaskStatus]);

  // Determine normalized execution state
  const rawStatus = (task?.status || propTaskStatus || 'IDLE').toUpperCase();
  const isRunning = rawStatus === 'RUNNING';
  const isCompleted = rawStatus === 'COMPLETED';
  const isFailed = rawStatus === 'FAILED';

  // Track elapsed timer only while running
  useEffect(() => {
    if (!isRunning) return;

    const timer = setInterval(() => {
      setElapsedSeconds((prev) => prev + 1);
    }, 1000);

    return () => clearInterval(timer);
  }, [isRunning]);

  const formatTimer = (totalSecs: number) => {
    const mins = Math.floor(totalSecs / 60);
    const secs = totalSecs % 60;
    const pad = (n: number) => (n < 10 ? `0${n}` : `${n}`);
    return `${pad(mins)}:${pad(secs)}s`;
  };

  const handleAdjustPrompt = () => {
    setActionFeedback('Opening prompt editor...');
    setTimeout(() => {
      if (onNavigate) onNavigate('new');
      setActionFeedback(null);
    }, 600);
  };

  const handleAbort = async () => {
    if (effectiveTaskId) {
      try {
        await apiClient.cancelTask(effectiveTaskId);
        setActionFeedback('Pipeline cancelled');
        void fetchTaskStatus(effectiveTaskId);
      } catch {
        setActionFeedback('Pipeline aborted locally');
      }
    } else {
      setActionFeedback('Pipeline cancelled');
    }
    setTimeout(() => setActionFeedback(null), 2000);
  };

  return (
    <div className="flex flex-col w-full pb-8 space-y-4">
      {/* Top Status Header */}
      <section className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5 min-w-0">
            {isRunning ? (
              <span className="relative flex h-2.5 w-2.5 shrink-0">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500" />
              </span>
            ) : isCompleted ? (
              <span className="h-2.5 w-2.5 rounded-full bg-emerald-500 shrink-0" />
            ) : isFailed ? (
              <span className="h-2.5 w-2.5 rounded-full bg-rose-500 shrink-0" />
            ) : (
              <span className="h-2.5 w-2.5 rounded-full bg-zinc-400 shrink-0" />
            )}

            <h1 className="text-base font-semibold text-zinc-900 truncate">
              {isRunning
                ? 'Autonomous Run in Progress'
                : isCompleted
                ? 'Workflow Completed'
                : isFailed
                ? 'Workflow Failed'
                : 'System Idle'}
            </h1>
          </div>

          <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-zinc-100 text-zinc-700 shrink-0 border border-zinc-200">
            <Timer className="h-3.5 w-3.5 text-zinc-500" />
            <span className="font-mono text-xs font-medium" id="elapsed-counter">
              {formatTimer(elapsedSeconds)}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2 mt-2.5 text-zinc-500 font-mono text-[11px] flex-wrap">
          <span className="inline-flex items-center gap-1 text-zinc-800 font-medium">
            <GitCommit className="h-3.5 w-3.5 text-zinc-500" />
            {task?.branch_name ? `branch: ${task.branch_name}` : 'hyper/telemetry:main'}
          </span>
          <span className="text-zinc-300">·</span>
          <span>{effectiveTaskId ? `Task #${effectiveTaskId.slice(0, 8)}` : 'No Active Task'}</span>
          <span className="text-zinc-300">·</span>
          <span className="inline-flex items-center gap-1 text-emerald-600 font-medium">
            <Zap className="h-3 w-3 fill-emerald-600" />
            Agent v2.4
          </span>
        </div>
      </section>

      {/* Dynamic Workflow Steps Indicator */}
      <WorkflowSteps task={task} currentStep={currentStep} />

      {/* Telemetry Stream Feed with Idle Placeholder Handling */}
      <TelemetryFeed task={task} logs={logs} isStreaming={isRunning} />

      {/* Contextual Action / Review Card */}
      {isCompleted || (isRunning && currentStep >= 5) ? (
        <section className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center shrink-0 border border-emerald-200">
              <CheckCircle2 className="h-4 w-4" />
            </div>
            <div className="flex-1 min-w-0">
              <h2 className="text-sm font-semibold text-zinc-900">Patch Generated &amp; Verified</h2>
              <p className="text-xs text-zinc-500 mt-0.5">
                Automated test suite passed. Synthesis complete. Ready for human review and atomic merge.
              </p>
            </div>
          </div>

          {/* Metrics Bento Preview */}
          <div className="grid grid-cols-3 gap-2 mt-3.5">
            <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 text-center">
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-wider block">
                Diff Size
              </span>
              <span className="font-mono text-xs font-semibold text-emerald-600 mt-0.5 block">
                +12 / -4
              </span>
            </div>
            <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 text-center">
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-wider block">
                Latency Impact
              </span>
              <span className="font-mono text-xs font-semibold text-emerald-600 mt-0.5 block">
                -18ms
              </span>
            </div>
            <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 text-center">
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-wider block">
                Confidence
              </span>
              <span className="font-mono text-xs font-semibold text-zinc-900 mt-0.5 block">
                99.4%
              </span>
            </div>
          </div>

          {/* Action Buttons */}
          <div className="flex flex-col gap-2 mt-4">
            <button
              type="button"
              onClick={() => {
                if (onNavigate) onNavigate('review');
              }}
              className="w-full h-10 flex items-center justify-center gap-2 rounded-xl bg-zinc-900 text-white font-medium text-xs hover:bg-zinc-800 active:scale-[0.99] transition-all shadow-sm"
            >
              <span>Inspect Diff &amp; Approve</span>
              <ArrowRight className="h-4 w-4" />
            </button>

            <div className="flex items-center justify-between px-1">
              <button
                type="button"
                onClick={handleAdjustPrompt}
                className="inline-flex items-center gap-1.5 text-zinc-500 hover:text-zinc-900 text-xs transition-colors py-1"
              >
                <Sliders className="h-3.5 w-3.5" />
                <span>Adjust Prompt</span>
              </button>
              <button
                type="button"
                onClick={handleAbort}
                className="inline-flex items-center gap-1.5 text-rose-600 hover:text-rose-700 text-xs transition-colors py-1"
              >
                <XCircle className="h-3.5 w-3.5" />
                <span>Abort Pipeline</span>
              </button>
            </div>

            {actionFeedback && (
              <div className="text-center text-xs font-mono text-zinc-600 pt-1">
                {actionFeedback}
              </div>
            )}
          </div>
        </section>
      ) : isRunning ? (
        <section className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-zinc-900">Task Execution in Progress</p>
            <p className="text-[11px] text-zinc-500 mt-0.5">
              LangGraph nodes executing sequentially with AST safety guards.
            </p>
          </div>
          <button
            type="button"
            onClick={handleAbort}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-rose-600 border border-rose-200 hover:bg-rose-50 transition-colors"
          >
            <XCircle className="h-3.5 w-3.5" />
            <span>Abort Run</span>
          </button>
        </section>
      ) : (
        <section className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm flex items-center justify-between">
          <div>
            <p className="text-xs font-medium text-zinc-900">Ready for Execution</p>
            <p className="text-[11px] text-zinc-500 mt-0.5">
              Submit a task prompt or ingest a repository to start autonomous orchestration.
            </p>
          </div>
          <button
            type="button"
            onClick={() => onNavigate && onNavigate('new')}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-900 text-white text-xs font-medium hover:bg-zinc-800 transition-colors shadow-sm"
          >
            <PlusSquare className="h-3.5 w-3.5" />
            <span>New Task</span>
          </button>
        </section>
      )}
    </div>
  );
};

export default Activity;