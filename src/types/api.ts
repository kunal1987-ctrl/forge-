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
  | 'failed';

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
}

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
  repo_id: string;
  query: string;
  limit?: number;
}

export interface RAGSearchDependency {
  symbol: string;
  file_path: string;
  code: string;
}

export interface RAGSearchResultItem {
  file_path: string;
  symbol_name: string;
  symbol_type?: string;
  score: number;
  content: string;
  enclosing_context?: string;
  dependencies?: RAGSearchDependency[];
}
