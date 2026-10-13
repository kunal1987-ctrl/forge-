import { useState, useEffect, useRef, useCallback } from 'react';
import type {
  AgentEventPayload,
  CodeContextChunk,
  ExecutionMode,
  PlanData,
  TaskStatus,
} from '../types/api';
import { apiClient, ApiError } from '../services/apiClient';

export interface ToastNotification {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
}

export interface UseAgentControllerReturn {
  // State
  taskId: string | null;
  status: TaskStatus;
  mode: ExecutionMode;
  logs: string[];
  currentDiff: string;
  prUrl: string | null;
  testMetrics: { passed: number; failed: number; retries: number };
  isSubmitting: boolean;
  isIngesting: boolean;
  activeModal: 'reject' | null;
  plan: PlanData | null;
  contextRetrieved: CodeContextChunk[];
  toasts: ToastNotification[];
  currentStep: number;
  repoId: string;

  // Setters
  setMode: (mode: ExecutionMode) => void;
  setActiveModal: (modal: 'reject' | null) => void;
  setRepoId: (repoId: string) => void;
  dismissToast: (id: string) => void;

  // Actions
  handleStartRun: (prompt: string, targetRepoId?: string) => Promise<void>;
  handleApprove: () => Promise<void>;
  handleReject: (feedbackText: string) => Promise<void>;
  handleCancel: () => Promise<void>;
  handleIngest: (repoUrl: string, branch?: string) => Promise<string | null>;
}

