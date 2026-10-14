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
  ExecutionMode,
  PlanData,
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

export interface RepositoryMetadata {
  id: string;
  name: string;
  url?: string;
  branch: string;
  local_path?: string;
  total_chunks?: number;
}

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
  task_id?: string | null;
  status?: GlobalTaskStatus | string | null;
  metrics?: TaskMetricsData | null;
  pr_url?: string | null;
  pr_created?: boolean;
  merged?: boolean;
  plan?: PlanData | null;
  execution_plan?: PlanData | null;
  executionPlan?: PlanData | null;
  active_node?: WorkflowStep | null;
  activeNode?: WorkflowStep | null;
  current_node?: WorkflowStep | null;
  completed_nodes?: (WorkflowStep | string)[];
  completedNodes?: (WorkflowStep | string)[];
  currentDiff?: string;
  patch?: string;
  generated_diff?: string;
  repo?: RepositoryMetadata;
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
  [key: string]: any;
}

export interface TaskContextType {
  // Required Central State per architectural specification
  taskId: string | null;
  status: GlobalTaskStatus; // 'IDLE' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'ABORTED'
  taskStatus: GlobalTaskStatus; // Alias for backward compatibility across components
  activeStep: WorkflowStep | null;
  completedSteps: WorkflowStep[];
  logs: string[];

  // Execution Duration & Timestamps
  durationSeconds: number | null;
  duration_seconds?: number | null;
  startTime: string | null;
  start_time?: string | null;
  endTime: string | null;
  end_time?: string | null;
  setDurationSeconds: (seconds: number | null) => void;

  // Dynamic Metrics & Global Task State Binding
  task: TaskStateObject & Partial<TaskStatusResponse>;
  metrics: TaskMetricsData | null;
  setMetrics: React.Dispatch<React.SetStateAction<TaskMetricsData | null>>;

  // Real-time Execution Telemetry (Pillars 1 & 2)
  testSuitePassed: number;
  test_suite_passed: number;
  retryCount: number;
  retry_count: number;
  targetScope: string | null;
  target_scope: string | null;
  prTarget: string | null;
  pr_target: string | null;

  // Synchronous State across tabs (Unified Diff, Event Stream, RAG Explorer, Execution Plan)
  currentRepo: RepositoryMetadata;
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
  setCurrentRepo: (repo: Partial<RepositoryMetadata> | string) => void;
  setCurrentDiff: (diff: string) => void;
  setPlan: (plan: PlanData | null) => void;
  setContextRetrieved: (chunks: CodeContextChunk[]) => void;
  setPrUrl: (url: string | null) => void;
  setCurrentStep: (step: number) => void;
  setExecutionMode: (mode: ExecutionMode) => void;
  setActiveModal: (modal: 'reject' | null) => void;
  addToast: (type: 'success' | 'error' | 'info', message: string) => void;
  dismissToast: (id: string) => void;

  // Hydration & Refresh Helpers
  refreshTask: (silent?: boolean) => Promise<TaskStatusResponse | null>;
  syncStatus: (silent?: boolean) => Promise<TaskStatusResponse | null>;

  // Synchronous Event Ingestion Action
  handleStreamEvent: (event: AgentEventPayload | any) => void;

