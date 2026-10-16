import React, { useMemo, useState } from 'react';
import { Check, Loader2, XCircle } from 'lucide-react';
import type {
  BackendTaskStatus,
  TaskNodeEvent,
  TaskStatusResponse,
  TaskStateObject,
} from '../types/api';
import {
  useTask,
  type WorkflowStep,
  type GlobalTaskStatus,
  normalizeWorkflowStep,
  normalizeTaskStatus,
} from '../context/TaskContext';

export interface WorkflowStepConfig {
  key: WorkflowStep;
  stepNumber: number;
  label: string;
  shortLabel: string;
  description: string;
}

export const WORKFLOW_STEP_CONFIGS: readonly WorkflowStepConfig[] = [
  {
    key: 'planner',
    stepNumber: 1,
    label: '1. Plan',
    shortLabel: 'Plan',
    description: 'Deconstruct prompt into files & repair steps',
  },
  {
    key: 'repro_gate',
    stepNumber: 2,
    label: '2. Repro Gate',
    shortLabel: 'Repro Gate',
    description: 'Isolate minimal failing reproduction test',
  },
  {
    key: 'investigator',
    stepNumber: 3,
    label: '3. Investigator',
    shortLabel: 'Investigator',
    description: 'Tree-sitter AST & pgvector context retrieval',
  },
  {
    key: 'implementer',
    stepNumber: 4,
    label: '4. Implementer',
    shortLabel: 'Implementer',
    description: 'Synthesize surgical unified git diff patch',
  },
  {
    key: 'verifier',
    stepNumber: 5,
    label: '5. Verifier',
    shortLabel: 'Verifier',
    description: 'Execute containerized Docker sandbox tests',
  },
  {
    key: 'reviewer',
    stepNumber: 6,
    label: '6. Reviewer',
    shortLabel: 'Reviewer',
    description: 'Human-in-the-loop review & upstream GitHub PR',
  },
] as const;

export type StepVisualState = 'completed' | 'running' | 'pending' | 'failed';

export interface WorkflowStepperProps {
  className?: string;
  showDescriptions?: boolean;
  /** Direct active step identifier (e.g. 'planner', 'repro_gate') */
  activeStep?: WorkflowStep | string | null;
  /** Backend active_node field override */
  active_node?: string | null;
  /** CamelCase activeNode alias */
  activeNode?: string | null;
  /** Direct completed steps list */
  completedSteps?: (WorkflowStep | string)[];
  /** Backend completed_nodes array override */
  completed_nodes?: (WorkflowStep | string)[];
  /** CamelCase completedNodes alias */
  completedNodes?: (WorkflowStep | string)[];
  /** Global task status override */
  status?: GlobalTaskStatus | string;
  /** Task state or API status response object */
  task?: TaskStatusResponse | TaskStateObject | null;
  /** Backend task status override */
  taskStatus?: BackendTaskStatus | string | null;
  /** Current node alias */
  currentNode?: string | null;
  /** Backend current_node alias */
  current_node?: string | null;
  /** Optional step number index */
  currentStep?: number;
}

/**
 * WorkflowStepper Component
 *
 * Strictly binds the 6 workflow steps:
 *   1. Plan
 *   2. Repro Gate
 *   3. Investigator
 *   4. Implementer
 *   5. Verifier
 *   6. Reviewer
 * to the backend `active_node` and `completed_nodes` array so checkmarks
 * and live spinners update in real-time as LangGraph nodes execute.
 */
