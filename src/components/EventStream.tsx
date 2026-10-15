import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  RefreshCw,
  AlertTriangle,
  Activity,
  WifiOff,
  CheckCircle2,
  ArrowDown,
  Terminal,
  Copy,
  Check,
  Trash2,
} from 'lucide-react';
import {
  useEventStream,
  TERMINAL_STREAM_STATUSES,
  type StreamConnectionStatus,
  type StreamEvent,
  type StreamLogEntry,
  type StreamLogLevel,
  type UseEventStreamReturn,
} from '../hooks/useEventStream';

export type ConnectionStatus = StreamConnectionStatus;
export type { StreamEvent, StreamLogEntry, StreamLogLevel };

export interface EventStreamProps {
  taskId: string | null;
  /**
   * Current task status. When provided, the stream auto-connects the moment the
   * task enters an active status (RUNNING et al) and tears down on a terminal one.
   */
  taskStatus?: string | null;
  endpointUrl?: string;
  autoReconnect?: boolean;
  maxRetries?: number;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  backoffFactor?: number;
  heartbeatTimeoutMs?: number;
  maxEntries?: number;
  maxHeight?: string;
  title?: string;
  onEvent?: (event: StreamEvent) => void;
  onStatusChange?: (status: StreamConnectionStatus) => void;
  onLogsUpdate?: (logs: StreamLogEntry[]) => void;
  className?: string;
  showUI?: boolean;
  children?: (state: UseEventStreamReturn) => React.ReactNode;
}

/**
 * Per-level terminal palette. Every level carries a distinct colour, a colour bar
 * and a text tag, so level is never communicated by colour alone.
 */
const LEVEL_STYLES: Record<
  StreamLogLevel,
  { text: string; badge: string; bar: string; tag: string }
> = {
  info: {
    text: 'text-zinc-200',
    badge: 'bg-zinc-800 text-zinc-300 border-zinc-700',
    bar: 'bg-zinc-600',
    tag: 'INFO',
  },
  success: {
    text: 'text-emerald-300',
    badge: 'bg-emerald-950/80 text-emerald-300 border-emerald-800',
    bar: 'bg-emerald-500',
    tag: 'OK',
  },
  warning: {
    text: 'text-amber-200',
    badge: 'bg-amber-950/80 text-amber-300 border-amber-800',
    bar: 'bg-amber-500',
    tag: 'WARN',
  },
  error: {
    text: 'text-rose-300',
    badge: 'bg-rose-950/80 text-rose-300 border-rose-800',
    bar: 'bg-rose-500',
    tag: 'ERR',
  },
};

interface LogRowProps {
  entry: StreamLogEntry;
}

/**
 * Memoised log line. Streaming appends one entry at a time, so memoisation keeps
 * every previously rendered row from re-rendering on each new event.
 */
const LogRow = React.memo<LogRowProps>(({ entry }) => {
  const style = LEVEL_STYLES[entry.level];
  return (
    <div className="flex items-start gap-2 rounded px-1.5 py-0.5 transition-colors hover:bg-zinc-900/60">
      <span className={`mt-1.5 h-3 w-0.5 shrink-0 rounded-full ${style.bar}`} aria-hidden="true" />
      <span className="shrink-0 select-none font-mono text-[11px] tabular-nums text-zinc-500">
        [{entry.timestamp}]
      </span>
      <span
        className={`w-9 shrink-0 rounded border px-1 text-center font-mono text-[10px] font-bold ${style.badge}`}
        title={entry.level}
      >
        {style.tag}
      </span>
      {entry.nodeLabel && (
        <span className="shrink-0 rounded border border-indigo-900/70 bg-indigo-950/60 px-1.5 font-mono text-[10px] font-semibold text-indigo-300">
          {entry.nodeLabel}
        </span>
      )}
      <span className={`min-w-0 flex-1 whitespace-pre-wrap break-words font-mono text-xs ${style.text}`}>
        {entry.message}
      </span>
      <span className="hidden shrink-0 font-mono text-[10px] tabular-nums text-zinc-600 sm:inline">
        {entry.elapsed}
      </span>
    </div>
  );
});
LogRow.displayName = 'LogRow';

/**
 * Live Event Stream Panel
 *
 * Connection lifecycles, backoff, heartbeat watchdog and replay de-duplication
 * live in `useEventStream`; this component owns presentation only.
 */
