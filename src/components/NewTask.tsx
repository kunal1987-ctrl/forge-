import React, { useState, useEffect, useCallback } from 'react';
import {
  Sparkles,
  FolderCode,
  Lock,
  GitBranch,
  ChevronDown,
  Database,
  Paperclip,
  Mic,
  Check,
  Zap,
  Loader2,
  CheckCircle2,
  ShieldCheck,
  GitCommit,
  RefreshCw,
  Sliders,
} from 'lucide-react';

interface NewTaskProps {
  onStartRun?: () => void;
  onNavigate?: (tab: 'new' | 'activity' | 'review') => void;
}

export const NewTask: React.FC<NewTaskProps> = ({ onStartRun, onNavigate }) => {
  const [prompt, setPrompt] = useState<string>(
    'Fix race condition in async telemetry dispatcher when socket drops during handshake. Add exponential backoff retry buffer and unit tests.'
  );
  const [mode, setMode] = useState<'standard' | 'audit'>('standard');
  const [runState, setRunState] = useState<'idle' | 'spawning' | 'dispatched'>('idle');

  const handleStartRun = useCallback(() => {
    if (onStartRun) {
      onStartRun();
      return;
    }

    if (runState !== 'idle') return;
    setRunState('spawning');

    setTimeout(() => {
      setRunState('dispatched');
      setTimeout(() => {
        setRunState('idle');
        if (onNavigate) {
          onNavigate('activity');
        }
      }, 1400);
    }, 1200);
  }, [runState, onNavigate, onStartRun]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        handleStartRun();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleStartRun]);

  return (
    <div className="flex flex-col w-full pb-8 space-y-4">
      {/* Title / Introduction */}
      <div className="flex items-center justify-between pb-1">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-zinc-900">New Task</h1>
          <p className="text-xs text-zinc-500 mt-1">
            Describe the bug or feature, and FORGE will investigate, patch, and test it.
          </p>
        </div>
        <div className="h-9 w-9 rounded-full bg-zinc-100 flex items-center justify-center text-zinc-600 shrink-0 border border-zinc-200">
          <Sparkles className="h-4 w-4" />
        </div>
      </div>

      {/* Workspace Context / Target Card */}
      <div className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-medium">
            Repository
          </span>
          <div className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200/60">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
            <span className="font-mono text-[11px] font-medium">Connected</span>
          </div>
        </div>

        <div className="flex items-center gap-2 bg-zinc-50 border border-zinc-200/80 px-3 py-2 rounded-lg">
          <FolderCode className="h-4 w-4 text-zinc-500 shrink-0" />
          <input
            className="bg-transparent font-mono text-xs text-zinc-800 w-full focus:outline-none select-all"
            readOnly
            type="text"
            value="github.com/hyper/telemetry"
          />
          <Lock className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
        </div>

        <div className="flex items-center justify-between pt-0.5">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-medium">
            Target Branch
          </span>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 bg-zinc-100 hover:bg-zinc-200/70 border border-zinc-200 px-2.5 py-1 rounded-lg text-zinc-800 transition-colors"
          >
            <GitBranch className="h-3.5 w-3.5 text-emerald-600" />
            <span className="font-mono text-xs font-semibold">main</span>
            <ChevronDown className="h-3.5 w-3.5 text-zinc-500" />
          </button>
        </div>
      </div>

      {/* Task Objective / Prompt Card */}
      <div className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-sm">
        <div className="flex items-center justify-between mb-2">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-medium">
            Task Objective
          </span>
          <span className="font-mono text-[11px] text-zinc-500 flex items-center gap-1.5">
            <Database className="h-3.5 w-3.5 text-emerald-600" />
            AST Index Active
          </span>
        </div>

        <div className="relative bg-zinc-50 border border-zinc-200 rounded-lg p-3 focus-within:border-zinc-400 focus-within:bg-white transition-colors">
          <textarea
            className="w-full bg-transparent text-sm text-zinc-900 placeholder:text-zinc-400 resize-none focus:outline-none min-h-[72px]"
            id="task-prompt"
            placeholder="Describe issue or paste error log..."
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />

          <div className="flex items-center justify-between mt-3 pt-2.5 border-t border-zinc-200/60">
            <div className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-zinc-200/60 text-zinc-700 text-xs font-mono">
              <FolderCode className="h-3 w-3 text-zinc-500" />
              <span>14 relevant files indexed</span>
            </div>

            <div className="flex items-center gap-1 text-zinc-500">
              <button
                type="button"
                className="w-7 h-7 rounded flex items-center justify-center hover:bg-zinc-200/70 hover:text-zinc-800 transition-colors"
                title="Attach Stack Trace"
              >
                <Paperclip className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                className="w-7 h-7 rounded flex items-center justify-center hover:bg-zinc-200/70 hover:text-zinc-800 transition-colors"
                title="Speech Dictation"
              >
                <Mic className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Execution Strategy Mode */}
      <div>
        <div className="flex items-center justify-between mb-2 px-0.5">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-medium">
            Execution Mode
          </span>
          <span className="font-mono text-[11px] text-emerald-600 font-medium">Dual-Pass Ready</span>
        </div>

        <div className="space-y-2">
          {/* Strategy Option 1: Standard */}
          <div
            onClick={() => setMode('standard')}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && setMode('standard')}
            className={`cursor-pointer rounded-xl p-3.5 border transition-all flex items-start gap-3 ${
              mode === 'standard'
                ? 'bg-white border-zinc-900 shadow-sm ring-1 ring-zinc-900'
                : 'bg-zinc-50 border-zinc-200 hover:bg-zinc-100/80'
            }`}
          >
            <div className="pt-0.5">
              <div
                className={`w-4 h-4 rounded-full flex items-center justify-center transition-colors ${
                  mode === 'standard'
                    ? 'bg-zinc-900 text-white'
                    : 'border border-zinc-300 bg-white'
                }`}
              >
                {mode === 'standard' && <Check className="h-2.5 w-2.5 stroke-[3]" />}
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold text-sm text-zinc-900">Standard</span>
                <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-zinc-100 text-zinc-600 font-mono text-[11px] border border-zinc-200">
                  Fast fix &amp; tests
                </span>
                <span className="font-mono text-xs text-emerald-600 ml-auto font-medium">~2 mins</span>
              </div>
              <p className="text-xs text-zinc-500 mt-1">
                Optimized for routine bug fixes, test coverage, and small-to-medium PRs.
              </p>
            </div>
          </div>

          {/* Strategy Option 2: Deep Audit */}
          <div
            onClick={() => setMode('audit')}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === 'Enter' && setMode('audit')}
            className={`cursor-pointer rounded-xl p-3.5 border transition-all flex items-start gap-3 ${
              mode === 'audit'
                ? 'bg-white border-zinc-900 shadow-sm ring-1 ring-zinc-900'
                : 'bg-zinc-50 border-zinc-200 hover:bg-zinc-100/80'
            }`}
          >
            <div className="pt-0.5">
              <div
                className={`w-4 h-4 rounded-full flex items-center justify-center transition-colors ${
                  mode === 'audit'
                    ? 'bg-zinc-900 text-white'
                    : 'border border-zinc-300 bg-white'
                }`}
              >
                {mode === 'audit' && <Check className="h-2.5 w-2.5 stroke-[3]" />}
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold text-sm text-zinc-900">Deep Audit</span>
                <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-zinc-100 text-zinc-600 font-mono text-[11px] border border-zinc-200">
                  Architecture &amp; Sec
                </span>
                <span className="font-mono text-xs text-zinc-500 ml-auto font-medium">~6 mins</span>
              </div>
              <p className="text-xs text-zinc-500 mt-1">
                Thorough AST inspection, multi-file architectural changes, and formal verification.
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Primary Trigger Action */}
      <button
        type="button"
        onClick={handleStartRun}
        disabled={runState !== 'idle'}
        className="w-full h-11 bg-zinc-900 hover:bg-zinc-800 disabled:opacity-90 text-white rounded-xl flex items-center justify-center gap-2 shadow-md active:scale-[0.99] transition-all font-medium text-sm"
      >
        {runState === 'idle' && (
          <>
            <Zap className="h-4 w-4 fill-white" />
            <span>Start Autonomous Run</span>
            <span className="ml-1 px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300 font-mono text-[11px]">
              ⌘↵
            </span>
          </>
        )}
        {runState === 'spawning' && (
          <>
            <Loader2 className="h-4 w-4 animate-spin text-white" />
            <span>Spawning Container...</span>
          </>
        )}
        {runState === 'dispatched' && (
          <>
            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
            <span>Workspace Dispatched</span>
          </>
        )}
      </button>

      {/* Sandbox Protection Note */}
      <div className="flex items-center justify-center gap-1.5 text-zinc-500 text-xs py-1">
        <ShieldCheck className="h-4 w-4 text-emerald-600 shrink-0" />
        <span className="font-mono text-[11px]">Isolated container · Zero uncommitted branch mutations</span>
      </div>

      {/* Recent Autonomous Tasks Section */}
      <div className="pt-2">
        <div className="flex items-center justify-between mb-2.5 px-0.5">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-medium">
            Recent Tasks
          </span>
          <span className="font-mono text-xs text-zinc-900 font-medium hover:underline cursor-pointer">
            View History
          </span>
        </div>

        <div className="flex flex-col gap-2">
          {/* Item 1 */}
          <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-sm flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center shrink-0 text-zinc-600">
                <GitCommit className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <p className="text-sm font-medium text-zinc-900 truncate">
                  Fix memory leak in WebSocket pool
                </p>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="font-mono text-[11px] text-zinc-500">#PR-842</span>
                  <span className="font-mono text-[11px] text-zinc-400">· 18m ago</span>
                </div>
              </div>
            </div>
            <div className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 shrink-0">
              <Check className="h-3 w-3 text-emerald-600 stroke-[2.5]" />
              <span className="font-mono text-[11px] font-medium">Merged</span>
            </div>
          </div>

          {/* Item 2 */}
          <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-sm flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center shrink-0 text-zinc-600">
                <RefreshCw className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <p className="text-sm font-medium text-zinc-900 truncate">
                  Migrate ORM queries to batched DataLoader
                </p>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="font-mono text-[11px] text-zinc-500">#PR-839</span>
                  <span className="font-mono text-[11px] text-zinc-400">· 1h ago</span>
                </div>
              </div>
            </div>
            <div className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-zinc-100 text-zinc-700 border border-zinc-200 shrink-0">
              <span className="font-mono text-[11px] font-medium">In Review</span>
            </div>
          </div>

          {/* Item 3 */}
          <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-sm flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-8 h-8 rounded-lg bg-zinc-100 flex items-center justify-center shrink-0 text-zinc-600">
                <Sliders className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <p className="text-sm font-medium text-zinc-900 truncate">
                  Add trace telemetry headers to gRPC client
                </p>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="font-mono text-[11px] text-zinc-500">#PR-837</span>
                  <span className="font-mono text-[11px] text-zinc-400">· 4h ago</span>
                </div>
              </div>
            </div>
            <div className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 shrink-0">
              <Check className="h-3 w-3 text-emerald-600 stroke-[2.5]" />
              <span className="font-mono text-[11px] font-medium">Merged</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};