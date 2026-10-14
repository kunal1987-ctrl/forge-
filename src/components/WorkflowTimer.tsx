import React, { useEffect, useState, useMemo, useRef } from 'react';
import { Timer, Clock, CheckCircle2, AlertCircle, Ban } from 'lucide-react';
import { useTask } from '../context/TaskContext';
import type { GlobalTaskStatus } from '../context/TaskContext';
import type { TaskStateObject } from '../types/api';

export interface FormatDurationOptions {
  /**
   * Whether to append 's' suffix (e.g. '02:05s'). Defaults to true.
   */
  includeSuffix?: boolean;

  /**
   * Fallback string when seconds value is null or undefined. Defaults to '00:00s'.
   */
  fallback?: string;

  /**
   * Force hours display even when hours is 0 (e.g. '00:02:05s'). Defaults to false.
   */
  alwaysIncludeHours?: boolean;
}

/**
 * Robust duration formatting helper:
 * Converts total seconds into minutes and seconds (`MM:ss` or `HH:mm:ss`),
 * e.g., 125 seconds becomes '02:05s', 3665 seconds becomes '01:01:05s'.
 */
export function formatWorkflowDuration(
  totalSeconds: number | null | undefined,
  options: FormatDurationOptions = {}
): string {
  const {
    includeSuffix = true,
    fallback = '00:00s',
    alwaysIncludeHours = false,
  } = options;

  if (totalSeconds === null || totalSeconds === undefined || isNaN(totalSeconds)) {
    return fallback;
  }

  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const suffix = includeSuffix ? 's' : '';

  const hours = Math.floor(safeSeconds / 3600);
  const remainingSeconds = safeSeconds % 3600;
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;

  const mm = String(minutes).padStart(2, '0');
  const ss = String(seconds).padStart(2, '0');

  if (hours > 0 || alwaysIncludeHours) {
    const hh = String(hours).padStart(2, '0');
    return `${hh}:${mm}:${ss}${suffix}`;
  }

  return `${mm}:${ss}${suffix}`;
}

/**
 * Backward-compatibility alias for formatWorkflowDuration.
 */
export const formatDuration = formatWorkflowDuration;

/**
 * Breaks total seconds down into individual components: hours, minutes, seconds.
 */
export function parseDurationParts(totalSeconds: number | null | undefined) {
  if (totalSeconds === null || totalSeconds === undefined || isNaN(totalSeconds)) {
    return { hours: 0, minutes: 0, seconds: 0, totalSeconds: 0 };
  }
  const safe = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const seconds = safe % 60;
  return { hours, minutes, seconds, totalSeconds: safe };
}

export interface WorkflowTimerProps {
  /**
   * Optional direct duration_seconds override.
   * If omitted, consumes `task.duration_seconds` or `duration_seconds` from TaskContext.
   */
  duration_seconds?: number | null;

  /**
   * Optional direct start timestamp ISO string override.
   * If omitted, consumes `task.start_time` or `startTime` from TaskContext.
   */
  startTime?: string | null;

  /**
   * Optional direct status override.
   * If omitted, consumes `task.status` or `status` from TaskContext.
   */
  status?: GlobalTaskStatus | string;

  /**
   * Optional direct task state object override.
   */
  task?: TaskStateObject | null;

  /**
   * Visual presentation style.
   * - 'pill': Rounded pill badge with icon and micro-animation dot (default).
   * - 'badge': Compact rounded-md badge.
   * - 'card': Boxed mini-card with label, value, and status readout.
   * - 'minimal': Plain typography with optional icon.
   * - 'inline': Inline text with timer icon.
   */
  variant?: 'pill' | 'badge' | 'card' | 'minimal' | 'inline';

  /**
   * Size variant.
   */
  size?: 'sm' | 'md' | 'lg';

  /**
   * Whether to show the timer clock icon. Defaults to true.
   */
  showIcon?: boolean;

  /**
   * Whether to display an active/status text label (e.g. "Live", "Completed"). Defaults to false.
   */
  showStatusBadge?: boolean;

  /**
   * Optional custom label rendered alongside or above the timer.
   */
  label?: string;

  /**
   * Optional fallback display when status is IDLE and no duration exists. Defaults to '00:00s'.
   */
  idlePlaceholder?: string;

  /**
   * Additional CSS classes.
   */
  className?: string;

  /**
   * Whether to include the 's' suffix in the formatted output. Defaults to true.
   */
  includeSuffix?: boolean;
}