export const EventStream: React.FC<EventStreamProps> = ({
  taskId,
  taskStatus,
  endpointUrl,
  autoReconnect = true,
  maxRetries,
  initialBackoffMs,
  maxBackoffMs,
  backoffFactor,
  heartbeatTimeoutMs,
  maxEntries,
  maxHeight = '380px',
  title = 'Execution Log & Stream',
  onEvent,
  onStatusChange,
  onLogsUpdate,
  className = '',
  showUI = true,
  children,
}) => {
  const streamState = useEventStream({
    taskId,
    taskStatus,
    endpointUrl,
    autoReconnect,
    maxRetries,
    initialBackoffMs,
    maxBackoffMs,
    backoffFactor,
    heartbeatTimeoutMs,
    maxEntries,
    onEvent,
  });

  const {
    connectionStatus,
    isLive,
    retryAttempt,
    maxRetries: resolvedMaxRetries,
    nextRetryDelayMs,
    logs,
    events,
    lastHeartbeat,
    error,
    reconnect,
    clear,
  } = streamState;

  const [isUserScrolledUp, setIsUserScrolledUp] = useState<boolean>(false);
  const [hasCopied, setHasCopied] = useState<boolean>(false);

  const viewportRef = useRef<HTMLDivElement | null>(null);

  // Notify parent of connection transitions
  useEffect(() => {
    onStatusChange?.(connectionStatus);
  }, [connectionStatus, onStatusChange]);

  useEffect(() => {
    onLogsUpdate?.(logs);
  }, [logs, onLogsUpdate]);

  useEffect(() => {
    setIsUserScrolledUp(false);
  }, [taskId]);

  const terminalStatus = useMemo(() => {
    const fromProp = (taskStatus || '').trim().toLowerCase();
    if (TERMINAL_STREAM_STATUSES.includes(fromProp)) {
      return fromProp;
    }
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const status = (events[index].status || '').trim().toLowerCase();
      if (TERMINAL_STREAM_STATUSES.includes(status)) return status;
    }
    return null;
  }, [events, taskStatus]);

  const handleScroll = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsUserScrolledUp(distanceFromBottom > 45);
  }, []);

  const scrollToBottom = useCallback((smooth = true) => {
    const el = viewportRef.current;
    if (!el) return;
    setIsUserScrolledUp(false);
    if (smooth) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }, []);

  // Stick to the newest entry unless the user scrolled up to inspect history.
  // Instant assignment (not smooth) avoids animation thrash under fast streams.
  useEffect(() => {
    if (isUserScrolledUp) return;
    const el = viewportRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [logs, isUserScrolledUp]);

  const handleCopyLogs = useCallback(async () => {
    if (logs.length === 0) return;
    const text = logs
      .map((entry) => `[${entry.timestamp}] ${entry.nodeLabel} ${entry.message}`.trim())
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
      setHasCopied(true);
      setTimeout(() => setHasCopied(false), 2000);
    } catch {
      setHasCopied(false);
    }
  }, [logs]);

  const handleClear = useCallback(() => {
    clear();
    setIsUserScrolledUp(false);
  }, [clear]);

  const totalEntries = logs.length;

  if (!showUI && children) {
    return <>{children(streamState)}</>;
  }

  return (
    <div
      className={`relative flex flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-[#0d1117] font-mono shadow-xl ${className}`}
    >
      {/* ── Terminal control bar ─────────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-2 border-b border-zinc-800/80 bg-[#161b22] px-4 py-2.5 select-none">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex items-center gap-1.5" aria-hidden="true">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-rose-500/80" />
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500/80" />
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-emerald-500/80" />
          </div>

          <div className="mx-0.5 h-4 w-px bg-zinc-700/60" aria-hidden="true" />

          <div className="flex items-center gap-2">
            <Terminal className="h-3.5 w-3.5 text-zinc-400" />
            <span className="text-xs font-semibold tracking-wide text-zinc-200">{title}</span>
          </div>

          {/* Connection state badge */}
          {connectionStatus === 'live' && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-400">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
              </span>
              <span>Live Stream</span>
            </span>
          )}

          {connectionStatus === 'connecting' && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-500/40 bg-blue-500/10 px-2 py-0.5 text-[10px] font-semibold text-blue-300">
              <RefreshCw className="h-3 w-3 animate-spin" />
              <span>Connecting</span>
            </span>
          )}

          {connectionStatus === 'reconnecting' && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold text-amber-300">
              <RefreshCw className="h-3 w-3 animate-spin" />
              <span>
                Retry {retryAttempt}/{resolvedMaxRetries} in{' '}
                {Math.max(1, Math.ceil(nextRetryDelayMs / 1000))}s
              </span>
            </span>
          )}

          {connectionStatus === 'idle' && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-zinc-700/50 bg-zinc-800/80 px-2 py-0.5 text-[10px] text-zinc-400">
              <WifiOff className="h-3 w-3" />
              <span>System Idle</span>
            </span>
          )}

          {connectionStatus === 'disconnected' && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-zinc-700/50 bg-zinc-800/80 px-2 py-0.5 text-[10px] text-zinc-400">
              <WifiOff className="h-3 w-3" />
              <span>Stream Paused</span>
            </span>
          )}

          {connectionStatus === 'completed' && terminalStatus !== 'failed' && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-400">
              <CheckCircle2 className="h-3 w-3" />
              <span>Workflow Completed</span>
            </span>
          )}

          {(connectionStatus === 'error' || terminalStatus === 'failed') && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-[10px] font-semibold text-rose-400">
              <AlertTriangle className="h-3 w-3" />
              <span>
                {terminalStatus === 'failed' ? 'Workflow Failed' : 'Connection Failed'}
              </span>
            </span>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2 text-[11px] text-zinc-500">
          {totalEntries > 0 && (
            <span className="hidden font-mono sm:inline">
              {totalEntries} {totalEntries === 1 ? 'event' : 'events'}
            </span>
          )}

          {lastHeartbeat && isLive && (
            <span className="hidden items-center gap-1 font-mono md:flex">
              <Activity className="h-3 w-3 text-emerald-500" />
              <span>ping {lastHeartbeat}</span>
            </span>
          )}

          {totalEntries > 0 && (
            <button
              type="button"
              onClick={handleCopyLogs}
              title="Copy all log lines"
              className="inline-flex items-center gap-1 rounded border border-zinc-700/60 bg-[#21262d] px-2 py-1 text-[11px] text-zinc-300 transition hover:bg-[#30363d]"
            >
              {hasCopied ? (
                <>
                  <Check className="h-3 w-3 text-emerald-400" />
                  <span className="text-emerald-400">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="h-3 w-3 text-zinc-400" />
                  <span>Copy</span>
                </>
              )}
            </button>
          )}

          {totalEntries > 0 && (
            <button
              type="button"
              onClick={handleClear}
              title="Clear terminal"
              className="rounded p-1 text-zinc-400 transition hover:bg-[#21262d] hover:text-zinc-200"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}

          {connectionStatus !== 'live' && connectionStatus !== 'completed' && (
            <button
              type="button"
              onClick={reconnect}
              className="inline-flex items-center gap-1 rounded border border-zinc-700/60 bg-[#21262d] px-2 py-1 text-[11px] text-zinc-200 transition hover:bg-[#30363d]"
            >
              <RefreshCw className="h-3 w-3" />
              <span>Reconnect</span>
            </button>
          )}
        </div>
      </div>

      {/* ── Error banner ─────────────────────────────────────────────────── */}
      {error && (
        <div className="flex items-center gap-2 border-b border-rose-900/50 bg-rose-950/40 px-4 py-2 text-[11px] text-rose-300">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* ── Terminal viewport ────────────────────────────────────────────── */}
      <div
        ref={viewportRef}
        onScroll={handleScroll}
        style={{ maxHeight }}
        className="min-h-[160px] flex-1 space-y-0.5 overflow-y-auto p-4"
        role="log"
        aria-live="polite"
        aria-label="Live task telemetry"
      >
        {logs.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-12 text-center text-zinc-500">
            <Terminal className="h-6 w-6 text-zinc-600" />
            <p className="text-xs">
              {connectionStatus === 'connecting'
                ? 'Opening telemetry stream, awaiting first packet...'
                : connectionStatus === 'live'
                ? 'Stream live. Awaiting first telemetry event...'
                : 'System Idle — dispatch a task to begin live execution output.'}
            </p>
          </div>
        ) : (
          logs.map((entry) => <LogRow key={entry.id} entry={entry} />)
        )}

        {isLive && logs.length > 0 && (
          <div className="flex items-center gap-1.5 px-1.5 pt-1 text-emerald-400">
            <span className="font-semibold text-zinc-500">forge:~$</span>
            <span className="inline-block h-3.5 w-2 animate-pulse bg-emerald-400" />
          </div>
        )}
      </div>

      {/* ── Scroll-resume affordance ─────────────────────────────────────── */}
      {isUserScrolledUp && logs.length > 0 && (
        <button
          type="button"
          onClick={() => scrollToBottom(true)}
          className="absolute bottom-3 right-3 z-20 inline-flex items-center gap-1.5 rounded-full bg-emerald-600 px-3 py-1.5 text-[11px] font-bold text-white shadow-lg transition hover:bg-emerald-500 active:scale-95"
        >
          <ArrowDown className="h-3.5 w-3.5 animate-bounce" />
          <span>Jump to latest</span>
        </button>
      )}
    </div>
  );
};

export default EventStream;
