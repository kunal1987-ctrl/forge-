import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import type { TaskHistoryItem } from '../types/api';
import { apiClient } from '../services/apiClient';
import { useTaskContext } from '../context/TaskContext';

export interface UseTaskHistoryOptions {
  limit?: number;
  repoId?: string;
  autoPoll?: boolean;
  pollIntervalMs?: number;
  enabled?: boolean;
}

export interface UseTaskHistoryReturn {
  tasks: TaskHistoryItem[];
  isLoading: boolean;
  isRefreshing: boolean;
  isSkeletonTimedOut: boolean;
  error: Error | null;
  refetch: (silent?: boolean) => Promise<TaskHistoryItem[]>;
  setTasks: React.Dispatch<React.SetStateAction<TaskHistoryItem[]>>;
}

interface CacheEntry {
  data: TaskHistoryItem[];
  timestamp: number;
}

// In-memory module-level SWR cache map and in-flight request tracker
const historyCache = new Map<string, CacheEntry>();
const inFlightRequests = new Map<string, Promise<TaskHistoryItem[]>>();
const CACHE_TTL_MS = 10_000; // 10 seconds SWR stale window

const wait = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Robust fetcher with exponential backoff retry logic.
 */
async function fetchTaskHistoryWithRetry(
  limit: number,
  repoId?: string,
  maxRetries = 3
): Promise<TaskHistoryItem[]> {
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const data = await apiClient.getTaskHistory(limit, repoId);
      if (Array.isArray(data)) {
        return data;
      }
      return [];
    } catch (err: unknown) {
      lastError = err;
      if (attempt < maxRetries) {
        const delayMs = Math.min(500 * Math.pow(2, attempt - 1), 2500);
        await wait(delayMs);
      }
    }
  }
  throw lastError || new Error('Failed to load task history after retries');
}

/**
 * Stable, memoized Task History hook with SWR caching, ref-guards,
 * and background revalidation that prevents flashing skeleton loaders.
 */
