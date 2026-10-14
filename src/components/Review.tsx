import React, { useState, useEffect, useMemo } from 'react';
import {
  GitPullRequest,
  RotateCcw,
  Loader2,
  FolderGit2,
  XCircle,
  ArrowRight,
  Sparkles,
  GitBranch,
} from 'lucide-react';
import type { TaskStatusResponse } from '../types/api';
import { TaskCompletionCard } from './TaskCompletionCard';
import { TaskMetrics } from './TaskMetrics';
import { UnifiedDiffReview } from './UnifiedDiffReview';
import { useTask } from '../context/TaskContext';

export interface ReviewProps {
  onNavigate?: (tab: 'workspace' | 'new' | 'activity' | 'review') => void;
  taskId?: string | null;
  task?: TaskStatusResponse | null;
}

export const Review: React.FC<ReviewProps> = ({
  onNavigate,
  taskId: propTaskId,
  task: propTask,
}) => {
  const globalTask = useTask();
  const [error] = useState<string | null>(null);

  // Review interaction state
  const [isSubmittingApproval, setIsSubmittingApproval] = useState<boolean>(false);
  const [isSubmittingRejection, setIsSubmittingRejection] = useState<boolean>(false);
  const [rejectionModalOpen, setRejectionModalOpen] = useState<boolean>(false);
  const [rejectionFeedback, setRejectionFeedback] = useState<string>('');
  const [actionNotice, setActionNotice] = useState<{
    type: 'success' | 'error' | 'info';
    message: string;
  } | null>(null);

  // Resolve effective task ID: prop -> propTask -> globalTask -> localStorage
  const effectiveTaskId =
    propTaskId ||
    propTask?.task_id ||
    globalTask.taskId ||
    (typeof window !== 'undefined' ? localStorage.getItem('forge_active_task_id') : null);

  const showNotice = (type: 'success' | 'error' | 'info', message: string) => {
    setActionNotice({ type, message });
    setTimeout(() => {
      setActionNotice(null);
    }, 4500);
  };

  // Single source of truth: propTask override, or unified globalTask.task
  const task: TaskStatusResponse | null = useMemo(() => {
    if (propTask) return propTask;
    if (!effectiveTaskId && !globalTask.taskId) return null;
    return {
      task_id: effectiveTaskId || globalTask.taskId || '',
      status: (globalTask.status as any) || 'IDLE',
      repo_id: globalTask.currentRepo?.id || 'default-repo',
      prompt: globalTask.task?.prompt || null,
      branch_name: globalTask.currentRepo?.branch || 'main',
      pr_created: Boolean(globalTask.prUrl || globalTask.status === 'COMPLETED'),
      pr_url: globalTask.prUrl,
      merged: false,
      retry_count: globalTask.retry_count ?? 0,
      current_node: globalTask.activeStep,
      active_node: globalTask.activeStep,
      completed_nodes: globalTask.completedSteps,
      execution_plan: globalTask.plan,
      plan: globalTask.plan,
      patch: globalTask.currentDiff,
      generated_diff: globalTask.currentDiff,
      metrics: globalTask.metrics,
      duration_seconds: globalTask.durationSeconds,
      start_time: globalTask.startTime,
      end_time: globalTask.endTime,
      test_suite_passed: globalTask.testSuitePassed,
      target_scope: globalTask.targetScope,
      pr_target: globalTask.prTarget,
    };
  }, [propTask, effectiveTaskId, globalTask]);

  // Sync fresh task state on mount
  useEffect(() => {
    if (effectiveTaskId) {
      void globalTask.refreshTask(true);
    }
  }, [effectiveTaskId, globalTask]);

  // Handler: Human reviewer approves patch
  const handleApprove = async () => {
    if (!effectiveTaskId || isSubmittingApproval) return;

    setIsSubmittingApproval(true);
    showNotice('info', 'Submitting reviewer approval to LangGraph orchestrator...');

    try {
      await globalTask.approveTask();
      showNotice('success', 'Approval registered. Generating upstream Pull Request...');
      await globalTask.refreshTask(true);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Approval failed';
      showNotice('error', msg);
    } finally {
      setIsSubmittingApproval(false);
    }
  };

  // Handler: Human reviewer rejects and requests changes
  const handleReject = async () => {
    if (!effectiveTaskId || isSubmittingRejection) return;

    if (!rejectionFeedback.trim()) {
      showNotice('error', 'Please provide feedback explaining the requested changes.');
      return;
    }

    setIsSubmittingRejection(true);
    showNotice('info', 'Re-routing task to implementer with feedback...');

    try {
      await globalTask.rejectTask(rejectionFeedback.trim());
      setRejectionModalOpen(false);
      setRejectionFeedback('');
      showNotice('info', 'Revisions requested. Task resumed in repair loop.');
      await globalTask.refreshTask(true);

      if (onNavigate) {
        onNavigate('activity');
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Rejection failed';
      showNotice('error', msg);
    } finally {
      setIsSubmittingRejection(false);
    }
  };

  // =========================================================================
  // CONDITION A: UNINITIALIZED / IDLE / NO REPOSITORY INGESTED
  // =========================================================================
  const isIdleOrUninitialized =
    !effectiveTaskId ||
    !task ||
    task.status === 'IDLE' ||
    !task.repo_id ||
    task.repo_id === 'default-repo';

  if (globalTask.isSubmitting && !task) {
    return (
      <div className="flex flex-col items-center justify-center p-12 bg-white rounded-2xl border border-zinc-200 text-center space-y-4">
        <Loader2 className="h-8 w-8 text-zinc-500 animate-spin" />
        <p className="text-xs font-medium text-zinc-600 font-mono">
          Querying backend task status &amp; verified PR state...
        </p>
      </div>
    );
  }

  if (isIdleOrUninitialized) {
    return (
      <div className="flex flex-col w-full pb-8 space-y-4">
        {/* Idle State Card - Hides fake success banner completely */}
        <div className="bg-white rounded-2xl p-8 border border-zinc-200/90 shadow-sm flex flex-col items-center text-center space-y-4">
          <div className="h-14 w-14 rounded-2xl bg-zinc-100 border border-zinc-200 flex items-center justify-center text-zinc-400">
            <FolderGit2 className="h-7 w-7" />
          </div>

          <div className="max-w-md space-y-1.5">
            <h2 className="text-base font-bold text-zinc-900 tracking-tight">
              No Repository Ingested
            </h2>
            <p className="text-xs text-zinc-500 leading-relaxed">
              No active repository has been ingested or executed. To generate code diffs, run verification test suites, and create Pull Requests, ingest a target repository and dispatch an autonomous task.
            </p>
          </div>

          <div className="flex items-center gap-3 pt-2">
            <button
              type="button"
              onClick={() => onNavigate?.('workspace')}
              className="h-9 px-4 bg-zinc-900 hover:bg-zinc-800 text-white text-xs font-semibold rounded-xl shadow-xs flex items-center gap-2 transition"
            >
              <span>Go to Workspace</span>
              <ArrowRight className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => onNavigate?.('new')}
              className="h-9 px-4 bg-white hover:bg-zinc-50 text-zinc-700 text-xs font-medium rounded-xl border border-zinc-200 shadow-xs flex items-center gap-2 transition"
            >
              <Sparkles className="h-3.5 w-3.5 text-zinc-500" />
              <span>Create New Task</span>
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Derived properties from real task status
  const isRunning = task.status === 'RUNNING' || task.status === 'PENDING';
  const isFailed = task.status === 'FAILED';

  return (
    <div className="flex flex-col w-full pb-8 space-y-4">
      {/* Toast Notice */}
      {actionNotice && (
        <div
          className={`p-3 rounded-xl text-xs font-medium flex items-center justify-between border shadow-sm ${
            actionNotice.type === 'success'
              ? 'bg-emerald-50 text-emerald-900 border-emerald-200'
              : actionNotice.type === 'error'
              ? 'bg-rose-50 text-rose-900 border-rose-200'
              : 'bg-zinc-50 text-zinc-900 border-zinc-200'
          }`}
        >
          <span>{actionNotice.message}</span>
        </div>
      )}

      {/* Backend fetch error */}
      {error && (
        <div className="p-3 rounded-xl text-xs font-medium flex items-center justify-between border bg-rose-50 text-rose-900 border-rose-200 shadow-sm">
          <span>Error loading task: {error}</span>
          <button
            type="button"
            onClick={() => effectiveTaskId && void globalTask.refreshTask(false)}
            className="underline font-semibold ml-2 hover:text-rose-950"
          >
            Retry
          </button>
        </div>
      )}

      {/* =========================================================================
          CONDITION B / C: COMPLETED TASK (TaskCompletionCard handles success and retry-pr)
          ========================================================================= */}
      {task.status === 'COMPLETED' && (
        <TaskCompletionCard
          task={task}
          onPRCreated={(newPrUrl) => {
            globalTask.setPrUrl(newPrUrl);
            globalTask.setTaskStatus('COMPLETED');
            showNotice('success', `GitHub Pull Request created: ${newPrUrl}`);
          }}
          onTaskUpdated={(_updated) => {
            void globalTask.refreshTask(true);
          }}
        />
      )}

      {/* =========================================================================
          CONDITION D: RUNNING OR PENDING EXECUTION STATE
          ========================================================================= */}
      {isRunning && (
        <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4 flex items-center justify-between shadow-sm animate-pulse">
          <div className="flex items-center gap-2.5">
            <Loader2 className="h-5 w-5 text-amber-600 animate-spin shrink-0" />
            <div>
              <h3 className="text-xs font-semibold text-amber-950">
                Autonomous Execution In Progress
              </h3>
              <p className="text-[11px] text-amber-800">
                Agent is currently planning, patching, and verifying test suites in sandbox.
              </p>
            </div>
          </div>
          <span className="font-mono text-[11px] text-amber-800 bg-white border border-amber-300 px-2.5 py-1 rounded-lg font-bold">
            {task.status}
          </span>
        </div>
      )}

      {/* =========================================================================
          CONDITION E: FAILED EXECUTION STATE
          ========================================================================= */}
      {isFailed && (
        <div className="bg-rose-50 border border-rose-200 rounded-2xl p-4 flex items-center justify-between shadow-sm">
          <div className="flex items-center gap-2.5">
            <XCircle className="h-5 w-5 text-rose-600 shrink-0" />
            <div>
              <h3 className="text-xs font-semibold text-rose-950">
                Task Execution Failed
              </h3>
              <p className="text-[11px] text-rose-800">
                {task.error || 'The autonomous loop stopped or encountered a critical error.'}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => onNavigate?.('workspace')}
            className="text-xs font-medium text-rose-700 hover:text-rose-900 underline"
          >
            Return to Workspace
          </button>
        </div>
      )}

      {/* Real Task Details Card */}
      <div className="bg-white rounded-2xl p-5 border border-zinc-200/90 shadow-sm flex flex-col space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[11px] uppercase tracking-wider text-zinc-500 font-semibold">
                TASK ID:
              </span>
              <span className="font-mono text-xs font-semibold text-zinc-800 truncate">
                {task.task_id}
              </span>
            </div>
            <h2 className="text-sm font-semibold text-zinc-900 leading-snug">
              {task.prompt || 'Autonomous code modification task'}
            </h2>
          </div>

          <div className="shrink-0 flex items-center gap-1.5">
            <span
              className={`font-mono text-[11px] px-2.5 py-1 rounded-lg border font-bold ${
                task.status === 'COMPLETED'
                  ? 'bg-emerald-50 text-emerald-800 border-emerald-200'
                  : task.status === 'FAILED'
                  ? 'bg-rose-50 text-rose-800 border-rose-200'
                  : task.status === 'RUNNING'
                  ? 'bg-amber-50 text-amber-800 border-amber-200'
                  : 'bg-zinc-100 text-zinc-800 border-zinc-200'
              }`}
            >
              {task.status}
            </span>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-2 text-xs">
          <div className="bg-zinc-50 rounded-xl p-3 border border-zinc-200/70">
            <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500 block mb-1">
              REPOSITORY
            </span>
            <span className="font-mono font-medium text-zinc-800 truncate block">
              {task.repo_id || 'unassigned'}
            </span>
          </div>

          <div className="bg-zinc-50 rounded-xl p-3 border border-zinc-200/70">
            <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500 block mb-1">
              BRANCH
            </span>
            <div className="flex items-center gap-1 font-mono font-medium text-zinc-800 truncate">
              <GitBranch className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
              <span className="truncate">{task.branch_name || 'main'}</span>
            </div>
          </div>

          <div className="bg-zinc-50 rounded-xl p-3 border border-zinc-200/70">
            <span className="text-[10px] font-mono uppercase tracking-wider text-zinc-500 block mb-1">
              PR VERIFICATION
            </span>
            <span
              className={`font-mono font-semibold ${
                task.pr_created ? 'text-emerald-700' : 'text-zinc-600'
              }`}
            >
              {task.pr_created ? (task.merged ? 'CREATED & MERGED' : 'OPENED') : 'NONE'}
            </span>
          </div>
        </div>
      </div>

      {/* Dynamic Telemetry Metrics Card */}
      <TaskMetrics task={task} metrics={task.metrics} />

      {/* Unified Diff Review Viewer */}
      <UnifiedDiffReview
        patch={task.patch || globalTask.currentDiff}
        task={task as any}
        title="Implementation Patch Review"
      />

      {/* Action Buttons: Only active when task is awaiting review or running */}
      {isRunning && (
        <div className="flex items-center gap-3 pt-2">
          <button
            type="button"
            onClick={() => setRejectionModalOpen(true)}
            disabled={isSubmittingRejection || isSubmittingApproval}
            className="h-11 px-4 bg-white hover:bg-zinc-100 text-zinc-800 text-xs font-medium rounded-xl border border-zinc-200 shadow-sm flex items-center justify-center gap-2 shrink-0 transition disabled:opacity-50"
          >
            <RotateCcw className="h-3.5 w-3.5 text-zinc-500" />
            <span>Request Changes</span>
          </button>

          <button
            type="button"
            onClick={handleApprove}
            disabled={isSubmittingApproval || isSubmittingRejection}
            className="h-11 px-4 text-xs font-semibold rounded-xl shadow-sm flex-1 flex items-center justify-center gap-2 bg-zinc-900 hover:bg-zinc-800 text-white transition disabled:opacity-50"
          >
            {isSubmittingApproval ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin text-white" />
                <span>Approving &amp; Verifying PR...</span>
              </>
            ) : (
              <>
                <GitPullRequest className="h-4 w-4" />
                <span>Approve &amp; Create PR</span>
              </>
            )}
          </button>
        </div>
      )}

      {/* Modal: Request Revisions */}
      {rejectionModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4">
          <div className="bg-white rounded-2xl max-w-md w-full p-5 space-y-4 shadow-xl border border-zinc-200">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-bold text-zinc-900">
                Request Revisions
              </h3>
              <button
                type="button"
                onClick={() => setRejectionModalOpen(false)}
                className="text-zinc-400 hover:text-zinc-700 text-xs"
              >
                Cancel
              </button>
            </div>
            <p className="text-xs text-zinc-600 leading-relaxed">
              Describe the adjustments or tests needed. This will be routed back to the implementer node:
            </p>
            <textarea
              value={rejectionFeedback}
              onChange={(e) => setRejectionFeedback(e.target.value)}
              placeholder="e.g., Please add boundary validation tests for socket timeouts..."
              rows={3}
              className="w-full text-xs font-mono p-3 rounded-xl border border-zinc-200 focus:outline-none focus:ring-1 focus:ring-zinc-400 resize-none"
            />
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => setRejectionModalOpen(false)}
                className="px-3.5 py-2 text-xs font-medium text-zinc-600 hover:text-zinc-900"
              >
                Close
              </button>
              <button
                type="button"
                onClick={handleReject}
                disabled={isSubmittingRejection}
                className="px-4 py-2 bg-amber-500 hover:bg-amber-600 text-amber-950 font-bold text-xs rounded-xl shadow-xs disabled:opacity-50"
              >
                {isSubmittingRejection ? 'Sending Feedback...' : 'Send Feedback'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Review;