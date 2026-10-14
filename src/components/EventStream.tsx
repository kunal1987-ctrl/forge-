import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  RefreshCw,
  AlertTriangle,
  Activity,
  Wifi,
  WifiOff,
  CheckCircle2,
  ArrowDown,
  Terminal,
  Copy,
  Check,
} from 'lucide-react';

export type ConnectionStatus =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'disconnected'
  | 'completed'
  | 'error';

export interface StreamEvent {
  task_id?: string;
  node?: string;
  step?: number;
  status?: string;
  message?: string;
  event?: string;
  type?: string;
  timestamp?: string;
  payload?: Record<string, unknown>;
  data?: unknown;
}

export interface StreamLogEntry {
  id: string;
  step: string;
  message: string;
  timestamp: string;
  type?: 'log' | 'heartbeat' | 'error' | 'transition';
  status?: string;
  node?: string;
}

export interface EventStreamProps {
  taskId: string | null;
  onEvent?: (event: StreamEvent) => void;
  onStatusChange?: (status: ConnectionStatus) => void;
  onLogsUpdate?: (logs: StreamLogEntry[]) => void;
  endpointUrl?: string;
  autoReconnect?: boolean;
  maxRetries?: number;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  backoffFactor?: number;
  heartbeatTimeoutMs?: number;
  className?: string;
  showUI?: boolean;
  children?: (state: EventStreamState) => React.ReactNode;
}

export interface EventStreamState {
  connectionStatus: ConnectionStatus;
  retryAttempt: number;
  nextRetryDelayMs: number;
  lastHeartbeat: string | null;
  logs: StreamLogEntry[];
  events: StreamEvent[];
  reconnect: () => void;
  disconnect: () => void;
  isUserScrolledUp: boolean;
  scrollToBottom: () => void;
}

// Persistent historical cache across tab switches within the same application session
const TASK_LOGS_CACHE = new Map<string, StreamLogEntry[]>();
const TASK_EVENTS_CACHE = new Map<string, StreamEvent[]>();

/**
 * Enterprise EventStream & Terminal Log Viewer (Pillar 2)
 *
 * Capabilities:
 * 1. Persistent Log Retention: Preserves complete historical telemetry even after the workflow
 *    reaches terminal status ('completed' or 'failed') or disconnects. Never resets to an idle placeholder.
 * 2. Smart Auto-Scroll with User Inspection: Automatically sticks to the latest log entry
 *    as events stream in, but gracefully pauses auto-scroll when user inspects history upwards.
 * 3. Reconnect Engine: Resilient backoff with heartbeat watchdog keepalive.
 */
