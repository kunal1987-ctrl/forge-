import React, { useState, useEffect } from 'react';
import {
  Timer,
  GitCommit,
  Zap,
  Check,
  Loader2,
  Clock,
  Terminal,
  Code,
  CheckCircle2,
  Sliders,
  XCircle,
  ArrowRight,
  ShieldCheck,
} from 'lucide-react';
import type { LogEntry } from '../hooks/useAgentStream';

interface ActivityProps {
  logs: LogEntry[];
  currentStep: number;
  onNavigate?: (tab: 'new' | 'activity' | 'review') => void;
}

const STEPS = [
  { step: 1, label: '1. Plan' },
  { step: 2, label: '2. Code' },
  { step: 3, label: '3. Write' },
  { step: 4, label: '4. Tests' },
  { step: 5, label: '5. Review' },
];

export const Activity: React.FC<ActivityProps> = ({ logs, currentStep, onNavigate }) => {
  const [elapsedSeconds, setElapsedSeconds] = useState<number>(0);
  const [showRawLogs, setShowRawLogs] = useState<boolean>(false);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsedSeconds((prev) => prev + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const formatTimer = (totalSecs: number) => {
    const mins = Math.floor(totalSecs / 60);
    const secs = totalSecs % 60;
    const pad = (n: number) => (n < 10 ? `0${n}` : `${n}`);
    return `${pad(mins)}:${pad(secs)}s`;
  };

  const handleAdjustPrompt = () => {
    setActionFeedback('Opening prompt editor...');
    setTimeout(() => {
      if (onNavigate) onNavigate('new');
      setActionFeedback(null);
    }, 600);
  };

  const handleAbort = () => {
    setActionFeedback('Pipeline cancelled');
    setTimeout(() => setActionFeedback(null), 2000);
  };

  return (
    <div className="flex flex-col w-full pb-8 space-y-4">
      {/* Top Status Header */}
      <section className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="relative flex h-2.5 w-2.5 shrink-0">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500" />
            </span>
            <h1 className="text-base font-semibold text-zinc-900 truncate">
              Autonomous Run in Progress
            </h1>
          </div>
          <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-zinc-100 text-zinc-700 shrink-0 border border-zinc-200">
            <Timer className="h-3.5 w-3.5 text-zinc-500" />
            <span className="font-mono text-xs font-medium" id="elapsed-counter">
              {formatTimer(elapsedSeconds)}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2 mt-2.5 text-zinc-500 font-mono text-[11px] flex-wrap">
          <span className="inline-flex items-center gap-1 text-zinc-800 font-medium">
            <GitCommit className="h-3.5 w-3.5 text-zinc-500" />
            hyper/telemetry:main
          </span>
          <span className="text-zinc-300">·</span>
          <span>Task #842</span>
          <span className="text-zinc-300">·</span>
          <span className="inline-flex items-center gap-1 text-emerald-600 font-medium">
            <Zap className="h-3 w-3 fill-emerald-600" />
            Agent v2.4
          </span>
        </div>
      </section>

      {/* Progress Stepper */}
      <section className="bg-white rounded-xl p-3.5 border border-zinc-200/80 shadow-sm">
        <div className="flex items-center justify-between text-center relative">
          {STEPS.map((s, index) => {
            const isCompleted = s.step < currentStep;
            const isActive = s.step === currentStep;
            const isUpcoming = s.step > currentStep;

            return (
              <React.Fragment key={s.step}>
                <div className="flex flex-col items-center flex-1 relative z-10">
                  <div
                    className={`w-6 h-6 rounded-full flex items-center justify-center mb-1.5 transition-colors ${
                      isCompleted
                        ? 'bg-emerald-100 text-emerald-700'
                        : isActive
                        ? 'bg-emerald-600 text-white shadow-sm ring-4 ring-emerald-100'
                        : 'bg-zinc-100 text-zinc-400 border border-zinc-200'
                    }`}
                  >
                    {isCompleted && <Check className="h-3.5 w-3.5 stroke-[3]" />}
                    {isActive && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    {isUpcoming && <Clock className="h-3.5 w-3.5" />}
                  </div>
                  <span
                    className={`text-[11px] truncate w-full px-0.5 ${
                      isActive
                        ? 'text-zinc-900 font-bold'
                        : isCompleted
                        ? 'text-zinc-700 font-medium'
                        : 'text-zinc-400 font-medium'
                    }`}
                  >
                    {s.label}
                  </span>
                </div>
                {index < STEPS.length - 1 && (
                  <div
                    className={`h-0.5 flex-1 -mt-4 transition-colors ${
                      s.step < currentStep ? 'bg-emerald-500' : 'bg-zinc-200'
                    }`}
                  />
                )}
              </React.Fragment>
            );
          })}
        </div>
      </section>

      {/* Activity Feed / Live Execution Log */}
      <section className="bg-white rounded-xl border border-zinc-200/80 shadow-sm overflow-hidden">
        {/* Card Header */}
        <div className="flex items-center justify-between px-4 py-3 bg-zinc-50 border-b border-zinc-200/80">
          <div className="flex items-center gap-2">
            <Terminal className="h-4 w-4 text-zinc-800" />
            <span className="text-xs text-zinc-900 uppercase tracking-wider font-semibold font-mono">
              Execution Log
            </span>
            <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 font-mono text-[10px] border border-emerald-200/60">
              Live stream
            </span>
          </div>

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
        </div>

        {/* Feed Entries */}
        <div className="p-4 space-y-3.5">
          {logs.length === 0 ? (
            <div className="py-6 text-center text-xs font-mono text-zinc-400 flex items-center justify-center gap-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-400" />
              <span>Initializing agent telemetry feed...</span>
            </div>
          ) : (
            logs.map((log) => {
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
            })
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

      {/* Next Step Staging Card */}
      <section className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="w-8 h-8 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center shrink-0 border border-emerald-200">
            <CheckCircle2 className="h-4 w-4" />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold text-zinc-900">Patch Generated &amp; Verified</h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              Automated test suite passed. Synthesis complete. Ready for human review and atomic merge.
            </p>
          </div>
        </div>

        {/* Metrics Bento Preview */}
        <div className="grid grid-cols-3 gap-2 mt-3.5">
          <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 text-center">
            <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-wider block">
              Diff Size
            </span>
            <span className="font-mono text-xs font-semibold text-emerald-600 mt-0.5 block">
              +12 / -4
            </span>
          </div>
          <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 text-center">
            <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-wider block">
              Latency Impact
            </span>
            <span className="font-mono text-xs font-semibold text-emerald-600 mt-0.5 block">
              -18ms
            </span>
          </div>
          <div className="p-2.5 rounded-lg bg-zinc-50 border border-zinc-200/80 text-center">
            <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-wider block">
              Confidence
            </span>
            <span className="font-mono text-xs font-semibold text-zinc-900 mt-0.5 block">
              99.4%
            </span>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex flex-col gap-2 mt-4">
          <button
            type="button"
            onClick={() => {
              if (onNavigate) onNavigate('review');
            }}
            className="w-full h-10 flex items-center justify-center gap-2 rounded-xl bg-zinc-900 text-white font-medium text-xs hover:bg-zinc-800 active:scale-[0.99] transition-all shadow-sm"
          >
            <span>Inspect Diff &amp; Approve</span>
            <ArrowRight className="h-4 w-4" />
          </button>

          <div className="flex items-center justify-between px-1">
            <button
              type="button"
              onClick={handleAdjustPrompt}
              className="inline-flex items-center gap-1.5 text-zinc-500 hover:text-zinc-900 text-xs transition-colors py-1"
            >
              <Sliders className="h-3.5 w-3.5" />
              <span>Adjust Prompt</span>
            </button>
            <button
              type="button"
              onClick={handleAbort}
              className="inline-flex items-center gap-1.5 text-rose-600 hover:text-rose-700 text-xs transition-colors py-1"
            >
              <XCircle className="h-3.5 w-3.5" />
              <span>Abort Pipeline</span>
            </button>
          </div>

          {actionFeedback && (
            <div className="text-center text-xs font-mono text-zinc-600 pt-1">
              {actionFeedback}
            </div>
          )}
        </div>
      </section>
    </div>
  );
};