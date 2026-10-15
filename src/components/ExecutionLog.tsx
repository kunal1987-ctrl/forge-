import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Terminal,
  Copy,
  Check,
  Trash2,
  ArrowDown,
  Radio,
} from 'lucide-react';
import { useTask, type GlobalTaskStatus } from '../context/TaskContext';

export interface ExecutionLogProps {
  logs?: string[];
  status?: GlobalTaskStatus;
  className?: string;
  maxHeight?: string;
  title?: string;
  showLineNumbers?: boolean;
}

/**
 * Highlights semantic tokens in raw telemetry log strings.
 */
function formatLogText(text: string): React.ReactNode {
  // Highlight bracket tags: [planner], [verifier], [Error], etc.
  const bracketMatch = text.match(/^(\[[^\]]+\])(.*)$/);
  if (bracketMatch) {
    const tag = bracketMatch[1];
    const rest = bracketMatch[2];

    let tagColor = 'text-blue-400 bg-blue-500/10 border-blue-500/30';
    const lowerTag = tag.toLowerCase();

    if (lowerTag.includes('error') || lowerTag.includes('fail')) {
      tagColor = 'text-rose-400 bg-rose-500/10 border-rose-500/30';
    } else if (lowerTag.includes('pass') || lowerTag.includes('success') || lowerTag.includes('approved')) {
      tagColor = 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
    } else if (lowerTag.includes('planner') || lowerTag.includes('plan')) {
      tagColor = 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30';
    } else if (lowerTag.includes('repro')) {
      tagColor = 'text-amber-300 bg-amber-500/10 border-amber-500/30';
    } else if (lowerTag.includes('investigat')) {
      tagColor = 'text-indigo-400 bg-indigo-500/10 border-indigo-500/30';
    } else if (lowerTag.includes('implement')) {
      tagColor = 'text-purple-400 bg-purple-500/10 border-purple-500/30';
    } else if (lowerTag.includes('verif') || lowerTag.includes('test')) {
      tagColor = 'text-teal-400 bg-teal-500/10 border-teal-500/30';
    } else if (lowerTag.includes('review')) {
      tagColor = 'text-amber-400 bg-amber-500/10 border-amber-500/30';
    }

    return (
      <span className="break-all">
        <span className={`inline-block px-1.5 py-0.5 rounded text-[11px] font-mono border mr-2 font-semibold ${tagColor}`}>
          {tag}
        </span>
        <span className={lowerTag.includes('error') ? 'text-rose-300' : 'text-zinc-300'}>
          {rest}
        </span>
      </span>
    );
  }

  if (text.toLowerCase().includes('error') || text.toLowerCase().includes('exception')) {
    return <span className="text-rose-400 break-all">{text}</span>;
  }
  if (text.toLowerCase().includes('passed') || text.toLowerCase().includes('success')) {
    return <span className="text-emerald-400 break-all">{text}</span>;
  }

  return <span className="text-zinc-300 break-all">{text}</span>;
}

