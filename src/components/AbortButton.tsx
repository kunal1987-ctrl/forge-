import React, { useState } from 'react';
import { Loader2, XCircle } from 'lucide-react';
import { useTaskContext } from '../context/TaskContext';
import { apiClient, ApiError } from '../services/apiClient';

export interface AbortButtonProps {
  /** Optional task identifier override; falls back to context taskId */
  taskId?: string | null;
  /** Button label text when idle (default: 'Abort Pipeline') */
  label?: string;
  /** Custom extra CSS classes */
  className?: string;
  /** Visual presentation variant */
  variant?: 'danger' | 'outline' | 'compact' | 'subtle' | 'ghost';
  /** Size variant */
  size?: 'sm' | 'md' | 'lg';
  /** Whether to show the icon */
  showIcon?: boolean;
  /** Explicit disabled override */
  disabled?: boolean;
  /** Optional callback fired when abort successfully completes and UI resets */
  onAborted?: () => void;
}

export const AbortButton: React.FC<AbortButtonProps> = ({
  taskId: propTaskId,
  label = 'Abort Pipeline',
  className = '',
  variant = 'danger',
  size = 'md',
  showIcon = true,
  disabled = false,
  onAborted,
}) => {
  const {
    taskId: contextTaskId,
    taskStatus,
    abortTask,
    resetTask,
    addToast,
    addLog,
  } = useTaskContext();

  const [isAborting, setIsAborting] = useState(false);

  const effectiveTaskId = propTaskId !== undefined ? propTaskId : contextTaskId;
  const isRunning = taskStatus === 'RUNNING';

  const handleAbort = async (e: React.MouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    e.stopPropagation();

    if (isAborting) return;

    setIsAborting(true);
    try {
      if (effectiveTaskId) {
        addToast('info', 'Halting pipeline and terminating active worker processes...');
        await apiClient.abortTask(effectiveTaskId);
        addToast('success', 'Pipeline aborted and engine state reset.');
        addLog(`[System] Task ${effectiveTaskId} forcefully halted and resources terminated.`);
      } else {
        await abortTask();
      }
      resetTask();
      if (onAborted) {
        onAborted();
      }
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `Abort notice: ${msg}`);
      addLog(`[System] Abort request completed with note: ${msg}`);
    } finally {
      setIsAborting(false);
    }
  };

  // Variant styling
  const variantStyles = {
    danger:
      'bg-rose-600 hover:bg-rose-700 text-white shadow-sm hover:shadow active:scale-[0.98] border border-transparent',
    outline:
      'bg-white hover:bg-rose-50 text-rose-600 border border-rose-300 hover:border-rose-400 shadow-sm active:scale-[0.98]',
    compact:
      'bg-rose-50 hover:bg-rose-100 text-rose-700 border border-rose-200 active:scale-[0.98]',
    subtle:
      'bg-transparent hover:bg-rose-50 text-rose-600 hover:text-rose-700 active:scale-[0.98]',
    ghost:
      'bg-transparent hover:bg-zinc-100 text-zinc-500 hover:text-rose-600',
  }[variant];

  // Size styling
  const sizeStyles = {
    sm: 'text-[11px] px-2.5 py-1 gap-1.5 rounded-lg',
    md: 'text-xs px-3.5 py-1.5 gap-2 rounded-xl',
    lg: 'text-sm px-4 py-2.5 gap-2.5 rounded-xl font-bold',
  }[size];

  const isDisabled = disabled || isAborting || !isRunning || !effectiveTaskId;

  return (
    <button
      type="button"
      onClick={handleAbort}
      disabled={isDisabled}
      aria-label={label}
      aria-busy={isAborting}
      className={`inline-flex items-center justify-center font-semibold transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-rose-500/40 disabled:opacity-50 disabled:cursor-not-allowed ${variantStyles} ${sizeStyles} ${className}`}
    >
      {isAborting ? (
        <>
          <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0 text-current" />
          <span>Aborting...</span>
        </>
      ) : (
        <>
          {showIcon && <XCircle className="h-3.5 w-3.5 shrink-0 text-current" />}
          <span>{label}</span>
        </>
      )}
    </button>
  );
};

export default AbortButton;