  // Global Workflows
  startTask: (prompt: string, targetRepoId?: string, mode?: ExecutionMode) => Promise<string>;
  approveTask: () => Promise<void>;
  rejectTask: (feedbackText: string) => Promise<void>;
  cancelTask: () => Promise<void>;
  abortTask: (targetTaskId?: string) => Promise<void>;
  retryPR: () => Promise<string | null>;
  ingestRepo: (repoUrl: string, branch?: string) => Promise<string | null>;
  resetTask: () => void;
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

const DEFAULT_REPO: RepositoryMetadata = {
  id: 'default-repo',
  name: 'default-repo',
  url: 'https://github.com/fastapi/fastapi',
  branch: 'main',
};

const TaskContext = createContext<TaskContextType | undefined>(undefined);

export const TaskProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  // 1. Central Task ID hydrated from localStorage
  const [taskId, setTaskIdState] = useState<string | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        return localStorage.getItem('forge_active_task_id');
      } catch {
        // ignore
      }
    }
    return null;
  });

  // 2. Central Execution Status
  const [status, setStatusState] = useState<GlobalTaskStatus>('IDLE');
  const [rawStatus, setRawStatus] = useState<GranularTaskStatus>('idle');

  // 3. Central Active Workflow Step & Completed Nodes
  const [activeStep, setActiveStepState] = useState<WorkflowStep | null>(null);
  const [completedSteps, setCompletedSteps] = useState<WorkflowStep[]>([]);

  // 4. Central Live Telemetry Logs
  const [logs, setLogs] = useState<string[]>([]);

  // 5. Current Active Repository hydrated from localStorage
  const [currentRepo, setCurrentRepoState] = useState<RepositoryMetadata>(() => {
    if (typeof window !== 'undefined') {
      try {
        const saved = localStorage.getItem('forge_current_repo');
        if (saved) return JSON.parse(saved);
      } catch {
        // ignore
      }
    }
    return DEFAULT_REPO;
  });

  // 6. Shared Synchronous Tab Data hydrated from localStorage
  const [currentDiff, setCurrentDiffState] = useState<string>(() => {
    if (typeof window !== 'undefined') {
      try {
        return localStorage.getItem('forge_current_diff') || '';
      } catch {
        // ignore
      }
    }
    return '';
  });

  const [plan, setPlanState] = useState<PlanData | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        const saved = localStorage.getItem('forge_execution_plan');
        if (saved) return JSON.parse(saved);
      } catch {
        // ignore
      }
    }
    return null;
  });

  const [contextRetrieved, setContextRetrieved] = useState<CodeContextChunk[]>([]);

  const [prUrl, setPrUrlState] = useState<string | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        return localStorage.getItem('forge_pr_url');
      } catch {
        // ignore
      }
    }
    return null;
  });

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
        const saved = localStorage.getItem('forge_duration_seconds');
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

  const [startTime, setStartTime] = useState<string | null>(null);
  const [endTime, setEndTime] = useState<string | null>(null);

  // Dynamic Metrics State hydrated from localStorage
  const [metrics, setMetricsState] = useState<TaskMetricsData | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        const saved = localStorage.getItem('forge_metrics');
        if (saved) return JSON.parse(saved);
      } catch {
        // ignore
      }
    }
    return null;
  });

  // Real-time Execution Telemetry State (Pillars 1 & 2)
  const [testSuitePassed, setTestSuitePassed] = useState<number>(0);
  const [targetScope, setTargetScope] = useState<string | null>(null);
  const [prTarget, setPrTarget] = useState<string | null>(null);

  // Persistent Setters to keep localStorage in sync across tabs and refreshes
  const setCurrentDiff = useCallback((diff: string) => {
    setCurrentDiffState(diff);
    try {
      if (diff) {
        localStorage.setItem('forge_current_diff', diff);
      } else {
        localStorage.removeItem('forge_current_diff');
      }
    } catch {
      // ignore
    }
  }, []);

  const setPlan = useCallback((p: PlanData | null) => {
    setPlanState(p);
    try {
      if (p) {
        localStorage.setItem('forge_execution_plan', JSON.stringify(p));
      } else {
        localStorage.removeItem('forge_execution_plan');
      }
    } catch {
      // ignore
    }
  }, []);

  const setPrUrl = useCallback((url: string | null) => {
    setPrUrlState(url);
    try {
      if (url) {
        localStorage.setItem('forge_pr_url', url);
      } else {
        localStorage.removeItem('forge_pr_url');
      }
    } catch {
      // ignore
    }
  }, []);

  const setMetrics = useCallback((val: React.SetStateAction<TaskMetricsData | null>) => {
    setMetricsState((prev) => {
      const next = typeof val === 'function' ? val(prev) : val;
      try {
        if (next) {
          localStorage.setItem('forge_metrics', JSON.stringify(next));
        } else {
          localStorage.removeItem('forge_metrics');
        }
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  const setDurationSeconds = useCallback((sec: number | null) => {
    setDurationSecondsState(sec);
    try {
      if (typeof sec === 'number') {
        localStorage.setItem('forge_duration_seconds', String(sec));
      } else {
        localStorage.removeItem('forge_duration_seconds');
      }
    } catch {
      // ignore
    }
  }, []);

  // Memoized unified task state object exposing task.metrics, duration_seconds, timestamps, execution_plan, active_node
  const task = useMemo<TaskStateObject & Partial<TaskStatusResponse>>(
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
      repo: currentRepo,
      repo_id: currentRepo.id,
      branch_name: currentRepo.branch || 'main',
      prompt: logs.length > 0 ? logs[0] : null,
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
      status,
      metrics,
      prUrl,
      plan,
      activeStep,
      completedSteps,
      currentDiff,
      currentRepo,
      durationSeconds,
      startTime,
      endTime,
      testSuitePassed,
      testMetrics.passed,
      testMetrics.retries,
      targetScope,
      prTarget,
      logs,
    ]
  );

  // Active SSE stream unsubscribe ref
  const unsubscribeRef = useRef<(() => void) | null>(null);

  // Sync currentRepo with localStorage
  const setCurrentRepo = useCallback((repoInput: Partial<RepositoryMetadata> | string) => {
    setCurrentRepoState((prev) => {
      let next: RepositoryMetadata;
      if (typeof repoInput === 'string') {
        next = {
          ...prev,
          id: repoInput,
          name: repoInput,
        };
      } else {
        next = {
          ...prev,
          ...repoInput,
          id: repoInput.id || prev.id,
          name: repoInput.name || prev.name,
          branch: repoInput.branch || prev.branch,
        };
      }
      try {
        localStorage.setItem('forge_current_repo', JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  // Sync taskId with localStorage
  const setTaskId = useCallback((id: string | null) => {
    setTaskIdState(id);
    try {
      if (id) {
        localStorage.setItem('forge_active_task_id', id);
      } else {
        localStorage.removeItem('forge_active_task_id');
      }
    } catch {
      // ignore
    }
  }, []);

  const setStatus = useCallback((nextStatus: GlobalTaskStatus) => {
    setStatusState(nextStatus);
  }, []);

  const setActiveStep = useCallback((step: WorkflowStep | null) => {
    setActiveStepState(step);
  }, []);

  const addCompletedStep = useCallback((step: WorkflowStep) => {
    setCompletedSteps((prev) => (prev.includes(step) ? prev : [...prev, step]));
  }, []);

  const addLog = useCallback((log: string) => {
    setLogs((prev) => [...prev, log]);
  }, []);

  const clearLogs = useCallback(() => {
    setLogs([]);
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

  // Teardown active event stream
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

  // Synchronously update task status, active step, completed steps, and live logs from SSE/WebSocket events
  const handleStreamEvent = useCallback(
    (event: AgentEventPayload | any) => {
      if (!event) return;

      // 1. Resolve raw status and normalized global status
      const rawEvtStatus = event.status || event.payload?.status || event.data?.status;
      if (rawEvtStatus) {
        const normalized = normalizeTaskStatus(rawEvtStatus);
        setRawStatus(rawEvtStatus as GranularTaskStatus);
        setStatusState(normalized);
      }

      // 2. Resolve active workflow step & completed steps
      const rawNode =
        event.node ||
        event.payload?.node ||
        event.data?.node ||
        event.current_node;
      const resolvedStep = normalizeWorkflowStep(rawNode);

      const evtStepNum = event.step ?? event.payload?.step ?? event.data?.step;
      let effectiveStep = resolvedStep;
      if (!effectiveStep && typeof evtStepNum === 'number' && evtStepNum >= 1 && evtStepNum <= 6) {
        effectiveStep = WORKFLOW_STEPS[evtStepNum - 1];
      }

      if (effectiveStep) {
        setActiveStepState(effectiveStep);
        const stepIdx = WORKFLOW_STEPS.indexOf(effectiveStep);
        setCurrentStep(stepIdx + 1);

        // Preceding workflow steps are marked as completed
        setCompletedSteps((prev) => {
          const next = new Set<WorkflowStep>(prev);
          for (let i = 0; i < stepIdx; i++) {
            next.add(WORKFLOW_STEPS[i]);
          }
          return Array.from(next);
        });
      }

      // Check explicit completed nodes array from backend
      const explicitCompleted =
        event.completed_nodes ||
        event.payload?.completed_nodes ||
        event.data?.completed_nodes;
      if (Array.isArray(explicitCompleted)) {
        setCompletedSteps((prev) => {
          const next = new Set<WorkflowStep>(prev);
          for (const item of explicitCompleted) {
            const step = normalizeWorkflowStep(item);
            if (step) next.add(step);
          }
          return Array.from(next);
        });
      }

      // 3. Synchronously append live telemetry logs
      const logMessage =
        event.message ||
        event.payload?.message ||
        event.data?.message ||
        (event.node ? `[${event.node}] Transitioned to status: ${rawEvtStatus || 'running'}` : null);
      if (logMessage) {
        addLog(String(logMessage));
      }

      // 4. Synchronize Plan, Context, Diff, Metrics, and PR URL
      const planData =
        event.execution_plan ||
        event.payload?.execution_plan ||
        event.data?.execution_plan ||
        event.plan ||
        event.payload?.plan ||
        event.data?.plan;
      if (planData) {
        setPlan(planData);
      }

      const contextData =
        event.context_retrieved ||
        event.payload?.context_retrieved ||
        event.data?.context_retrieved;
      if (contextData) {
        setContextRetrieved(contextData);
      }

      const diffData =
        event.generated_diff ||
        event.payload?.generated_diff ||
        event.data?.generated_diff ||
        event.patch;
      if (diffData) {
        setCurrentDiff(diffData);
      }

      const testResults =
        event.test_results ||
        event.payload?.test_results ||
        event.data?.test_results;
      const testPassedVal =
        event.test_suite_passed ??
        event.payload?.test_suite_passed ??
        event.data?.test_suite_passed ??
        event.passed_count ??
        event.test_results?.passed_count ??
        event.payload?.test_results?.passed_count ??
        event.data?.test_results?.passed_count;
      if (typeof testPassedVal === 'number') {
        setTestSuitePassed(testPassedVal);
      }

      if (testResults) {
        setTestMetrics((prev) => ({
          ...prev,
          passed: typeof testPassedVal === 'number' ? testPassedVal : (testResults.passed_count ?? prev.passed),
          failed: testResults.failed_count ?? prev.failed,
        }));
      }

      const retryCount =
        event.retry_count ??
        event.payload?.retry_count ??
        event.data?.retry_count;
      if (typeof retryCount === 'number') {
        setTestMetrics((prev) => ({
          ...prev,
          retries: retryCount,
        }));
      }

      const targetScopeVal =
        event.target_scope ??
        event.payload?.target_scope ??
        event.data?.target_scope;
      if (targetScopeVal) {
        setTargetScope(String(targetScopeVal));
      }

      const prTargetVal =
        event.pr_target ??
        event.payload?.pr_target ??
        event.data?.pr_target;
      if (prTargetVal) {
        setPrTarget(String(prTargetVal));
      }

      const prUrlVal =
        event.pr_url ||
        event.payload?.pr_url ||
        event.data?.pr_url;
      if (prUrlVal) {
        setPrUrl(String(prUrlVal));
      }

      // Synchronize Dynamic Task Metrics (diff_size, latency_impact, confidence)
      const metricsData =
        event.metrics ||
        event.payload?.metrics ||
        event.data?.metrics;
      if (metricsData) {
        setMetrics(metricsData);
      } else if (
        event.confidence_score !== undefined ||
        event.confidence !== undefined ||
        event.diff_stats !== undefined ||
        event.latency_impact !== undefined
      ) {
        setMetrics((prev) => {
          const added =
            event.diff_stats?.lines_added ??
            event.lines_added ??
            prev?.lines_added ??
            0;
          const deleted =
            event.diff_stats?.lines_deleted ??
            event.lines_deleted ??
            prev?.lines_deleted ??
            0;
          const conf =
            event.confidence_score ??
            event.confidence ??
            prev?.confidence;
          const latency =
            event.latency_impact ??
            prev?.latency_impact;
          return {
            ...(prev || {}),
            confidence: conf,
            confidence_score: conf,
            diff_stats: event.diff_stats ?? prev?.diff_stats,
            diff_size: { added, deleted },
            lines_added: added,
            lines_deleted: deleted,
            latency_impact: latency,
            latency_impact_ms:
              event.latency_impact_ms ??
              (typeof latency === 'number' && Math.abs(latency) < 10
                ? latency * 1000
                : latency),
          };
        });
      }

      // Synchronize execution duration and timestamps
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

      // 5. Handle terminal / review gate transitions
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
    [addLog, addToast, closeStream, setCurrentDiff, setPlan, setPrUrl, setMetrics, setDurationSeconds]
  );

  /**
   * Applies full backend task state to global store.
   * Hydrates status, diff, plan, prUrl, metrics, duration, timestamps, telemetry, and steps.
   */
  const applyTaskUpdate = useCallback(
    (taskInfo: TaskStatusResponse) => {
      if (!taskInfo) return;

      const normalized = normalizeTaskStatus(taskInfo.status);
      setStatusState(normalized);
      setRawStatus(taskInfo.status as GranularTaskStatus);

      // Hydrate PR URL
      if (taskInfo.pr_url) {
        setPrUrl(taskInfo.pr_url);
      }

      // Hydrate Diff / Patch (Crucial for Unified Diff Viewer & Review)
      const patchData = taskInfo.patch || taskInfo.generated_diff;
      if (patchData) {
        setCurrentDiff(patchData);
      }

      // Hydrate Execution Plan
      const planHydration = taskInfo.execution_plan || taskInfo.plan;
      if (planHydration) {
        setPlan(planHydration as PlanData);
      }

      // Hydrate active node and completed nodes
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

      // Hydrate Dynamic Task Metrics (diff_size, latency_impact, confidence)
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

      // Hydrate Execution Duration & Timestamps
      if (typeof taskInfo.duration_seconds === 'number') {
        setDurationSeconds(taskInfo.duration_seconds);
      }
      if (taskInfo.start_time) {
        setStartTime(taskInfo.start_time);
      }
      if (taskInfo.end_time) {
        setEndTime(taskInfo.end_time);
      }

      // Hydrate Real-time Telemetry (Pillars 1 & 2)
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

      // Terminal state transitions
      if (normalized === 'COMPLETED') {
        setIsSubmitting(false);
        setCompletedSteps([...WORKFLOW_STEPS]);
        setActiveStepState(null);
        closeStream();
      } else if (normalized === 'FAILED') {
        setIsSubmitting(false);
        setActiveStepState(null);
        closeStream();
      } else if (normalized === 'ABORTED') {
        setIsSubmitting(false);
        setActiveStepState(null);
        closeStream();
      } else if ((taskInfo.status as string) === 'awaiting_approval' || taskInfo.current_node === 'reviewer') {
        setIsSubmitting(false);
        setActiveStepState('reviewer');
      }
    },
    [closeStream, setCurrentDiff, setPlan, setPrUrl, setMetrics, setDurationSeconds]
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

        // If task is still actively executing and no SSE stream is open, connect stream
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

  // 1. Initial hydration on mount or when taskId changes
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

  // 2. CENTRALIZED BACKGROUND POLLING:
  // Automatically poll every 2 seconds while task status is RUNNING / PENDING
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
      const effectiveRepoId = targetRepoId || currentRepo.id || 'default-repo';

      setIsSubmitting(true);
      closeStream();
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
      executionMode,
      currentRepo.id,
      closeStream,
      setTaskId,
      setCurrentDiff,
      setPlan,
      setPrUrl,
      setMetrics,
      setDurationSeconds,
      addToast,
      addLog,
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
      }
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `Approval failed: ${msg}`);
    } finally {
      setIsSubmitting(false);
    }
  }, [taskId, isSubmitting, addToast, addLog, addCompletedStep, applyTaskUpdate, setPrUrl]);

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

    try {
      localStorage.removeItem('forge_active_task_id');
      localStorage.removeItem('forge_current_diff');
      localStorage.removeItem('forge_execution_plan');
      localStorage.removeItem('forge_pr_url');
      localStorage.removeItem('forge_metrics');
      localStorage.removeItem('forge_duration_seconds');
    } catch {
      // ignore
    }
  }, [closeStream, setTaskId, setCurrentDiff, setPlan, setPrUrl, setMetrics, setDurationSeconds]);

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
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `Cancellation error: ${msg}`);
      setStatusState('ABORTED');
    } finally {
      setIsSubmitting(false);
    }
  }, [taskId, closeStream, addToast, addLog]);

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
      try {
        addToast('info', 'Halting pipeline and terminating active worker processes...');
        await apiClient.abortTask(effectiveId);
        setStatusState('ABORTED');
        setRawStatus('aborted');
        setActiveStepState(null);
        addLog('[System] Task execution forcefully halted. Engine state reset.');
        addToast('success', 'Pipeline aborted and resources terminated.');
      } catch (err: unknown) {
        const msg = err instanceof ApiError ? err.message : (err as Error).message;
        addToast('error', `Abort notice: ${msg}`);
        addLog(`[System] Abort request completed: ${msg}`);
        setStatusState('ABORTED');
        setRawStatus('aborted');
      } finally {
        setIsSubmitting(false);
        resetTask();
      }
    },
    [taskId, closeStream, addToast, addLog, resetTask]
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
  }, [taskId, addToast, addLog, setPrUrl]);

  // Action: Ingest Target Repository
  const ingestRepo = useCallback(
    async (repoUrl: string, branch = 'main'): Promise<string | null> => {
      setIsIngesting(true);
      try {
        addToast('info', `Ingesting and indexing repository "${repoUrl}"...`);
        const res = await apiClient.ingestRepository({ repo_url: repoUrl, branch });

        const repoName = repoUrl.split('/').pop()?.replace('.git', '') || 'repo';
        const newRepo: RepositoryMetadata = {
          id: res.repo_id,
          name: repoName,
          url: repoUrl,
          branch,
          total_chunks: res.total_chunks,
        };

        setCurrentRepo(newRepo);
        addToast('success', `Repository indexed! ${res.total_chunks} code chunks stored in pgvector.`);
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
    [addToast, setCurrentRepo, addLog]
  );

  const value = useMemo<TaskContextType>(
    () => ({
      // Central State
      taskId,
      status,
      taskStatus: status, // Backward compatibility alias
      activeStep,
      completedSteps,
      logs,

      // Dynamic Metrics & Global Task State Binding
      task,
      metrics,
      setMetrics,

      // Real-time Execution Telemetry (Pillars 1 & 2)
      testSuitePassed,
      test_suite_passed: testSuitePassed,
      retryCount: testMetrics.retries,
      retry_count: testMetrics.retries,
      targetScope: targetScope || (plan?.target_files?.[0] ?? 'services/api/forge/routers/rag.py'),
      target_scope: targetScope || (plan?.target_files?.[0] ?? 'services/api/forge/routers/rag.py'),
      prTarget: prTarget || (taskId ? `forge/auto-patch-${taskId}` : 'forge/auto-patch'),
      pr_target: prTarget || (taskId ? `forge/auto-patch-${taskId}` : 'forge/auto-patch'),

      // Execution Duration & Timestamps
      durationSeconds,
      duration_seconds: durationSeconds,
      startTime,
      start_time: startTime,
      endTime,
      end_time: endTime,
      setDurationSeconds,

      // Synchronous Tab State
      currentRepo,
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
      setCurrentRepo,
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
      approveTask,
      rejectTask,
      cancelTask,
      abortTask,
      retryPR,
      ingestRepo,
      resetTask,
    }),
    [
      taskId,
      status,
      activeStep,
      completedSteps,
      logs,
      durationSeconds,
      startTime,
      endTime,
      setDurationSeconds,
      task,
      metrics,
      setMetrics,
      testSuitePassed,
      targetScope,
      prTarget,
      currentRepo,
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
      setStatus,
      setActiveStep,
      addCompletedStep,
      addLog,
      clearLogs,
      setTaskId,
      setCurrentRepo,
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
