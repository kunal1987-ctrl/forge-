import { useCallback } from 'react';
import type {
  CodeContextChunk,
  ExecutionMode,
  PlanData,
  TaskStatus,
} from '../types/api';
import {
  useTask,
  type ToastNotification,
  type GlobalTaskStatus,
  type RepositoryMetadata,
} from '../context/TaskContext';

export type { ToastNotification, GlobalTaskStatus, RepositoryMetadata };

export interface UseAgentControllerReturn {
  // State
  taskId: string | null;
  taskStatus: GlobalTaskStatus;
  currentRepo: RepositoryMetadata;
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

/**
 * Controller hook bridging to the global TaskContext store.
 * Synchronizes the active repository, running task ID, live execution status,
 * patch diff, execution plan, RAG context, and logs globally across all tabs.
 */
export function useAgentController(): UseAgentControllerReturn {
  const task = useTask();

  const handleStartRun = useCallback(
    async (prompt: string, targetRepoId?: string) => {
      await task.startTask(prompt, targetRepoId);
    },
    [task]
  );

  const setRepoId = useCallback(
    (repoId: string) => {
      task.setCurrentRepo({ id: repoId, name: repoId });
    },
    [task]
  );

  return {
    taskId: task.taskId,
    taskStatus: task.taskStatus,
    currentRepo: task.currentRepo,
    status: task.rawStatus,
    mode: task.executionMode,
    logs: task.logs,
    currentDiff: task.currentDiff,
    prUrl: task.prUrl,
    testMetrics: task.testMetrics,
    isSubmitting: task.isSubmitting,
    isIngesting: task.isIngesting,
    activeModal: task.activeModal,
    plan: task.plan,
    contextRetrieved: task.contextRetrieved,
    toasts: task.toasts,
    currentStep: task.currentStep,
    repoId: task.currentRepo.id,

    setMode: task.setExecutionMode,
    setActiveModal: task.setActiveModal,
    setRepoId,
    dismissToast: task.dismissToast,

    handleStartRun,
    handleApprove: task.approveTask,
    handleReject: task.rejectTask,
    handleCancel: task.cancelTask,
    handleIngest: task.ingestRepo,
  };
}

export default useAgentController;
