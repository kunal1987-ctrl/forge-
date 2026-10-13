import React, { useState } from 'react';
import {
  Terminal,
  Code,
  Loader2,
  Check,
  ShieldCheck,
  Radio,
} from 'lucide-react';
import type { LogEntry } from '../hooks/useAgentStream';
import type { BackendTaskStatus, TaskStatusResponse } from '../types/api';

export interface TelemetryFeedProps {
  task?: TaskStatusResponse | null;
  taskStatus?: BackendTaskStatus | string | null;
  logs?: LogEntry[];
  isStreaming?: boolean;
  className?: string;
}

export const TelemetryFeed: React.FC<TelemetryFeedProps> = ({
  task,
  taskStatus,
  logs = [],
  isStreaming = false,
  className = '',
}) => {
  const [showRawLogs, setShowRawLogs] = useState<boolean>(false);

  // Normalize execution status
  const normalizedStatus = (task?.status || taskStatus || 'IDLE').toUpperCase();
  const isRunning = normalizedStatus === 'RUNNING' || isStreaming;
  const hasLogs = logs.length > 0;

  return (
    <section className={`bg-white rounded-xl border border-zinc-200/80 shadow-sm overflow-hidden ${className}`}>
      {/* Feed Card Header */}
      <div className="flex items-center justify-between px-4 py-3 bg-zinc-50 border-b border-zinc-200/80">
        <div className="flex items-center gap-2">
          <Terminal className="h-4 w-4 text-zinc-800" />
          <span className="text-xs text-zinc-900 uppercase tracking-wider font-semibold font-mono">
            Execution Log
          </span>

          {isRunning ? (
            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 font-mono text-[10px] border border-emerald-200/60">
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
              </span>
              Live stream
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-zinc-100 text-zinc-500 font-mono text-[10px] border border-zinc-200">
              Idle
            </span>
          )}
        </div>

        {hasLogs && (
          <button
            type="button"
            onClick={() => setShowRawLogs(!showRawLogs)}
            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-mono transition-colors border ${
              showRawLogs
                ? 'bg-zinc-200 text-zinc-900 border-zinc-300'
                : 'bg-white hover:bg-zinc-100 text-zinc-600 border-zinc-200'
            }`}
          >
            <Code className="h-3.5 w-3.5" />
            <span>{showRawLogs ? 'Hide Raw' : 'Raw Logs'}</span>
          </button>
        )}
      </div>

      {/* Feed Body Content */}
      <div className="p-4 space-y-3.5">
        {/* State A: Idle or non-running with no active execution logs */}
        {!isRunning && !hasLogs && (
          <div
            id="telemetry-idle-placeholder"
            className="py-12 px-4 text-center flex flex-col items-center justify-center gap-3"
          >
            <div className="w-10 h-10 rounded-full bg-zinc-100 flex items-center justify-center text-zinc-400 border border-zinc-200/80">
              <Radio className="h-4 w-4" />
            </div>
            <p className="text-xs font-mono text-zinc-500 font-medium max-w-sm leading-relaxed">
              System Idle. Ingest a repository to begin execution.
            </p>
          </div>
        )}

        {/* State B: Actively running task waiting for initial event payload */}
        {isRunning && !hasLogs && (
          <div
            id="telemetry-initializing"
            className="py-10 text-center text-xs font-mono text-zinc-500 flex items-center justify-center gap-2"
          >
            <Loader2 className="h-4 w-4 animate-spin text-emerald-600" />
            <span>Initializing agent telemetry feed...</span>
          </div>
        )}

        {/* State C: Log events stream actively receiving entries */}
        {hasLogs && (
          <div className="space-y-3.5">
            {logs.map((log) => {
              const tagMatch = log.message.match(/^(\[[^\]]+\])\s*(.*)$/);
              const tag = tagMatch ? tagMatch[1] : `[Step ${log.step}]`;
              const text = tagMatch ? tagMatch[2] : log.message;

              const isCode = log.message.includes('[Code]');
              const isTests = log.message.includes('[Tests]');
              const isPlan = log.message.includes('[Plan]');
              const isInvestigate = log.message.includes('[Investigate]');

              return (
                <div key={log.id} className="flex items-start gap-3">
                  <div className="w-5 h-5 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center shrink-0 mt-0.5">
                    <Check className="h-3 w-3 stroke-[3]" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-baseline justify-between gap-2">
                      <p className="text-xs text-zinc-900 leading-relaxed">
                        <span className="font-mono text-[10px] text-emerald-700 bg-emerald-50 border border-emerald-200/60 px-1.5 py-0.5 rounded font-medium mr-1.5">
                          {tag}
                        </span>
                        {text}
                      </p>
                      <span className="font-mono text-[11px] text-zinc-400 shrink-0">
                        {log.timestamp}
                      </span>
                    </div>

                    {isPlan && (
                      <p className="font-mono text-[11px] text-zinc-500 mt-0.5 truncate">
                        → dispatch.py, telemetry/stream_buffer.py
                      </p>
                    )}

                    {isInvestigate && (
                      <p className="font-mono text-[11px] text-zinc-500 mt-0.5">
                        Buffer lock unlocked prematurely during asynchronous context switch.
                      </p>
                    )}

                    {isCode && (
                      <div className="mt-2 p-2.5 rounded-lg bg-zinc-900 font-mono text-xs space-y-1 overflow-x-auto shadow-inner">
                        <div className="text-rose-400 font-medium">
                          - async def flush_all(): lock.release()
                        </div>
                        <div className="text-emerald-400 font-medium">
                          + async with self._flush_lock: await self._safe_flush()
                        </div>
                      </div>
                    )}

                    {isTests && (
                      <div className="mt-2 p-2 rounded-lg bg-zinc-50 border border-zinc-200/80">
                        <div className="flex items-center gap-2.5">
                          <div className="flex-1 h-2 bg-zinc-200 rounded-full overflow-hidden">
                            <div className="h-full bg-emerald-500 w-full transition-all duration-500" />
                          </div>
                          <span className="font-mono text-xs font-semibold text-emerald-700">
                            14/14
                          </span>
                        </div>
                        <p className="font-mono text-[11px] text-zinc-500 mt-2 flex items-center gap-1.5">
                          <ShieldCheck className="h-3.5 w-3.5 text-emerald-600 shrink-0" />
                          <span>
                            0 regressions detected across{' '}
                            <span className="text-zinc-800 font-medium">test_dispatcher.py</span>
                          </span>
                        </p>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Collapsible Raw CLI Snippet */}
        {showRawLogs && (
          <div className="rounded-lg bg-zinc-950 text-zinc-200 p-3.5 font-mono text-xs space-y-1.5 shadow-inner border border-zinc-800">
            <div className="flex items-center justify-between text-zinc-400 text-[10px] border-b border-zinc-800 pb-1 mb-1">
              <span>CONTAINER: forge-agent-sandbox-01</span>
              <span>STDOUT</span>
            </div>
            <div className="text-emerald-400">pytest -v tests/test_dispatcher.py --asyncio-mode=auto</div>
            <div className="text-zinc-500">
              ============================= test session starts ==============================
            </div>
            <div className="text-zinc-300">
              tests/test_dispatcher.py::test_concurrent_flushes{' '}
              <span className="text-emerald-400 font-bold">PASSED [ 50%]</span>
            </div>
            <div className="text-zinc-300">
              tests/test_dispatcher.py::test_overflow_pressure{' '}
              <span className="text-emerald-400 font-bold">PASSED [100%]</span>
            </div>
            <div className="text-emerald-300 font-medium">== 14 passed in 0.42s ==</div>
          </div>
        )}
      </div>
    </section>
  );
};

export default TelemetryFeed;
