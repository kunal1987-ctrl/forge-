import React, { useState } from 'react';
import {
  CheckCheck,
  ExternalLink,
  AlertTriangle,
  RotateCcw,
  Loader2,
  GitPullRequest,
  GitBranch,
} from 'lucide-react';
import { apiClient } from '../services/apiClient';
import type { TaskStatusResponse } from '../types/api';

export interface TaskCompletionCardProps {
  task: TaskStatusResponse;
  onPRCreated?: (prUrl: string) => void;
  onTaskUpdated?: (updatedTask: TaskStatusResponse) => void;
  errorMessage?: string | null;
  className?: string;
}

/**
 * TaskCompletionCard Component (Pillar 1)
 *
 * Displays:
 * 1. Prominent GitHub Pull Request link with success badge when pr_url is present.
 * 2. Distinct amber/red warning banner stating "PR Creation Failed: [Error Message]"
 *    accompanied by an interactive "Re-create PR" button when pr_url is null or creation failed.
 * 3. Wires "Re-create PR" to POST /api/tasks/{task_id}/retry-pr and updates UI live.
 */
export const TaskCompletionCard: React.FC<TaskCompletionCardProps> = ({
  task,
  onPRCreated,
  onTaskUpdated,
  errorMessage: initialErrorMessage,
  className = '',
}) => {
  const [isRetrying, setIsRetrying] = useState<boolean>(false);
  const [currentPrUrl, setCurrentPrUrl] = useState<string | null>(task.pr_url || null);
  const [retryError, setRetryError] = useState<string | null>(initialErrorMessage || task.error || null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  const effectivePrUrl = currentPrUrl || task.pr_url;
  const isPrPresent = Boolean(effectivePrUrl && effectivePrUrl.trim().length > 0);

  const handleRetryPR = async () => {
    if (!task?.task_id || isRetrying) return;

    setIsRetrying(true);
    setRetryError(null);
    setActionNotice('Authenticating remote branch and creating GitHub Pull Request...');

    try {
      const result = await apiClient.retryPullRequest(task.task_id);
      const newUrl = result.pr_url || null;

      if (newUrl) {
        setCurrentPrUrl(newUrl);
        setActionNotice(`Successfully opened GitHub Pull Request!`);
        if (onPRCreated) {
          onPRCreated(newUrl);
        }
        if (onTaskUpdated) {
          onTaskUpdated({
            ...task,
            pr_created: true,
            pr_url: newUrl,
            branch_name: result.branch_name || task.branch_name,
            status: 'COMPLETED',
          });
        }
      } else {
        throw new Error(result.error || result.message || 'Backend did not return a PR web URL.');
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to re-create Pull Request';
      setRetryError(msg);
      setActionNotice(null);
    } finally {
      setIsRetrying(false);
    }
  };

  return (
    <div className={`w-full flex flex-col space-y-3 ${className}`}>
      {/* Toast Notice */}
      {actionNotice && (
        <div className="p-3 rounded-xl text-xs font-medium flex items-center justify-between border bg-emerald-50 text-emerald-900 border-emerald-200 shadow-xs animate-in fade-in">
          <span>{actionNotice}</span>
        </div>
      )}

      {/* =========================================================================
          STATE 1: SUCCESSFUL PR CREATION (Prominent Link + Verified Badge)
          ========================================================================= */}
      {isPrPresent ? (
        <div
          id="task-pr-success-banner"
          className="bg-emerald-50 border-2 border-emerald-300 rounded-2xl p-5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 shadow-sm animate-in fade-in"
        >
          <div className="flex items-center gap-3.5 min-w-0">
            <div className="h-11 w-11 rounded-xl bg-emerald-600 text-white flex items-center justify-center shrink-0 shadow-xs">
              <CheckCheck className="h-6 w-6 stroke-[2.5]" />
            </div>
            <div className="min-w-0 space-y-0.5">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-bold text-emerald-950 uppercase tracking-wider">
                  Pull Request Created &amp; Merged!
                </span>
                <span className="font-mono text-[10px] bg-emerald-100 text-emerald-800 border border-emerald-300 px-2 py-0.5 rounded-full font-bold">
                  VERIFIED GITHUB PR
                </span>
              </div>
              <p className="text-xs text-emerald-800 truncate">
                Target branch:{' '}
                <span className="font-mono font-semibold">
                  {task.branch_name || `forge-fix-${task.task_id}`}
                </span>
                {task.merged ? ' • Merged to upstream repository' : ' • Upstream Pull Request Active'}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0 self-end sm:self-center">
            {effectivePrUrl && (
              <a
                id="link-open-github-pr"
                href={effectivePrUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-emerald-700 hover:bg-emerald-800 text-white text-xs font-bold shadow-xs transition transform hover:scale-[1.01] active:scale-[0.99]"
              >
                <GitPullRequest className="h-4 w-4" />
                <span>Open GitHub PR</span>
                <ExternalLink className="h-3.5 w-3.5 opacity-80" />
              </a>
            )}
          </div>
        </div>
      ) : (
        /* =========================================================================
           STATE 2: PR CREATION FAILED / NULL (Distinct Amber/Red Warning Banner)
           ========================================================================= */
        <div
          id="task-pr-failed-banner"
          className="bg-amber-50/90 border-2 border-amber-300/90 rounded-2xl p-5 flex flex-col md:flex-row items-start md:items-center justify-between gap-4 shadow-sm animate-in fade-in"
        >
          <div className="flex items-start gap-3.5 min-w-0 flex-1">
            <div className="h-11 w-11 rounded-xl bg-amber-500 text-amber-950 flex items-center justify-center shrink-0 shadow-xs mt-0.5">
              <AlertTriangle className="h-6 w-6 stroke-[2.2]" />
            </div>

            <div className="space-y-1.5 min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-bold text-amber-950 uppercase tracking-wider">
                  PR Creation Incomplete
                </span>
                <span className="font-mono text-[10px] bg-amber-100 text-amber-900 border border-amber-300 px-2 py-0.5 rounded-full font-bold">
                  PR NOT CREATED
                </span>
              </div>

              {/* Explicit Amber/Red Error Message Callout */}
              <div className="bg-amber-100/80 border border-amber-300/80 rounded-xl px-3 py-2 text-xs font-mono text-amber-950">
                <span className="font-bold text-rose-800">PR Creation Failed: </span>
                <span className="break-words">
                  {retryError ||
                    task.error ||
                    'Upstream Pull Request could not be created automatically. Local branch exists and is ready for push.'}
                </span>
              </div>

              <div className="flex items-center gap-2 text-[11px] text-amber-800/90 pt-0.5">
                <GitBranch className="h-3.5 w-3.5 shrink-0" />
                <span>
                  Local branch:{' '}
                  <span className="font-mono font-semibold">
                    {task.branch_name || `forge-fix-${task.task_id}`}
                  </span>
                </span>
              </div>
            </div>
          </div>

          {/* Explicit "Re-create PR" Button wired to POST /api/tasks/{task_id}/retry-pr */}
          <div className="flex items-center gap-2 shrink-0 self-end md:self-center">
            <button
              id="btn-recreate-pr"
              type="button"
              onClick={handleRetryPR}
              disabled={isRetrying}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-amber-600 hover:bg-amber-700 text-white text-xs font-bold shadow-xs transition transform hover:scale-[1.01] active:scale-[0.99] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isRetrying ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin text-white" />
                  <span>Re-creating PR...</span>
                </>
              ) : (
                <>
                  <RotateCcw className="h-4 w-4" />
                  <span>Re-create PR</span>
                </>
              )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default TaskCompletionCard;
