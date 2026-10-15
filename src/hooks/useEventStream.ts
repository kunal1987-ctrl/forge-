import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE_URL } from '../services/apiClient';

/**
 * Resilient Server-Sent Events connection hook for live task telemetry.
 *
 * Backend route: `GET /api/tasks/{task_id}/events` (see
 * services/api/forge/routers/stream.py). The server replays buffered history on
 * every (re)connect, so this hook de-duplicates replayed events — without that,
 * each reconnect would duplicate the entire log.
 */

export type StreamConnectionStatus =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'disconnected'
  | 'completed'
  | 'error';

export type StreamLogLevel = 'info' | 'success' | 'warning' | 'error';

export interface StreamEvent {
  task_id?: string;
  node?: string;
  step?: number;
  status?: string;
  message?: string;
  event?: string;
  event_type?: string;
  type?: string;
  timestamp?: string;
  error?: string;
  retry_count?: number;
  payload?: Record<string, unknown>;
  data?: unknown;
}

export interface StreamLogEntry {
  id: string;
  level: StreamLogLevel;
  /** Raw node identifier, e.g. `implementer_node`. */
  node: string | null;
  /** Short display badge, e.g. `[implementer]`. */
  nodeLabel: string;
  step: string;
  message: string;
  /** Wall-clock time of the event, `HH:MM:SS`. */
  timestamp: string;
  /** Seconds elapsed since this hook started streaming. */
  elapsed: string;
  status?: string;
  eventType?: string;
}

export interface UseEventStreamOptions {
  /** Task to stream. Falsy keeps the stream idle. */
  taskId: string | null | undefined;
  /**
   * Current task status. When supplied, the stream only connects for statuses in
   * `connectStatuses` and tears down on a terminal status.
   */
  taskStatus?: string | null;
  /** Statuses that should hold the stream open (case-insensitive). */
  connectStatuses?: readonly string[];
  /** Override the full SSE endpoint URL. */
  endpointUrl?: string;
  autoReconnect?: boolean;
  maxRetries?: number;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  backoffFactor?: number;
  /** Silence the stream this long before forcing a reconnect. */
  heartbeatTimeoutMs?: number;
  /** Ring-buffer cap on retained log entries. */
  maxEntries?: number;
  onEvent?: (event: StreamEvent) => void;
}

export interface UseEventStreamReturn {
  connectionStatus: StreamConnectionStatus;
  /** True once the stream is open AND at least one packet has arrived. */
  isLive: boolean;
  retryAttempt: number;
  maxRetries: number;
  nextRetryDelayMs: number;
  logs: StreamLogEntry[];
  events: StreamEvent[];
  lastHeartbeat: string | null;
  error: string | null;
  connect: () => void;
  disconnect: () => void;
  reconnect: () => void;
  clear: () => void;
}

/**
 * Statuses during which the backend can still emit telemetry. `RUNNING` is the
 * headline case, but the engine also emits throughout planning/investigation —
 * gating on the literal string `RUNNING` alone would drop every earlier event.
 */
export const ACTIVE_STREAM_STATUSES: readonly string[] = [
  'running',
  'pending',
  'planning',
  'investigating',
  'reproducing',
  'repro_gate',
  'implementing',
  'implemented',
  'verifying',
  'verified',
  'awaiting_approval',
];

export const TERMINAL_STREAM_STATUSES: readonly string[] = [
  'completed',
  'failed',
  'aborted',
  'cancelled',
  'cleaned_up',
  'pr_created',
];

const DEFAULT_MAX_RETRIES = 10;
const DEFAULT_INITIAL_BACKOFF_MS = 1_000;
const DEFAULT_MAX_BACKOFF_MS = 30_000;
const DEFAULT_BACKOFF_FACTOR = 1.5;
const DEFAULT_HEARTBEAT_TIMEOUT_MS = 35_000;
const DEFAULT_MAX_ENTRIES = 2_000;
/** How many recent dedupe keys to retain. Bounded so long runs cannot leak. */
const DEDUPE_KEY_LIMIT = 4_000;
/** Most recently streamed tasks whose logs stay cached across mounts. */
const MAX_CACHED_TASKS = 10;

interface StreamCacheEntry {
  logs: StreamLogEntry[];
  events: StreamEvent[];
  keys: string[];
}

/**
 * Cross-mount history cache: unmounting the viewer (e.g. switching workspace
 * tabs) must not blank the terminal. Dedupe keys are cached alongside the
 * entries so a replayed backlog cannot duplicate restored history.
 */
