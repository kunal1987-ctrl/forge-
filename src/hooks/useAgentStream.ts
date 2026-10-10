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
  startRun: (customPrompt?: string) => Promise<void>;
  approveTask: () => Promise<void>;
}

export const useAgentStream = (): UseAgentStreamReturn => {
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
    async (customPrompt?: string) => {
      closeStream();

      setStatus('running');
      setCurrentStep(1);
      setLogs([]);

      const promptText =
        customPrompt ||
        'Fix race condition in async telemetry dispatcher when socket drops during handshake. Add exponential backoff retry buffer and unit tests.';

      try {
        // 1. Create task via FastAPI backend
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
        const createdTaskId: string = data.task_id;
        setTaskId(createdTaskId);

        // 2. Connect to Server-Sent Events stream
        const startTime = Date.now();
        const eventSource = new EventSource(
          `http://localhost:8000/api/tasks/${createdTaskId}/events`
        );
        eventSourceRef.current = eventSource;

        eventSource.onmessage = (event: MessageEvent) => {
          try {
            if (!event.data) return;
            const payload = JSON.parse(event.data);

            if (typeof payload.step === 'number') {
              setCurrentStep(payload.step);
            }

            if (payload.status) {
              setStatus(payload.status as AgentStatus);
            }

            const elapsedSecs = Math.floor((Date.now() - startTime) / 1000);
            const paddedSecs = elapsedSecs < 10 ? `0${elapsedSecs}` : `${elapsedSecs}`;
            const formattedTimestamp = `00:${paddedSecs}s`;

            setLogs((prev) => [
              ...prev,
              {
                id: `log-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
                step: String(payload.step ?? 1),
                message: payload.message ?? '',
                timestamp: formattedTimestamp,
              },
            ]);

            // If task finished terminal state, disconnect stream
            if (payload.status === 'completed' || payload.status === 'failed') {
              eventSource.close();
            }
          } catch (parseErr) {
            console.error('Failed to parse SSE payload:', parseErr, event.data);
          }
        };

        eventSource.onerror = (err) => {
          console.warn('SSE connection closed or disconnected:', err);
          // If task reached terminal or awaiting review state, close
          if (status === 'awaiting_approval' || status === 'completed') {
            eventSource.close();
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
    [closeStream, status]
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