export const EventStream: React.FC<EventStreamProps> = ({
  taskId,
  onEvent,
  onStatusChange,
  onLogsUpdate,
  endpointUrl,
  autoReconnect = true,
  maxRetries = 10,
  initialBackoffMs = 1000,
  maxBackoffMs = 30000,
  backoffFactor = 1.5,
  heartbeatTimeoutMs = 35000,
  className = '',
  showUI = true,
  children,
}) => {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('idle');
  const [retryAttempt, setRetryAttempt] = useState<number>(0);
  const [nextRetryDelayMs, setNextRetryDelayMs] = useState<number>(0);
  const [lastHeartbeat, setLastHeartbeat] = useState<string | null>(null);

  // Initialize with cached historical telemetry if previously loaded
  const [logs, setLogs] = useState<StreamLogEntry[]>(() => {
    return taskId && TASK_LOGS_CACHE.has(taskId) ? TASK_LOGS_CACHE.get(taskId)! : [];
  });
  const [events, setEvents] = useState<StreamEvent[]>(() => {
    return taskId && TASK_EVENTS_CACHE.has(taskId) ? TASK_EVENTS_CACHE.get(taskId)! : [];
  });

  const [isUserScrolledUp, setIsUserScrolledUp] = useState<boolean>(false);
  const [hasCopied, setHasCopied] = useState<boolean>(false);

  const eventSourceRef = useRef<EventSource | null>(null);
  const retryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatWatchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef<number>(0);
  const isManuallyClosedRef = useRef<boolean>(false);
  const terminalStatusRef = useRef<'completed' | 'failed' | null>(null);
  const startTimeRef = useRef<number>(Date.now());
  const logContainerRef = useRef<HTMLDivElement | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  // Notify parent of status changes
  const updateStatus = useCallback(
    (newStatus: ConnectionStatus) => {
      setConnectionStatus(newStatus);
      if (onStatusChange) {
        onStatusChange(newStatus);
      }
    },
    [onStatusChange]
  );

  // Clear watchdog timer
  const clearHeartbeatWatchdog = useCallback(() => {
    if (heartbeatWatchdogRef.current) {
      clearTimeout(heartbeatWatchdogRef.current);
      heartbeatWatchdogRef.current = null;
    }
  }, []);

  // Proactive watchdog: If no message or heartbeat is received in 35s, reconnect
  const resetHeartbeatWatchdog = useCallback(() => {
    clearHeartbeatWatchdog();
    // Do not set watchdog if workflow concluded
    if (terminalStatusRef.current) {
      return;
    }
    heartbeatWatchdogRef.current = setTimeout(() => {
      if (terminalStatusRef.current) return;
      console.warn(
        `[EventStream] Heartbeat watchdog timeout (${heartbeatTimeoutMs}ms elapsed). Reconnecting...`
      );
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      triggerReconnect();
    }, heartbeatTimeoutMs);
  }, [clearHeartbeatWatchdog, heartbeatTimeoutMs]);

  // Clean close of active stream without clearing log history
  const disconnect = useCallback(() => {
    isManuallyClosedRef.current = true;
    clearHeartbeatWatchdog();
    if (retryTimeoutRef.current) {
      clearTimeout(retryTimeoutRef.current);
      retryTimeoutRef.current = null;
    }
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    if (!terminalStatusRef.current) {
      updateStatus('disconnected');
    }
  }, [clearHeartbeatWatchdog, updateStatus]);

  // Exponential backoff reconnection logic
  const triggerReconnect = useCallback(() => {
    // If workflow has already reached terminal completion or failure, do not reconnect
    if (terminalStatusRef.current) {
      return;
    }
    if (!autoReconnect || isManuallyClosedRef.current || !taskId) {
      return;
    }

    if (attemptRef.current >= maxRetries) {
      console.error(`[EventStream] Max reconnect retries (${maxRetries}) exhausted.`);
      updateStatus('error');
      return;
    }

    attemptRef.current += 1;
    const currentAttempt = attemptRef.current;
    setRetryAttempt(currentAttempt);

    const baseDelay = initialBackoffMs * Math.pow(backoffFactor, currentAttempt - 1);
    const jitter = Math.random() * 400;
    const computedDelay = Math.min(baseDelay + jitter, maxBackoffMs);

    setNextRetryDelayMs(Math.round(computedDelay));
    updateStatus('reconnecting');

    console.info(
      `[EventStream] Stream disconnected. Scheduling reconnect attempt #${currentAttempt} in ${Math.round(
        computedDelay
      )}ms...`
    );

    if (retryTimeoutRef.current) {
      clearTimeout(retryTimeoutRef.current);
    }

    retryTimeoutRef.current = setTimeout(() => {
      connect();
    }, computedDelay);
  }, [
    autoReconnect,
    taskId,
    maxRetries,
    initialBackoffMs,
    backoffFactor,
    maxBackoffMs,
    updateStatus,
  ]);

  // Connect to SSE Endpoint
  const connect = useCallback(() => {
    if (!taskId) {
      updateStatus('idle');
      return;
    }

    // If workflow already concluded, do not re-open active stream
    if (terminalStatusRef.current === 'completed' || terminalStatusRef.current === 'failed') {
      updateStatus('completed');
      return;
    }

    isManuallyClosedRef.current = false;
    clearHeartbeatWatchdog();

    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    updateStatus('connecting');

    const baseUrl =
      endpointUrl ||
      (typeof window !== 'undefined'
        ? `/api/tasks/${taskId}/events`
        : `http://localhost:8000/api/tasks/${taskId}/events`);

    try {
      const sse = new EventSource(baseUrl);
      eventSourceRef.current = sse;

      sse.onopen = () => {
        console.info(`[EventStream] Connection established to ${baseUrl}`);
        updateStatus('connected');
        attemptRef.current = 0;
        setRetryAttempt(0);
        setNextRetryDelayMs(0);
        resetHeartbeatWatchdog();
      };

      sse.onmessage = (e: MessageEvent) => {
        resetHeartbeatWatchdog();
        try {
          if (!e.data) return;
          const payload: StreamEvent = JSON.parse(e.data);

          // Update event history
          setEvents((prev) => {
            const nextEvents = [...prev, payload];
            if (taskId) {
              TASK_EVENTS_CACHE.set(taskId, nextEvents);
            }
            return nextEvents;
          });

          if (onEvent) {
            onEvent(payload);
          }

          // Format timestamp
          const elapsedSecs = Math.floor((Date.now() - startTimeRef.current) / 1000);
          const mins = Math.floor(elapsedSecs / 60);
          const secs = elapsedSecs % 60;
          const formattedTimestamp = `${String(mins).padStart(2, '0')}:${String(secs).padStart(
            2,
            '0'
          )}s`;

          const messageText =
            payload.message ||
            (typeof payload.data === 'string'
              ? payload.data
              : `Transitioned to ${payload.node || `step ${payload.step || 1}`}`);

          const newLog: StreamLogEntry = {
            id: `log-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            step: String(payload.step || 1),
            message: messageText,
            timestamp: payload.timestamp
              ? new Date(payload.timestamp).toLocaleTimeString()
              : formattedTimestamp,
            type: payload.status === 'failed' ? 'error' : 'log',
            status: payload.status,
            node: payload.node,
          };

          setLogs((prev) => {
            const nextLogs = [...prev, newLog];
            if (taskId) {
              TASK_LOGS_CACHE.set(taskId, nextLogs);
            }
            if (onLogsUpdate) {
              onLogsUpdate(nextLogs);
            }
            return nextLogs;
          });

          // Check if workflow reached a terminal status
          if (payload.status === 'completed' || payload.status === 'failed') {
            console.info(`[EventStream] Terminal execution status '${payload.status}' received.`);
            terminalStatusRef.current = payload.status as 'completed' | 'failed';
            clearHeartbeatWatchdog();
            updateStatus('completed');
            if (eventSourceRef.current) {
              eventSourceRef.current.close();
              eventSourceRef.current = null;
            }
          }
        } catch (err) {
          console.warn('[EventStream] Error parsing message chunk:', err, e.data);
        }
      };

      sse.addEventListener('ping', (e: MessageEvent) => {
        resetHeartbeatWatchdog();
        try {
          if (e.data) {
            const parsed = JSON.parse(e.data);
            if (parsed.timestamp) {
              setLastHeartbeat(new Date(parsed.timestamp).toLocaleTimeString());
              return;
            }
          }
        } catch {
          // ignore
        }
        setLastHeartbeat(new Date().toLocaleTimeString());
      });

      sse.onerror = (err) => {
        clearHeartbeatWatchdog();
        if (eventSourceRef.current) {
          eventSourceRef.current.close();
          eventSourceRef.current = null;
        }

        // If workflow already completed normally, do not trigger an error or reconnect loop
        if (terminalStatusRef.current === 'completed' || terminalStatusRef.current === 'failed') {
          updateStatus('completed');
          return;
        }

        console.warn('[EventStream] Connection dropped, reconnecting...', err);
        triggerReconnect();
      };
    } catch (err) {
      console.error('[EventStream] Exception initializing EventSource:', err);
      if (!terminalStatusRef.current) {
        triggerReconnect();
      }
    }
  }, [
    taskId,
    endpointUrl,
    updateStatus,
    clearHeartbeatWatchdog,
    resetHeartbeatWatchdog,
    onEvent,
    onLogsUpdate,
    triggerReconnect,
  ]);

  // Auto-scroll handler: detect if user scrolled up to inspect logs
  const handleScroll = () => {
    const el = logContainerRef.current;
    if (!el) return;
    const threshold = 45; // pixels from bottom
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsUserScrolledUp(distanceFromBottom > threshold);
  };

  const scrollToBottom = useCallback(() => {
    setIsUserScrolledUp(false);
    if (logContainerRef.current) {
      logContainerRef.current.scrollTo({
        top: logContainerRef.current.scrollHeight,
        behavior: 'smooth',
      });
    }
  }, []);

  // Stick to bottom when new logs arrive, UNLESS user is actively inspecting history upwards
  useEffect(() => {
    if (!isUserScrolledUp && logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [logs, isUserScrolledUp]);

  // Connect when taskId becomes available or changes
  useEffect(() => {
    if (taskId) {
      startTimeRef.current = Date.now();
      attemptRef.current = 0;
      terminalStatusRef.current = null;

      // Restore logs from cache if available, otherwise initialize empty
      if (TASK_LOGS_CACHE.has(taskId)) {
        // oxlint-disable-next-line react/set-state-in-effect
        setLogs(TASK_LOGS_CACHE.get(taskId)!);
      } else {
        // oxlint-disable-next-line react/set-state-in-effect
        setLogs([]);
      }

      if (TASK_EVENTS_CACHE.has(taskId)) {
        // oxlint-disable-next-line react/set-state-in-effect
        setEvents(TASK_EVENTS_CACHE.get(taskId)!);
      } else {
        // oxlint-disable-next-line react/set-state-in-effect
        setEvents([]);
      }

      connect();
    } else {
      disconnect();
    }

    return () => {
      disconnect();
    };
  }, [taskId, connect, disconnect]);

  // Copy logs to clipboard
  const handleCopyLogs = () => {
    if (logs.length === 0) return;
    const text = logs
      .map((l) => `[${l.timestamp}] [Step ${l.step}] ${l.message}`)
      .join('\n');
    navigator.clipboard.writeText(text).then(() => {
      setHasCopied(true);
      setTimeout(() => setHasCopied(false), 2000);
    });
  };

  const isWorkflowCompleted =
    terminalStatusRef.current === 'completed' || connectionStatus === 'completed';
  const isWorkflowFailed = terminalStatusRef.current === 'failed';

  const stateData: EventStreamState = {
    connectionStatus,
    retryAttempt,
    nextRetryDelayMs,
    lastHeartbeat,
    logs,
    events,
    reconnect: () => {
      terminalStatusRef.current = null;
      attemptRef.current = 0;
      connect();
    },
    disconnect,
    isUserScrolledUp,
    scrollToBottom,
  };

  if (children) {
    return <>{children(stateData)}</>;
  }

  if (!showUI) {
    return null;
  }

  return (
    <div
      className={`relative rounded-2xl border border-zinc-200/90 bg-white shadow-sm overflow-hidden flex flex-col font-mono text-xs ${className}`}
    >
      {/* Terminal Top Control Bar */}
      <div className="flex items-center justify-between px-4 py-3 bg-zinc-50 border-b border-zinc-200/80">
        <div className="flex items-center gap-2.5">
          <Terminal className="h-4 w-4 text-zinc-700" />
          <span className="text-xs font-bold text-zinc-900 uppercase tracking-wider">
            Execution Log &amp; Stream
          </span>

          {/* Connection Status Badge */}
          {connectionStatus === 'connected' && (
            <span className="flex items-center gap-1.5 text-emerald-800 bg-emerald-50 px-2.5 py-0.5 rounded-full border border-emerald-300 text-[11px] font-semibold">
              <Wifi className="h-3 w-3 animate-pulse text-emerald-600" />
              <span>Live Stream Active</span>
            </span>
          )}

          {isWorkflowCompleted && (
            <span className="flex items-center gap-1.5 text-emerald-800 bg-emerald-50 px-2.5 py-0.5 rounded-full border border-emerald-300 text-[11px] font-semibold">
              <CheckCircle2 className="h-3 w-3 text-emerald-600" />
              <span>Workflow Completed</span>
            </span>
          )}

          {isWorkflowFailed && (
            <span className="flex items-center gap-1.5 text-rose-800 bg-rose-50 px-2.5 py-0.5 rounded-full border border-rose-300 text-[11px] font-semibold">
              <AlertTriangle className="h-3 w-3 text-rose-600" />
              <span>Workflow Failed</span>
            </span>
          )}

          {connectionStatus === 'reconnecting' && (
            <span className="flex items-center gap-1.5 text-amber-800 bg-amber-50 px-2.5 py-0.5 rounded-full border border-amber-300 text-[11px] font-semibold">
              <RefreshCw className="h-3 w-3 animate-spin text-amber-600" />
              <span>
                Reconnecting ({retryAttempt}/{maxRetries} in {Math.ceil(nextRetryDelayMs / 1000)}s)
              </span>
            </span>
          )}

          {connectionStatus === 'connecting' && (
            <span className="flex items-center gap-1.5 text-blue-800 bg-blue-50 px-2.5 py-0.5 rounded-full border border-blue-300 text-[11px] font-semibold">
              <RefreshCw className="h-3 w-3 animate-spin text-blue-600" />
              <span>Connecting to stream...</span>
            </span>
          )}

          {(connectionStatus === 'disconnected' || connectionStatus === 'idle') &&
            !isWorkflowCompleted &&
            !isWorkflowFailed && (
              <span className="flex items-center gap-1.5 text-zinc-500 bg-zinc-100 px-2.5 py-0.5 rounded-full border border-zinc-200 text-[11px]">
                <WifiOff className="h-3 w-3" />
                <span>Stream Inactive</span>
              </span>
            )}

          {connectionStatus === 'error' && (
            <span className="flex items-center gap-1.5 text-rose-800 bg-rose-50 px-2.5 py-0.5 rounded-full border border-rose-300 text-[11px] font-semibold">
              <AlertTriangle className="h-3 w-3 text-rose-600" />
              <span>Connection Failed</span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-3 text-[11px] text-zinc-500">
          {logs.length > 0 && (
            <span className="hidden sm:inline-block text-zinc-400">
              {logs.length} {logs.length === 1 ? 'event' : 'events'} logged
            </span>
          )}

          {lastHeartbeat && connectionStatus === 'connected' && (
            <span className="hidden md:flex items-center gap-1 text-zinc-500">
              <Activity className="h-3 w-3 text-emerald-500" />
              <span>Ping: {lastHeartbeat}</span>
            </span>
          )}

          {logs.length > 0 && (
            <button
              type="button"
              onClick={handleCopyLogs}
              title="Copy all logs to clipboard"
              className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-white hover:bg-zinc-100 text-zinc-700 border border-zinc-200 transition shadow-xs text-xs"
            >
              {hasCopied ? (
                <>
                  <Check className="h-3 w-3 text-emerald-600" />
                  <span className="text-emerald-700 font-semibold">Copied!</span>
                </>
              ) : (
                <>
                  <Copy className="h-3 w-3 text-zinc-500" />
                  <span>Copy</span>
                </>
              )}
            </button>
          )}

          {connectionStatus !== 'connected' && !isWorkflowCompleted && (
            <button
              type="button"
              onClick={stateData.reconnect}
              className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-white font-medium transition shadow-xs text-xs"
            >
              <RefreshCw className="h-3 w-3" />
              <span>Reconnect</span>
            </button>
          )}
        </div>
      </div>

      {/* Terminal Output Log Container */}
      <div
        ref={logContainerRef}
        onScroll={handleScroll}
        className="relative p-4 space-y-2 max-h-80 overflow-y-auto bg-zinc-950 text-zinc-100 selection:bg-zinc-700"
      >
        {/* If no logs have been recorded yet */}
        {logs.length === 0 ? (
          <div className="py-10 text-center flex flex-col items-center justify-center space-y-2 text-zinc-400">
            <Terminal className="h-6 w-6 text-zinc-600" />
            <p className="text-xs">
              {connectionStatus === 'connecting'
                ? 'Initializing telemetry stream and awaiting first node entry...'
                : 'No telemetry events received yet. Dispatch a task to view execution output.'}
            </p>
          </div>
        ) : (
          /* Render full persistent historical log lines */
          logs.map((log) => {
            const isError =
              log.type === 'error' ||
              log.message.toLowerCase().includes('failed') ||
              log.message.toLowerCase().includes('error');
            const isSuccess =
              log.message.toLowerCase().includes('success') ||
              log.message.toLowerCase().includes('passed') ||
              log.message.toLowerCase().includes('pr created');
            const isTransition =
              log.message.toLowerCase().includes('entering') ||
              log.message.toLowerCase().includes('transition');

            return (
              <div
                key={log.id}
                className="flex items-start gap-2.5 py-0.5 leading-relaxed hover:bg-zinc-900/60 rounded px-1.5 transition-colors"
              >
                {/* Monospace Timestamp */}
                <span className="text-zinc-500 select-none text-[11px] shrink-0 font-mono">
                  [{log.timestamp}]
                </span>

                {/* Step / Node Badge */}
                <span
                  className={`px-1.5 py-0.2 rounded text-[10px] font-bold uppercase shrink-0 font-mono border ${
                    isError
                      ? 'bg-rose-950/80 text-rose-300 border-rose-800'
                      : isSuccess
                      ? 'bg-emerald-950/80 text-emerald-300 border-emerald-800'
                      : isTransition
                      ? 'bg-blue-950/80 text-blue-300 border-blue-800'
                      : 'bg-zinc-800 text-zinc-300 border-zinc-700'
                  }`}
                >
                  Step {log.step}
                </span>

                {/* Log Content */}
                <span
                  className={`flex-1 break-words font-mono text-xs ${
                    isError
                      ? 'text-rose-300'
                      : isSuccess
                      ? 'text-emerald-300'
                      : 'text-zinc-200'
                  }`}
                >
                  {log.message}
                </span>
              </div>
            );
          })
        )}
        <div ref={logEndRef} className="h-0" />
      </div>

      {/* Floating "Jump to Latest" Pill when User Inspects Logs Upwards */}
      {isUserScrolledUp && logs.length > 0 && (
        <button
          type="button"
          onClick={scrollToBottom}
          className="absolute bottom-4 right-4 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-emerald-600 hover:bg-emerald-500 text-white text-[11px] font-mono font-bold shadow-lg transition transform hover:scale-105 active:scale-95 z-20"
        >
          <ArrowDown className="h-3.5 w-3.5 animate-bounce" />
          <span>Jump to latest</span>
        </button>
      )}
    </div>
  );
};

export default EventStream;
