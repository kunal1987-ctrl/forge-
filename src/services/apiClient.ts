import type {
  AgentEventPayload,
  RAGSearchRequest,
  RAGSearchResultItem,
  RepoIngestRequest,
  RepoIngestResponse,
  ReviewActionRequest,
  TaskCreateRequest,
  TaskCreateResponse,
  TaskStatusResponse,
} from '../types/api';

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000/api';

export class ApiError extends Error {
  status: number;
  details?: unknown;

  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

/**
 * Perform fetch with a strict timeout using AbortController to prevent indefinite loading states.
 */
async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeoutMs = 15000
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return res;
  } catch (err: unknown) {
    if (err instanceof Error && err.name === 'AbortError') {
      throw new ApiError(
        `Request to ${url} timed out after ${Math.round(timeoutMs / 1000)}s. Please check if backend service is running.`,
        408
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Handle FastAPI HTTP responses and extract detailed error messages.
 */
async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let errorMessage = `HTTP error ${res.status}: ${res.statusText}`;
    let errorDetails: unknown = null;
    try {
      const data = await res.json();
      errorDetails = data;
      if (typeof data.detail === 'string') {
        errorMessage = data.detail;
      } else if (Array.isArray(data.detail)) {
        errorMessage = data.detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join('; ');
      } else if (data.message) {
        errorMessage = data.message;
      }
    } catch {
      // response is not JSON
    }
    throw new ApiError(errorMessage, res.status, errorDetails);
  }

  // Handle 204 No Content
  if (res.status === 204) {
    return {} as T;
  }

  return (await res.json()) as T;
}

export const apiClient = {
  /**
   * Clone repository, execute Tree-sitter AST parsing, compute embeddings and ingest into Postgres.
   * 40s timeout for repository indexing.
   */
  async ingestRepository(payload: RepoIngestRequest): Promise<RepoIngestResponse> {
    const res = await fetchWithTimeout(`${API_BASE}/repos/ingest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }, 40000);
    return handleResponse<RepoIngestResponse>(res);
  },

  /**
   * Create an autonomous task and dispatch background LangGraph orchestration.
   * 15s timeout.
   */
  async createTask(payload: TaskCreateRequest): Promise<TaskCreateResponse> {
    const res = await fetchWithTimeout(`${API_BASE}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }, 15000);
    return handleResponse<TaskCreateResponse>(res);
  },

  /**
   * Fetch normalized real execution status and verified PR flags for a task.
   * Status values: 'IDLE', 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED'.
   */
  async getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
    const res = await fetchWithTimeout(
      `${API_BASE}/tasks/${encodeURIComponent(taskId)}`,
      {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
      },
      10000
    );
    return handleResponse<TaskStatusResponse>(res);
  },

  /**
   * Resume paused LangGraph execution with human decision (approve, reject, or cancel).
   */
  async submitReviewDecision(
    taskId: string,
    payload: ReviewActionRequest
  ): Promise<{ status: string }> {
    if (payload.decision === 'reject') {
      const res = await fetchWithTimeout(`${API_BASE}/tasks/${taskId}/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feedback: payload.feedback || '' }),
      }, 15000);
      return handleResponse<{ status: string }>(res);
    }

    const res = await fetchWithTimeout(`${API_BASE}/tasks/${taskId}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }, 15000);
    return handleResponse<{ status: string }>(res);
  },

  /**
   * Search codebase using hybrid reciprocal rank fusion (RRF) and FlashRank reranking.
   */
  async searchCodebase(payload: RAGSearchRequest): Promise<RAGSearchResultItem[]> {
    const res = await fetchWithTimeout(`${API_BASE}/retrieval/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }, 15000);
    return handleResponse<RAGSearchResultItem[]>(res);
  },

  /**
   * Cancel task execution, abort the LangGraph loop, and cleanup worktree branch.
   * 8s fast timeout.
   */
  async cancelTask(taskId: string): Promise<void> {
    const res = await fetchWithTimeout(`${API_BASE}/tasks/${taskId}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }, 8000);
    await handleResponse<{ status: string }>(res);
  },

  /**
   * Open Server-Sent Events (SSE) connection to live task events channel.
   * Returns an unsubscribe function to safely close the connection.
   */
  subscribeToTaskEvents(
    taskId: string,
    onEvent: (data: AgentEventPayload) => void,
    onError?: (error: Event) => void
  ): () => void {
    const url = `${API_BASE}/tasks/${taskId}/events`;
    const eventSource = new EventSource(url);

    eventSource.onmessage = (event) => {
      if (!event.data) return;
      try {
        const parsed = JSON.parse(event.data) as AgentEventPayload;
        onEvent(parsed);
      } catch (err) {
        console.error('Failed to parse SSE task event payload:', err, event.data);
      }
    };

    eventSource.onerror = (error) => {
      console.warn(`SSE connection warning for task ${taskId}:`, error);
      if (onError) {
        onError(error);
      }
    };

    return () => {
      eventSource.close();
    };
  },
};
