import { useState, useRef, useCallback, useEffect } from 'react';

export type AgentStatus = 'idle' | 'running' | 'awaiting_approval' | 'completed' | 'failed';

export interface LogEntry {
  id: string;
  step: string;
  message: string;
  timestamp: string;
}

export interface UseAgentStreamReturn {
  status: AgentStatus;
  currentStep: number;
  logs: LogEntry[];
  taskId: string | null;
  startRun: (overridePrompt?: string) => Promise<void>;
  approveTask: () => Promise<void>;
}

/**
 * React hook to manage autonomous agent runs via FastAPI and Redis Server-Sent Events (SSE).
 *
 * @param prompt Prompt supplied by the user for the agent task.
 */
export const useAgentStream = (
  prompt: string = ''
): UseAgentStreamReturn => {
  const [status, setStatus] = useState<AgentStatus>('idle');
  const [currentStep, setCurrentStep] = useState<number>(1);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [taskId, setTaskId] = useState<string | null>(null);

  const eventSourceRef = useRef<EventSource | null>(null);

  const closeStream = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  }, []);

  const startRun = useCallback(
    async (overridePrompt?: string) => {
      const promptText = (overridePrompt ?? prompt).trim();
      if (!promptText) {
        throw new Error('Please provide instructions for the task.');
      }

      closeStream();

      setStatus('running');
      setCurrentStep(1);
      setLogs([]);

      try {
        // 1. Initiate task on backend to obtain task ID
        const res = await fetch('http://localhost:8000/api/tasks', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            prompt: promptText,
          }),
        });

        if (!res.ok) {
          throw new Error(`Failed to create task (HTTP ${res.status}): ${res.statusText}`);
        }

        const data = await res.json();
        const id: string = data.task_id;
        setTaskId(id);

        // 2. Instantiate EventSource connecting to the task SSE stream
        const startTime = Date.now();
        const sse = new EventSource(`http://localhost:8000/api/tasks/${id}/events`);
        eventSourceRef.current = sse;

        // 3. Map incoming SSE messages to logs, currentStep, and status states
        sse.onmessage = (event: MessageEvent) => {
          try {
            if (!event.data) return;
            const payload = JSON.parse(event.data);

            // Update step
            if (typeof payload.step === 'number') {
              setCurrentStep(payload.step);
            } else if (payload.node) {
              const nodeStepMap: Record<string, number> = {
                planner_node: 1,
                investigator_node: 2,
                implementer_node: 3,
                verifier_node: 4,
                reviewer_node: 5,
                __interrupt__: 5,
              };
              if (nodeStepMap[payload.node]) {
                setCurrentStep(nodeStepMap[payload.node]);
              }
            }

            // Update status
            if (payload.status) {
              setStatus(payload.status as AgentStatus);
            }

            // Calculate timestamp string
            const elapsedSecs = Math.floor((Date.now() - startTime) / 1000);
            const formattedTimestamp = `00:${elapsedSecs < 10 ? `0${elapsedSecs}` : elapsedSecs}s`;

            // Extract message
            const messageText =
              payload.message ||
              (typeof payload.data === 'string'
                ? payload.data
                : `Executed step ${payload.step ?? 1}`);

            setLogs((prev) => [
              ...prev,
              {
                id: `log-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
                step: String(payload.step ?? 1),
                message: messageText,
                timestamp: formattedTimestamp,
              },
            ]);

            // Disconnect on completion or failure
            if (payload.status === 'completed' || payload.status === 'failed') {
              sse.close();
              eventSourceRef.current = null;
            }
          } catch (parseErr) {
            console.error('Failed to parse SSE payload:', parseErr, event.data);
          }
        };

        // Listen for heartbeat pings
        sse.addEventListener('ping', () => {
          // Heartbeat received from server; keepalive confirmed
        });

        let retryCount = 0;
        const maxRetries = 10;

        sse.onerror = (err) => {
          console.warn('SSE connection closed or disconnected:', err);
          if (status === 'awaiting_approval' || status === 'completed') {
            sse.close();
            eventSourceRef.current = null;
            return;
          }

          if (retryCount < maxRetries) {
            retryCount += 1;
            const delay = Math.min(1000 * Math.pow(1.5, retryCount - 1) + Math.random() * 500, 30000);
            console.info(`[useAgentStream] Reconnecting SSE in ${Math.round(delay)}ms (attempt ${retryCount}/${maxRetries})...`);
            setTimeout(() => {
              if (eventSourceRef.current === sse) {
                sse.close();
                const newSse = new EventSource(`http://localhost:8000/api/tasks/${id}/events`);
                eventSourceRef.current = newSse;
                newSse.onmessage = sse.onmessage;
                newSse.onerror = sse.onerror;
              }
            }, delay);
          } else {
            sse.close();
            eventSourceRef.current = null;
          }
        };
      } catch (err: unknown) {
        console.error('Error starting agent task run:', err);
        setStatus('failed');
        setLogs((prev) => [
          ...prev,
          {
            id: `log-error-${Date.now()}`,
            step: '1',
            message: `[Error] ${err instanceof Error ? err.message : String(err)}`,
            timestamp: '00:00s',
          },
        ]);
      }
    },
    [closeStream, prompt, status]
  );

  const approveTask = useCallback(async () => {
    if (!taskId) return;
    try {
      const res = await fetch(`http://localhost:8000/api/tasks/${taskId}/approve`, {
        method: 'POST',
      });
      if (res.ok) {
        setStatus('completed');
      }
    } catch (err) {
      console.error('Failed to approve task:', err);
    }
  }, [taskId]);

  // Clean up the connection on unmount
  useEffect(() => {
    return () => {
      closeStream();
    };
  }, [closeStream]);

  return {
    status,
    currentStep,
    logs,
    taskId,
    startRun,
    approveTask,
  };
};

export default useAgentStream;
