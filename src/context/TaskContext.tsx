import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  useRef,
  useCallback,
  useMemo,
} from 'react';
import type {
  AgentEventPayload,
  CodeContextChunk,
  ExecutionPlanData,
  ExecutionMode,
  PlanData,
  TaskHistoryItem,
  TaskMetricsData,
  TaskStatus as GranularTaskStatus,
  TaskStatusResponse,
} from '../types/api';
import { apiClient, ApiError } from '../services/apiClient';

/**
 * Strict Global Task Execution Status enum requested by the architecture.
 */
export type GlobalTaskStatus =
  | 'IDLE'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'ABORTED';

/**
 * Canonical Workflow Steps in the LangGraph orchestration engine.
 */
export type WorkflowStep =
  | 'planner'
  | 'repro_gate'
  | 'investigator'
  | 'implementer'
  | 'verifier'
  | 'reviewer';

export const WORKFLOW_STEPS: readonly WorkflowStep[] = [
  'planner',
  'repro_gate',
  'investigator',
  'implementer',
  'verifier',
  'reviewer',
] as const;

/**
 * Normalizes raw node or step strings into canonical WorkflowStep identifiers.
 */
export function normalizeWorkflowStep(raw?: string | null): WorkflowStep | null {
  if (!raw) return null;
  const lower = raw.toLowerCase().trim().replace(/_node$/, '');
  if (lower === 'planner' || lower === 'plan' || lower === 'planning' || lower === 'critique') return 'planner';
  if (lower === 'repro_gate' || lower === 'repro' || lower === 'reproduction') return 'repro_gate';
  if (lower === 'investigator' || lower === 'investigate' || lower === 'investigating') return 'investigator';
  if (lower === 'implementer' || lower === 'implement' || lower === 'implementing') return 'implementer';
  if (lower === 'verifier' || lower === 'verify' || lower === 'verifying' || lower === 'prune_state') return 'verifier';
  if (
    lower === 'reviewer' ||
    lower === 'review' ||
    lower === 'awaiting_approval' ||
    lower === '__interrupt__' ||
    lower === 'pr_creation' ||
    lower === 'pr_generator' ||
    lower === 'cleanup'
  ) {
    return 'reviewer';
  }
  return null;
}

/**
 * Normalizes granular or mixed-case task status into strict GlobalTaskStatus.
 */
export function normalizeTaskStatus(raw?: string | null): GlobalTaskStatus {
  if (!raw) return 'IDLE';
  const upper = raw.toUpperCase().trim();
  if (upper === 'IDLE') return 'IDLE';
  if (['COMPLETED', 'PR_CREATED', 'DONE', 'MERGED'].includes(upper)) return 'COMPLETED';
  if (['FAILED', 'ERROR'].includes(upper)) return 'FAILED';
  if (['ABORTED', 'CANCELLED', 'CLEANED_UP'].includes(upper)) return 'ABORTED';
  if (
    [
      'RUNNING',
      'PENDING',
      'PLANNING',
      'INVESTIGATING',
      'IMPLEMENTING',
      'VERIFYING',
      'AWAITING_APPROVAL',
    ].includes(upper)
  ) {
    return 'RUNNING';
  }
  return 'RUNNING';
}

/**
 * Persistent Global Repository state contract per architecture.
 */
export interface RepositoryState {
  id: string;
  name: string;
  url: string;
  branch: string;
  ast_symbol_count: number;
  ingested_at: string | null;
  total_chunks?: number; // Alias for backward compatibility with RepositoryMetadata
  local_path?: string;
}

/**
 * Legacy RepositoryMetadata alias for backward compatibility.
 */
export type RepositoryMetadata = RepositoryState;

export interface ToastNotification {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
}

export interface TestMetrics {
  passed: number;
  failed: number;
  retries: number;
}

export interface TaskStateObject {
  id?: string | null;
  taskId?: string | null;
  task_id?: string;
  status?: GlobalTaskStatus | string | null;
  metrics?: TaskMetricsData | null;
  pr_url?: string | null;
  pr_created?: boolean;
  merged?: boolean;
  plan?: PlanData | null;
  execution_plan?: ExecutionPlanData | null;
  executionPlan?: PlanData | null;
  active_node?: WorkflowStep | null;
  activeNode?: WorkflowStep | null;
  current_node?: WorkflowStep | null;
  completed_nodes?: (WorkflowStep | string)[];
  completedNodes?: (WorkflowStep | string)[];
  currentDiff?: string;
  patch?: string;
  generated_diff?: string;
  repo?: RepositoryState;
  repo_id?: string;
  branch_name?: string | null;
  prompt?: string | null;
  duration_seconds?: number | null;
  durationSeconds?: number | null;
  start_time?: string | null;
  startTime?: string | null;
  end_time?: string | null;
  endTime?: string | null;
  test_suite_passed?: number;
  retry_count?: number;
  target_scope?: string;
  pr_target?: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}

/**
 * Persistent Global Active Task state contract per architecture.
 */
export interface ActiveTaskState extends TaskStateObject {
  id: string | null;
  task_id: string;
  status: GlobalTaskStatus;
  execution_plan: ExecutionPlanData | null;
  patch: string;
  metrics: TaskMetricsData | null;
}

export interface TaskContextType {
  // 1. PERSISTENT GLOBAL REPOSITORY STATE
  repository: RepositoryState;
  currentRepo: RepositoryMetadata; // Backward compatibility alias
  setRepository: (repo: Partial<RepositoryState> | string) => void;
  setCurrentRepo: (repo: Partial<RepositoryMetadata> | string) => void;

  // 2. PERSISTENT GLOBAL ACTIVE TASK STATE
  activeTask: ActiveTaskState;
  task: ActiveTaskState & Partial<TaskStatusResponse>; // Backward compatibility alias
  taskId: string | null;
  status: GlobalTaskStatus;
  taskStatus: GlobalTaskStatus;
  setActiveTask: (taskUpdate: Partial<ActiveTaskState> | null) => void;

  // 3. PERSISTENT GLOBAL TASK HISTORY
  taskHistory: TaskHistoryItem[];
  setTaskHistory: React.Dispatch<React.SetStateAction<TaskHistoryItem[]>>;
  refreshTaskHistory: () => Promise<TaskHistoryItem[]>;

  // Synchronous State across tabs
  activeStep: WorkflowStep | null;
  completedSteps: WorkflowStep[];
  logs: string[];
  rawStatus: GranularTaskStatus;
  currentDiff: string;
  plan: PlanData | null;
  contextRetrieved: CodeContextChunk[];
  prUrl: string | null;
  currentStep: number;
  executionMode: ExecutionMode;
  testMetrics: TestMetrics;
  activeModal: 'reject' | null;
  toasts: ToastNotification[];
  isSubmitting: boolean;
  isIngesting: boolean;