const STREAM_CACHE = new Map<string, StreamCacheEntry>();

const normalizeStatus = (value: string | null | undefined): string =>
  (value || '').trim().toLowerCase();

/** `implementer_node` -> `[implementer]`; `__interrupt__` -> `[interrupt]`. */
export function formatNodeLabel(node?: string | null): string {
  if (!node) return '';
  const base = node
    .replace(/_node$/i, '')
    .replace(/^_+|_+$/g, '')
    .trim();
  return base ? `[${base}]` : '';
}

/** Map an event onto one of four log levels for colour coding. */
export function deriveLogLevel(event: StreamEvent): StreamLogLevel {
  const status = normalizeStatus(event.status);
  const message = (event.message || '').toLowerCase();
  const eventType = normalizeStatus(event.event_type || event.event || event.type);
  const errorText = (event.error || '').toLowerCase();

  if (
    status === 'failed' ||
    status === 'aborted' ||
    status === 'error' ||
    eventType.includes('error') ||
    message.includes('error') ||
    message.includes('failed') ||
    message.includes('exception') ||
    message.includes('traceback') ||
    errorText
  ) {
    return 'error';
  }

  if (
    status === 'awaiting_approval' ||
    message.includes('warning') ||
    message.includes('retry') ||
    message.includes('retrying') ||
    message.includes('regression')
  ) {
    return 'warning';
  }

  if (
    status === 'completed' ||
    status === 'pr_created' ||
    status === 'verified' ||
    message.includes('passed') ||
    message.includes('success') ||
    message.includes('created') ||
    message.includes('approved')
  ) {
    return 'success';
  }

  return 'info';
}

/** Stable identity for an event, used to drop SSE replay duplicates. */
function dedupeKeyFor(event: StreamEvent): string {
  return [
    event.timestamp || '',
    event.event_type || event.event || event.type || '',
    event.node || '',
    String(event.step ?? ''),
    event.status || '',
    (event.message || '').slice(0, 120),
  ].join('|');
}

function formatClockTime(iso?: string): string {
  if (iso) {
    const parsed = new Date(iso);
    if (!Number.isNaN(parsed.getTime())) {
      return parsed.toLocaleTimeString([], { hour12: false });
    }
  }
  return new Date().toLocaleTimeString([], { hour12: false });
}