export function useTaskHistory(options: UseTaskHistoryOptions = {}): UseTaskHistoryReturn {
  const { limit = 10, repoId, autoPoll = true, pollIntervalMs = 6000, enabled = true } = options;
  const context = useTaskContext();

  const cacheKey = useMemo(() => `${repoId || 'all'}:${limit}`, [repoId, limit]);

  // Initial state derived from cached memory or context store to prevent skeleton flash
  const [tasks, setTasks] = useState<TaskHistoryItem[]>(() => {
    const cached = historyCache.get(cacheKey);
    if (cached && cached.data.length > 0) {
      return cached.data;
    }
    if (context.taskHistory && context.taskHistory.length > 0) {
      return context.taskHistory;
    }
    return [];
  });

  const [isLoading, setIsLoading] = useState<boolean>(() => {
    const cached = historyCache.get(cacheKey);
    const hasCached = (cached && cached.data.length > 0) || (context.taskHistory && context.taskHistory.length > 0);
    return !hasCached;
  });

  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [isSkeletonTimedOut, setIsSkeletonTimedOut] = useState<boolean>(false);
  const [error, setError] = useState<Error | null>(null);

  const isMountedRef = useRef<boolean>(true);
  const optionsRef = useRef({ limit, repoId, autoPoll, pollIntervalMs, enabled });
  const tasksRef = useRef(tasks);

  // Keep references current without triggering re-render cascades
  useEffect(() => {
    optionsRef.current = { limit, repoId, autoPoll, pollIntervalMs, enabled };
    tasksRef.current = tasks;
  }, [limit, repoId, autoPoll, pollIntervalMs, enabled, tasks]);

  // Sync with global TaskContext whenever taskHistory changes (e.g. optimistic dispatch)
  useEffect(() => {
    if (context.taskHistory && context.taskHistory.length > 0) {
      setTasks(context.taskHistory);
      setIsLoading(false);
      setIsSkeletonTimedOut(true);
    }
  }, [context.taskHistory]);

  // 3-second maximum timeout on initial skeleton loaders before falling back to clean empty state
  useEffect(() => {
    let timer: number | undefined;
    if (isLoading && tasks.length === 0 && !isSkeletonTimedOut) {
      timer = window.setTimeout(() => {
        if (isMountedRef.current) {
          setIsSkeletonTimedOut(true);
          setIsLoading(false);
        }
      }, 3000);
    }
    return () => {
      if (timer) window.clearTimeout(timer);
    };
  }, [isLoading, tasks.length, isSkeletonTimedOut]);

  /**
   * Stable refetch function with in-flight deduplication and silent updates
   */
  const refetch = useCallback(
    async (silent = false): Promise<TaskHistoryItem[]> => {
      const { limit: targetLimit, repoId: targetRepo, enabled: isEnabled } = optionsRef.current;
      if (!isEnabled) return [];

      const currentKey = `${targetRepo || 'all'}:${targetLimit}`;
      const currentTasksCount = tasksRef.current.length;

      // Only show full loading state if we have ZERO tasks and it's a non-silent explicit request
      if (!silent) {
        if (currentTasksCount === 0) {
          setIsLoading(true);
          setIsSkeletonTimedOut(false);
        } else {
          setIsRefreshing(true);
        }
      } else {
        setIsRefreshing(true);
      }

      // In-flight request deduplication to prevent duplicate concurrent network calls
      let fetchPromise = inFlightRequests.get(currentKey);
      if (!fetchPromise) {
        fetchPromise = fetchTaskHistoryWithRetry(targetLimit, targetRepo, 3)
          .then((data) => {
            historyCache.set(currentKey, { data, timestamp: Date.now() });
            return data;
          })
          .finally(() => {
            inFlightRequests.delete(currentKey);
          });
        inFlightRequests.set(currentKey, fetchPromise);
      }

      try {
        const data = await fetchPromise;
        if (isMountedRef.current) {
          setTasks(data || []);
          setError(null);
          if (context.setTaskHistory && Array.isArray(data)) {
            context.setTaskHistory(data);
          }
        }
        return data || [];
      } catch (err: unknown) {
        const errorObj = err instanceof Error ? err : new Error(String(err));
        console.warn('[useTaskHistory] History fetch fallback:', errorObj.message);
        if (isMountedRef.current) {
          setError(errorObj);
          // Defensive fallback to global context state if available
          if (context.taskHistory && context.taskHistory.length > 0) {
            setTasks(context.taskHistory);
          }
        }
        return [];
      } finally {
        if (isMountedRef.current) {
          setIsLoading(false);
          setIsRefreshing(false);
          setIsSkeletonTimedOut(true);
        }
      }
    },
    [context.setTaskHistory, context.taskHistory]
  );

  // STRICT MOUNT EFFECT: Executes strictly on mount with an empty dependency array
  useEffect(() => {
    isMountedRef.current = true;

    const { limit: targetLimit, repoId: targetRepo, enabled: isEnabled } = optionsRef.current;
    if (!isEnabled) return;

    const key = `${targetRepo || 'all'}:${targetLimit}`;
    const cached = historyCache.get(key);
    const isFresh = cached && Date.now() - cached.timestamp < CACHE_TTL_MS;

    if (isFresh && cached && cached.data.length > 0) {
      setTasks(cached.data);
      setIsLoading(false);
      setIsSkeletonTimedOut(true);
      // Silent background revalidation
      void refetch(true);
    } else {
      const hasInitialData =
        Boolean(cached?.data?.length) ||
        Boolean(context.taskHistory && context.taskHistory.length > 0);
      void refetch(hasInitialData);
    }

    return () => {
      isMountedRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Strictly on mount

  // Re-fetch automatically when global task status transitions (e.g. task completes or fails)
  useEffect(() => {
    if (
      context.taskStatus === 'COMPLETED' ||
      context.taskStatus === 'FAILED' ||
      context.taskStatus === 'ABORTED'
    ) {
      const timer = window.setTimeout(() => {
        void refetch(true);
      }, 1000);
      return () => window.clearTimeout(timer);
    }
  }, [context.taskStatus, refetch]);

  // Periodic polling interval to keep execution statuses live without flashing skeletons
  const hasRunningTask =
    context.taskStatus === 'RUNNING' || tasks.some((t) => t.status === 'RUNNING');

  useEffect(() => {
    if (!autoPoll || !enabled) return;

    const intervalMs = hasRunningTask ? pollIntervalMs : 15000;
    const interval = window.setInterval(() => {
      void refetch(true); // Silent revalidation prevents skeleton loaders
    }, intervalMs);

    return () => window.clearInterval(interval);
  }, [autoPoll, enabled, hasRunningTask, pollIntervalMs, refetch]);

  return {
    tasks,
    isLoading,
    isRefreshing,
    isSkeletonTimedOut,
    error,
    refetch,
    setTasks,
  };
}

export default useTaskHistory;