/**
 * Dynamic Workflow Timer Component
 *
 * Consumes `duration_seconds`, `start_time`, and execution `status` from global `TaskContext`.
 * - When actively running (`status === 'RUNNING'`):
 *   Renders a live incrementing counter ticking every second with high-precision timestamp calculation.
 * - When completed (`status === 'COMPLETED'`):
 *   Renders the final static `duration_seconds` recorded by the engine.
 * - When failed or aborted:
 *   Displays the terminal static duration with appropriate visual styling.
 */
export const WorkflowTimer: React.FC<WorkflowTimerProps> = ({
  duration_seconds: propDuration,
  startTime: propStartTime,
  status: propStatus,
  task: propTask,
  variant = 'pill',
  size = 'md',
  showIcon = true,
  showStatusBadge = false,
  label,
  idlePlaceholder = '00:00s',
  className = '',
  includeSuffix = true,
}) => {
  // 1. Consume from global TaskContext
  const context = useTask();
  const globalTask = context.task;
  const globalStatus = context.status;
  const contextDuration = context.durationSeconds ?? context.duration_seconds;
  const contextStartTime = context.startTime ?? context.start_time;

  // 2. Resolve effective inputs
  const effectiveTask = propTask ?? globalTask;
  const effectiveStatus = (
    propStatus ??
    effectiveTask?.status ??
    globalStatus ??
    'IDLE'
  ).toUpperCase() as GlobalTaskStatus;

  const effectiveStartTime =
    propStartTime ??
    effectiveTask?.start_time ??
    effectiveTask?.startTime ??
    contextStartTime ??
    null;

  const serverDurationSeconds =
    propDuration !== undefined
      ? propDuration
      : effectiveTask?.duration_seconds !== undefined
      ? effectiveTask?.duration_seconds
      : contextDuration !== undefined
      ? contextDuration
      : null;

  const isRunning = effectiveStatus === 'RUNNING';
  const isTerminal =
    effectiveStatus === 'COMPLETED' ||
    effectiveStatus === 'FAILED' ||
    effectiveStatus === 'ABORTED';

  // 3. Live incrementing timer state
  const [liveElapsedSeconds, setLiveElapsedSeconds] = useState<number>(() => {
    if (effectiveStartTime) {
      const startMs = new Date(effectiveStartTime).getTime();
      if (!isNaN(startMs) && startMs > 0) {
        return Math.max(0, Math.floor((Date.now() - startMs) / 1000));
      }
    }
    if (typeof serverDurationSeconds === 'number' && serverDurationSeconds >= 0) {
      return serverDurationSeconds;
    }
    return 0;
  });

  // Track initial start time anchor to compute wall-clock delta without drift
  const startTimeAnchorRef = useRef<number | null>(null);

  useEffect(() => {
    if (effectiveStartTime) {
      const parsed = new Date(effectiveStartTime).getTime();
      if (!isNaN(parsed) && parsed > 0) {
        startTimeAnchorRef.current = parsed;
        setLiveElapsedSeconds(Math.max(0, Math.floor((Date.now() - parsed) / 1000)));
        return;
      }
    }
    // Fallback: anchor to now minus whatever duration is already known
    if (isRunning && startTimeAnchorRef.current === null) {
      const initialOffset = (serverDurationSeconds || 0) * 1000;
      startTimeAnchorRef.current = Date.now() - initialOffset;
    }
  }, [effectiveStartTime, isRunning, serverDurationSeconds]);

  // High-precision 1-second ticker while running
  useEffect(() => {
    if (!isRunning) {
      startTimeAnchorRef.current = null;
      return;
    }

    const updateTimer = () => {
      if (startTimeAnchorRef.current !== null) {
        const deltaSec = Math.max(0, Math.floor((Date.now() - startTimeAnchorRef.current) / 1000));
        setLiveElapsedSeconds(deltaSec);
      } else if (effectiveStartTime) {
        const parsed = new Date(effectiveStartTime).getTime();
        if (!isNaN(parsed)) {
          startTimeAnchorRef.current = parsed;
          setLiveElapsedSeconds(Math.max(0, Math.floor((Date.now() - parsed) / 1000)));
        }
      } else {
        setLiveElapsedSeconds((prev) => prev + 1);
      }
    };

    // Immediate first tick
    updateTimer();

    const intervalId = window.setInterval(updateTimer, 1000);
    return () => {
      window.clearInterval(intervalId);
    };
  }, [isRunning, effectiveStartTime]);

  // 4. Resolve the displayed total seconds
  const displaySeconds = useMemo(() => {
    if (isRunning) {
      return liveElapsedSeconds;
    }
    if (isTerminal) {
      if (typeof serverDurationSeconds === 'number' && serverDurationSeconds >= 0) {
        return serverDurationSeconds;
      }
      return liveElapsedSeconds > 0 ? liveElapsedSeconds : 0;
    }
    // Status is IDLE
    if (typeof serverDurationSeconds === 'number' && serverDurationSeconds > 0) {
      return serverDurationSeconds;
    }
    return null;
  }, [isRunning, isTerminal, liveElapsedSeconds, serverDurationSeconds]);

  // Formatted string: e.g. "02:05s"
  const formattedTime = useMemo(() => {
    if (displaySeconds === null && effectiveStatus === 'IDLE') {
      return idlePlaceholder;
    }
    return formatWorkflowDuration(displaySeconds ?? 0, { includeSuffix });
  }, [displaySeconds, effectiveStatus, idlePlaceholder, includeSuffix]);

  // 5. Visual Styling Classes & Icons based on Status & Size
  const sizeClasses = useMemo(() => {
    switch (size) {
      case 'sm':
        return {
          wrapper: 'text-xs px-2 py-0.5 gap-1.5',
          icon: 'w-3.5 h-3.5',
          font: 'text-xs font-mono font-medium',
          dot: 'h-1.5 w-1.5',
        };
      case 'lg':
        return {
          wrapper: 'text-base px-4 py-2 gap-2.5',
          icon: 'w-5 h-5',
          font: 'text-base font-mono font-semibold tracking-wide',
          dot: 'h-2.5 w-2.5',
        };
      case 'md':
      default:
        return {
          wrapper: 'text-sm px-2.5 py-1 gap-2',
          icon: 'w-4 h-4',
          font: 'text-sm font-mono font-medium tracking-tight',
          dot: 'h-2 w-2',
        };
    }
  }, [size]);

  const statusTheme = useMemo(() => {
    switch (effectiveStatus) {
      case 'RUNNING':
        return {
          border: 'border-emerald-500/30 dark:border-emerald-500/40',
          bg: 'bg-emerald-500/10 dark:bg-emerald-950/30',
          text: 'text-emerald-600 dark:text-emerald-400',
          dotColor: 'bg-emerald-500',
          dotPing: 'bg-emerald-400',
          statusText: 'Live',
          badgeClass: 'bg-emerald-500/20 text-emerald-600 dark:text-emerald-300 border-emerald-500/30',
          IconComponent: Timer,
          iconClass: 'animate-pulse text-emerald-600 dark:text-emerald-400',
        };
      case 'COMPLETED':
        return {
          border: 'border-blue-500/20 dark:border-blue-500/30',
          bg: 'bg-blue-500/10 dark:bg-blue-950/20',
          text: 'text-blue-600 dark:text-blue-400',
          dotColor: 'bg-blue-500',
          dotPing: 'bg-blue-400',
          statusText: 'Completed',
          badgeClass: 'bg-blue-500/20 text-blue-600 dark:text-blue-300 border-blue-500/30',
          IconComponent: CheckCircle2,
          iconClass: 'text-blue-600 dark:text-blue-400',
        };
      case 'FAILED':
        return {
          border: 'border-rose-500/20 dark:border-rose-500/30',
          bg: 'bg-rose-500/10 dark:bg-rose-950/20',
          text: 'text-rose-600 dark:text-rose-400',
          dotColor: 'bg-rose-500',
          dotPing: 'bg-rose-400',
          statusText: 'Failed',
          badgeClass: 'bg-rose-500/20 text-rose-600 dark:text-rose-300 border-rose-500/30',
          IconComponent: AlertCircle,
          iconClass: 'text-rose-600 dark:text-rose-400',
        };
      case 'ABORTED':
        return {
          border: 'border-amber-500/20 dark:border-amber-500/30',
          bg: 'bg-amber-500/10 dark:bg-amber-950/20',
          text: 'text-amber-600 dark:text-amber-400',
          dotColor: 'bg-amber-500',
          dotPing: 'bg-amber-400',
          statusText: 'Aborted',
          badgeClass: 'bg-amber-500/20 text-amber-600 dark:text-amber-300 border-amber-500/30',
          IconComponent: Ban,
          iconClass: 'text-amber-600 dark:text-amber-400',
        };
      case 'IDLE':
      default:
        return {
          border: 'border-zinc-200 dark:border-zinc-800',
          bg: 'bg-zinc-100/80 dark:bg-zinc-900/60',
          text: 'text-zinc-600 dark:text-zinc-400',
          dotColor: 'bg-zinc-400 dark:bg-zinc-600',
          dotPing: 'bg-zinc-300',
          statusText: 'Idle',
          badgeClass: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-400 border-zinc-300 dark:border-zinc-700',
          IconComponent: Clock,
          iconClass: 'text-zinc-400 dark:text-zinc-500',
        };
    }
  }, [effectiveStatus]);

  const StatusIcon = statusTheme.IconComponent;

  // 6. Variant Rendering
  if (variant === 'minimal') {
    return (
      <span
        className={`inline-flex items-center gap-1.5 ${statusTheme.text} ${sizeClasses.font} ${className}`}
        title={`Execution Duration: ${formattedTime}`}
      >
        {showIcon && <StatusIcon className={`${sizeClasses.icon} ${statusTheme.iconClass}`} />}
        {formattedTime}
      </span>
    );
  }

  if (variant === 'inline') {
    return (
      <span
        className={`inline-flex items-center gap-1 font-mono ${statusTheme.text} ${className}`}
        title={`Execution Duration: ${formattedTime}`}
      >
        {showIcon && <StatusIcon className={`${sizeClasses.icon} ${statusTheme.iconClass}`} />}
        <span>{formattedTime}</span>
      </span>
    );
  }

  if (variant === 'card') {
    return (
      <div
        className={`flex flex-col p-3 rounded-lg border ${statusTheme.border} ${statusTheme.bg} backdrop-blur-sm transition-all duration-200 ${className}`}
      >
        <div className="flex items-center justify-between text-xs text-zinc-500 dark:text-zinc-400 mb-1">
          <span className="flex items-center gap-1.5 font-medium">
            {showIcon && <StatusIcon className={`${sizeClasses.icon} ${statusTheme.iconClass}`} />}
            {label || 'Execution Duration'}
          </span>
          {showStatusBadge && (
            <span
              className={`text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded border ${statusTheme.badgeClass}`}
            >
              {statusTheme.statusText}
            </span>
          )}
        </div>
        <div className="flex items-baseline gap-2">
          <span className={`text-xl font-bold font-mono tracking-tight ${statusTheme.text}`}>
            {formattedTime}
          </span>
          {isRunning && (
            <span className="flex h-2 w-2 relative">
              <span
                className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${statusTheme.dotPing}`}
              />
              <span className={`relative inline-flex rounded-full h-2 w-2 ${statusTheme.dotColor}`} />
            </span>
          )}
        </div>
      </div>
    );
  }

  // Default 'pill' and 'badge' variants
  const isPill = variant === 'pill';
  const radiusClass = isPill ? 'rounded-full' : 'rounded-md';

  return (
    <div
      className={`inline-flex items-center border ${radiusClass} ${statusTheme.border} ${statusTheme.bg} ${sizeClasses.wrapper} transition-colors duration-200 select-none shadow-sm ${className}`}
      role="timer"
      aria-label={`Task execution time: ${formattedTime}`}
      title={`Status: ${effectiveStatus} | Duration: ${formattedTime}`}
    >
      {/* Icon or Running Indicator Dot */}
      {showIcon ? (
        <StatusIcon className={`${sizeClasses.icon} ${statusTheme.iconClass} shrink-0`} />
      ) : isRunning ? (
        <span className="relative flex h-2 w-2 shrink-0">
          <span
            className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${statusTheme.dotPing}`}
          />
          <span className={`relative inline-flex rounded-full h-2 w-2 ${statusTheme.dotColor}`} />
        </span>
      ) : null}

      {/* Optional Label */}
      {label && (
        <span className="text-zinc-500 dark:text-zinc-400 font-sans font-medium text-xs">
          {label}:
        </span>
      )}

      {/* Monospace Digits */}
      <span className={`${sizeClasses.font} ${statusTheme.text}`}>
        {formattedTime}
      </span>

      {/* Live Pulsing Dot for Pill mode when running */}
      {isRunning && showIcon && (
        <span className="relative flex h-2 w-2 shrink-0 ml-0.5">
          <span
            className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${statusTheme.dotPing}`}
          />
          <span className={`relative inline-flex rounded-full h-2 w-2 ${statusTheme.dotColor}`} />
        </span>
      )}

      {/* Optional Status Badge */}
      {showStatusBadge && (
        <span
          className={`text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.2 rounded border ml-1 ${statusTheme.badgeClass}`}
        >
          {statusTheme.statusText}
        </span>
      )}
    </div>
  );
};

export default WorkflowTimer;
