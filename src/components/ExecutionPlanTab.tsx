import React, { useState, useMemo } from 'react';
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
import type { TaskStateObject } from '../types/api';

export interface ExecutionPlanStep {
  step: number;
  title: string;
  description: string;
}

export interface ExecutionPlanTabProps {
  /** Optional direct execution_plan override (array or object) */
  execution_plan?: ExecutionPlanStep[] | Record<string, any> | null;
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
 * Normalizes raw step text into clean title and description.
 */
function normalizeStep(raw: any, index: number): ExecutionPlanStep {
  const stepNumber = typeof raw?.step === 'number' ? raw.step : index + 1;
  let title = '';
  let description = '';

  if (typeof raw === 'string') {
    title = raw.trim();
  } else if (raw && typeof raw === 'object') {
    title = String(raw.title || raw.name || raw.action || `Step ${stepNumber}`).trim();
    description = String(raw.description || raw.detail || raw.explanation || '').trim();
  } else {
    title = `Step ${stepNumber}`;
    description = String(raw || '');
  }

  // Strip redundant leading "Step X:" prefixes from title if already present
  const cleanTitle = title.replace(/^(step\s*\d+[:.-]?|\d+[:.-]?)\s*/i, '');

  return {
    step: stepNumber,
    title: cleanTitle ? `Step ${stepNumber}: ${cleanTitle}` : `Step ${stepNumber}`,
    description,
  };
}

/**
 * ExecutionPlanTab Component
 *
 * Consumes `task.execution_plan` directly from the global TaskContext.
 * Renders each step card with its step number, title, and description cleanly.
 * Displays a descriptive helper state ("Planner node is analyzing repository structure...")
 * if execution_plan is empty or null.
 */
const ExecutionPlanTabComponent: React.FC<ExecutionPlanTabProps> = ({
  execution_plan: propExecutionPlan,
  task: propTask,
  className = '',
  isRunning: propIsRunning,
  activeStep: propActiveStep,
}) => {
  const context = useTask();
  const [copiedCode, setCopiedCode] = useState<boolean>(false);
  const [isReproExpanded, setIsReproExpanded] = useState<boolean>(true);

  // 1. Resolve task and execution_plan from props or global TaskContext
  const effectiveTask = propTask ?? context.task ?? context.activeTask;
  const rawPlan =
    propExecutionPlan ??
    effectiveTask?.execution_plan ??
    effectiveTask?.plan ??
    context.plan ??
    null;

  // 2. Normalize execution plan into a clean array of step cards and metadata
  const { planSteps, targetFiles, testStrategy, reproductionCode } = useMemo(() => {
    if (!rawPlan) {
      return { planSteps: [], targetFiles: [], testStrategy: '', reproductionCode: '' };
    }

    let parsed = rawPlan;
    if (typeof rawPlan === 'string') {
      try {
        parsed = JSON.parse(rawPlan);
      } catch {
        return {
          planSteps: [normalizeStep(rawPlan, 0)],
          targetFiles: [],
          testStrategy: '',
          reproductionCode: '',
        };
      }
    }

    let rawList: any[] = [];
    let files: string[] = [];
    let strategy = '';
    let repro = '';

    if (Array.isArray(parsed)) {
      rawList = parsed;
    } else if (parsed && typeof parsed === 'object') {
      if (Array.isArray(parsed.steps)) {
        rawList = parsed.steps;
      } else if (Array.isArray(parsed.logic_steps)) {
        rawList = parsed.logic_steps;
      }
      if (Array.isArray(parsed.target_files)) {
        files = parsed.target_files;
      }
      if (typeof parsed.test_strategy === 'string') {
        strategy = parsed.test_strategy;
      }
      repro = parsed.reproduction_test_code || parsed.repro_test_code || parsed.test_code || '';
    }

    const steps = rawList.map((item, idx) => normalizeStep(item, idx));

    return {
      planSteps: steps,
      targetFiles: files,
      testStrategy: strategy,
      reproductionCode: repro,
    };
  }, [rawPlan]);

  // 3. Execution state resolution
  const isRunning = propIsRunning ?? (context.status === 'RUNNING');
  const activeStep = propActiveStep ?? context.activeStep;
  const isCompleted = context.status === 'COMPLETED';

  // Copy reproduction test code snippet helper
  const handleCopyReproCode = async () => {
    if (!reproductionCode) return;
    try {
      await navigator.clipboard.writeText(reproductionCode);
      setCopiedCode(true);
      setTimeout(() => setCopiedCode(false), 2000);
      context.addToast('info', 'Reproduction test code copied to clipboard.');
    } catch {
      setCopiedCode(false);
    }
  };

  // ---------------------------------------------------------------------------
  // HELPER / EMPTY STATE: Plan is empty or null
  // ---------------------------------------------------------------------------
  if (!planSteps || planSteps.length === 0) {
    return (
      <div
        className={`flex flex-col items-center justify-center p-8 sm:p-12 text-center bg-zinc-50 border border-zinc-200/80 rounded-2xl space-y-4 animate-in fade-in duration-300 ${className}`}
      >
        <div className="relative flex items-center justify-center h-12 w-12 rounded-2xl bg-blue-100 text-blue-700">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
        <div className="space-y-1 max-w-md">
          <h3 className="text-sm font-bold text-zinc-900">
            Planner node is analyzing repository structure...
          </h3>
          <p className="text-xs text-zinc-500 leading-relaxed">
            Deconstructing task prompt, inspecting AST symbol dependencies, and selecting minimal repair targets.
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
  // POPULATED STATE: Render structured multi-step plan cards cleanly
  // ---------------------------------------------------------------------------
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
              {planSteps.length} sequential logic {planSteps.length === 1 ? 'step' : 'steps'}
              {targetFiles.length > 0 && ` • ${targetFiles.length} target ${targetFiles.length === 1 ? 'file' : 'files'}`}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 text-xs font-mono text-zinc-500 self-start sm:self-auto">
          <Clock className="h-3.5 w-3.5 text-zinc-400" />
          <span>Strategy: {testStrategy || 'pytest'}</span>
        </div>
      </div>

      {/* Main Grid: Steps & Optional Target Files */}
      <div className={`grid grid-cols-1 ${targetFiles.length > 0 ? 'lg:grid-cols-3' : ''} gap-5`}>
        {/* Multi-Step Sequential Cards */}
        <div className={`${targetFiles.length > 0 ? 'lg:col-span-2' : ''} space-y-3`}>
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
            {planSteps.map((stepItem, index) => {
              const isPastStep = isCompleted || (activeStep && activeStep !== 'planner' && index === 0);

              return (
                <div
                  key={index}
                  className={`flex items-start gap-3.5 p-3.5 rounded-xl border transition-all ${
                    isPastStep
                      ? 'bg-white border-zinc-200/90 shadow-xs'
                      : 'bg-zinc-50/70 border-zinc-200/80 hover:bg-white'
                  }`}
                >
                  {/* Step Number Badge */}
                  <div
                    className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg font-mono text-xs font-bold transition-all ${
                      isCompleted
                        ? 'bg-emerald-600 text-white'
                        : isRunning && activeStep !== 'planner' && index === 0
                        ? 'bg-blue-600 text-white shadow-xs'
                        : 'bg-blue-50 text-blue-700 border border-blue-200'
                    }`}
                  >
                    {isCompleted ? <Check className="h-4 w-4" /> : stepItem.step}
                  </div>

                  {/* Step Title & Description */}
                  <div className="flex-1 space-y-1 min-w-0">
                    <p className="text-xs sm:text-sm font-semibold text-zinc-900 leading-snug break-words">
                      {stepItem.title}
                    </p>
                    {stepItem.description && (
                      <p className="text-xs text-zinc-600 font-sans leading-relaxed mt-0.5">
                        {stepItem.description}
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

        {/* Right Column: Target Files Scope & Strategy if present */}
        {targetFiles.length > 0 && (
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
            </div>

            {/* Test Strategy Card */}
            <div className="bg-white border border-zinc-200 rounded-xl p-4 shadow-xs space-y-2">
              <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-600 flex items-center gap-1.5">
                <Terminal className="h-4 w-4 text-teal-600" />
                <span>Verification Strategy</span>
              </h4>
              <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 font-mono text-xs text-zinc-800 leading-relaxed">
                {testStrategy || 'Execute pytest in containerized Docker test runner.'}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Reproduction Test Code Section if provided */}
      {reproductionCode && (
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
              {reproductionCode}
            </pre>
          )}
        </div>
      )}
    </div>
  );
};

export const ExecutionPlanTab = React.memo<ExecutionPlanTabProps>(ExecutionPlanTabComponent);
ExecutionPlanTab.displayName = 'ExecutionPlanTab';

export default ExecutionPlanTab;
