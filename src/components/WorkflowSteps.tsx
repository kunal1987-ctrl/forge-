import React from 'react';
import { WorkflowStepper, type WorkflowStepperProps } from './WorkflowStepper';

export type WorkflowStepsProps = WorkflowStepperProps;

export const WorkflowSteps: React.FC<WorkflowStepsProps> = (props) => {
  return <WorkflowStepper {...props} />;
};

export { WorkflowStepper };
export default WorkflowSteps;
