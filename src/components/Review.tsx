import React, { useState } from 'react';
import {
  CheckCircle2,
  GitPullRequest,
  Bot,
  Lock,
  RotateCcw,
  ShieldCheck,
  Check,
  FileText,
  Loader2,
  CheckCheck,
  MessageSquare,
} from 'lucide-react';

interface ReviewProps {
  onNavigate?: (tab: 'new' | 'activity' | 'review') => void;
}

export const Review: React.FC<ReviewProps> = ({ onNavigate }) => {
  const [approveState, setApproveState] = useState<'idle' | 'creating' | 'merged'>('idle');
  const [requestState, setRequestState] = useState<'idle' | 'sent'>('idle');

  const handleApprove = () => {
    if (approveState !== 'idle') return;
    setApproveState('creating');

    setTimeout(() => {
      setApproveState('merged');
    }, 900);
  };

  const handleRequestChanges = () => {
    if (requestState !== 'idle') return;
    setRequestState('sent');

    setTimeout(() => {
      setRequestState('idle');
      if (onNavigate) {
        onNavigate('new');
      }
    }, 1200);
  };

  return (
    <div className="flex flex-col w-full pb-8 space-y-4">
      {/* Top Banner */}
      <div className="bg-emerald-50 border border-emerald-200/80 rounded-xl p-3 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-2.5 min-w-0">
          <CheckCircle2 className="h-5 w-5 text-emerald-600 shrink-0" />
          <span className="text-xs font-semibold text-emerald-900 truncate">
            All Checks Passed — Ready for Review
          </span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0 ml-2">
          <span className="font-mono text-[11px] bg-white border border-emerald-200 text-emerald-800 px-2 py-0.5 rounded shadow-2xs font-medium">
            PR #1084
          </span>
          <span className="font-mono text-[11px] bg-white border border-zinc-200 text-zinc-600 px-2 py-0.5 rounded shadow-2xs font-medium">
            4e91fb0
          </span>
        </div>
      </div>

      {/* PR Summary Card */}
      <div className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm flex flex-col">
        <div className="flex items-start justify-between gap-2 mb-1.5">
          <h2 className="font-mono text-sm font-semibold text-zinc-900 break-all">
            fix(telemetry): guard dispatcher buffer with asyncio mutex
          </h2>
        </div>

        <div className="flex items-center gap-2 text-zinc-500 text-xs mb-3.5 flex-wrap">
          <span className="inline-flex items-center gap-1">
            <Bot className="h-3.5 w-3.5 text-emerald-600" />
            <span>Authored by Autonomous Agent</span>
          </span>
          <span className="text-zinc-300">•</span>
          <span>
            Target:{' '}
            <span className="font-mono text-[11px] text-zinc-800 bg-zinc-100 border border-zinc-200 px-1.5 py-0.5 rounded font-medium">
              main
            </span>
          </span>
        </div>

        <div className="bg-zinc-50 rounded-lg p-3 border border-zinc-200/80 flex flex-col gap-2.5">
          <div className="flex items-start gap-2.5">
            <Lock className="h-4 w-4 text-emerald-600 mt-0.5 shrink-0" />
            <p className="text-xs text-zinc-700 leading-relaxed">
              Wrapped buffer flush in asyncio lock to prevent concurrent race condition under heavy spike load.
            </p>
          </div>
          <div className="flex items-start gap-2.5">
            <RotateCcw className="h-4 w-4 text-emerald-600 mt-0.5 shrink-0" />
            <p className="text-xs text-zinc-700 leading-relaxed">
              Added retry backoff for network drops to prevent dropping analytics telemetry packets.
            </p>
          </div>
          <div className="flex items-start gap-2.5">
            <ShieldCheck className="h-4 w-4 text-emerald-600 mt-0.5 shrink-0" />
            <p className="text-xs text-zinc-700 leading-relaxed">
              Updated unit test suite with 14 concurrency test scenarios to prevent regressions.
            </p>
          </div>
        </div>
      </div>

      {/* Verification Metrics Grid */}
      <div className="grid grid-cols-3 gap-2.5">
        <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-sm flex flex-col justify-between">
          <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500 font-medium">
            TESTS
          </span>
          <div className="my-1.5">
            <span className="text-xs font-semibold text-zinc-900 flex items-center gap-1">
              14/14 Pass
            </span>
          </div>
          <div className="flex items-center gap-1 text-emerald-600 font-mono text-[11px]">
            <Check className="h-3 w-3 stroke-[2.5]" />
            <span>0.82s pytest</span>
          </div>
        </div>

        <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-sm flex flex-col justify-between">
          <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500 font-medium">
            TYPE CHECK
          </span>
          <div className="my-1.5">
            <span className="text-xs font-semibold text-zinc-900 flex items-center gap-1">
              0 Errors
            </span>
          </div>
          <div className="flex items-center gap-1 text-emerald-600 font-mono text-[11px]">
            <Check className="h-3 w-3 stroke-[2.5]" />
            <span>mypy strict</span>
          </div>
        </div>

        <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-sm flex flex-col justify-between">
          <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500 font-medium">
            SECURITY
          </span>
          <div className="my-1.5">
            <span className="text-xs font-semibold text-zinc-900 flex items-center gap-1">
              Clean
            </span>
          </div>
          <div className="flex items-center gap-1 text-emerald-600 font-mono text-[11px]">
            <ShieldCheck className="h-3 w-3 stroke-[2.5]" />
            <span>0 CVEs</span>
          </div>
        </div>
      </div>

      {/* Interactive Diff Viewer */}
      <div className="bg-white rounded-xl overflow-hidden border border-zinc-200/80 shadow-sm flex flex-col">
        <div className="bg-zinc-50 border-b border-zinc-200/80 px-3.5 py-2.5 flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <FileText className="h-4 w-4 text-zinc-500 shrink-0" />
            <span className="font-mono text-xs font-semibold text-zinc-800 truncate">
              src/telemetry/dispatcher.py
            </span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <span className="font-mono text-[11px] text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 rounded font-medium">
              +18
            </span>
            <span className="font-mono text-[11px] text-rose-700 bg-rose-50 border border-rose-200 px-1.5 py-0.5 rounded font-medium">
              -6
            </span>
          </div>
        </div>

        <div className="overflow-x-auto w-full font-mono text-xs py-2 bg-zinc-950 text-zinc-100">
          <div className="flex items-stretch bg-zinc-900/80 text-zinc-400 px-3 py-1 select-none font-medium">
            <span className="w-12 text-zinc-500 text-right pr-3">@@</span>
            <span className="w-full pl-2 text-zinc-400">-40,8 +40,12 @@</span>
          </div>

          <div className="flex items-stretch px-3 py-0.5 hover:bg-zinc-900/50">
            <span className="w-6 text-zinc-600 text-right pr-2 select-none">40</span>
            <span className="w-6 text-zinc-600 text-right pr-3 select-none">40</span>
            <span className="w-4 text-center select-none text-zinc-600">&nbsp;</span>
            <pre className="text-zinc-300">class TelemetryDispatcher:</pre>
          </div>

          <div className="flex items-stretch px-3 py-0.5 hover:bg-zinc-900/50">
            <span className="w-6 text-zinc-600 text-right pr-2 select-none">41</span>
            <span className="w-6 text-zinc-600 text-right pr-3 select-none">41</span>
            <span className="w-4 text-center select-none text-zinc-600">&nbsp;</span>
            <pre className="text-zinc-300">    def __init__(self, buffer_size: int = 1000):</pre>
          </div>

          <div className="flex items-stretch bg-rose-950/40 text-rose-300 px-3 py-0.5 border-l-2 border-rose-500">
            <span className="w-6 text-rose-400/80 text-right pr-2 select-none">42</span>
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-4 text-center select-none text-rose-400 font-bold">-</span>
            <pre className="text-rose-200 font-medium">        self.queue = Queue(maxsize=buffer_size)</pre>
          </div>

          <div className="flex items-stretch bg-emerald-950/40 text-emerald-300 px-3 py-0.5 border-l-2 border-emerald-500">
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-6 text-emerald-400/80 text-right pr-3 select-none">43</span>
            <span className="w-4 text-center select-none text-emerald-400 font-bold">+</span>
            <pre className="text-emerald-200 font-medium">        self._lock = asyncio.Lock()</pre>
          </div>

          <div className="flex items-stretch bg-emerald-950/40 text-emerald-300 px-3 py-0.5 border-l-2 border-emerald-500">
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-6 text-emerald-400/80 text-right pr-3 select-none">44</span>
            <span className="w-4 text-center select-none text-emerald-400 font-bold">+</span>
            <pre className="text-emerald-200 font-medium">        self._is_flushing = False</pre>
          </div>

          <div className="flex items-stretch bg-zinc-900/80 text-zinc-400 px-3 py-1 select-none font-medium mt-1">
            <span className="w-12 text-zinc-500 text-right pr-3">@@</span>
            <span className="w-full pl-2 text-zinc-400">-58,6 +64,10 @@</span>
          </div>

          <div className="flex items-stretch bg-rose-950/40 text-rose-300 px-3 py-0.5 border-l-2 border-rose-500">
            <span className="w-6 text-rose-400/80 text-right pr-2 select-none">59</span>
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-4 text-center select-none text-rose-400 font-bold">-</span>
            <pre className="text-rose-200 font-medium">        batch = list(self._buffer)</pre>
          </div>

          <div className="flex items-stretch bg-rose-950/40 text-rose-300 px-3 py-0.5 border-l-2 border-rose-500">
            <span className="w-6 text-rose-400/80 text-right pr-2 select-none">60</span>
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-4 text-center select-none text-rose-400 font-bold">-</span>
            <pre className="text-rose-200 font-medium">        self._buffer.clear()</pre>
          </div>

          <div className="flex items-stretch bg-emerald-950/40 text-emerald-300 px-3 py-0.5 border-l-2 border-emerald-500">
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-6 text-emerald-400/80 text-right pr-3 select-none">65</span>
            <span className="w-4 text-center select-none text-emerald-400 font-bold">+</span>
            <pre className="text-emerald-200 font-medium">        async with self._lock:</pre>
          </div>

          <div className="flex items-stretch bg-emerald-950/40 text-emerald-300 px-3 py-0.5 border-l-2 border-emerald-500">
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-6 text-emerald-400/80 text-right pr-3 select-none">66</span>
            <span className="w-4 text-center select-none text-emerald-400 font-bold">+</span>
            <pre className="text-emerald-200 font-medium">            if not self._buffer:</pre>
          </div>

          <div className="flex items-stretch bg-emerald-950/40 text-emerald-300 px-3 py-0.5 border-l-2 border-emerald-500">
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-6 text-emerald-400/80 text-right pr-3 select-none">67</span>
            <span className="w-4 text-center select-none text-emerald-400 font-bold">+</span>
            <pre className="text-emerald-200 font-medium">                return 0</pre>
          </div>

          <div className="flex items-stretch bg-emerald-950/40 text-emerald-300 px-3 py-0.5 border-l-2 border-emerald-500">
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-6 text-emerald-400/80 text-right pr-3 select-none">68</span>
            <span className="w-4 text-center select-none text-emerald-400 font-bold">+</span>
            <pre className="text-emerald-200 font-medium">            batch = self._buffer[:self.MAX_BATCH]</pre>
          </div>

          <div className="flex items-stretch bg-emerald-950/40 text-emerald-300 px-3 py-0.5 border-l-2 border-emerald-500">
            <span className="w-6 select-none">&nbsp;</span>
            <span className="w-6 text-emerald-400/80 text-right pr-3 select-none">69</span>
            <span className="w-4 text-center select-none text-emerald-400 font-bold">+</span>
            <pre className="text-emerald-200 font-medium">            del self._buffer[:self.MAX_BATCH]</pre>
          </div>
        </div>
      </div>

      {/* Action Buttons */}
      <div className="flex items-center gap-3 pt-1">
        <button
          type="button"
          onClick={handleRequestChanges}
          disabled={requestState !== 'idle'}
          className="h-11 px-4 bg-white hover:bg-zinc-100 text-zinc-800 text-xs font-medium rounded-xl border border-zinc-200 shadow-sm flex items-center justify-center gap-2 shrink-0 transition-all active:scale-[0.98]"
        >
          {requestState === 'idle' && (
            <>
              <RotateCcw className="h-3.5 w-3.5 text-zinc-500" />
              <span>Request Changes</span>
            </>
          )}
          {requestState === 'sent' && (
            <>
              <MessageSquare className="h-3.5 w-3.5 text-emerald-600" />
              <span className="text-emerald-700 font-medium">Prompt Sent</span>
            </>
          )}
        </button>

        <button
          type="button"
          onClick={handleApprove}
          disabled={approveState !== 'idle'}
          className={`h-11 px-4 text-xs font-medium rounded-xl shadow-sm flex-1 flex items-center justify-center gap-2 transition-all active:scale-[0.99] ${
            approveState === 'merged'
              ? 'bg-emerald-600 text-white'
              : 'bg-zinc-900 hover:bg-zinc-800 text-white'
          }`}
        >
          {approveState === 'idle' && (
            <>
              <GitPullRequest className="h-4 w-4" />
              <span className="truncate">Approve &amp; Create PR</span>
            </>
          )}
          {approveState === 'creating' && (
            <>
              <Loader2 className="h-4 w-4 animate-spin text-white" />
              <span>Creating PR #1084...</span>
            </>
          )}
          {approveState === 'merged' && (
            <>
              <CheckCheck className="h-4 w-4 text-white" />
              <span>PR Created &amp; Merged!</span>
            </>
          )}
        </button>
      </div>
    </div>
  );
};