import React from 'react';
import { Check, Clock, Loader2, XCircle } from 'lucide-react';
import type { BackendTaskStatus, TaskStatusResponse } from '../types/api';

export interface WorkflowStepDef {
  key: string;
  aliases: string[];
  label: string;
  stepNumber: number;
}

const WORKFLOW_STEPS: WorkflowStepDef[] = [
  { key: 'planner', aliases: ['plan', 'planner', 'planner_node'], label: '1. Plan', stepNumber: 1 },
  { key: 'repro_gate', aliases: ['repro_gate', 'repro', 'repro_gate_node'], label: '2. Repro Gate', stepNumber: 2 },
  { key: 'investigator', aliases: ['investigate', 'investigator', 'investigator_node'], label: '3. Investigator', stepNumber: 3 },
  { key: 'implementer', aliases: ['implement', 'implementer', 'implementer_node'], label: '4. Implementer', stepNumber: 4 },
  { key: 'verifier', aliases: ['verify', 'verifier', 'verifier_node'], label: '5. Verifier', stepNumber: 5 },
  { key: 'reviewer', aliases: ['review', 'reviewer', 'reviewer_node', 'awaiting_approval', '__interrupt__'], label: '6. Reviewer', stepNumber: 6 },
];

export interface WorkflowStepsProps {
  task?: TaskStatusResponse | null;
  taskStatus?: BackendTaskStatus | string | null;
  currentNode?: string | null;
  completedNodes?: string[];
  currentStep?: number;
  className?: string;
}

export const WorkflowSteps: React.FC<WorkflowStepsProps> = ({
  task,
  taskStatus,
  currentNode,
  completedNodes,
  currentStep,
  className = '',
}) => {
  // Normalize task status: defaults to IDLE when null or missing
  const normalizedStatus = (task?.status || taskStatus || 'IDLE').toUpperCase();
  const isIdle = !task && !taskStatus ? true : normalizedStatus === 'IDLE' || normalizedStatus === 'PENDING';
  const isCompletedTask = normalizedStatus === 'COMPLETED';
  const isFailedTask = normalizedStatus === 'FAILED';
  const isRunningTask = normalizedStatus === 'RUNNING';

  const rawCompletedNodes = task?.completed_nodes || completedNodes || [];
  const completedNodeSet = new Set(
    rawCompletedNodes.map((n) => n.replace('_node', '').trim().toLowerCase())
  );

  const rawCurrentNode = task?.current_node || currentNode || null;
  const activeNodeKey = rawCurrentNode
    ? rawCurrentNode.replace('_node', '').trim().toLowerCase()
    : null;

  return (
    <section className={`bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm ${className}`}>
      <div className="flex items-center justify-between text-center relative">
        {WORKFLOW_STEPS.map((stepDef, index) => {
          let stepState: 'inactive' | 'active' | 'completed' | 'failed' = 'inactive';

          if (isIdle) {
            // Rule 1: IDLE or no task -> Render all steps in Inactive/Pending state (gray icons, no spinning animations)
            stepState = 'inactive';
          } else if (isCompletedTask) {
            // Completed task -> All workflow steps completed with static green checkmarks
            stepState = 'completed';
          } else if (isRunningTask) {
            // Rule 2: RUNNING -> Active step spins; completed steps have checkmarks; subsequent wait
            const isStepCompleted =
              (rawCompletedNodes.includes(stepDef.key) ||
                stepDef.aliases.some((a) =>
                  rawCompletedNodes.includes(a) || completedNodeSet.has(a.toLowerCase())
                )) &&
              activeNodeKey !== stepDef.key;

            const isStepActive = activeNodeKey
              ? stepDef.aliases.some((a) => a.toLowerCase() === activeNodeKey)
              : currentStep === stepDef.stepNumber;

            if (isStepCompleted) {
              stepState = 'completed';
            } else if (isStepActive) {
              stepState = 'active';
            } else {
              stepState = 'inactive';
            }
          } else if (isFailedTask) {
            const isStepCompleted =
              rawCompletedNodes.includes(stepDef.key) ||
              stepDef.aliases.some((a) =>
                rawCompletedNodes.includes(a) || completedNodeSet.has(a.toLowerCase())
              );
            const isStepFailed = activeNodeKey
              ? stepDef.aliases.some((a) => a.toLowerCase() === activeNodeKey)
              : false;

            if (isStepFailed) {
              stepState = 'failed';
            } else if (isStepCompleted) {
              stepState = 'completed';
            } else {
              stepState = 'inactive';
            }
          }

          return (
            <React.Fragment key={stepDef.key}>
              <div className="flex flex-col items-center flex-1 relative z-10">
                <div
                  className={`w-7 h-7 rounded-full flex items-center justify-center mb-1.5 transition-colors ${
                    stepState === 'completed'
                      ? 'bg-emerald-100 text-emerald-700'
                      : stepState === 'active'
                      ? 'bg-emerald-600 text-white shadow-sm ring-4 ring-emerald-100'
                      : stepState === 'failed'
                      ? 'bg-rose-100 text-rose-700 border border-rose-300'
                      : 'bg-zinc-100 text-zinc-400 border border-zinc-200'
                  }`}
                >
                  {stepState === 'completed' && <Check className="h-3.5 w-3.5 stroke-[3]" />}
                  {stepState === 'active' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  {stepState === 'failed' && <XCircle className="h-3.5 w-3.5 text-rose-600" />}
                  {stepState === 'inactive' && <Clock className="h-3.5 w-3.5" />}
                </div>

                <span
                  className={`text-[11px] truncate w-full px-0.5 ${
                    stepState === 'active'
                      ? 'text-zinc-900 font-bold'
                      : stepState === 'completed'
                      ? 'text-zinc-700 font-medium'
                      : stepState === 'failed'
                      ? 'text-rose-700 font-medium'
                      : 'text-zinc-400 font-medium'
                  }`}
                >
                  {stepDef.label}
                </span>
              </div>

              {index < WORKFLOW_STEPS.length - 1 && (
                <div
                  className={`h-0.5 flex-1 -mt-4 transition-colors ${
                    stepState === 'completed' ? 'bg-emerald-500' : 'bg-zinc-200'
                  }`}
                />
              )}
            </React.Fragment>
          );
        })}
      </div>
    </section>
  );
};

export default WorkflowSteps;