export function useAgentController(): UseAgentControllerReturn {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [status, setStatus] = useState<TaskStatus>('idle');
  const [mode, setMode] = useState<ExecutionMode>('fast');
  const [logs, setLogs] = useState<string[]>([]);
  const [currentDiff, setCurrentDiff] = useState<string>('');
  const [prUrl, setPrUrl] = useState<string | null>(null);
  const [testMetrics, setTestMetrics] = useState<{ passed: number; failed: number; retries: number }>({
    passed: 0,
    failed: 0,
    retries: 0,
  });
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [isIngesting, setIsIngesting] = useState<boolean>(false);
  const [activeModal, setActiveModal] = useState<'reject' | null>(null);
  const [plan, setPlan] = useState<PlanData | null>(null);
  const [contextRetrieved, setContextRetrieved] = useState<CodeContextChunk[]>([]);
  const [toasts, setToasts] = useState<ToastNotification[]>([]);
  const [currentStep, setCurrentStep] = useState<number>(0);
  const [repoId, setRepoId] = useState<string>('default-repo');

  // Ref to hold cleanup function for active SSE stream
  const unsubscribeRef = useRef<(() => void) | null>(null);

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

  // Safe teardown of event source
  const closeStream = useCallback(() => {
    if (unsubscribeRef.current) {
      unsubscribeRef.current();
      unsubscribeRef.current = null;
    }
  }, []);

  // Teardown stream on unmount
  useEffect(() => {
    return () => {
      closeStream();
    };
  }, [closeStream]);

  // Handle incoming SSE task events
  const handleEvent = useCallback(
    (event: AgentEventPayload) => {
      if (event.status) {
        setStatus(event.status);
      }

      if (event.step !== undefined) {
        setCurrentStep(event.step);
      }

      if (event.message) {
        setLogs((prev) => [...prev, event.message!]);
      } else if (event.node) {
        setLogs((prev) => [...prev, `[${event.node}] Status transitioned to ${event.status}`]);
      }

      if (event.plan) {
        setPlan(event.plan);
      }

      if (event.context_retrieved) {
        setContextRetrieved(event.context_retrieved);
      }

      if (event.generated_diff) {
        setCurrentDiff(event.generated_diff);
      }

      if (event.test_results) {
        setTestMetrics((prev) => ({
          ...prev,
          passed: event.test_results?.passed_count ?? prev.passed,
          failed: event.test_results?.failed_count ?? prev.failed,
        }));
      }

      if (event.retry_count !== undefined) {
        setTestMetrics((prev) => ({
          ...prev,
          retries: event.retry_count,
        }));
      }

      if (event.pr_url) {
        setPrUrl(event.pr_url);
      }

      // Automatically handle terminal states
      if (event.status === 'pr_created' || event.status === 'cleaned_up') {
        setIsSubmitting(false);
        addToast('success', 'Task finished successfully! Pull request created.');
        closeStream();
      } else if (event.status === 'failed') {
        setIsSubmitting(false);
        addToast('error', event.error || 'Autonomous task failed.');
        closeStream();
      } else if (event.status === 'awaiting_approval') {
        setIsSubmitting(false);
        addToast('info', 'Task reached Review Gate: Human review required.');
      }
    },
    [addToast, closeStream]
  );

  /**
   * Action 1: Start Autonomous Run
   */
  const handleStartRun = useCallback(
    async (prompt: string, targetRepoId?: string) => {
      if (isSubmitting || status === 'planning' || status === 'implementing') {
        return;
      }

      if (!prompt.trim()) {
        addToast('error', 'Please provide a task prompt or problem description.');
        return;
      }

      setIsSubmitting(true);
      closeStream();
      setLogs([`[Client] Dispatching task with ${mode.toUpperCase()} mode...`]);
      setCurrentDiff('');
      setPlan(null);
      setContextRetrieved([]);
      setPrUrl(null);
      setTestMetrics({ passed: 0, failed: 0, retries: 0 });
      setCurrentStep(1);
      setStatus('planning');

      try {
        const effectiveRepoId = targetRepoId || repoId || 'default-repo';
        const res = await apiClient.createTask({
          prompt: prompt.trim(),
          repo_id: effectiveRepoId,
          mode,
        });

        setTaskId(res.task_id);
        try {
          localStorage.setItem('forge_active_task_id', res.task_id);
        } catch {
          // ignore
        }
        setStatus(res.status || 'planning');
        setIsSubmitting(false);
        addToast('success', `Task ${res.task_id.slice(0, 8)} accepted by FORGE orchestrator.`);

        // Subscribe to real-time events via SSE
        const unsubscribe = apiClient.subscribeToTaskEvents(
          res.task_id,
          handleEvent,
          (err) => {
            console.warn('SSE subscription warning:', err);
          }
        );
        unsubscribeRef.current = unsubscribe;
      } catch (err) {
        setIsSubmitting(false);
        setStatus('idle');
        const message = err instanceof ApiError ? err.message : (err as Error).message || 'Failed to start task.';
        addToast('error', message);
        setLogs((prev) => [...prev, `[Error] ${message}`]);
      }
    },
    [isSubmitting, status, mode, repoId, closeStream, addToast, handleEvent]
  );

  /**
   * Action 2: Human Approval (Approve & Create PR)
   */
  const handleApprove = useCallback(async () => {
    if (!taskId || isSubmitting) return;

    setIsSubmitting(true);
    try {
      addToast('info', 'Submitting reviewer approval to LangGraph...');
      await apiClient.submitReviewDecision(taskId, { decision: 'approve' });
      setLogs((prev) => [...prev, '[Reviewer] Human decision approved. Generating Pull Request.']);

      // Verify real task status from backend instead of setting premature success flag
      try {
        const verified = await apiClient.getTaskStatus(taskId);
        if (verified.status === 'COMPLETED' && verified.pr_created) {
          setStatus('pr_created');
          if (verified.pr_url) setPrUrl(verified.pr_url);
          addToast('success', 'Pull request approved and created in GitHub!');
        } else {
          addToast('info', 'Approval registered. Awaiting verified Pull Request from backend...');
        }
      } catch {
        // SSE will stream final status when PR is verified
      }
    } catch (err) {
      const message = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `Approval failed: ${message}`);
    } finally {
      setIsSubmitting(false);
    }
  }, [taskId, isSubmitting, addToast]);

  /**
   * Action 3: Human Rejection (Request Changes)
   */
  const handleReject = useCallback(
    async (feedbackText: string) => {
      if (!taskId || isSubmitting) return;

      setIsSubmitting(true);
      try {
        addToast('info', 'Submitting revision feedback to repair loop...');
        await apiClient.submitReviewDecision(taskId, {
          decision: 'reject',
          feedback: feedbackText,
        });
        setActiveModal(null);
        setStatus('implementing');
        setLogs((prev) => [
          ...prev,
          `[Reviewer] Human requested revisions: "${feedbackText}". Re-routing to implementer.`,
        ]);
        addToast('info', 'Changes requested. FORGE implementer is refining patch.');
      } catch (err) {
        const message = err instanceof ApiError ? err.message : (err as Error).message;
        addToast('error', `Rejection submission failed: ${message}`);
      } finally {
        setIsSubmitting(false);
      }
    },
    [taskId, isSubmitting, addToast]
  );

  /**
   * Action 4: Cancel Task & Clean Worktree
   */
  const handleCancel = useCallback(async () => {
    if (!taskId) {
      setStatus('idle');
      return;
    }

    setIsSubmitting(true);
    closeStream();
    try {
      addToast('info', 'Cancelling task and cleaning worktree...');
      await apiClient.cancelTask(taskId);
      setStatus('cleaned_up');
      setLogs((prev) => [...prev, '[System] Task cancelled by user. Worktree wiped.']);
      addToast('success', 'Task cancelled and isolated worktree cleaned up.');
      setTimeout(() => {
        setStatus('idle');
        setTaskId(null);
      }, 1000);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : (err as Error).message;
      addToast('error', `Cancellation failed: ${message}`);
      setStatus('idle');
      setTaskId(null);
    } finally {
      setIsSubmitting(false);
    }
  }, [taskId, closeStream, addToast]);

  /**
   * Action 5: Ingest Repository via AST Parser
   */
  const handleIngest = useCallback(
    async (repoUrl: string, branch = 'main'): Promise<string | null> => {
      if (!repoUrl.trim() || isIngesting) return null;

      setIsIngesting(true);
      addToast('info', `Cloning & indexing repository ${repoUrl}...`);
      try {
        const res = await apiClient.ingestRepository({
          repo_url: repoUrl.trim(),
          branch,
        });
        setRepoId(res.repo_id);
        addToast('success', `Ingested ${res.total_chunks || 0} code symbols into Graph-RAG!`);
        return res.repo_id;
      } catch (err) {
        const message = err instanceof ApiError ? err.message : (err as Error).message;
        addToast('error', `Repository ingestion failed: ${message}`);
        return null;
      } finally {
        setIsIngesting(false);
      }
    },
    [isIngesting, addToast]
  );

  return {
    taskId,
    status,
    mode,
    logs,
    currentDiff,
    prUrl,
    testMetrics,
    isSubmitting,
    isIngesting,
    activeModal,
    plan,
    contextRetrieved,
    toasts,
    currentStep,
    repoId,
    setMode,
    setActiveModal,
    setRepoId,
    dismissToast,
    handleStartRun,
    handleApprove,
    handleReject,
    handleCancel,
    handleIngest,
  };
}
