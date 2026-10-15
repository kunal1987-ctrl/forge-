/**
 * Strict TypeScript API Contracts matching FORGE FastAPI & LangGraph backend.
 */

export type ExecutionMode = 'fast' | 'deep';

export type TaskStatus =
  | 'idle'
  | 'planning'
  | 'investigating'
  | 'implementing'
  | 'verifying'
  | 'awaiting_approval'
  | 'pr_created'
  | 'cleaned_up'
  | 'failed'
  | 'aborted';

export interface TaskCreateRequest {
  repo_id: string;
  prompt: string;
  mode: ExecutionMode;
}

export interface TaskCreateResponse {
  task_id: string;
  status: TaskStatus;
  repo_id?: string | null;
  mode?: ExecutionMode;
}

export interface ReviewActionRequest {
  decision: 'approve' | 'reject' | 'cancel';
  feedback?: string;
}

export interface PlanData {
  target_files: string[];
  logic_steps: string[];
  test_strategy: string;
  reproduction_test_code?: string;
  steps?: (string | Record<string, any>)[];
  [key: string]: any;
}

export interface ExecutionPlanStepData {
  step: number;
  title: string;
  description: string;
}

export type ExecutionPlanData = PlanData | ExecutionPlanStepData[];

export interface CodeContextChunk {
  file_path: string;
  symbol_name: string;
  content: string;
}

export interface TestResultsData {
  exit_code: number;
  passed_count: number;
  failed_count: number;
  raw_output: string;
  stdout?: string;
  stderr?: string;
}

export interface AgentEventPayload {
  node: string;
  status: TaskStatus;
  plan?: PlanData;
  execution_plan?: ExecutionPlanData;
  context_retrieved?: CodeContextChunk[];
  generated_diff?: string;
  test_results?: TestResultsData;
  retry_count: number;
  pr_url?: string;
  error?: string;
  timestamp: string;
  message?: string;
  step?: number;
  data?: Record<string, unknown>;
}

export interface RepoIngestRequest {
  repo_url: string;
  branch?: string;
}

export interface RepoIngestResponse {
  repo_id: string;
  total_chunks: number;
  status?: string;
}

export interface RAGSearchRequest {
  repo_id?: string;
  query: string;
  limit?: number;
}

export interface RAGSearchDependency {
  symbol: string;
  file_path: string;
  code?: string;
}

export interface RAGSearchResultItem {
  file_path: string;
  symbol_name: string;
  snippet?: string;
  start_line?: number;
  symbol_type?: string;
  score: number;
  content: string;
  enclosing_context?: string;
  dependencies?: RAGSearchDependency[];
}

/**
 * Flat Tree-sitter AST symbol projection persisted to `code_chunks` during ingestion.
 * Powers the Ingested Symbols viewer.
 */
export interface ASTSymbol {
  file_path: string;
  symbol_name: string;
  symbol_type: string;
  /** 1-indexed line where the definition begins. Null when the revision predates line capture. */
  line_number: number | null;
  parent_symbol?: string | null;
  end_line?: number | null;
  content?: string;
}

export interface ASTSymbolsSummary {
  total_files_parsed: number;
  total_nodes_indexed: number;
  /** Optional server-side breakdown, e.g. { class: 42, function: 318, method: 96 }. */
  by_type?: Record<string, number> | null;
}

export interface ASTSymbolsIndexResponse {
  repo_id: string;
  commit_sha?: string | null;
  summary?: ASTSymbolsSummary | null;
  symbols: ASTSymbol[];
}

export type BackendTaskStatus = 'IDLE' | 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'ABORTED';

export interface DiffSizeMetrics {
  added: number;
  deleted: number;
  insertions?: number;
  deletions?: number;
}

export interface DiffStatsMetrics {
  lines_added?: number;
  lines_deleted?: number;
  files_changed?: number;
  files_list?: string[];
  formatted_summary?: string;
  total_lines_changed?: number;
  description?: string;
}

export interface LatencyDetailsMetrics {
  duration_delta_seconds?: number;
  duration_delta_ms?: number;
  percentage_change?: number;
  estimated_overhead_ms?: number;
  is_regression?: boolean;
  description?: string;
}

export interface TaskMetricsData {
  diff_size?: DiffSizeMetrics | string | null;
  diff_stats?: DiffStatsMetrics | Record<string, any> | null;
  lines_added?: number | null;
  lines_deleted?: number | null;
  files_changed?: number | null;
  files_list?: string[] | null;
  formatted_diff?: string | null;
  diff_summary?: string | null;
  confidence?: number | null;
  confidence_score?: number | null;
  latency_impact?: number | null;
  latency_impact_ms?: number | null;
  latency_details?: LatencyDetailsMetrics | Record<string, any> | null;
  retry_count?: number | null;
  tests_passed?: number | null;
  tests_failed?: number | null;
  test_exit_code?: number | null;
  computed_at?: string | null;
  [key: string]: any;
}

export interface TaskStatusResponse {
  task_id: string;
  status: BackendTaskStatus;
  repo_id?: string | null;
  prompt?: string | null;
  branch_name?: string | null;
  pr_created: boolean;
  pr_url?: string | null;
  merged: boolean;
  retry_count: number;
  created_at?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  duration_seconds?: number | null;
  error?: string | null;
  current_node?: string | null;
  active_node?: string | null;
  activeNode?: string | null;
  completed_nodes?: string[];
  completedNodes?: string[];
  execution_plan?: ExecutionPlanData | null;
  plan?: PlanData | null;
  confidence_score?: number | null;
  diff_stats?: DiffStatsMetrics | Record<string, any> | null;
  latency_impact?: number | null;
  metrics?: TaskMetricsData | Record<string, any> | null;
  patch?: string | null;
  generated_diff?: string | null;
  test_suite_passed?: number | null;
  target_scope?: string | null;
  pr_target?: string | null;
}

export interface TaskStateObject {
  id?: string | null;
  taskId?: string | null;
  status?: BackendTaskStatus | string | null;
  metrics?: TaskMetricsData | null;
  pr_url?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  duration_seconds?: number | null;
  active_node?: string | null;
  activeNode?: string | null;
  current_node?: string | null;
  completed_nodes?: string[];
  completedNodes?: string[];
  execution_plan?: ExecutionPlanData | null;
  plan?: PlanData | null;
  currentDiff?: string | null;
  patch?: string | null;
  generated_diff?: string | null;
  test_suite_passed?: number | null;
  retry_count?: number | null;
  target_scope?: string | null;
  pr_target?: string | null;
  repo?: any;
  [key: string]: any;
}

export interface EngineStatusResponse {
  is_server_connected: boolean;
  repo_connected: boolean;
  task_active: boolean;
  active_tasks_count?: number;
  repo_id?: string | null;
}

export interface TaskRetryPRResponse {
  task_id: string;
  status: string;
  pr_url?: string | null;
  branch_name?: string | null;
  message?: string | null;
  error?: string | null;
}

export interface TaskAbortResponse {
  task_id: string;
  status: string;
  message: string;
  killed_processes?: number;
  killed_containers?: number;
}

export interface TaskHistoryItem {
  task_id: string;
  title: string;
  repo_url: string;
  status: 'MERGED' | 'IN_REVIEW' | 'FAILED' | 'RUNNING' | string;
  pr_url?: string | null;
  created_at: string;
  start_time?: string | null;
  end_time?: string | null;
  duration_seconds?: number | null;
  relative_time?: string | null;
  branch_name?: string | null;
  retry_count?: number;
}