  // Duration & Timestamps
  durationSeconds: number | null;
  duration_seconds?: number | null;
  startTime: string | null;
  start_time?: string | null;
  endTime: string | null;
  end_time?: string | null;
  setDurationSeconds: (seconds: number | null) => void;

  // Dynamic Metrics & Real-time Execution Telemetry
  metrics: TaskMetricsData | null;
  setMetrics: React.Dispatch<React.SetStateAction<TaskMetricsData | null>>;
  testSuitePassed: number;
  test_suite_passed: number;
  retryCount: number;
  retry_count: number;
  targetScope: string | null;
  target_scope: string | null;
  prTarget: string | null;
  pr_target: string | null;

  // Actions & Mutators
  setStatus: (status: GlobalTaskStatus) => void;
  setTaskStatus: (status: GlobalTaskStatus) => void;
  setActiveStep: (step: WorkflowStep | null) => void;
  setCompletedSteps: React.Dispatch<React.SetStateAction<WorkflowStep[]>>;
  addCompletedStep: (step: WorkflowStep) => void;
  setLogs: React.Dispatch<React.SetStateAction<string[]>>;
  addLog: (log: string) => void;
  clearLogs: () => void;
  setTaskId: (id: string | null) => void;
  setCurrentDiff: (diff: string) => void;
  setPlan: (plan: PlanData | null) => void;
  setContextRetrieved: (chunks: CodeContextChunk[]) => void;
  setPrUrl: (url: string | null) => void;
  setCurrentStep: (step: number) => void;
  setExecutionMode: (mode: ExecutionMode) => void;
  setActiveModal: (modal: 'reject' | null) => void;
  addToast: (type: 'success' | 'error' | 'info', message: string) => void;
  dismissToast: (id: string) => void;

  // Centralized Hydration & Refresh Helpers
  refreshTask: (silent?: boolean) => Promise<TaskStatusResponse | null>;
  syncStatus: (silent?: boolean) => Promise<TaskStatusResponse | null>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  handleStreamEvent: (event: AgentEventPayload | any) => void;

  // Global Workflows
  startTask: (prompt: string, targetRepoId?: string, mode?: ExecutionMode) => Promise<string>;
  dispatchTask: (prompt: string, targetRepoId?: string, mode?: ExecutionMode) => Promise<string>;
  approveTask: () => Promise<void>;
  rejectTask: (feedbackText: string) => Promise<void>;
  cancelTask: () => Promise<void>;
  abortTask: (targetTaskId?: string) => Promise<void>;
  retryPR: () => Promise<string | null>;
  ingestRepo: (repoUrl: string, branch?: string) => Promise<string | null>;
  resetTask: () => void;
}

const STORAGE_KEYS = {
  REPOSITORY: 'forge_repository',
  CURRENT_REPO: 'forge_current_repo',
  ACTIVE_TASK: 'forge_active_task',
  ACTIVE_TASK_ID: 'forge_active_task_id',
  CURRENT_DIFF: 'forge_current_diff',
  EXECUTION_PLAN: 'forge_execution_plan',
  PR_URL: 'forge_pr_url',
  METRICS: 'forge_metrics',
  DURATION_SECONDS: 'forge_duration_seconds',
  TASK_HISTORY: 'forge_task_history',
  LOGS: 'forge_logs',
} as const;