export const WorkflowStepper: React.FC<WorkflowStepperProps> = ({
  className = '',
  showDescriptions = false,
  activeStep: propActiveStep,
  active_node: propActiveNode,
  activeNode: propActiveNodeCamel,
  completedSteps: propCompletedSteps,
  completed_nodes: propCompletedNodesUnderscore,
  completedNodes: propCompletedNodesCamel,
  status: propStatus,
  task,
  taskStatus: propTaskStatus,
  currentNode: propCurrentNode,
  current_node: propCurrentNodeUnderscore,
}) => {
  const globalTask = useTask();
  const [selectedStep, setSelectedStep] = useState<WorkflowStep | null>(null);

  // 1. Resolve normalized execution status
  const rawStatus =
    propStatus ??
    propTaskStatus ??
    task?.status ??
    globalTask.task?.status ??
    globalTask.status;
  const taskStatus = rawStatus ? normalizeTaskStatus(String(rawStatus)) : globalTask.status;

  // 2. Resolve active node strictly checking backend active_node, current_node, and TaskContext
  const rawNode =
    propActiveNode ??
    propActiveNodeCamel ??
    propActiveStep ??
    propCurrentNode ??
    propCurrentNodeUnderscore ??
    task?.active_node ??
    task?.activeNode ??
    task?.current_node ??
    globalTask.task?.active_node ??
    globalTask.task?.activeNode ??
    globalTask.task?.current_node ??
    globalTask.activeStep;

  const activeStep: WorkflowStep | null = useMemo(() => {
    if (rawNode !== undefined && rawNode !== null) {
      return normalizeWorkflowStep(String(rawNode));
    }
    return globalTask.activeStep;
  }, [rawNode, globalTask.activeStep]);

  // 3. Resolve completed nodes strictly checking backend completed_nodes and TaskContext
  const rawCompleted =
    propCompletedNodesUnderscore ??
    propCompletedNodesCamel ??
    propCompletedSteps ??
    task?.completed_nodes ??
    task?.completedNodes ??
    globalTask.task?.completed_nodes ??
    globalTask.task?.completedNodes ??
    globalTask.completedSteps;

  const completedSteps: WorkflowStep[] = useMemo(() => {
    if (rawCompleted !== undefined && Array.isArray(rawCompleted)) {
      return rawCompleted
        .map((item) => normalizeWorkflowStep(String(item)))
        .filter((s): s is WorkflowStep => s !== null);
    }
    return globalTask.completedSteps;
  }, [rawCompleted, globalTask.completedSteps]);

  const nodeEvents: TaskNodeEvent[] =
    task?.node_events ?? globalTask.task?.node_events ?? [];
  const selectedEvent = selectedStep
    ? [...nodeEvents]
        .reverse()
        .find((event) => normalizeWorkflowStep(event.node) === selectedStep)
    : undefined;

  const isFailedTask = taskStatus === 'FAILED';
  const isRunningTask = taskStatus === 'RUNNING';

  const completedSet = useMemo(() => new Set(completedSteps), [completedSteps]);

  const activeStepIndex = useMemo(() => {
    if (!activeStep) return -1;
    return WORKFLOW_STEP_CONFIGS.findIndex((cfg) => cfg.key === activeStep);
  }, [activeStep]);

  /**
   * Evaluates the visual state of a specific node in the 6-step pipeline:
   * - 'completed': Green badge with Check icon
   * - 'running': Glowing pulse badge with animated Loader2 spinner
   * - 'failed': Rose badge with XCircle icon
   * - 'pending': Neutral gray badge with Step Number
   */
  const getStepState = (stepKey: WorkflowStep, stepIndex: number): StepVisualState => {
    // 1. If node is actively running in the execution loop
    if (isRunningTask && activeStep === stepKey) {
      return 'running';
    }

    // 2. If node is in backend completed_nodes and not currently active
    if (completedSet.has(stepKey) && activeStep !== stepKey) {
      return 'completed';
    }

    // 3. During a live run, preceding steps are complete once a later step starts.
    if (isRunningTask && activeStepIndex > stepIndex) {
      return 'completed';
    }

    // 4. If task failed while at this specific step
    if (isFailedTask && activeStep === stepKey) {
      return 'failed';
    }

    // 5. Default: Gray outline / Pending
    return 'pending';
  };

  return (
    <section
      aria-label="Workflow Stepper"
      className={`bg-white rounded-2xl p-4 sm:p-5 border border-zinc-200/80 shadow-xs ${className}`}
    >
      <div className="flex items-center justify-between text-center relative w-full">
        {WORKFLOW_STEP_CONFIGS.map((step, index) => {
          const stepState = getStepState(step.key, index);

          return (
            <React.Fragment key={step.key}>
              {/* Step Node */}
              <div className="flex flex-col items-center flex-1 min-w-0 relative z-10 px-1">
                <button
                  type="button"
                  className="flex flex-col items-center w-full rounded-lg px-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                  aria-label={`View ${step.shortLabel} execution details`}
                  aria-pressed={selectedStep === step.key}
                  onClick={() => setSelectedStep(step.key)}
                >
                  <span
                    className={`w-8 h-8 rounded-full flex items-center justify-center mb-2 transition-all duration-300 select-none ${
                      stepState === 'completed'
                        ? 'bg-emerald-500 text-white shadow-xs border-2 border-emerald-600'
                        : stepState === 'running'
                        ? 'bg-emerald-600 text-white border-2 border-emerald-400 ring-4 ring-emerald-400/30 shadow-[0_0_15px_rgba(16,185,129,0.5)] animate-pulse'
                        : stepState === 'failed'
                        ? 'bg-rose-500 text-white border-2 border-rose-600 shadow-xs'
                        : 'bg-zinc-50 text-zinc-400 border-2 border-zinc-200 hover:border-zinc-300'
                    }`}
                    title={`${step.label}: ${stepState.toUpperCase()}`}
                  >
                    {stepState === 'completed' && (
                      <Check className="h-4 w-4 stroke-[3] text-white" aria-hidden="true" />
                    )}
                    {stepState === 'running' && (
                      <Loader2 className="h-4 w-4 animate-spin text-white" aria-hidden="true" />
                    )}
                    {stepState === 'failed' && (
                      <XCircle className="h-4 w-4 stroke-[2.5] text-white" aria-hidden="true" />
                    )}
                    {stepState === 'pending' && (
                      <span className="text-xs font-mono font-bold text-zinc-400">
                        {step.stepNumber}
                      </span>
                    )}
                  </span>
                  <span
                    className={`text-xs truncate w-full transition-colors ${
                      stepState === 'running'
                        ? 'text-zinc-950 font-bold tracking-tight'
                        : stepState === 'completed'
                        ? 'text-zinc-800 font-semibold'
                        : stepState === 'failed'
                        ? 'text-rose-700 font-bold'
                        : 'text-zinc-400 font-medium'
                    }`}
                  >
                    {step.label}
                  </span>
                  <span
                    className={`text-[10px] font-mono mt-0.5 uppercase tracking-wider ${
                      stepState === 'running'
                        ? 'text-emerald-600 font-bold animate-pulse'
                        : stepState === 'completed'
                        ? 'text-emerald-700 font-semibold'
                        : stepState === 'failed'
                        ? 'text-rose-600 font-bold'
                        : 'text-zinc-400'
                    }`}
                  >
                    {stepState === 'running'
                      ? 'Running'
                      : stepState === 'completed'
                      ? 'Done'
                      : stepState === 'failed'
                      ? 'Failed'
                      : 'Pending'}
                  </span>
                </button>
                {showDescriptions && (
                  <p className="hidden md:block text-[11px] text-zinc-500 mt-1 max-w-[140px] leading-tight">
                    {step.description}
                  </p>
                )}
              </div>

              {/* Connecting Pipeline Line */}
              {index < WORKFLOW_STEP_CONFIGS.length - 1 && (
                <div
                  className={`h-0.5 flex-1 -mt-7 sm:-mt-8 mx-0.5 transition-all duration-300 ${
                    stepState === 'completed'
                      ? 'bg-emerald-500'
                      : stepState === 'running'
                      ? 'bg-gradient-to-r from-emerald-500 to-zinc-200'
                      : 'bg-zinc-200'
                  }`}
                />
              )}
            </React.Fragment>
          );
        })}
      </div>
      {selectedStep && (
        <div className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-left">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold text-zinc-900">
              {WORKFLOW_STEP_CONFIGS.find((step) => step.key === selectedStep)?.label} details
            </h3>
            <button
              type="button"
              className="text-xs text-zinc-500 hover:text-zinc-900"
              onClick={() => setSelectedStep(null)}
            >
              Close
            </button>
          </div>
          {selectedEvent ? (
            <div className="mt-2 space-y-2 text-xs">
              <p className="font-medium text-zinc-700">
                Status: <span className="uppercase">{selectedEvent.status}</span>
              </p>
              {selectedEvent.message && <p className="text-zinc-600">{selectedEvent.message}</p>}
              {selectedEvent.error && (
                <p className="rounded bg-rose-50 p-2 text-rose-700">
                  {selectedEvent.error}
                </p>
              )}
              {selectedEvent.details && Object.keys(selectedEvent.details).length > 0 && (
                <pre className="max-h-48 overflow-auto rounded bg-white p-2 text-[11px] text-zinc-700">
                  {JSON.stringify(selectedEvent.details, null, 2)}
                </pre>
              )}
            </div>
          ) : (
            <p className="mt-2 text-xs text-zinc-500">
              No execution details have been recorded for this step yet.
            </p>
          )}
        </div>
      )}
    </section>
  );
};

export default WorkflowStepper;
