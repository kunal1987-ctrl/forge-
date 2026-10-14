import React, { useMemo } from 'react';
import {
  GitCommit,
  Timer,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  Minus,
  Loader2,
  Activity,
  FileCode,
  Sparkles,
} from 'lucide-react';
import { useTask } from '../context/TaskContext';
import type { TaskMetricsData, TaskStateObject } from '../types/api';

export interface MetricsCardProps {
  /**
   * Optional direct metrics override if passing metrics explicitly from a parent.
   * If omitted, consumes `task.metrics` directly from the global `TaskContext`.
   */
  metrics?: TaskMetricsData | null;

  /**
   * Optional task state object override.
   */
  task?: TaskStateObject | null;

  /**
   * Optional manual loading state override.
   */
  isLoading?: boolean;

  /**
   * Optional custom CSS class name for the wrapper.
   */
  className?: string;

  /**
   * Optional custom card title.
   */
  title?: string;

  /**
   * Whether to render in compact mode (e.g., inside side panels or dense lists).
   */
  compact?: boolean;
}

/**
 * MetricsCard Component
 *
 * Dynamically binds to `task.metrics` from the global `TaskContext` and renders:
 * 1. Diff Size: Actual insertions/deletions (`+{added} / -{deleted}`) with colored highlights.
 * 2. Latency Impact: Calculated millisecond delta with conditional coloring:
 *    - Green for negative/faster (< -0.05ms)
 *    - Neutral for zero (0.0ms)
 *    - Amber/Rose for positive overhead (> 0.05ms)
 * 3. Confidence: Percentage rounded to one decimal place (`{confidence * 100}%`).
 * 4. Loading States: Subtle skeleton loaders and dashes (`--`) while metrics are computing.
 */