function safeStorageGet<T>(key: string, fallback: T): T {
  if (typeof window === 'undefined') return fallback;
  try {
    const raw = localStorage.getItem(key);
    if (raw === null || raw === undefined || raw === 'undefined' || raw === 'null') {
      return fallback;
    }
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function safeStorageSet(key: string, value: any): void {
  if (typeof window === 'undefined') return;
  try {
    if (value === null || value === undefined) {
      localStorage.removeItem(key);
    } else if (typeof value === 'string') {
      localStorage.setItem(key, value);
    } else {
      localStorage.setItem(key, JSON.stringify(value));
    }
  } catch (err) {
    console.warn(`[TaskContext] Failed to save key "${key}" to localStorage:`, err);
  }
}

function normalizeExecutionPlan(plan: ExecutionPlanData | null | undefined): PlanData | null {
  if (!plan) return null;
  if (!Array.isArray(plan)) return plan;
  return {
    target_files: [],
    logic_steps: plan.map((step) => step.description),
    test_strategy: '',
  };
}

const DEFAULT_REPOSITORY: RepositoryState = {
  id: 'default-repo',
  name: 'default-repo',
  url: 'https://github.com/fastapi/fastapi',
  branch: 'main',
  ast_symbol_count: 0,
  total_chunks: 0,
  ingested_at: null,
};

const TaskContext = createContext<TaskContextType | undefined>(undefined);

export const TaskProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  // ===========================================================================
  // 1. PERSISTENT GLOBAL REPOSITORY STORE
  // ===========================================================================
  const [repository, setRepositoryState] = useState<RepositoryState>(() => {
    const fromRepo = safeStorageGet<RepositoryState | null>(STORAGE_KEYS.REPOSITORY, null);
    if (fromRepo && fromRepo.url) {
      return {
        ...DEFAULT_REPOSITORY,
        ...fromRepo,
        ast_symbol_count: fromRepo.ast_symbol_count ?? fromRepo.total_chunks ?? 0,
        total_chunks: fromRepo.total_chunks ?? fromRepo.ast_symbol_count ?? 0,
      };
    }
    const fromLegacy = safeStorageGet<RepositoryMetadata | null>(STORAGE_KEYS.CURRENT_REPO, null);
    if (fromLegacy && fromLegacy.url) {
      return {
        ...DEFAULT_REPOSITORY,
        ...fromLegacy,
        ast_symbol_count: fromLegacy.total_chunks ?? 0,
        total_chunks: fromLegacy.total_chunks ?? 0,
        ingested_at: null,
      };
    }
    return DEFAULT_REPOSITORY;
  });

  const setRepository = useCallback((repoInput: Partial<RepositoryState> | string) => {
    setRepositoryState((prev) => {
      let next: RepositoryState;
      if (typeof repoInput === 'string') {
        const segments = repoInput.split('/').filter(Boolean);
        const repoName = (segments[segments.length - 1] || repoInput).replace(/\.git$/i, '') || repoInput;
        next = {
          ...prev,
          id: repoInput,
          name: repoName,
          url: repoInput.startsWith('http') ? repoInput : `https://github.com/${repoInput}`,
        };
      } else {
        const safeUrl = repoInput.url ?? prev.url;
        let derivedName = repoInput.name ?? prev.name;
        if (!derivedName && safeUrl) {
          const segments = safeUrl.split('/').filter(Boolean);
          derivedName = (segments[segments.length - 1] || 'repo').replace(/\.git$/i, '');
        }
        const symbolCount = repoInput.ast_symbol_count ?? repoInput.total_chunks ?? prev.ast_symbol_count;
        next = {
          ...prev,
          ...repoInput,
          id: repoInput.id || prev.id,
          name: derivedName,
          url: safeUrl,
          branch: repoInput.branch || prev.branch,
          ast_symbol_count: symbolCount,
          total_chunks: symbolCount,
          ingested_at: repoInput.ingested_at ?? prev.ingested_at,
        };
      }
      safeStorageSet(STORAGE_KEYS.REPOSITORY, next);
      safeStorageSet(STORAGE_KEYS.CURRENT_REPO, next);
      return next;
    });
  }, []);

  const setCurrentRepo = useCallback(
    (repoInput: Partial<RepositoryMetadata> | string) => {
      setRepository(repoInput as Partial<RepositoryState> | string);
    },
    [setRepository]
  );

  // ===========================================================================
  // 2. PERSISTENT GLOBAL ACTIVE TASK STORE
  // ===========================================================================
  const [taskId, setTaskIdState] = useState<string | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        return localStorage.getItem(STORAGE_KEYS.ACTIVE_TASK_ID);
      } catch {
        // ignore
      }
    }
    return null;
  });

  const [taskPrompt, setTaskPrompt] = useState<string | null>(null);
  const [status, setStatusState] = useState<GlobalTaskStatus>('IDLE');
  const [rawStatus, setRawStatus] = useState<GranularTaskStatus>('idle');
  const [activeStep, setActiveStepState] = useState<WorkflowStep | null>(null);
  const [completedSteps, setCompletedSteps] = useState<WorkflowStep[]>([]);

  // Logs hydrated from localStorage so activity tab persists
  const [logs, setLogsState] = useState<string[]>(() => {
    return safeStorageGet<string[]>(STORAGE_KEYS.LOGS, []);
  });

  const setLogs = useCallback((val: React.SetStateAction<string[]>) => {
    setLogsState((prev) => {
      const next = typeof val === 'function' ? val(prev) : val;
      safeStorageSet(STORAGE_KEYS.LOGS, next.slice(-200)); // Cap stored logs at 200 items
      return next;
    });
  }, []);

  const addLog = useCallback((log: string) => {
    setLogs((prev) => [...prev, log]);
  }, [setLogs]);

  const clearLogs = useCallback(() => {
    setLogs([]);
  }, [setLogs]);

  // Diff / Patch hydrated from localStorage
  const [currentDiff, setCurrentDiffState] = useState<string>(() => {
    if (typeof window !== 'undefined') {
      try {
        return localStorage.getItem(STORAGE_KEYS.CURRENT_DIFF) || '';
      } catch {
        // ignore
      }
    }
    return '';
  });

  const setCurrentDiff = useCallback((diff: string) => {
    setCurrentDiffState(diff);
    safeStorageSet(STORAGE_KEYS.CURRENT_DIFF, diff || null);
  }, []);

  // Execution Plan hydrated from localStorage
  const [plan, setPlanState] = useState<PlanData | null>(() => {
    return safeStorageGet<PlanData | null>(STORAGE_KEYS.EXECUTION_PLAN, null);
  });

  const setPlan = useCallback((p: PlanData | null) => {
    setPlanState(p);
    safeStorageSet(STORAGE_KEYS.EXECUTION_PLAN, p);
  }, []);

  // Pull Request URL hydrated from localStorage
  const [prUrl, setPrUrlState] = useState<string | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        return localStorage.getItem(STORAGE_KEYS.PR_URL);
      } catch {
        // ignore
      }
    }
    return null;
  });

  const setPrUrl = useCallback((url: string | null) => {
    setPrUrlState(url);
    safeStorageSet(STORAGE_KEYS.PR_URL, url || null);
  }, []);

  const [contextRetrieved, setContextRetrieved] = useState<CodeContextChunk[]>([]);
  const [currentStep, setCurrentStep] = useState<number>(0);
  const [executionMode, setExecutionMode] = useState<ExecutionMode>('fast');
  const [testMetrics, setTestMetrics] = useState<TestMetrics>({ passed: 0, failed: 0, retries: 0 });
  const [activeModal, setActiveModal] = useState<'reject' | null>(null);
  const [toasts, setToasts] = useState<ToastNotification[]>([]);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [isIngesting, setIsIngesting] = useState<boolean>(false);

  // Execution Duration & Timestamps State hydrated from localStorage
  const [durationSeconds, setDurationSecondsState] = useState<number | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        const saved = localStorage.getItem(STORAGE_KEYS.DURATION_SECONDS);
        if (saved !== null && saved !== undefined && saved !== '') {
          const num = Number(saved);
          return isNaN(num) ? null : num;
        }
      } catch {
        // ignore
      }
    }
    return null;
  });

  const setDurationSeconds = useCallback((sec: number | null) => {
    setDurationSecondsState(sec);
    safeStorageSet(STORAGE_KEYS.DURATION_SECONDS, typeof sec === 'number' ? String(sec) : null);
  }, []);

  const [startTime, setStartTime] = useState<string | null>(null);
  const [endTime, setEndTime] = useState<string | null>(null);

  // Dynamic Metrics State hydrated from localStorage
  const [metrics, setMetricsState] = useState<TaskMetricsData | null>(() => {
    return safeStorageGet<TaskMetricsData | null>(STORAGE_KEYS.METRICS, null);
  });

  const setMetrics = useCallback((val: React.SetStateAction<TaskMetricsData | null>) => {
    setMetricsState((prev) => {
      const next = typeof val === 'function' ? val(prev) : val;
      safeStorageSet(STORAGE_KEYS.METRICS, next);
      return next;
    });
  }, []);

  // Real-time Execution Telemetry State
  const [testSuitePassed, setTestSuitePassed] = useState<number>(0);
  const [targetScope, setTargetScope] = useState<string | null>(null);
  const [prTarget, setPrTarget] = useState<string | null>(null);

  // Memoized unified activeTask state contract
  const activeTask = useMemo<ActiveTaskState>(
    () => ({
      id: taskId,
      taskId,
      task_id: taskId || '',
      status,
      metrics,
      pr_url: prUrl,
      pr_created: Boolean(prUrl || status === 'COMPLETED'),
      merged: false,
      plan,
      execution_plan: plan,
      executionPlan: plan,
      active_node: activeStep,
      activeNode: activeStep,
      current_node: activeStep,
      completed_nodes: completedSteps,
      completedNodes: completedSteps,
      currentDiff,
      patch: currentDiff,
      generated_diff: currentDiff,
      repo: repository,
      repo_id: repository.id,
      branch_name: repository.branch || 'main',
      prompt: taskPrompt,
      duration_seconds: durationSeconds,
      durationSeconds,
      start_time: startTime,
      startTime,
      end_time: endTime,
      endTime,
      test_suite_passed: testSuitePassed || testMetrics.passed,
      retry_count: testMetrics.retries,
      target_scope: targetScope || (plan?.target_files?.[0] ?? 'services/api/forge/routers/rag.py'),
      pr_target: prTarget || (taskId ? `forge/auto-patch-${taskId}` : 'forge/auto-patch'),
    }),
    [
      taskId,
      taskPrompt,
      status,
      metrics,
      prUrl,
      plan,
      activeStep,
      completedSteps,
      currentDiff,
      repository,
      durationSeconds,
      startTime,
      endTime,
      testSuitePassed,
      testMetrics.passed,
      testMetrics.retries,
      targetScope,
      prTarget,
    ]
  );

  // Sync active task snapshot to localStorage
  useEffect(() => {
    if (taskId) {
      safeStorageSet(STORAGE_KEYS.ACTIVE_TASK, {
        id: taskId,
        status,
        prompt: taskPrompt,
        execution_plan: plan,
        patch: currentDiff,
        metrics,
        pr_url: prUrl,
      });
    }
  }, [taskId, status, taskPrompt, plan, currentDiff, metrics, prUrl]);

  // Sync taskId with localStorage
  const setTaskId = useCallback((id: string | null) => {
    setTaskIdState(id);
    safeStorageSet(STORAGE_KEYS.ACTIVE_TASK_ID, id || null);
  }, []);

  const setActiveTask = useCallback(
    (taskUpdate: Partial<ActiveTaskState> | null) => {
      if (!taskUpdate) {
        setTaskId(null);
        setTaskPrompt(null);
        setStatusState('IDLE');
        setPlan(null);
        setCurrentDiff('');
        setMetrics(null);
        setPrUrl(null);
        return;
      }
      if (taskUpdate.id !== undefined) setTaskId(taskUpdate.id);
      if (taskUpdate.status !== undefined) setStatusState(taskUpdate.status);
      if (taskUpdate.prompt !== undefined) setTaskPrompt(taskUpdate.prompt);
      if (taskUpdate.plan !== undefined) {
        setPlan(taskUpdate.plan);
      } else if (taskUpdate.execution_plan !== undefined) {
        setPlan(normalizeExecutionPlan(taskUpdate.execution_plan));
      }
      if (taskUpdate.patch !== undefined) setCurrentDiff(taskUpdate.patch);
      if (taskUpdate.metrics !== undefined) setMetrics(taskUpdate.metrics);
      if (taskUpdate.pr_url !== undefined) setPrUrl(taskUpdate.pr_url);
    },
    [setTaskId, setTaskPrompt, setPlan, setCurrentDiff, setMetrics, setPrUrl]
  );

  // ===========================================================================
  // 3. PERSISTENT GLOBAL TASK HISTORY
  // ===========================================================================
  const [taskHistory, setTaskHistoryState] = useState<TaskHistoryItem[]>(() => {
    return safeStorageGet<TaskHistoryItem[]>(STORAGE_KEYS.TASK_HISTORY, []);
  });

  const setTaskHistory = useCallback(
    (historyInput: TaskHistoryItem[] | ((prev: TaskHistoryItem[]) => TaskHistoryItem[])) => {
      setTaskHistoryState((prev) => {
        const next = typeof historyInput === 'function' ? historyInput(prev) : historyInput;
        safeStorageSet(STORAGE_KEYS.TASK_HISTORY, next);
        return next;
      });
    },
    []
  );

  const refreshTaskHistory = useCallback(async (): Promise<TaskHistoryItem[]> => {
    try {
      const data = await apiClient.getTaskHistory(25, repository.id);
      if (Array.isArray(data)) {
        setTaskHistory(data);
        return data;
      }
      return [];
    } catch (err) {
      console.warn('[TaskContext] Could not fetch task history:', err);
      return [];
    }
  }, [repository.id, setTaskHistory]);

  // Initial task history hydration on mount
  useEffect(() => {
    void refreshTaskHistory();
  }, [refreshTaskHistory]);

  // ===========================================================================
  // 4. STORAGE EVENT SYNCHRONIZATION ACROSS BROWSER TABS
  // ===========================================================================
  useEffect(() => {
    const handleStorageChange = (e: StorageEvent) => {
      if (!e.key) return;
      if (e.key === STORAGE_KEYS.REPOSITORY || e.key === STORAGE_KEYS.CURRENT_REPO) {
        if (e.newValue) {
          try {
            const parsed = JSON.parse(e.newValue);
            setRepositoryState(parsed);
          } catch {
            // ignore
          }
        }
      } else if (e.key === STORAGE_KEYS.ACTIVE_TASK_ID) {
        setTaskIdState(e.newValue || null);
      } else if (e.key === STORAGE_KEYS.CURRENT_DIFF) {
        setCurrentDiffState(e.newValue || '');
      } else if (e.key === STORAGE_KEYS.EXECUTION_PLAN) {
        try {
          setPlanState(e.newValue ? JSON.parse(e.newValue) : null);
        } catch {
          // ignore
        }
      } else if (e.key === STORAGE_KEYS.METRICS) {
        try {
          setMetricsState(e.newValue ? JSON.parse(e.newValue) : null);
        } catch {
          // ignore
        }
      } else if (e.key === STORAGE_KEYS.TASK_HISTORY) {
        try {
          setTaskHistoryState(e.newValue ? JSON.parse(e.newValue) : []);
        } catch {
          // ignore
        }
      }
    };

    window.addEventListener('storage', handleStorageChange);
    return () => window.removeEventListener('storage', handleStorageChange);
  }, []);

  // SSE Stream ref
  const unsubscribeRef = useRef<(() => void) | null>(null);

  const setStatus = useCallback((nextStatus: GlobalTaskStatus) => {
    setStatusState(nextStatus);
  }, []);

  const setActiveStep = useCallback((step: WorkflowStep | null) => {
    setActiveStepState(step);
  }, []);

  const addCompletedStep = useCallback((step: WorkflowStep) => {
    setCompletedSteps((prev) => (prev.includes(step) ? prev : [...prev, step]));
  }, []);

  const addToast = useCallback((type: 'success' | 'error' | 'info', message: string) => {
    const id = `${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
    setToasts((prev) => [...prev, { id, type, message }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 5000);
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const closeStream = useCallback(() => {
    if (unsubscribeRef.current) {
      unsubscribeRef.current();
      unsubscribeRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      closeStream();
    };
  }, [closeStream]);

  // Synchronously update task state from live SSE events
  const handleStreamEvent = useCallback(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (event: AgentEventPayload | any) => {
      if (!event) return;

      const rawEvtStatus = event.status || event.payload?.status || event.data?.status;
      if (rawEvtStatus) {
        setRawStatus(rawEvtStatus);
        const norm = normalizeTaskStatus(rawEvtStatus);
        setStatusState(norm);
      }

      const activeNode =
        event.node ||
        event.active_node ||
        event.payload?.node ||
        event.payload?.active_node ||
        event.data?.node ||
        event.data?.active_node;

      if (activeNode) {
        const step = normalizeWorkflowStep(activeNode);
        if (step) {
          setActiveStepState(step);
          const stepIdx = WORKFLOW_STEPS.indexOf(step);
          setCurrentStep(stepIdx + 1);
        }
      }

      const completedNode =
        event.completed_node ||
        event.payload?.completed_node ||
        event.data?.completed_node;

      if (completedNode) {
        const compStep = normalizeWorkflowStep(completedNode);
        if (compStep) {
          setCompletedSteps((prev) => (prev.includes(compStep) ? prev : [...prev, compStep]));
        }
      }

      const diff =
        event.generated_diff ||
        event.diff ||
        event.patch ||
        event.payload?.generated_diff ||
        event.payload?.diff ||
        event.payload?.patch ||
        event.data?.generated_diff ||
        event.data?.patch;

      if (diff && typeof diff === 'string') {
        setCurrentDiff(diff);
      }

      const planData =
        event.execution_plan ||
        event.payload?.execution_plan ||
        event.payload?.plan ||
        event.data?.execution_plan ||
        event.data?.plan;
      const planMetadata =
        event.plan ||
        event.payload?.plan ||
        event.data?.plan;

      if (planData) {
        if (
          Array.isArray(planData) &&
          planMetadata &&
          typeof planMetadata === 'object' &&
          !Array.isArray(planMetadata)
        ) {
          setPlan({ ...planMetadata, steps: planData } as PlanData);
        } else {
          setPlan(planData as PlanData);
        }
      } else if (planMetadata) {
        setPlan(planMetadata as PlanData);
      }

      const url =
        event.pr_url ||
        event.payload?.pr_url ||
        event.data?.pr_url ||
        event.data?.url;

      if (url && typeof url === 'string') {
        setPrUrl(url);
      }

      const metricsData =
        event.metrics ||
        event.payload?.metrics ||
        event.data?.metrics;

      if (metricsData) {
        setMetrics(metricsData as TaskMetricsData);
      }

      const logMsg =
        event.message ||
        event.payload?.message ||
        event.data?.message ||
        (event.node ? `[Node] Executed ${event.node}` : null);

      if (logMsg && typeof logMsg === 'string') {
        addLog(logMsg);
      }
      const eventPrompt = event.prompt || event.payload?.prompt || event.data?.prompt;
      if (typeof eventPrompt === 'string' && eventPrompt.trim()) {
        setTaskPrompt(eventPrompt);
      }

      const durSec =
        event.duration_seconds ??
        event.payload?.duration_seconds ??
        event.data?.duration_seconds;

      if (typeof durSec === 'number') {
        setDurationSeconds(durSec);
      }

      const stTime =
        event.start_time ??
        event.payload?.start_time ??
        event.data?.start_time;

      if (stTime) {
        setStartTime(String(stTime));
      }

      const etTime =
        event.end_time ??
        event.payload?.end_time ??
        event.data?.end_time;

      if (etTime) {
        setEndTime(String(etTime));
      }

      // Terminal state transitions
      const normalizedStatus = normalizeTaskStatus(rawEvtStatus);
      if (
        rawEvtStatus === 'pr_created' ||
        rawEvtStatus === 'completed' ||
        rawEvtStatus === 'merged' ||
        normalizedStatus === 'COMPLETED'
      ) {
        setIsSubmitting(false);
        setStatusState('COMPLETED');
        setCompletedSteps([...WORKFLOW_STEPS]);
        setActiveStepState(null);
        addToast('success', 'Pull Request successfully created & verified!');
        closeStream();
        void refreshTaskHistory();
      } else if (
        rawEvtStatus === 'cleaned_up' ||
        rawEvtStatus === 'aborted' ||
        rawEvtStatus === 'cancelled' ||
        normalizedStatus === 'ABORTED'
      ) {
        setIsSubmitting(false);
        setStatusState('ABORTED');
        setActiveStepState(null);
        closeStream();
        void refreshTaskHistory();
      } else if (
        rawEvtStatus === 'failed' ||
        rawEvtStatus === 'error' ||
        normalizedStatus === 'FAILED'
      ) {
        setIsSubmitting(false);
        setStatusState('FAILED');
        const err =
          event.error ||
          event.payload?.error ||
          event.data?.error ||
          'Task execution failed.';
        addToast('error', String(err));
        closeStream();
        void refreshTaskHistory();
      } else if (rawEvtStatus === 'awaiting_approval') {
        setIsSubmitting(false);
        setStatusState('RUNNING');
        setActiveStepState('reviewer');
        setCompletedSteps((prev) => {
          const next = new Set<WorkflowStep>(prev);
          for (let i = 0; i < 5; i++) {
            next.add(WORKFLOW_STEPS[i]);
          }
          return Array.from(next);
        });
        addToast('info', 'Human-in-the-Loop review gate reached.');
      }
    },
    [addLog, addToast, closeStream, setCurrentDiff, setPlan, setPrUrl, setMetrics, setDurationSeconds, setTaskPrompt, refreshTaskHistory]
  );

  /**
   * Applies full backend task state to global store.
   */
  const applyTaskUpdate = useCallback(
    (taskInfo: TaskStatusResponse) => {
      if (!taskInfo) return;

      const normalized = normalizeTaskStatus(taskInfo.status);
      setStatusState(normalized);
      setRawStatus(taskInfo.status as GranularTaskStatus);
      if (typeof taskInfo.prompt === 'string') {
        setTaskPrompt(taskInfo.prompt);
      }

      if (taskInfo.pr_url) {
        setPrUrl(taskInfo.pr_url);
      }

      const patchData = taskInfo.patch || taskInfo.generated_diff;
      if (patchData) {
        setCurrentDiff(patchData);
      }

      const planHydration = taskInfo.plan ?? normalizeExecutionPlan(taskInfo.execution_plan);
      setPlan(planHydration);

      const activeNodeRaw = taskInfo.active_node || taskInfo.current_node;
      if (activeNodeRaw) {
        const step = normalizeWorkflowStep(activeNodeRaw);
        if (step) {
          setActiveStepState(step);
          const stepIdx = WORKFLOW_STEPS.indexOf(step);
          setCurrentStep(stepIdx + 1);
        }
      }

      if (taskInfo.completed_nodes && taskInfo.completed_nodes.length > 0) {
        const steps = taskInfo.completed_nodes
          .map(normalizeWorkflowStep)
          .filter((s): s is WorkflowStep => s !== null);
        setCompletedSteps(steps);
      } else if (normalized === 'COMPLETED') {
        setCompletedSteps([...WORKFLOW_STEPS]);
      }

      if (taskInfo.metrics) {
        setMetrics(taskInfo.metrics as TaskMetricsData);
      } else if (
        taskInfo.diff_stats ||
        taskInfo.confidence_score !== undefined ||
        taskInfo.latency_impact !== undefined
      ) {
        const added = taskInfo.diff_stats?.lines_added ?? 0;
        const deleted = taskInfo.diff_stats?.lines_deleted ?? 0;
        setMetrics({
          diff_stats: taskInfo.diff_stats,
          diff_size: { added, deleted },
          lines_added: added,
          lines_deleted: deleted,
          confidence: taskInfo.confidence_score ?? undefined,
          confidence_score: taskInfo.confidence_score ?? undefined,
          latency_impact: taskInfo.latency_impact ?? undefined,
          latency_impact_ms:
            taskInfo.latency_impact !== undefined && taskInfo.latency_impact !== null
              ? (Math.abs(taskInfo.latency_impact) < 10
                  ? taskInfo.latency_impact * 1000
                  : taskInfo.latency_impact)
              : undefined,
        });
      }

      if (typeof taskInfo.duration_seconds === 'number') {
        setDurationSeconds(taskInfo.duration_seconds);
      }
      if (taskInfo.start_time) {
        setStartTime(taskInfo.start_time);
      }
      if (taskInfo.end_time) {
        setEndTime(taskInfo.end_time);
      }

      if (typeof taskInfo.test_suite_passed === 'number') {
        setTestSuitePassed(taskInfo.test_suite_passed);
        setTestMetrics((prev) => ({ ...prev, passed: taskInfo.test_suite_passed ?? prev.passed }));
      }
      if (typeof taskInfo.retry_count === 'number') {
        setTestMetrics((prev) => ({ ...prev, retries: taskInfo.retry_count }));
      }
      if (taskInfo.target_scope) {
        setTargetScope(taskInfo.target_scope);
      }
      if (taskInfo.pr_target || taskInfo.branch_name) {
        setPrTarget(taskInfo.pr_target || taskInfo.branch_name || null);
      }

      if (normalized === 'COMPLETED') {
        setIsSubmitting(false);
        setCompletedSteps([...WORKFLOW_STEPS]);
        setActiveStepState(null);
        closeStream();
      } else if (normalized === 'FAILED' || normalized === 'ABORTED') {
        setIsSubmitting(false);
        setActiveStepState(null);
        closeStream();
      } else if ((taskInfo.status as string) === 'awaiting_approval' || taskInfo.current_node === 'reviewer') {
        setIsSubmitting(false);
        setActiveStepState('reviewer');
      }
    },
    [closeStream, setCurrentDiff, setPlan, setPrUrl, setMetrics, setDurationSeconds, setTaskPrompt]
  );

  /**
   * Fetches latest task status from GET /api/tasks/{task_id} and synchronizes the store.
   */
  const syncStatus = useCallback(
    async (silent = false): Promise<TaskStatusResponse | null> => {
      if (!taskId) return null;
      try {
        const taskInfo = await apiClient.getTaskStatus(taskId);
        applyTaskUpdate(taskInfo);

        const normalized = normalizeTaskStatus(taskInfo.status);
        if (
          (normalized === 'RUNNING' || taskInfo.status === 'PENDING') &&
          !unsubscribeRef.current
        ) {
          const unsub = apiClient.subscribeToTaskEvents(
            taskId,
            handleStreamEvent,
            (err) => console.warn('SSE subscription notice:', err)
          );
          unsubscribeRef.current = unsub;
        }

        return taskInfo;
      } catch (err) {
        if (!silent) {
          console.warn(`[TaskContext] Could not sync task status for ${taskId}:`, err);
        }
        return null;
      }
    },
    [taskId, applyTaskUpdate, handleStreamEvent]
  );

  // Initial task hydration on mount
  useEffect(() => {
    if (!taskId) return;
    let isMounted = true;

    void syncStatus(false).then((info) => {
      if (!isMounted || !info) return;
    });

    return () => {
      isMounted = false;
    };
  }, [taskId, syncStatus]);

  // Background polling every 2 seconds when running
  useEffect(() => {
    if (!taskId) return;

    const isRunning =
      status === 'RUNNING' ||
      [
        'pending',
        'running',
        'planning',
        'investigating',
        'implementing',
        'verifying',
        'awaiting_approval',
      ].includes(String(rawStatus).toLowerCase());

    if (!isRunning) return;

    const intervalId = window.setInterval(() => {
      void syncStatus(true);
    }, 2000);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [taskId, status, rawStatus, syncStatus]);

  // Action: Start Autonomous Run
  const startTask = useCallback(
    async (prompt: string, targetRepoId?: string, mode?: ExecutionMode): Promise<string> => {
      if (isSubmitting) return taskId || '';
      if (!prompt.trim()) {
        addToast('error', 'Please provide instructions for the task.');
        throw new Error('Empty task prompt');
      }

      const activeMode = mode || executionMode;
      const effectiveRepoId = targetRepoId || repository.id || 'default-repo';

      setIsSubmitting(true);
      closeStream();

      if (taskId && status === 'RUNNING') {
        try {
          addToast('info', 'Stopping the previous task before starting this one...');
          await apiClient.abortTask(taskId);
          void refreshTaskHistory();
        } catch (err: unknown) {
          setIsSubmitting(false);
          const message =
            err instanceof ApiError ? err.message : (err as Error).message || 'Could not stop the previous task.';
          addToast('error', message);
          throw err;
        }
      }

      setTaskPrompt(prompt.trim());
      setLogs([`[Client] Initializing task with ${activeMode.toUpperCase()} mode...`]);
      setCurrentDiff('');
      setPlan(null);
      setContextRetrieved([]);
      setPrUrl(null);
      setMetrics(null);
      setDurationSeconds(0);
      setStartTime(new Date().toISOString());
      setEndTime(null);
      setTestMetrics({ passed: 0, failed: 0, retries: 0 });
      setCurrentStep(1);
      setRawStatus('planning');
      setStatusState('RUNNING');
      setActiveStepState('planner');
      setCompletedSteps([]);

      try {
        const res = await apiClient.createTask({
          prompt: prompt.trim(),
          repo_id: effectiveRepoId,
          mode: activeMode,
        });

        setTaskId(res.task_id);
        setRawStatus(res.status || 'planning');
        setStatusState('RUNNING');
        setIsSubmitting(false);
        addToast('success', `Task ${res.task_id.slice(0, 8)} started successfully.`);

        // Optimistic update: immediately prepend newly created task into taskHistory
        const firstLine = prompt.trim().split('\n')[0].trim();
        const newHistoryItem: TaskHistoryItem = {
          task_id: res.task_id,
          title: firstLine.slice(0, 100) || 'Autonomous Task Run',
          repo_url: repository.url || 'https://github.com/fastapi/fastapi',
          status: 'RUNNING',
          created_at: new Date().toISOString(),
          start_time: new Date().toISOString(),
          duration_seconds: 0,
          relative_time: 'Just now',
          branch_name: repository.branch || 'main',
          retry_count: 0,
        };
        setTaskHistory((prev) => [newHistoryItem, ...(prev || []).filter((t) => t.task_id !== res.task_id)]);

        // Automatically trigger background refetch of GET /api/tasks/history to ensure full database synchronization
        void refreshTaskHistory();
        setTimeout(() => {
          void refreshTaskHistory();
        }, 1200);


        // Subscribe to live SSE stream
        const unsub = apiClient.subscribeToTaskEvents(
          res.task_id,
          handleStreamEvent,
          (err) => console.warn('SSE stream error:', err)
        );
        unsubscribeRef.current = unsub;
        return res.task_id;
      } catch (err: unknown) {
        setIsSubmitting(false);
        setStatusState('FAILED');
        setRawStatus('failed');
        setActiveStepState(null);
        const msg = err instanceof ApiError ? err.message : (err as Error).message || 'Failed to start task.';
        addToast('error', msg);
        addLog(`[Error] ${msg}`);
        throw err;
      }
    },
    [
      isSubmitting,
      taskId,
      status,
      executionMode,
      repository.id,
      repository.url,
      repository.branch,
      closeStream,
      setTaskId,
      setCurrentDiff,
      setPlan,
      setPrUrl,
      setMetrics,
      setDurationSeconds,
      setTaskPrompt,
      setLogs,
      addToast,
      addLog,
      setTaskHistory,
      refreshTaskHistory,
      handleStreamEvent,
    ]
  );

  // Action: Approve & Create PR
  const approveTask = useCallback(async () => {
    if (!taskId || isSubmitting) return;

    setIsSubmitting(true);
    try {
      addToast('info', 'Submitting reviewer approval...');
      await apiClient.submitReviewDecision(taskId, { decision: 'approve' });
      addLog('[Reviewer] Human reviewer approved patch. Publishing PR...');
      addCompletedStep('reviewer');

      const verified = await apiClient.getTaskStatus(taskId);
      applyTaskUpdate(verified);
      if (verified.status === 'COMPLETED' && verified.pr_created) {
        setStatusState('COMPLETED');
        setRawStatus('pr_created');
        setCompletedSteps([...WORKFLOW_STEPS]);
        setActiveStepState(null);
        if (verified.pr_url) setPrUrl(verified.pr_url);
        addToast('success', 'Verified Pull Request opened in GitHub!');
        void refreshTaskHistory();
      }
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `Approval failed: ${msg}`);
    } finally {
      setIsSubmitting(false);
    }
  }, [taskId, isSubmitting, addToast, addLog, addCompletedStep, applyTaskUpdate, setPrUrl, refreshTaskHistory]);

  // Action: Reject & Request Changes
  const rejectTask = useCallback(
    async (feedbackText: string) => {
      if (!taskId || isSubmitting) return;

      setIsSubmitting(true);
      try {
        addToast('info', 'Submitting change request feedback...');
        await apiClient.submitReviewDecision(taskId, {
          decision: 'reject',
          feedback: feedbackText,
        });
        setActiveModal(null);
        setRawStatus('implementing');
        setStatusState('RUNNING');
        setActiveStepState('implementer');
        addLog(`[Reviewer] Requested changes: "${feedbackText}". Re-routing to implementer.`);
        addToast('info', 'Task re-routed to implementer node.');
        await syncStatus(true);
      } catch (err: unknown) {
        const msg = err instanceof ApiError ? err.message : (err as Error).message;
        addToast('error', `Rejection failed: ${msg}`);
      } finally {
        setIsSubmitting(false);
      }
    },
    [taskId, isSubmitting, addToast, addLog, syncStatus]
  );

  // Action: Reset Task UI to IDLE and clear task cache
  const resetTask = useCallback(() => {
    closeStream();
    setTaskId(null);
    setTaskPrompt(null);
    setStatusState('IDLE');
    setRawStatus('idle');
    setActiveStepState(null);
    setCompletedSteps([]);
    setLogs([]);
    setCurrentDiff('');
    setPlan(null);
    setContextRetrieved([]);
    setPrUrl(null);
    setMetrics(null);
    setDurationSeconds(null);
    setStartTime(null);
    setEndTime(null);
    setCurrentStep(0);
    setTestMetrics({ passed: 0, failed: 0, retries: 0 });
    setTestSuitePassed(0);
    setTargetScope(null);
    setPrTarget(null);

    safeStorageSet(STORAGE_KEYS.ACTIVE_TASK, null);
    safeStorageSet(STORAGE_KEYS.ACTIVE_TASK_ID, null);
    safeStorageSet(STORAGE_KEYS.CURRENT_DIFF, null);
    safeStorageSet(STORAGE_KEYS.EXECUTION_PLAN, null);
    safeStorageSet(STORAGE_KEYS.PR_URL, null);
    safeStorageSet(STORAGE_KEYS.METRICS, null);
    safeStorageSet(STORAGE_KEYS.DURATION_SECONDS, null);
  }, [closeStream, setTaskId, setTaskPrompt, setCurrentDiff, setPlan, setPrUrl, setMetrics, setDurationSeconds, setLogs]);

  // Action: Cancel Task & Cleanup Worktree
  const cancelTask = useCallback(async () => {
    if (!taskId) {
      setStatusState('IDLE');
      setRawStatus('idle');
      setActiveStepState(null);
      return;
    }

    setIsSubmitting(true);
    closeStream();
    try {
      addToast('info', 'Aborting task execution and cleaning worktree...');
      await apiClient.cancelTask(taskId);
      setStatusState('ABORTED');
      setRawStatus('cleaned_up');
      setActiveStepState(null);
      addLog('[System] Task aborted by user. Ephemeral worktree destroyed.');
      addToast('success', 'Task cancelled cleanly.');
      void refreshTaskHistory();
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `Cancellation error: ${msg}`);
      void syncStatus(true);
    } finally {
      setIsSubmitting(false);
    }
  }, [taskId, closeStream, addToast, addLog, refreshTaskHistory, syncStatus]);

  // Action: Forcefully Abort Task, Kill Processes, and Reset Engine State
  const abortTask = useCallback(
    async (targetTaskId?: string) => {
      const effectiveId = targetTaskId || taskId;
      if (!effectiveId) {
        resetTask();
        return;
      }

      setIsSubmitting(true);
      closeStream();
      let aborted = false;
      try {
        addToast('info', 'Halting pipeline and terminating active worker processes...');
        await apiClient.abortTask(effectiveId);
        aborted = true;
        setStatusState('ABORTED');
        setRawStatus('aborted');
        setActiveStepState(null);
        addLog('[System] Task execution forcefully halted. Engine state reset.');
        addToast('success', 'Pipeline aborted and resources terminated.');
      } catch (err: unknown) {
        const msg = err instanceof ApiError ? err.message : (err as Error).message;
        addToast('error', `Abort notice: ${msg}`);
        addLog(`[System] Abort request completed: ${msg}`);
        void syncStatus(true);
      } finally {
        setIsSubmitting(false);
        if (aborted) resetTask();
        void refreshTaskHistory();
      }
    },
      [taskId, closeStream, addToast, addLog, resetTask, refreshTaskHistory, syncStatus]
  );

  // Action: Retry Pull Request
  const retryPR = useCallback(async (): Promise<string | null> => {
    if (!taskId) return null;
    setIsSubmitting(true);
    try {
      addToast('info', 'Retrying authenticated git push & PR creation...');
      const res = await apiClient.retryPullRequest(taskId);
      if (res.pr_url) {
        setPrUrl(res.pr_url);
        setStatusState('COMPLETED');
        setRawStatus('pr_created');
        setCompletedSteps([...WORKFLOW_STEPS]);
        setActiveStepState(null);
        addToast('success', 'Pull Request successfully created!');
        addLog(`[PR Generator] Successfully created Pull Request: ${res.pr_url}`);
        void refreshTaskHistory();
        return res.pr_url;
      }
      return null;
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `PR retry failed: ${msg}`);
      addLog(`[Error] PR retry failed: ${msg}`);
      throw err;
    } finally {
      setIsSubmitting(false);
    }
  }, [taskId, addToast, addLog, setPrUrl, refreshTaskHistory]);

  // Action: Ingest Target Repository
  const ingestRepo = useCallback(
    async (repoUrl: string, branch = 'main'): Promise<string | null> => {
      setIsIngesting(true);
      try {
        addToast('info', `Ingesting and indexing repository "${repoUrl}"...`);
        const res = await apiClient.ingestRepository({ repo_url: repoUrl, branch });

        const segments = repoUrl.split('/').filter(Boolean);
        const repoName = (segments[segments.length - 1] || 'repo').replace(/\.git$/i, '') || 'repo';
        const newRepo: RepositoryState = {
          id: res.repo_id,
          name: repoName,
          url: repoUrl,
          branch,
          ast_symbol_count: res.total_chunks,
          total_chunks: res.total_chunks,
          ingested_at: new Date().toISOString(),
        };

        setRepository(newRepo);
        addToast('success', `Repository indexed! ${res.total_chunks} code symbols stored in Graph-RAG.`);
        addLog(`[Ingestion] Repository ${repoName} (${res.repo_id}) ingested successfully.`);
        return res.repo_id;
      } catch (err: unknown) {
        const msg = err instanceof ApiError ? err.message : (err as Error).message;
        addToast('error', `Repository ingestion failed: ${msg}`);
        return null;
      } finally {
        setIsIngesting(false);
      }
    },
    [addToast, setRepository, addLog]
  );

  const value = useMemo<TaskContextType>(
    () => ({
      // 1. Persistent Global Repository State
      repository,
      currentRepo: repository, // Backward compatibility
      setRepository,
      setCurrentRepo,

      // 2. Persistent Global Active Task State
      activeTask,
      task: activeTask, // Backward compatibility
      taskId,
      status,
      taskStatus: status,
      setActiveTask,

      // 3. Persistent Global Task History
      taskHistory,
      setTaskHistory,
      refreshTaskHistory,

      // Synchronous Tab State
      activeStep,
      completedSteps,
      logs,
      rawStatus,
      currentDiff,
      plan,
      contextRetrieved,
      prUrl,
      currentStep,
      executionMode,
      testMetrics,
      activeModal,
      toasts,
      isSubmitting,
      isIngesting,

      // Duration & Timestamps
      durationSeconds,
      duration_seconds: durationSeconds,
      startTime,
      start_time: startTime,
      endTime,
      end_time: endTime,
      setDurationSeconds,

      // Dynamic Metrics & Telemetry
      metrics,
      setMetrics,
      testSuitePassed,
      test_suite_passed: testSuitePassed,
      retryCount: testMetrics.retries,
      retry_count: testMetrics.retries,
      targetScope: targetScope || (plan?.target_files?.[0] ?? 'services/api/forge/routers/rag.py'),
      target_scope: targetScope || (plan?.target_files?.[0] ?? 'services/api/forge/routers/rag.py'),
      prTarget: prTarget || (taskId ? `forge/auto-patch-${taskId}` : 'forge/auto-patch'),
      pr_target: prTarget || (taskId ? `forge/auto-patch-${taskId}` : 'forge/auto-patch'),

      // Actions
      setStatus,
      setTaskStatus: setStatus,
      setActiveStep,
      setCompletedSteps,
      addCompletedStep,
      setLogs,
      addLog,
      clearLogs,
      setTaskId,
      setCurrentDiff,
      setPlan,
      setContextRetrieved,
      setPrUrl,
      setCurrentStep,
      setExecutionMode,
      setActiveModal,
      addToast,
      dismissToast,

      // Centralized Hydration & Refresh Helpers
      refreshTask: syncStatus,
      syncStatus,
      handleStreamEvent,
      startTask,
      dispatchTask: startTask,
      approveTask,
      rejectTask,
      cancelTask,
      abortTask,
      retryPR,
      ingestRepo,
      resetTask,
    }),
    [
      repository,
      setRepository,
      setCurrentRepo,
      activeTask,
      taskId,
      status,
      setActiveTask,
      taskHistory,
      setTaskHistory,
      refreshTaskHistory,
      activeStep,
      completedSteps,
      logs,
      rawStatus,
      currentDiff,
      setCurrentDiff,
      plan,
      setPlan,
      contextRetrieved,
      prUrl,
      setPrUrl,
      currentStep,
      executionMode,
      testMetrics,
      activeModal,
      toasts,
      isSubmitting,
      isIngesting,
      durationSeconds,
      startTime,
      endTime,
      setDurationSeconds,
      metrics,
      setMetrics,
      testSuitePassed,
      targetScope,
      prTarget,
      setStatus,
      setActiveStep,
      addCompletedStep,
      setLogs,
      addLog,
      clearLogs,
      setTaskId,
      addToast,
      dismissToast,
      syncStatus,
      handleStreamEvent,
      startTask,
      approveTask,
      rejectTask,
      cancelTask,
      abortTask,
      retryPR,
      ingestRepo,
      resetTask,
    ]
  );

  return <TaskContext.Provider value={value}>{children}</TaskContext.Provider>;
};

/**
 * Hook to consume the unified global TaskContext store.
 */
export function useTask(): TaskContextType {
  const context = useContext(TaskContext);
  if (!context) {
    throw new Error('useTask must be used within a <TaskProvider>');
  }
  return context;
}

export const useTaskContext = useTask;

export default TaskContext;
