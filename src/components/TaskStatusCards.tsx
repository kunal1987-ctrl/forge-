import React, { useState } from 'react';
import {
  CheckCircle2,
  XCircle,
  RotateCcw,
  FileCode,
  GitBranch,
  ExternalLink,
  Copy,
  Check,
  Loader2,
} from 'lucide-react';
import { useTask } from '../context/TaskContext';
import type { TaskStateObject } from '../types/api';

export interface TaskStatusCardsProps {
  /** Optional task state override; defaults to global TaskContext */
  task?: TaskStateObject | null;
  /** Optional container CSS class */
  className?: string;
}

/**
 * TaskStatusCards Component
 *
 * Renders four real-time execution telemetry cards:
 * 1. Test Suite: Real pass/fail counts (`{task.test_suite_passed} passed`) with green/red conditional color coding.
 * 2. Repair Iterations: Real retry progress (`{task.retry_count} / 3 max`).
 * 3. Target Scope: Dynamic active file path or module name identified by planner.
 * 4. PR Target: Dynamic branch name combining prefix and task ID (`forge/auto-patch-{task_id}`) with PR link.
 */
export const TaskStatusCards: React.FC<TaskStatusCardsProps> = ({
  task: propTask,
  className = '',
}) => {
  const context = useTask();
  const effectiveTask = propTask ?? context.task;

  // 1. Resolve real execution telemetry from TaskContext / task object
  const testSuitePassed =
    effectiveTask?.test_suite_passed ??
    context.test_suite_passed ??
    context.testMetrics?.passed ??
    0;

  const testFailedCount = context.testMetrics?.failed ?? 0;

  const retryCount =
    effectiveTask?.retry_count ??
    context.retry_count ??
    context.testMetrics?.retries ??
    0;

  const targetScope =
    effectiveTask?.target_scope ??
    context.target_scope ??
    context.plan?.target_files?.[0] ??
    'services/api/forge/routers/rag.py';

  const taskId = effectiveTask?.taskId || effectiveTask?.id || context.taskId;
  const prTarget =
    effectiveTask?.pr_target ??
    context.pr_target ??
    (taskId ? `forge/auto-patch-${taskId}` : 'forge/auto-patch');

  const prUrl = effectiveTask?.pr_url ?? context.prUrl;
  const isRunning = context.status === 'RUNNING';
  const activeStep = context.activeStep;

  // Clipboard copy state for branch and file path
  const [copiedScope, setCopiedScope] = useState(false);
  const [copiedBranch, setCopiedBranch] = useState(false);

  const handleCopyScope = async () => {
    try {
      await navigator.clipboard.writeText(targetScope);
      setCopiedScope(true);
      setTimeout(() => setCopiedScope(false), 2000);
      context.addToast('info', `Target scope "${targetScope}" copied.`);
    } catch {
      setCopiedScope(false);
    }
  };

  const handleCopyBranch = async () => {
    try {
      await navigator.clipboard.writeText(prTarget);
      setCopiedBranch(true);
      setTimeout(() => setCopiedBranch(false), 2000);
      context.addToast('info', `Branch name "${prTarget}" copied.`);
    } catch {
      setCopiedBranch(false);
    }
  };

  // Determine test status badge and color theme
  const hasFailures = testFailedCount > 0;
  const hasPassed = testSuitePassed > 0;
  const isVerifying = isRunning && activeStep === 'verifier';

  return (
    <div className={`grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5 ${className}`}>
      {/* =========================================================================
          CARD 1: TEST SUITE (Real pass/fail counts & conditional color coding)
          ========================================================================= */}
      <div
        className={`relative flex flex-col justify-between p-4 rounded-xl border transition-all shadow-xs ${
          hasFailures
            ? 'bg-rose-50/50 border-rose-200/90 text-rose-950'
            : hasPassed
            ? 'bg-emerald-50/40 border-emerald-200/90 text-emerald-950'
            : 'bg-white border-zinc-200/80 text-zinc-900'
        }`}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            {isVerifying ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-indigo-600" />
            ) : hasFailures ? (
              <XCircle className="h-3.5 w-3.5 text-rose-600" />
            ) : hasPassed ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
            ) : (
              <CheckCircle2 className="h-3.5 w-3.5 text-zinc-400" />
            )}
            <span>Test Suite</span>
          </div>

          {/* Conditional Status Tag */}
          <span
            className={`text-[10px] font-mono px-2 py-0.5 rounded-full font-semibold border ${
              hasFailures
                ? 'bg-rose-100 text-rose-800 border-rose-200'
                : hasPassed
                ? 'bg-emerald-100 text-emerald-800 border-emerald-200'
                : isVerifying
                ? 'bg-indigo-100 text-indigo-800 border-indigo-200'
                : 'bg-zinc-100 text-zinc-600 border-zinc-200'
            }`}
          >
            {isVerifying ? 'RUNNING' : hasFailures ? 'FAILING' : hasPassed ? 'PASSED' : 'STANDBY'}
          </span>
        </div>

        <div className="mt-3 flex items-baseline gap-2">
          <span
            className={`text-2xl font-bold font-mono tracking-tight ${
              hasFailures
                ? 'text-rose-600'
                : hasPassed
                ? 'text-emerald-600'
                : 'text-zinc-700'
            }`}
          >
            {testSuitePassed}
          </span>
          <span className="text-xs text-zinc-500 font-medium">passed</span>

          {hasFailures && (
            <span className="text-xs font-mono font-bold text-rose-600 ml-1">
              ({testFailedCount} failed)
            </span>
          )}
        </div>

        <div className="mt-2 text-[11px] text-zinc-400 truncate">
          {isVerifying
            ? 'Sandboxed pytest container active'
            : hasFailures
            ? 'Regression isolated by verifier'
            : hasPassed
            ? 'Green-Phase verification verified'
            : 'Awaiting execution run'}
        </div>
      </div>

      {/* =========================================================================
          CARD 2: REPAIR ITERATIONS (Real retry progress)
          ========================================================================= */}
      <div className="relative flex flex-col justify-between p-4 rounded-xl bg-white border border-zinc-200/80 shadow-xs">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            <RotateCcw
              className={`h-3.5 w-3.5 ${
                retryCount > 0 ? 'text-amber-500' : 'text-zinc-400'
              } ${isRunning && activeStep === 'implementer' && retryCount > 0 ? 'animate-spin' : ''}`}
            />
            <span>Repair Iterations</span>
          </div>

          {/* Iteration progress dots */}
          <div className="flex items-center gap-1" title={`${retryCount} of 3 retries used`}>
            {[1, 2, 3].map((step) => {
              const isFilled = retryCount >= step;
              return (
                <span
                  key={step}
                  className={`h-2 w-2 rounded-full transition-colors ${
                    isFilled
                      ? retryCount >= 3
                        ? 'bg-rose-500'
                        : 'bg-amber-500'
                      : 'bg-zinc-200'
                  }`}
                />
              );
            })}
          </div>
        </div>

        <div className="mt-3 flex items-baseline gap-1.5">
          <span
            className={`text-2xl font-bold font-mono tracking-tight ${
              retryCount >= 3
                ? 'text-rose-600'
                : retryCount > 0
                ? 'text-amber-600'
                : 'text-zinc-900'
            }`}
          >
            {retryCount}
          </span>
          <span className="text-xs text-zinc-400 font-mono">/ 3 max</span>
        </div>

        <div className="mt-2 text-[11px] text-zinc-400 truncate">
          {retryCount === 0
            ? 'First iteration pass'
            : retryCount >= 3
            ? 'Maximum repair budget reached'
            : `Repair loop iteration ${retryCount} active`}
        </div>
      </div>

      {/* =========================================================================
          CARD 3: TARGET SCOPE (Active file path or module name)
          ========================================================================= */}
      <div className="relative flex flex-col justify-between p-4 rounded-xl bg-white border border-zinc-200/80 shadow-xs">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            <FileCode className="h-3.5 w-3.5 text-zinc-600" />
            <span>Target Scope</span>
          </div>

          <button
            type="button"
            onClick={handleCopyScope}
            className="p-1 rounded text-zinc-400 hover:text-zinc-700 hover:bg-zinc-100 transition"
            title="Copy target file path"
            aria-label="Copy target file path"
          >
            {copiedScope ? (
              <Check className="h-3.5 w-3.5 text-emerald-600" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </button>
        </div>

        <div className="mt-3">
          <div
            className="text-xs font-mono font-bold text-zinc-900 truncate"
            title={targetScope}
          >
            {targetScope.split('/').pop() || targetScope}
          </div>
          <div
            className="text-[11px] font-mono text-zinc-500 truncate mt-0.5"
            title={targetScope}
          >
            {targetScope}
          </div>
        </div>

        <div className="mt-2 text-[11px] text-zinc-400 truncate">
          {context.plan?.target_files?.length
            ? `${context.plan.target_files.length} files in execution plan`
            : 'Planned surgical patch file'}
        </div>
      </div>

      {/* =========================================================================
          CARD 4: PR TARGET (Exact generated branch name & PR link)
          ========================================================================= */}
      <div className="relative flex flex-col justify-between p-4 rounded-xl bg-white border border-zinc-200/80 shadow-xs">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
            <GitBranch className="h-3.5 w-3.5 text-zinc-600" />
            <span>PR Target</span>
          </div>

          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={handleCopyBranch}
              className="p-1 rounded text-zinc-400 hover:text-zinc-700 hover:bg-zinc-100 transition"
              title="Copy branch name"
              aria-label="Copy branch name"
            >
              {copiedBranch ? (
                <Check className="h-3.5 w-3.5 text-emerald-600" />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )}
            </button>
          </div>
        </div>

        <div className="mt-3">
          <div
            className="text-xs font-mono font-bold text-zinc-900 truncate"
            title={prTarget}
          >
            {prTarget}
          </div>

          <div className="mt-1 flex items-center gap-2">
            {prUrl ? (
              <a
                href={prUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-700 hover:text-emerald-800 hover:underline"
              >
                <span>View GitHub PR</span>
                <ExternalLink className="h-3 w-3" />
              </a>
            ) : (
              <span className="text-[11px] font-mono text-zinc-400">
                {isRunning ? 'Branch staged for PR' : 'Target patch branch'}
              </span>
            )}
          </div>
        </div>

        <div className="mt-2 text-[11px] text-zinc-400 truncate">
          {prUrl ? 'Pull request created & verified' : 'Automated branch dispatch'}
        </div>
      </div>
    </div>
  );
};

export default TaskStatusCards;
