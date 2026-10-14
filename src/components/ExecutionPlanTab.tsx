import React, { useState } from 'react';
import {
  FileCode,
  ShieldCheck,
  Terminal,
  CheckCircle2,
  Clock,
  Loader2,
  Copy,
  Check,
  Code2,
  ListOrdered,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { useTask, type WorkflowStep } from '../context/TaskContext';
import type { PlanData, TaskStateObject } from '../types/api';

export interface ExecutionPlanTabProps {
  /** Optional direct execution_plan override */
  execution_plan?: PlanData | Record<string, any> | null;
  /** Optional task state object override */
  task?: TaskStateObject | null;
  /** Optional className for custom layout */
  className?: string;
  /** Whether the parent view is actively executing */
  isRunning?: boolean;
  /** Optional active workflow step */
  activeStep?: WorkflowStep | string | null;
}

/**
 * Normalizes a raw logic step string or object into a clean title and description.
 * Ensures the required format (e.g., "Step 1: Locate utility", "Step 2: Add validation").
 */
function normalizeStepText(raw: any, index: number): { stepNumber: number; title: string; detail?: string } {
  const stepNumber = index + 1;
  let fullText = '';
  let detail: string | undefined = undefined;

  if (typeof raw === 'string') {
    fullText = raw.trim();
  } else if (raw && typeof raw === 'object') {
    fullText = String(raw.title || raw.step || raw.description || raw.action || JSON.stringify(raw)).trim();
    if (raw.detail || raw.explanation || raw.code) {
      detail = String(raw.detail || raw.explanation || raw.code);
    }
  } else {
    fullText = `Execute pipeline phase ${stepNumber}`;
  }

  // Strip leading "Step X:" or "X." or "X -" so we don't duplicate prefixes
  const cleanTitle = fullText.replace(/^(step\s*\d+[:.-]?|\d+[:.-]?)\s*/i, '');

  return {
    stepNumber,
    title: `Step ${stepNumber}: ${cleanTitle}`,
    detail,
  };
}

/**
 * ExecutionPlanTab Component
 *
 * Binds directly to `task.execution_plan` (or `task.plan`) from backend state.
 * When the planner node completes, it renders the structured multi-step plan steps:
 * - Step 1: Locate utility
 * - Step 2: Add validation
 * - Step 3: Write unit test
 * Along with target files, test strategy, and reproduction code.
 */
export const ExecutionPlanTab: React.FC<ExecutionPlanTabProps> = ({
  execution_plan: propExecutionPlan,
  task: propTask,
  className = '',
  isRunning: propIsRunning,
  activeStep: propActiveStep,
}) => {
  const context = useTask();
  const [copiedCode, setCopiedCode] = useState<boolean>(false);
  const [isReproExpanded, setIsReproExpanded] = useState<boolean>(true);

  // 1. Resolve effective task and execution_plan
  const effectiveTask = propTask ?? context.task;
  const rawPlan =
    propExecutionPlan ??
    effectiveTask?.execution_plan ??
    effectiveTask?.plan ??
    context.plan ??
    null;

  // 2. Resolve normalized PlanData
  const planData: PlanData | null = React.useMemo(() => {
    if (!rawPlan) return null;
    if (typeof rawPlan === 'string') {
      try {
        return JSON.parse(rawPlan);
      } catch {
        return {
          target_files: [],
          logic_steps: [rawPlan],
          test_strategy: 'pytest',
        };
      }
    }
    return {
      target_files: Array.isArray(rawPlan.target_files) ? rawPlan.target_files : [],
      logic_steps: Array.isArray(rawPlan.logic_steps)
        ? rawPlan.logic_steps
        : Array.isArray(rawPlan.steps)
        ? rawPlan.steps
        : [],
      test_strategy: typeof rawPlan.test_strategy === 'string' ? rawPlan.test_strategy : 'pytest',
      reproduction_test_code:
        rawPlan.reproduction_test_code || rawPlan.repro_test_code || rawPlan.test_code || '',
    };
  }, [rawPlan]);

  // 3. Resolve Execution Lifecyle
  const isRunning = propIsRunning ?? (context.status === 'RUNNING');
  const activeStep = propActiveStep ?? context.activeStep;
  const isPlannerRunning = isRunning && (activeStep === 'planner' || !planData);
  const isCompleted = context.status === 'COMPLETED';

  // Copy reproduction test code snippet
  const handleCopyReproCode = async () => {
    const code = planData?.reproduction_test_code;
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopiedCode(true);
      setTimeout(() => setCopiedCode(false), 2000);
      context.addToast('info', 'Reproduction test code copied to clipboard.');
    } catch {
      setCopiedCode(false);
    }
  };

  // ---------------------------------------------------------------------------
  // STATE 1: PLANNER ACTIVELY FORMULATING PLAN (SKELETON / LOADER)
  // ---------------------------------------------------------------------------
  if (isPlannerRunning && !planData?.logic_steps?.length) {
    return (
      <div
        className={`flex flex-col items-center justify-center p-8 sm:p-12 text-center bg-zinc-50 border border-zinc-200/80 rounded-2xl space-y-4 animate-in fade-in duration-300 ${className}`}
      >
        <div className="relative flex items-center justify-center h-12 w-12 rounded-2xl bg-blue-100 text-blue-700">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
        <div className="space-y-1 max-w-md">
          <h3 className="text-sm font-bold text-zinc-900">
            Formulating Structured Execution Plan...
          </h3>
          <p className="text-xs text-zinc-500 leading-relaxed">
            The planner node is deconstructing your prompt, inspecting AST symbol dependencies, and selecting minimal repair targets.
          </p>
        </div>
        <div className="flex items-center gap-2 text-[11px] font-mono text-blue-700 bg-blue-50 px-3 py-1 rounded-full border border-blue-200">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500" />
          </span>
          <span>planner_node active</span>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // STATE 2: EMPTY STATE (NO TASK / AWAITING DISPATCH)
  // ---------------------------------------------------------------------------
  if (!planData || (!planData.logic_steps?.length && !planData.target_files?.length)) {
    return (
      <div
        className={`flex flex-col items-center justify-center p-8 sm:p-12 text-center bg-zinc-50 border border-zinc-200/80 rounded-2xl space-y-3 ${className}`}
      >
        <div className="flex items-center justify-center h-12 w-12 rounded-2xl bg-zinc-100 text-zinc-400 border border-zinc-200">
          <ListOrdered className="h-6 w-6" />
        </div>
        <div className="space-y-1 max-w-md">
          <h3 className="text-sm font-bold text-zinc-800">No Execution Plan Generated Yet</h3>
          <p className="text-xs text-zinc-500 leading-relaxed">
            Dispatch an autonomous task to let the planner node formulate target files, surgical logical steps, and test strategy.
          </p>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // STATE 3: PLAN AVAILABLE (STRUCTURED MULTI-STEP PLAN RENDERING)
  // ---------------------------------------------------------------------------
  const steps = planData.logic_steps || [];
  const targetFiles = planData.target_files || [];

  return (
    <div className={`space-y-5 font-sans ${className}`}>
      {/* Header Summary Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 rounded-xl bg-white border border-zinc-200 shadow-xs">
        <div className="flex items-center gap-3">
          <div className="h-9 w-9 rounded-xl bg-blue-600 text-white flex items-center justify-center font-bold shadow-xs">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div className="space-y-0.5">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-bold text-zinc-950">Structured Execution Plan</h3>
              <span className="text-[10px] uppercase font-mono px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 font-bold">
                Planner Verified
              </span>
            </div>
            <p className="text-xs text-zinc-500 font-mono">
              {steps.length} sequential logic {steps.length === 1 ? 'step' : 'steps'} • {targetFiles.length} target {targetFiles.length === 1 ? 'file' : 'files'}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 text-xs font-mono text-zinc-500 self-start sm:self-auto">
          <Clock className="h-3.5 w-3.5 text-zinc-400" />
          <span>Strategy: {planData.test_strategy || 'pytest'}</span>
        </div>
      </div>

      {/* Main Grid: Steps & Target Files */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Left Column: Multi-Step Sequential Plan */}
        <div className="lg:col-span-2 space-y-3">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-600 flex items-center gap-1.5">
              <ListOrdered className="h-4 w-4 text-zinc-500" />
              <span>Multi-Step Repair Plan</span>
            </h4>
            <span className="text-[11px] font-mono text-zinc-400">
              {isCompleted ? 'All steps resolved' : isRunning ? 'In progress' : 'Ready'}
            </span>
          </div>

          <div className="space-y-2.5">
            {steps.map((rawStep, index) => {
              const { stepNumber, title, detail } = normalizeStepText(rawStep, index);
              const isPastStep = isCompleted || (activeStep && activeStep !== 'planner' && index === 0);

              return (
                <div
                  key={index}
                  className={`flex items-start gap-3.5 p-3.5 rounded-xl border transition-all ${
                    isPastStep
                      ? 'bg-white border-zinc-200/90 shadow-xs'
                      : 'bg-zinc-50/70 border-zinc-200/80'
                  }`}
                >
                  {/* Step Number Badge */}
                  <div
                    className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg font-mono text-xs font-bold transition-all ${
                      isCompleted
                        ? 'bg-emerald-600 text-white'
                        : isRunning && activeStep !== 'planner' && index === 0
                        ? 'bg-blue-600 text-white shadow-xs'
                        : 'bg-zinc-100 text-zinc-700 border border-zinc-300'
                    }`}
                  >
                    {isCompleted ? <Check className="h-4 w-4" /> : stepNumber}
                  </div>

                  {/* Step Description */}
                  <div className="flex-1 space-y-1 min-w-0">
                    <p className="text-xs sm:text-sm font-semibold text-zinc-900 leading-snug break-words">
                      {title}
                    </p>
                    {detail && (
                      <p className="text-xs text-zinc-500 font-mono leading-relaxed mt-1">
                        {detail}
                      </p>
                    )}
                  </div>

                  {/* Status Indicator */}
                  {isCompleted ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0 mt-0.5" />
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>

        {/* Right Column: Target Files Scope & Strategy */}
        <div className="space-y-4">
          {/* Target Files Card */}
          <div className="bg-white border border-zinc-200 rounded-xl p-4 shadow-xs space-y-3">
            <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-600 flex items-center gap-1.5">
              <FileCode className="h-4 w-4 text-indigo-600" />
              <span>Target Files In Scope</span>
              <span className="ml-auto font-mono text-[10px] bg-zinc-100 text-zinc-600 px-1.5 py-0.2 rounded">
                {targetFiles.length}
              </span>
            </h4>

            {targetFiles.length > 0 ? (
              <ul className="space-y-1.5">
                {targetFiles.map((file, i) => (
                  <li
                    key={i}
                    className="flex items-center gap-2 p-2 rounded-lg bg-zinc-50 border border-zinc-200/80 font-mono text-xs text-zinc-800 break-all hover:bg-zinc-100/70 transition"
                  >
                    <Code2 className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
                    <span className="truncate">{file}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-zinc-400 italic">No specific files targeted.</p>
            )}
          </div>

          {/* Test Strategy Card */}
          <div className="bg-white border border-zinc-200 rounded-xl p-4 shadow-xs space-y-2">
            <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-600 flex items-center gap-1.5">
              <Terminal className="h-4 w-4 text-teal-600" />
              <span>Verification Strategy</span>
            </h4>
            <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 font-mono text-xs text-zinc-800 leading-relaxed">
              {planData.test_strategy || 'Execute pytest in containerized Docker test runner.'}
            </div>
          </div>
        </div>
      </div>

      {/* Optional Reproduction Test Code Section */}
      {planData.reproduction_test_code && (
        <div className="border border-zinc-200 rounded-xl overflow-hidden bg-zinc-950 shadow-xs">
          <div className="flex items-center justify-between bg-zinc-900 px-4 py-2.5 border-b border-zinc-800">
            <button
              type="button"
              onClick={() => setIsReproExpanded(!isReproExpanded)}
              className="flex items-center gap-2 text-xs font-bold text-zinc-200 hover:text-white transition"
            >
              <Terminal className="h-4 w-4 text-amber-400" />
              <span>Reproduction Test Case</span>
              {isReproExpanded ? (
                <ChevronUp className="h-3.5 w-3.5 text-zinc-400" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5 text-zinc-400" />
              )}
            </button>

            <button
              type="button"
              onClick={handleCopyReproCode}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-[11px] font-mono transition"
            >
              {copiedCode ? (
                <>
                  <Check className="h-3 w-3 text-emerald-400" />
                  <span className="text-emerald-300">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="h-3 w-3 text-zinc-400" />
                  <span>Copy Code</span>
                </>
              )}
            </button>
          </div>

          {isReproExpanded && (
            <pre className="p-4 font-mono text-xs text-zinc-200 overflow-x-auto leading-relaxed max-h-64">
              {planData.reproduction_test_code}
            </pre>
          )}
        </div>
      )}
    </div>
  );
};

export default ExecutionPlanTab;