function formatElapsed(startedAtMs: number): string {
  const totalSeconds = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}s`;
}

function buildDefaultEndpoint(taskId: string): string {
  return `${API_BASE_URL}/tasks/${encodeURIComponent(taskId)}/events`;
}

export function useEventStream({
  taskId,
  taskStatus,
  connectStatuses = ACTIVE_STREAM_STATUSES,
  endpointUrl,
  autoReconnect = true,
  maxRetries = DEFAULT_MAX_RETRIES,
  initialBackoffMs = DEFAULT_INITIAL_BACKOFF_MS,
  maxBackoffMs = DEFAULT_MAX_BACKOFF_MS,
  backoffFactor = DEFAULT_BACKOFF_FACTOR,
  heartbeatTimeoutMs = DEFAULT_HEARTBEAT_TIMEOUT_MS,
  maxEntries = DEFAULT_MAX_ENTRIES,
  onEvent,
}: UseEventStreamOptions): UseEventStreamReturn {
  const [connectionStatus, setConnectionStatus] = useState<StreamConnectionStatus>('idle');
  const [logs, setLogs] = useState<StreamLogEntry[]>([]);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [retryAttempt, setRetryAttempt] = useState<number>(0);
  const [nextRetryDelayMs, setNextRetryDelayMs] = useState<number>(0);
  const [lastHeartbeat, setLastHeartbeat] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const sourceRef = useRef<EventSource | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef<number>(0);
  const manuallyClosedRef = useRef<boolean>(false);
  const terminalRef = useRef<boolean>(false);
  const firstPacketRef = useRef<boolean>(false);
  const startedAtRef = useRef<number>(Date.now());
  const dedupeRef = useRef<Set<string>>(new Set());
  const onEventRef = useRef<UseEventStreamOptions['onEvent']>(onEvent);

  // Keep the latest callback without re-creating the connection.
  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);

  const connectRef = useRef<() => void>(() => {});

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const closeSource = useCallback(() => {
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }
  }, []);

  const armWatchdog = useCallback(() => {
    clearWatchdog();
    if (terminalRef.current) return;
    watchdogRef.current = setTimeout(() => {
      if (terminalRef.current) return;
      console.warn(
        `[useEventStream] No packet for ${heartbeatTimeoutMs}ms — forcing reconnect.`
      );
      closeSource();
      if (attemptRef.current < maxRetries) {
        attemptRef.current += 1;
      }
      connectRef.current();
    }, heartbeatTimeoutMs);
  }, [clearWatchdog, closeSource, heartbeatTimeoutMs, maxRetries]);

  /** Mark the stream live. Called on the first data packet OR heartbeat. */
  const markLive = useCallback(() => {
    if (firstPacketRef.current) return;
    firstPacketRef.current = true;
    setConnectionStatus('live');
  }, []);

  const appendEvent = useCallback(
    (event: StreamEvent) => {
      const key = dedupeKeyFor(event);
      if (dedupeRef.current.has(key)) {
        // Server replayed a buffered event we already rendered.
        return;
      }
      dedupeRef.current.add(key);
      if (dedupeRef.current.size > DEDUPE_KEY_LIMIT) {
        // Drop the oldest half; Set preserves insertion order.
        const keys = Array.from(dedupeRef.current);
        dedupeRef.current = new Set(keys.slice(Math.floor(keys.length / 2)));
      }

      setEvents((prev) => {
        const next = [...prev, event];
        return next.length > maxEntries ? next.slice(next.length - maxEntries) : next;
      });

      const rawMessage =
        event.message ||
        (typeof event.data === 'string' ? event.data : '') ||
        (event.node ? `Transitioned to ${event.node}` : `Step ${event.step ?? 1}`);

      const entry: StreamLogEntry = {
        id: `log-${event.timestamp || Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        level: deriveLogLevel(event),
        node: event.node || null,
        nodeLabel: formatNodeLabel(event.node),
        step: String(event.step ?? 1),
        message: rawMessage,
        timestamp: formatClockTime(event.timestamp),
        elapsed: formatElapsed(startedAtRef.current),
        status: event.status,
        eventType: event.event_type || event.event || event.type,
      };

      setLogs((prev) => {
        const next = [...prev, entry];
        return next.length > maxEntries ? next.slice(next.length - maxEntries) : next;
      });

      onEventRef.current?.(event);
    },
    [maxEntries]
  );

  const scheduleReconnect = useCallback(() => {
    if (terminalRef.current || manuallyClosedRef.current || !autoReconnect) return;

    if (attemptRef.current >= maxRetries) {
      setConnectionStatus('error');
      setError(`Stream unavailable after ${maxRetries} reconnect attempts.`);
      return;
    }

    attemptRef.current += 1;
    setRetryAttempt(attemptRef.current);

    const baseDelay = initialBackoffMs * Math.pow(backoffFactor, attemptRef.current - 1);
    const jitter = Math.random() * 400;
    const delay = Math.round(Math.min(baseDelay + jitter, maxBackoffMs));

    setNextRetryDelayMs(delay);
    setConnectionStatus('reconnecting');
    clearRetryTimer();
    retryTimerRef.current = setTimeout(() => connectRef.current(), delay);
  }, [
    autoReconnect,
    backoffFactor,
    clearRetryTimer,
    initialBackoffMs,
    maxBackoffMs,
    maxRetries,
  ]);

  const connect = useCallback(() => {
    if (!taskId) {
      setConnectionStatus('idle');
      return;
    }
    if (terminalRef.current) {
      setConnectionStatus('completed');
      return;
    }

    manuallyClosedRef.current = false;
    setError(null);
    clearWatchdog();
    clearRetryTimer();
    closeSource();

    setConnectionStatus((prev) => (prev === 'reconnecting' ? 'reconnecting' : 'connecting'));

    const url = endpointUrl || buildDefaultEndpoint(taskId);

    try {
      const source = new EventSource(url);
      sourceRef.current = source;

      source.onopen = () => {
        attemptRef.current = 0;
        setRetryAttempt(0);
        setNextRetryDelayMs(0);
        // Deliberately NOT 'live' yet: an open socket can still be silent.
        setConnectionStatus((prev) => (prev === 'reconnecting' ? 'reconnecting' : 'connecting'));
        armWatchdog();
      };

      source.onmessage = (messageEvent: MessageEvent) => {
        armWatchdog();
        markLive();
        if (!messageEvent.data) return;
        try {
          appendEvent(JSON.parse(messageEvent.data) as StreamEvent);
        } catch (parseErr) {
          console.warn('[useEventStream] Unparseable SSE payload:', parseErr);
        }
      };

      source.addEventListener('ping', (pingEvent: MessageEvent) => {
        armWatchdog();
        markLive();
        try {
          const parsed = pingEvent.data ? JSON.parse(pingEvent.data) : null;
          setLastHeartbeat(formatClockTime(parsed?.timestamp));
        } catch {
          setLastHeartbeat(formatClockTime());
        }
      });

      source.onerror = () => {
        clearWatchdog();
        closeSource();
        if (terminalRef.current) {
          setConnectionStatus('completed');
          return;
        }
        console.warn('[useEventStream] Stream dropped; scheduling reconnect.');
        scheduleReconnect();
      };
    } catch (initErr) {
      console.error('[useEventStream] Failed to initialise EventSource:', initErr);
      setError(initErr instanceof Error ? initErr.message : 'Stream initialisation failed');
      scheduleReconnect();
    }
  }, [
    appendEvent,
    armWatchdog,
    clearRetryTimer,
    clearWatchdog,
    closeSource,
    endpointUrl,
    markLive,
    scheduleReconnect,
    taskId,
  ]);

  connectRef.current = connect;

  const disconnect = useCallback(() => {
    manuallyClosedRef.current = true;
    clearRetryTimer();
    clearWatchdog();
    closeSource();
    if (!terminalRef.current) {
      setConnectionStatus('disconnected');
    }
  }, [clearRetryTimer, clearWatchdog, closeSource]);

  const reconnect = useCallback(() => {
    terminalRef.current = false;
    manuallyClosedRef.current = false;
    attemptRef.current = 0;
    setRetryAttempt(0);
    setError(null);
    connectRef.current();
  }, []);

  const clear = useCallback(() => {
    dedupeRef.current = new Set();
    setLogs([]);
    setEvents([]);
    if (taskId) {
      STREAM_CACHE.delete(taskId);
    }
  }, [taskId]);

  // Restore buffered history when the target task changes, so remounting the
  // viewer continues the existing log instead of starting from a blank terminal.
  useEffect(() => {
    firstPacketRef.current = false;
    startedAtRef.current = Date.now();

    if (!taskId) {
      setLogs([]);
      setEvents([]);
      dedupeRef.current = new Set();
      return;
    }

    const cached = STREAM_CACHE.get(taskId);
    setLogs(cached ? cached.logs : []);
    setEvents(cached ? cached.events : []);
    dedupeRef.current = new Set(cached ? cached.keys : []);
  }, [taskId]);

  // Mirror live state into the cross-mount cache, evicting the least-recent task.
  useEffect(() => {
    if (!taskId) return;
    STREAM_CACHE.delete(taskId);
    STREAM_CACHE.set(taskId, { logs, events, keys: Array.from(dedupeRef.current) });
    while (STREAM_CACHE.size > MAX_CACHED_TASKS) {
      const oldestTask = STREAM_CACHE.keys().next().value;
      if (oldestTask === undefined) break;
      STREAM_CACHE.delete(oldestTask);
    }
  }, [taskId, logs, events]);

  const statusKey = normalizeStatus(taskStatus);
  const isTerminalStatus = statusKey ? TERMINAL_STREAM_STATUSES.includes(statusKey) : false;
  const isConnectableStatus = useMemo(() => {
    if (!statusKey) return true; // no status supplied: fall back to taskId-only gating
    return connectStatuses.map(normalizeStatus).includes(statusKey);
  }, [connectStatuses, statusKey]);

  useEffect(() => {
    if (!taskId) {
      disconnect();
      return;
    }

    if (isTerminalStatus) {
      terminalRef.current = true;
      clearWatchdog();
      clearRetryTimer();
      closeSource();
      setConnectionStatus('completed');
      return;
    }

    if (!isConnectableStatus) {
      // e.g. a queued/idle task: hold the stream closed until it starts running.
      disconnect();
      setConnectionStatus('idle');
      return;
    }

    terminalRef.current = false;
    startedAtRef.current = Date.now();
    firstPacketRef.current = false;
    attemptRef.current = 0;
    connectRef.current();

    return () => {
      disconnect();
    };
  }, [
    taskId,
    isTerminalStatus,
    isConnectableStatus,
    clearRetryTimer,
    clearWatchdog,
    closeSource,
    disconnect,
  ]);

  return {
    connectionStatus,
    isLive: connectionStatus === 'live',
    retryAttempt,
    maxRetries,
    nextRetryDelayMs,
    logs,
    events,
    lastHeartbeat,
    error,
    connect,
    disconnect,
    reconnect,
    clear,
  };
}

export default useEventStream;