export const ExecutionLog: React.FC<ExecutionLogProps> = ({
  logs: propLogs,
  status: propStatus,
  className = '',
  maxHeight = '420px',
  title = 'Execution Log Terminal',
  showLineNumbers = true,
}) => {
  const globalTask = useTask();

  // Resolve state: explicit props take precedence, fallback to global TaskContext
  const logs = propLogs !== undefined ? propLogs : globalTask.logs;
  const status = propStatus !== undefined ? propStatus : globalTask.status;

  const [copied, setCopied] = useState<boolean>(false);
  const [isAutoScrollLocked, setIsAutoScrollLocked] = useState<boolean>(true);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalEndRef = useRef<HTMLDivElement | null>(null);

  const isRunning = status === 'RUNNING';
  const hasLogs = logs.length > 0;

  // Auto-scroll to the newest entry whenever logs change
  const scrollToBottom = useCallback((smooth = true) => {
    if (terminalEndRef.current) {
      terminalEndRef.current.scrollIntoView({
        behavior: smooth ? 'smooth' : 'auto',
        block: 'end',
      });
    }
  }, []);

  useEffect(() => {
    if (isAutoScrollLocked && (hasLogs || isRunning)) {
      scrollToBottom(true);
    }
  }, [logs.length, isRunning, isAutoScrollLocked, scrollToBottom]);

  // Detect manual scroll-up to pause sticky auto-scroll
  const handleScroll = () => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 40;
    setIsAutoScrollLocked(isAtBottom);
  };

  const handleCopyLogs = async () => {
    if (!logs.length) return;
    try {
      await navigator.clipboard.writeText(logs.join('\n'));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <section
      aria-label="Execution Log Terminal"
      className={`terminal-stream rounded-2xl border border-zinc-800 bg-slate-900 text-green-400 shadow-xl overflow-hidden flex flex-col font-mono ${className}`}
    >
      {/* Terminal Title Bar */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-[#161b22] border-b border-zinc-800/80 select-none">
        {/* Left: Window Controls & Title */}
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-rose-500/80 inline-block" />
            <span className="w-2.5 h-2.5 rounded-full bg-amber-500/80 inline-block" />
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-500/80 inline-block" />
          </div>

          <div className="h-4 w-px bg-zinc-700/60 mx-0.5" />

          <div className="flex items-center gap-2">
            <Terminal className="h-3.5 w-3.5 text-zinc-400" />
            <span className="text-xs font-semibold text-zinc-200 tracking-wide">
              {title}
            </span>
          </div>

          {/* Real-time Status Badge */}
          {isRunning ? (
            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 font-mono text-[10px] border border-emerald-500/30">
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
              </span>
              LIVE STREAM
            </span>
          ) : status === 'COMPLETED' ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 font-mono text-[10px] border border-emerald-500/30">
              <Check className="h-3 w-3" />
              FINISHED
            </span>
          ) : status === 'FAILED' ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-rose-500/10 text-rose-400 font-mono text-[10px] border border-rose-500/30">
              FAILED
            </span>
          ) : status === 'ABORTED' ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-400 font-mono text-[10px] border border-zinc-700">
              ABORTED
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-zinc-800/80 text-zinc-400 font-mono text-[10px] border border-zinc-700/50">
              IDLE
            </span>
          )}
        </div>

        {/* Right: Actions (Copy, Clear, Log Count) */}
        <div className="flex items-center gap-2">
          {hasLogs && (
            <span className="text-[11px] text-zinc-500 font-mono hidden sm:inline">
              {logs.length} lines
            </span>
          )}

          {hasLogs && (
            <button
              type="button"
              onClick={handleCopyLogs}
              title="Copy all logs to clipboard"
              className="inline-flex items-center gap-1 px-2 py-1 rounded bg-[#21262d] hover:bg-[#30363d] text-zinc-300 text-xs transition border border-zinc-700/60 cursor-pointer"
            >
              {copied ? (
                <>
                  <Check className="h-3 w-3 text-emerald-400" />
                  <span className="text-emerald-400 text-[11px]">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="h-3 w-3 text-zinc-400" />
                  <span className="text-[11px]">Copy</span>
                </>
              )}
            </button>
          )}

          {hasLogs && (
            <button
              type="button"
              onClick={globalTask.clearLogs}
              title="Clear terminal logs"
              className="p-1 rounded hover:bg-[#21262d] text-zinc-400 hover:text-zinc-200 transition"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Terminal Viewport */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        style={{ maxHeight }}
        className="flex-1 overflow-y-auto p-4 space-y-1.5 scrollbar-thin scrollbar-thumb-zinc-700 scrollbar-track-transparent text-xs"
      >
        {/* STATE A: IDLE & NO LOGS */}
        {!isRunning && !hasLogs ? (
          <div
            id="execution-log-idle"
            className="py-14 px-4 text-center flex flex-col items-center justify-center gap-3 text-zinc-400 select-none"
          >
            <div className="w-12 h-12 rounded-2xl bg-[#161b22] border border-zinc-800 flex items-center justify-center text-zinc-500 shadow-inner">
              <Radio className="h-5 w-5 text-emerald-500/70" />
            </div>
            <div className="space-y-1">
              <h3 className="text-xs font-bold text-zinc-300 uppercase tracking-wider">
                Autonomous Engine Standing By
              </h3>
              <p className="text-[11px] text-zinc-500 max-w-sm leading-relaxed">
                Terminal stream will activate automatically upon task dispatch. Live events, AST context, and test outputs stream here in real time.
              </p>
            </div>
          </div>
        ) : (
          /* STATE B: RUNNING OR LOGS PRESENT */
          <div className="space-y-1 font-mono">
            {logs.map((line, idx) => (
              <div
                key={idx}
                className="flex items-start gap-2.5 hover:bg-[#161b22]/60 px-1 py-0.5 rounded transition-colors group"
              >
                {showLineNumbers && (
                  <span className="text-zinc-600 select-none w-8 text-right shrink-0 font-mono text-[11px]">
                    {String(idx + 1).padStart(3, '0')}
                  </span>
                )}
                <div className="flex-1 leading-relaxed">
                  {formatLogText(line)}
                </div>
              </div>
            ))}

            {/* Actively executing terminal prompt cursor */}
            {isRunning && (
              <div className="flex items-center gap-2.5 px-1 py-0.5 text-emerald-400">
                {showLineNumbers && (
                  <span className="text-zinc-600 select-none w-8 text-right shrink-0 font-mono text-[11px]">
                    {String(logs.length + 1).padStart(3, '0')}
                  </span>
                )}
                <div className="flex items-center gap-1.5">
                  <span className="text-zinc-500 font-semibold">forge:~$</span>
                  <span className="animate-pulse inline-block w-2 h-4 bg-emerald-400" />
                  <span className="text-[11px] text-zinc-500 italic ml-1">
                    Worker processing workflow step...
                  </span>
                </div>
              </div>
            )}

            <div ref={terminalEndRef} />
          </div>
        )}
      </div>

      {/* Sticky Resume Auto-Scroll Button */}
      {!isAutoScrollLocked && hasLogs && (
        <div className="p-2 bg-[#161b22] border-t border-zinc-800 flex justify-center">
          <button
            type="button"
            onClick={() => {
              setIsAutoScrollLocked(true);
              scrollToBottom(true);
            }}
            className="flex items-center gap-1.5 px-3 py-1 rounded-full bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-400 text-xs border border-emerald-500/40 shadow-sm transition"
          >
            <ArrowDown className="h-3.5 w-3.5 animate-bounce" />
            <span>Scroll to latest entry</span>
          </button>
        </div>
      )}
    </section>
  );
};

export default ExecutionLog;
