import React from 'react';
import { TaskMetrics, type TaskMetricsProps } from './TaskMetrics';

export type MetricsCardProps = TaskMetricsProps;

/**
 * MetricsCard Component
 * Backward-compatible wrapper and alias for TaskMetrics.
 */
export const MetricsCard: React.FC<MetricsCardProps> = (props) => {
  return <TaskMetrics {...props} />;
};

export { TaskMetrics };
export default MetricsCard;