export const MetricsCard: React.FC<MetricsCardProps> = ({
  metrics: propMetrics,
  task: propTask,
  isLoading: propIsLoading,
  className = '',
  title = 'Task Execution Metrics',
  compact = false,
}) => {
  // 1. Consume task state and status from global TaskContext
  const { task: globalTask, status: globalStatus } = useTask();

  // Resolve effective task and metrics
  const effectiveTask = propTask ?? globalTask;
  const metrics = propMetrics ?? effectiveTask?.metrics ?? null;
  const status = effectiveTask?.status ?? globalStatus;

  // Determine if metrics are actively computing
  const isComputing = Boolean(
    propIsLoading ||
    ((status === 'RUNNING' || status === 'PENDING') && !metrics)
  );

  // 2. Parse Diff Size (+{added} / -{deleted})
  const diffStats = useMemo(() => {
    let added: number | null = null;
    let deleted: number | null = null;
    let filesChanged: number | null = null;

    if (metrics) {
      // Priority A: metrics.diff_size object or string
      if (metrics.diff_size) {
        if (typeof metrics.diff_size === 'object') {
          added =
            metrics.diff_size.added ??
            metrics.diff_size.insertions ??
            null;
          deleted =
            metrics.diff_size.deleted ??
            metrics.diff_size.deletions ??
            null;
        } else if (typeof metrics.diff_size === 'string') {
          const plusMinusMatch = metrics.diff_size.match(/\+(\d+)\s*\/\s*-(\d+)/);
          if (plusMinusMatch) {
            added = parseInt(plusMinusMatch[1], 10);
            deleted = parseInt(plusMinusMatch[2], 10);
          } else {
            const insMatch = metrics.diff_size.match(/(\d+)\s+insertion/);
            const delMatch = metrics.diff_size.match(/(\d+)\s+deletion/);
            if (insMatch) added = parseInt(insMatch[1], 10);
            if (delMatch) deleted = parseInt(delMatch[2], 10);
          }
        }
      }

      // Priority B: metrics.diff_stats nested dictionary
      if ((added === null || deleted === null) && metrics.diff_stats) {
        if (typeof metrics.diff_stats.lines_added === 'number') {
          added = metrics.diff_stats.lines_added;
        }
        if (typeof metrics.diff_stats.lines_deleted === 'number') {
          deleted = metrics.diff_stats.lines_deleted;
        }
        if (typeof metrics.diff_stats.files_changed === 'number') {
          filesChanged = metrics.diff_stats.files_changed;
        }
      }

      // Priority C: Direct lines_added and lines_deleted fields
      if (added === null && typeof metrics.lines_added === 'number') {
        added = metrics.lines_added;
      }
      if (deleted === null && typeof metrics.lines_deleted === 'number') {
        deleted = metrics.lines_deleted;
      }
      if (filesChanged === null && typeof metrics.files_changed === 'number') {
        filesChanged = metrics.files_changed;
      }
      if (filesChanged === null && Array.isArray(metrics.files_list)) {
        filesChanged = metrics.files_list.length;
      }
    }

    const hasData = added !== null && deleted !== null;
    const formatted = hasData ? `+${added} / -${deleted}` : '--';

    return {
      added,
      deleted,
      filesChanged,
      formatted,
      hasData,
    };
  }, [metrics]);

  // 3. Parse Latency Impact (Millisecond delta with conditional coloring)
  const latencyStats = useMemo(() => {
    let deltaMs: number | null = null;
    let description: string | null = null;
    let isRegression = false;

    if (metrics) {
      // Direct millisecond delta field
      if (typeof metrics.latency_impact_ms === 'number' && !isNaN(metrics.latency_impact_ms)) {
        deltaMs = metrics.latency_impact_ms;
      }
      // Nested latency_details
      else if (
        metrics.latency_details &&
        typeof metrics.latency_details.duration_delta_ms === 'number' &&
        !isNaN(metrics.latency_details.duration_delta_ms)
      ) {
        deltaMs = metrics.latency_details.duration_delta_ms;
        description = metrics.latency_details.description ?? null;
        isRegression = Boolean(metrics.latency_details.is_regression);
      }
      // Latency impact in seconds (e.g., -0.012 or 0.005) or ms
      else if (typeof metrics.latency_impact === 'number' && !isNaN(metrics.latency_impact)) {
        if (Math.abs(metrics.latency_impact) < 10) {
          deltaMs = metrics.latency_impact * 1000;
        } else {
          deltaMs = metrics.latency_impact;
        }
      }
    }

    // Conditional coloring logic:
    // - Green for negative/faster
    // - Neutral for zero
    // - Amber/Rose for positive/regression
    let colorClass = 'text-zinc-600 dark:text-zinc-400';
    let badgeBg = 'bg-zinc-100 text-zinc-600 border-zinc-200 dark:bg-zinc-800 dark:text-zinc-400 dark:border-zinc-700';
    let statusLabel = 'Neutral';
    let DirectionIcon = Minus;

    if (deltaMs !== null) {
      if (deltaMs < -0.05) {
        // Faster execution -> Green
        colorClass = 'text-emerald-600 dark:text-emerald-400';
        badgeBg = 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800';
        statusLabel = 'Faster';
        DirectionIcon = TrendingDown;
      } else if (Math.abs(deltaMs) <= 0.05) {
        // Zero delta -> Neutral
        colorClass = 'text-zinc-600 dark:text-zinc-400';
        badgeBg = 'bg-zinc-100 text-zinc-600 border-zinc-200 dark:bg-zinc-800 dark:text-zinc-400 dark:border-zinc-700';
        statusLabel = '0.0ms delta';
        DirectionIcon = Minus;
      } else {
        // Positive overhead -> Amber or Rose
        const severe = deltaMs > 25.0 || isRegression;
        colorClass = severe ? 'text-rose-600 dark:text-rose-400' : 'text-amber-600 dark:text-amber-400';
        badgeBg = severe
          ? 'bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/40 dark:text-rose-300 dark:border-rose-800'
          : 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800';
        statusLabel = severe ? 'Regression' : 'Overhead';
        DirectionIcon = TrendingUp;
      }
    }

    const formatted = deltaMs !== null
      ? `${deltaMs > 0 ? '+' : ''}${deltaMs.toFixed(1)}ms`
      : '--';

    return {
      deltaMs,
      formatted,
      colorClass,
      badgeBg,
      statusLabel,
      DirectionIcon,
      description,
      isRegression,
      hasData: deltaMs !== null,
    };
  }, [metrics]);

  // 4. Parse Confidence Percentage ({confidence * 100}%)
  const confidenceStats = useMemo(() => {
    let rawScore: number | null = null;

    if (metrics) {
      if (typeof metrics.confidence === 'number' && !isNaN(metrics.confidence)) {
        rawScore = metrics.confidence;
      } else if (typeof metrics.confidence_score === 'number' && !isNaN(metrics.confidence_score)) {
        rawScore = metrics.confidence_score;
      }
    }

    let percentage: number | null = null;
    if (rawScore !== null) {
      percentage = rawScore <= 1.0 ? rawScore * 100 : rawScore;
      percentage = Math.max(0, Math.min(100, percentage));
    }

    const formatted = percentage !== null
      ? `${percentage.toFixed(1)}%`
      : '--';

    let colorClass = 'text-zinc-600 dark:text-zinc-400';
    let barColor = 'bg-zinc-400';
    let badgeLabel = 'Pending';

    if (percentage !== null) {
      if (percentage >= 90.0) {
        colorClass = 'text-emerald-600 dark:text-emerald-400';
        barColor = 'bg-emerald-500';
        badgeLabel = 'High Confidence';
      } else if (percentage >= 70.0) {
        colorClass = 'text-blue-600 dark:text-blue-400';
        barColor = 'bg-blue-500';
        badgeLabel = 'Standard';
      } else if (percentage >= 50.0) {
        colorClass = 'text-amber-600 dark:text-amber-400';
        barColor = 'bg-amber-500';
        badgeLabel = 'Moderate';
      } else {
        colorClass = 'text-rose-600 dark:text-rose-400';
        barColor = 'bg-rose-500';
        badgeLabel = 'Review Advised';
      }
    }

    return {
      rawScore,
      percentage,
      formatted,
      colorClass,
      barColor,
      badgeLabel,
      hasData: percentage !== null,
    };
  }, [metrics]);

  return (
    <div
      id="metrics-card-root"
      className={`bg-white dark:bg-zinc-900 border border-zinc-200/90 dark:border-zinc-800 rounded-2xl shadow-xs overflow-hidden transition-all ${className}`}
    >
      {/* Optional Card Header */}
      {title && (
        <div className="px-4 py-3 border-b border-zinc-100 dark:border-zinc-800/80 flex items-center justify-between bg-zinc-50/50 dark:bg-zinc-900/50">
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-zinc-500 dark:text-zinc-400" />
            <span className="text-xs font-semibold text-zinc-800 dark:text-zinc-200 uppercase tracking-wider font-mono">
              {title}
            </span>
          </div>

          <div className="flex items-center gap-2">
            {isComputing && (
              <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono font-medium bg-amber-50 text-amber-700 border border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800 animate-pulse">
                <Loader2 className="h-2.5 w-2.5 animate-spin" />
                <span>Computing Metrics...</span>
              </span>
            )}
            {!isComputing && metrics && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono font-medium bg-emerald-50 text-emerald-700 border border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800">
                <Sparkles className="h-2.5 w-2.5" />
                <span>Live Telemetry</span>
              </span>
            )}
            {!isComputing && !metrics && (
              <span className="text-[10px] font-mono text-zinc-400">
                Awaiting Execution
              </span>
            )}
          </div>
        </div>
      )}

      {/* Grid of 3 Dynamic Metric Tiles */}
      <div className={`p-4 grid gap-3 ${compact ? 'grid-cols-1 sm:grid-cols-3' : 'grid-cols-1 sm:grid-cols-3'}`}>
        {/* =========================================================================
            TILE 1: DIFF SIZE (+{added} / -{deleted})
            ========================================================================= */}
        <div
          id="metrics-diff-size"
          className="bg-zinc-50/70 dark:bg-zinc-800/40 border border-zinc-200/70 dark:border-zinc-700/60 rounded-xl p-3.5 flex flex-col justify-between transition hover:border-zinc-300 dark:hover:border-zinc-600"
        >
          <div className="flex items-center justify-between text-zinc-500 dark:text-zinc-400 mb-1.5">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider">
              <GitCommit className="h-3.5 w-3.5" />
              <span>Diff Size</span>
            </div>
            {diffStats.filesChanged !== null && (
              <span className="text-[10px] font-mono text-zinc-500 dark:text-zinc-400 flex items-center gap-1">
                <FileCode className="h-3 w-3" />
                <span>{diffStats.filesChanged} file{diffStats.filesChanged === 1 ? '' : 's'}</span>
              </span>
            )}
          </div>

          <div className="mt-1">
            {isComputing ? (
              <div className="flex items-center gap-2">
                <div className="h-6 w-24 bg-zinc-200 dark:bg-zinc-700/60 rounded animate-pulse" />
                <span className="text-[11px] font-mono text-zinc-400">computing</span>
              </div>
            ) : diffStats.hasData ? (
              <div className="flex items-baseline gap-1.5 font-mono">
                <span className="text-xl font-bold text-emerald-600 dark:text-emerald-400">
                  +{diffStats.added}
                </span>
                <span className="text-zinc-400 font-semibold text-base">/</span>
                <span className="text-xl font-bold text-rose-600 dark:text-rose-400">
                  -{diffStats.deleted}
                </span>
              </div>
            ) : (
              <div className="text-xl font-bold font-mono text-zinc-400 dark:text-zinc-500">
                --
              </div>
            )}
          </div>

          <div className="mt-2 text-[11px] text-zinc-500 dark:text-zinc-400 flex items-center justify-between">
            <span>Insertions / Deletions</span>
            {diffStats.hasData && (
              <span className="font-mono text-[10px] text-zinc-400">
                {(diffStats.added ?? 0) + (diffStats.deleted ?? 0)} lines total
              </span>
            )}
          </div>
        </div>

        {/* =========================================================================
            TILE 2: LATENCY IMPACT (Millisecond Delta with Conditional Coloring)
            ========================================================================= */}
        <div
          id="metrics-latency-impact"
          className="bg-zinc-50/70 dark:bg-zinc-800/40 border border-zinc-200/70 dark:border-zinc-700/60 rounded-xl p-3.5 flex flex-col justify-between transition hover:border-zinc-300 dark:hover:border-zinc-600"
        >
          <div className="flex items-center justify-between text-zinc-500 dark:text-zinc-400 mb-1.5">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider">
              <Timer className="h-3.5 w-3.5" />
              <span>Latency Impact</span>
            </div>

            {latencyStats.hasData && (
              <span
                className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-mono font-semibold border ${latencyStats.badgeBg}`}
              >
                <latencyStats.DirectionIcon className="h-2.5 w-2.5" />
                <span>{latencyStats.statusLabel}</span>
              </span>
            )}
          </div>

          <div className="mt-1">
            {isComputing ? (
              <div className="flex items-center gap-2">
                <div className="h-6 w-20 bg-zinc-200 dark:bg-zinc-700/60 rounded animate-pulse" />
                <span className="text-[11px] font-mono text-zinc-400">computing</span>
              </div>
            ) : latencyStats.hasData ? (
              <div className={`text-xl font-bold font-mono ${latencyStats.colorClass}`}>
                {latencyStats.formatted}
              </div>
            ) : (
              <div className="text-xl font-bold font-mono text-zinc-400 dark:text-zinc-500">
                --
              </div>
            )}
          </div>

          <div className="mt-2 text-[11px] text-zinc-500 dark:text-zinc-400 flex items-center justify-between">
            <span>Execution Duration Delta</span>
            {latencyStats.deltaMs !== null && (
              <span className="font-mono text-[10px] text-zinc-400">
                {latencyStats.deltaMs < -0.05 ? 'performance improved' : 'overhead evaluated'}
              </span>
            )}
          </div>
        </div>

        {/* =========================================================================
            TILE 3: CONFIDENCE SCORE ({confidence * 100}% rounded to 1 decimal place)
            ========================================================================= */}
        <div
          id="metrics-confidence"
          className="bg-zinc-50/70 dark:bg-zinc-800/40 border border-zinc-200/70 dark:border-zinc-700/60 rounded-xl p-3.5 flex flex-col justify-between transition hover:border-zinc-300 dark:hover:border-zinc-600"
        >
          <div className="flex items-center justify-between text-zinc-500 dark:text-zinc-400 mb-1.5">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider">
              <ShieldCheck className="h-3.5 w-3.5" />
              <span>Confidence</span>
            </div>

            {confidenceStats.hasData && (
              <span className="text-[10px] font-mono font-semibold text-zinc-500 dark:text-zinc-400">
                {confidenceStats.badgeLabel}
              </span>
            )}
          </div>

          <div className="mt-1">
            {isComputing ? (
              <div className="flex items-center gap-2">
                <div className="h-6 w-20 bg-zinc-200 dark:bg-zinc-700/60 rounded animate-pulse" />
                <span className="text-[11px] font-mono text-zinc-400">computing</span>
              </div>
            ) : confidenceStats.hasData ? (
              <div className={`text-xl font-bold font-mono ${confidenceStats.colorClass}`}>
                {confidenceStats.formatted}
              </div>
            ) : (
              <div className="text-xl font-bold font-mono text-zinc-400 dark:text-zinc-500">
                --
              </div>
            )}
          </div>

          {/* Miniature Progress Bar */}
          <div className="mt-2">
            <div className="w-full bg-zinc-200 dark:bg-zinc-700 h-1.5 rounded-full overflow-hidden">
              <div
                className={`h-full transition-all duration-500 ${confidenceStats.barColor}`}
                style={{
                  width: `${confidenceStats.percentage ?? 0}%`,
                }}
              />
            </div>
            <div className="mt-1 text-[10px] text-zinc-400 flex items-center justify-between">
              <span>Verifier &amp; Test Weight</span>
              <span className="font-mono">
                {confidenceStats.hasData ? `${confidenceStats.percentage?.toFixed(1)}/100` : '--'}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default MetricsCard;
