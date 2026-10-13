import React, { useState, useEffect, useRef } from 'react';
import {
  Sparkles,
  Zap,
  Brain,
  Download,
  Check,
  Copy,
  ExternalLink,
  RotateCcw,
  CheckCircle2,
  AlertCircle,
  XCircle,
  Clock,
  Terminal,
  Search,
  Code2,
  GitPullRequest,
  Loader2,
  X,
  FolderGit2,
  Database,
  ArrowRight,
  ShieldCheck,
  Flame,
  Paperclip,
  Mic,
  GitBranch,
  ChevronDown,
} from 'lucide-react';
import { useAgentController } from '../hooks/useAgentController';
import { apiClient } from '../services/apiClient';
import type { RAGSearchResultItem } from '../types/api';

export const Workspace: React.FC = () => {
  const {
    taskId,
    status,
    mode,
    logs,
    currentDiff,
    prUrl,
    testMetrics,
    isSubmitting,
    isIngesting,
    activeModal,
    plan,
    toasts,
    repoId,
    setMode,
    setActiveModal,
    setRepoId,
    dismissToast,
    handleStartRun,
    handleApprove,
    handleReject,
    handleCancel,
    handleIngest,
  } = useAgentController();

  // Local state for interactive controls
  const [prompt, setPrompt] = useState<string>(
    'Fix race condition in async telemetry dispatcher when socket drops during handshake. Add exponential backoff retry buffer and unit tests.'
  );
  const [ingestModalOpen, setIngestModalOpen] = useState<boolean>(false);
  const [ingestUrl, setIngestUrl] = useState<string>('https://github.com/fastapi/fastapi');
  const [ingestBranch, setIngestBranch] = useState<string>('main');

  const [rejectFeedback, setRejectFeedback] = useState<string>('');
  const [copiedDiff, setCopiedDiff] = useState<boolean>(false);

  // RAG Search interactive state
  const [ragQuery, setRagQuery] = useState<string>('telemetry dispatcher retry buffer');
  const [ragResults, setRagResults] = useState<RAGSearchResultItem[]>([]);
  const [isSearchingRag, setIsSearchingRag] = useState<boolean>(false);
  const [ragSearchError, setRagSearchError] = useState<string | null>(null);

  // Active bottom inspection tab
  const [activeTab, setActiveTab] = useState<'diff' | 'logs' | 'rag' | 'plan'>('diff');

  // Logs auto-scroll ref
  const logsEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (activeTab === 'logs') {
      logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, activeTab]);

  const isTaskActive = [
    'planning',
    'investigating',
    'implementing',
    'verifying',
    'awaiting_approval',
  ].includes(status);

  // Copy diff to clipboard handler
  const handleCopyDiff = async () => {
    if (!currentDiff) return;
    try {
      await navigator.clipboard.writeText(currentDiff);
      setCopiedDiff(true);
      setTimeout(() => setCopiedDiff(false), 2000);
    } catch {
      // Fallback
      setCopiedDiff(false);
    }
  };

  // Perform live RAG test search
  const handleTestSearch = async () => {
    if (!ragQuery.trim()) return;
    setIsSearchingRag(true);
    setRagSearchError(null);
    try {
      const results = await apiClient.searchCodebase({
        repo_id: repoId,
        query: ragQuery.trim(),
        limit: 5,
      });
      setRagResults(results);
      setActiveTab('rag');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Search failed';
      setRagSearchError(msg);
    } finally {
      setIsSearchingRag(false);
    }
  };

  // Target Branch State
  const [selectedBranch, setSelectedBranch] = useState<string>('main');
  const [isBranchMenuOpen, setIsBranchMenuOpen] = useState<boolean>(false);

  // Stack Trace File Attachment State
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [_attachedFiles, setAttachedFiles] = useState<string[]>([]);

  // Speech Dictation State
  const [isListening, setIsListening] = useState<boolean>(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const recognitionRef = useRef<any>(null);

  const [ingestModalError, setIngestModalError] = useState<string | null>(null);

  const handleAttachClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (event) => {
      const content = event.target?.result as string;
      const snippet = `\n\n--- [Attached: ${file.name}] ---\n${content.slice(0, 5000)}${
        content.length > 5000 ? '\n... (truncated)' : ''
      }`;
      setPrompt((prev) => `${prev}${snippet}`);
      setAttachedFiles((prev) => [...prev, file.name]);
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  const handleToggleVoiceDictation = () => {
    if (isListening) {
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
      setIsListening(false);
      return;
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const win = window as any;
    const SpeechRecognition = win.SpeechRecognition || win.webkitSpeechRecognition;

    if (!SpeechRecognition) {
      setPrompt((prev) => `${prev} [Voice Dictation: Ensure socket timeout handler triggers reconnection with exponential backoff.]`);
      return;
    }

    try {
      const rec = new SpeechRecognition();
      rec.lang = 'en-US';
      rec.continuous = false;
      rec.interimResults = false;

      rec.onstart = () => setIsListening(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      rec.onresult = (event: any) => {
        const text = event.results?.[0]?.[0]?.transcript;
        if (text) {
          setPrompt((prev) => `${prev} ${text}`);
        }
      };
      rec.onerror = () => setIsListening(false);
      rec.onend = () => setIsListening(false);
      recognitionRef.current = rec;
      rec.start();
    } catch {
      setIsListening(false);
    }
  };

  // Submit ingestion
  const onSubmitIngest = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ingestUrl.trim()) return;
    setIngestModalError(null);
    try {
      const resultId = await handleIngest(ingestUrl.trim(), ingestBranch.trim() || selectedBranch || 'main');
      if (resultId) {
        setRepoId(resultId);
        setIngestModalOpen(false);
      } else {
        setIngestModalError('Ingestion failed. Please check repository URL or branch.');
      }
    } catch (err: unknown) {
      setIngestModalError(err instanceof Error ? err.message : 'Ingestion failed');
    }
  };

  // Submit rejection feedback
  const onSubmitReject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!rejectFeedback.trim()) return;
    await handleReject(rejectFeedback.trim());
    setRejectFeedback('');
  };

  return (
    <div className="relative flex flex-col w-full min-h-[calc(100vh-120px)] space-y-6 pb-12 font-sans text-zinc-900">
      {/* Toast Notification Container */}
      <div className="fixed top-16 right-4 z-50 flex flex-col space-y-2 max-w-sm pointer-events-none">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`pointer-events-auto flex items-start gap-3 p-3.5 rounded-xl border shadow-lg backdrop-blur-md transition-all duration-300 animate-in fade-in slide-in-from-top-2 ${
              toast.type === 'success'
                ? 'bg-emerald-50/95 border-emerald-300 text-emerald-900'
                : toast.type === 'error'
                ? 'bg-rose-50/95 border-rose-300 text-rose-900'
                : 'bg-zinc-900/95 border-zinc-700 text-zinc-100'
            }`}
          >
            {toast.type === 'success' && <CheckCircle2 className="h-5 w-5 text-emerald-600 shrink-0 mt-0.5" />}
            {toast.type === 'error' && <XCircle className="h-5 w-5 text-rose-600 shrink-0 mt-0.5" />}
            {toast.type === 'info' && <AlertCircle className="h-5 w-5 text-zinc-400 shrink-0 mt-0.5" />}
            <div className="flex-1 text-xs font-medium leading-relaxed">{toast.message}</div>
            <button
              onClick={() => dismissToast(toast.id)}
              className="text-zinc-400 hover:text-zinc-600 transition shrink-0"
              aria-label="Dismiss toast"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        ))}
      </div>

      {/* TOP BAR CONTROLS */}
      <div className="bg-white border border-zinc-200/80 rounded-2xl p-4 shadow-xs flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 rounded-xl bg-zinc-900 text-white flex items-center justify-center font-bold tracking-tight shadow-sm">
            <Flame className="h-5 w-5 text-amber-400" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-base font-bold text-zinc-900 tracking-tight">FORGE Autonomous Engine</h1>
              <span
                className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold border ${
                  status === 'idle'
                    ? 'bg-zinc-100 text-zinc-700 border-zinc-200'
                    : status === 'awaiting_approval'
                    ? 'bg-amber-50 text-amber-800 border-amber-300 animate-pulse'
                    : status === 'pr_created'
                    ? 'bg-emerald-50 text-emerald-800 border-emerald-300'
                    : status === 'failed'
                    ? 'bg-rose-50 text-rose-800 border-rose-300'
                    : 'bg-indigo-50 text-indigo-800 border-indigo-300 animate-pulse'
                }`}
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    status === 'idle'
                      ? 'bg-zinc-400'
                      : status === 'awaiting_approval'
                      ? 'bg-amber-500'
                      : status === 'pr_created'
                      ? 'bg-emerald-500'
                      : status === 'failed'
                      ? 'bg-rose-500'
                      : 'bg-indigo-500'
                  }`}
                />
                {status.toUpperCase().replace('_', ' ')}
              </span>
            </div>
            <div className="text-xs text-zinc-500 font-mono flex items-center gap-2 mt-0.5">
              <span>Repo:</span>
              <span className="font-semibold text-zinc-800">{repoId}</span>
              <span className="text-zinc-300">•</span>
              <div className="relative inline-flex items-center">
                <button
                  type="button"
                  onClick={() => setIsBranchMenuOpen((prev) => !prev)}
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-zinc-100 hover:bg-zinc-200 text-zinc-800 font-semibold"
                >
                  <GitBranch className="h-3 w-3 text-emerald-600" />
                  <span>{selectedBranch}</span>
                  <ChevronDown className="h-3 w-3 text-zinc-400" />
                </button>
                {isBranchMenuOpen && (
                  <div className="absolute left-0 top-6 z-30 w-36 bg-white border border-zinc-200 rounded-lg shadow-md p-1 space-y-0.5 text-xs">
                    {['main', 'dev', 'staging'].map((b) => (
                      <button
                        key={b}
                        type="button"
                        onClick={() => {
                          setSelectedBranch(b);
                          setIsBranchMenuOpen(false);
                        }}
                        className={`w-full flex items-center justify-between px-2 py-1 rounded text-left ${
                          selectedBranch === b ? 'bg-zinc-100 font-bold' : 'hover:bg-zinc-50'
                        }`}
                      >
                        <span>{b}</span>
                        {selectedBranch === b && <Check className="h-3 w-3 text-emerald-600" />}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              {taskId && (
                <>
                  <span className="text-zinc-300">•</span>
                  <span>Task:</span>
                  <span className="text-zinc-700 truncate max-w-[120px]">{taskId}</span>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Action Controls in Top Bar */}
        <div className="flex items-center gap-2.5">
          {/* Mode Selector Toggle */}
          <div className="flex items-center p-1 bg-zinc-100 rounded-xl border border-zinc-200">
            <button
              type="button"
              disabled={isTaskActive}
              onClick={() => setMode('fast')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                mode === 'fast'
                  ? 'bg-white text-zinc-900 shadow-xs'
                  : 'text-zinc-500 hover:text-zinc-900 disabled:opacity-50'
              }`}
              title="Fast Mode: gpt-4o-mini"
            >
              <Zap className="h-3.5 w-3.5 text-amber-500" />
              <span>⚡ Fast (gpt-4o-mini)</span>
            </button>
            <button
              type="button"
              disabled={isTaskActive}
              onClick={() => setMode('deep')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                mode === 'deep'
                  ? 'bg-white text-zinc-900 shadow-xs'
                  : 'text-zinc-500 hover:text-zinc-900 disabled:opacity-50'
              }`}
              title="Deep Thinking Mode: DeepSeek R1 via OpenRouter"
            >
              <Brain className="h-3.5 w-3.5 text-indigo-500" />
              <span>🧠 Deep Thinking (DeepSeek R1)</span>
            </button>
          </div>

          {/* Ingest Repository Button */}
          <button
            type="button"
            disabled={isIngesting || isTaskActive}
            onClick={() => setIngestModalOpen(true)}
            className="flex items-center gap-2 px-3.5 py-2 rounded-xl text-xs font-semibold border border-zinc-300 bg-white hover:bg-zinc-50 text-zinc-800 shadow-xs transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isIngesting ? (
              <Loader2 className="h-4 w-4 text-zinc-600 animate-spin" />
            ) : (
              <FolderGit2 className="h-4 w-4 text-zinc-600" />
            )}
            <span>{isIngesting ? 'Ingesting AST...' : 'Ingest Repository'}</span>
          </button>
        </div>
      </div>

      {/* HUMAN-IN-THE-LOOP REVIEW GATE BANNER */}
      {status === 'awaiting_approval' && (
        <div className="bg-gradient-to-r from-amber-50 via-amber-100/50 to-orange-50 border-2 border-amber-400 rounded-2xl p-5 shadow-md animate-in fade-in slide-in-from-top-3">
          <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="flex h-3 w-3 relative">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-3 w-3 bg-amber-500" />
                </span>
                <h2 className="text-sm font-bold text-amber-950 uppercase tracking-wider">
                  Human-in-the-Loop Review Gate Active
                </h2>
              </div>
              <p className="text-xs text-amber-900/90 leading-relaxed max-w-2xl">
                The autonomous repair loop successfully verified tests in Docker sandbox with {testMetrics.passed} passed tests ({testMetrics.retries} retries).
                Review the patch diff below before publishing the upstream Pull Request.
              </p>
            </div>

            <div className="flex items-center gap-2.5 shrink-0 w-full md:w-auto">
              {/* Green Approve Button */}
              <button
                type="button"
                disabled={isSubmitting}
                onClick={handleApprove}
                className="flex-1 md:flex-none flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold shadow-sm transition disabled:opacity-50"
              >
                {isSubmitting ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <CheckCircle2 className="h-4 w-4" />
                )}
                <span>Approve & Create PR</span>
              </button>

              {/* Amber Reject Button */}
              <button
                type="button"
                disabled={isSubmitting}
                onClick={() => setActiveModal('reject')}
                className="flex-1 md:flex-none flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-amber-500 hover:bg-amber-600 text-amber-950 text-xs font-bold shadow-sm transition disabled:opacity-50"
              >
                <RotateCcw className="h-4 w-4" />
                <span>Reject & Request Changes</span>
              </button>

              {/* Red Abort Button */}
              <button
                type="button"
                disabled={isSubmitting}
                onClick={handleCancel}
                className="flex-1 md:flex-none flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-rose-600 hover:bg-rose-700 text-white text-xs font-bold shadow-sm transition disabled:opacity-50"
              >
                <XCircle className="h-4 w-4" />
                <span>Abort & Clean Worktree</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* PR CREATED CELEBRATION BANNER - strictly bound to verified PR */}
      {status === 'pr_created' && Boolean(prUrl) && (
        <div className="bg-emerald-50 border border-emerald-300 rounded-2xl p-4 flex items-center justify-between gap-4 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="h-10 w-10 rounded-xl bg-emerald-600 text-white flex items-center justify-center font-bold">
              <Check className="h-6 w-6" />
            </div>
            <div>
              <h3 className="text-sm font-bold text-emerald-950">Pull Request Created Successfully</h3>
              <p className="text-xs text-emerald-800">
                All automated verifications and human review gates passed. Worktree state cleanly reconciled.
              </p>
            </div>
          </div>
          {prUrl && (
            <a
              href={prUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 px-4 py-2 rounded-xl bg-emerald-700 hover:bg-emerald-800 text-white text-xs font-bold shadow-xs transition"
            >
              <span>Open GitHub PR</span>
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
        </div>
      )}

      {/* Hidden File Input for Paperclip */}
      <input
        type="file"
        ref={fileInputRef}
        onChange={handleFileChange}
        className="hidden"
        accept=".txt,.log,.py,.ts,.js,.json,.md,.diff,.patch,*"
      />

      {/* EXECUTION PROMPT & TASK DISPATCH CARD */}
      <div className="bg-white border border-zinc-200 rounded-2xl p-5 shadow-xs space-y-4">
        <div className="flex items-center justify-between">
          <label htmlFor="task-prompt" className="text-xs font-bold uppercase tracking-wider text-zinc-500">
            Autonomous Task Instructions & Spec
          </label>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleAttachClick}
              className="w-7 h-7 rounded-lg flex items-center justify-center text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100 transition border border-transparent hover:border-zinc-200"
              title="Attach Stack Trace or Code File"
              aria-label="Attach File"
            >
              <Paperclip className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={handleToggleVoiceDictation}
              className={`w-7 h-7 rounded-lg flex items-center justify-center transition border ${
                isListening
                  ? 'bg-rose-50 text-rose-600 border-rose-300 animate-pulse ring-2 ring-rose-400/30'
                  : 'text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100 border-transparent hover:border-zinc-200'
              }`}
              title={isListening ? 'Stop Listening' : 'Voice Dictation (Speech to Text)'}
              aria-label="Speech Dictation"
            >
              <Mic className="h-3.5 w-3.5" />
            </button>
            <span className="text-[11px] font-mono text-zinc-400">Ctrl + Enter to dispatch</span>
          </div>
        </div>

        <textarea
          id="task-prompt"
          rows={3}
          value={prompt}
          disabled={isTaskActive}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Describe the bug, feature request, or refactoring task..."
          className="w-full rounded-xl border border-zinc-200 bg-zinc-50/70 p-3.5 text-xs font-mono text-zinc-900 placeholder:text-zinc-400 focus:bg-white focus:border-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-200 transition disabled:opacity-60 resize-y"
        />

        <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
          <div className="flex items-center gap-3 text-xs text-zinc-500">
            <span className="flex items-center gap-1">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
              Isolated Git Worktree
            </span>
            <span>•</span>
            <span className="flex items-center gap-1">
              <Database className="h-3.5 w-3.5 text-indigo-600" />
              Graph-RAG 1-Hop Neighbor
            </span>
            <span>•</span>
            <span className="flex items-center gap-1">
              <Clock className="h-3.5 w-3.5 text-amber-600" />
              Max 3 Repair Loops
            </span>
          </div>

          <div className="flex items-center gap-2">
            {/* Cancel Task Button (Visible only when task is active) */}
            {isTaskActive && (
              <button
                type="button"
                disabled={status === 'cleaned_up'}
                onClick={handleCancel}
                className="flex items-center gap-1.5 px-4 py-2 rounded-xl border border-rose-300 bg-rose-50 hover:bg-rose-100 text-rose-700 text-xs font-bold transition shadow-2xs disabled:opacity-50 cursor-pointer"
              >
                {isSubmitting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-rose-600" />
                ) : (
                  <XCircle className="h-3.5 w-3.5" />
                )}
                <span>{isSubmitting ? 'Cancelling...' : 'Cancel Task'}</span>
              </button>
            )}

            {/* Start Autonomous Run Button */}
            <button
              type="button"
              disabled={!prompt.trim() || status !== 'idle' || isSubmitting}
              onClick={() => handleStartRun(prompt, repoId)}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-zinc-950 hover:bg-zinc-800 text-white text-xs font-bold shadow-sm transition disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {isSubmitting || isTaskActive ? (
                <Loader2 className="h-4 w-4 animate-spin text-amber-400" />
              ) : (
                <Sparkles className="h-4 w-4 text-amber-400" />
              )}
              <span>
                {isSubmitting
                  ? 'Dispatching Task...'
                  : isTaskActive
                  ? `Running (${status})...`
                  : 'Start Autonomous Run'}
              </span>
            </button>
          </div>
        </div>
      </div>

      {/* METRICS & VERIFICATION SUMMARY STRIP */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-white border border-zinc-200/80 rounded-xl p-3.5 shadow-2xs">
          <div className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Test Suite</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="text-xl font-bold font-mono text-emerald-600">{testMetrics.passed}</span>
            <span className="text-xs text-zinc-500">passed</span>
            {testMetrics.failed > 0 && (
              <span className="text-xs font-bold text-rose-600 font-mono">({testMetrics.failed} failed)</span>
            )}
          </div>
        </div>

        <div className="bg-white border border-zinc-200/80 rounded-xl p-3.5 shadow-2xs">
          <div className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Repair Iterations</div>
          <div className="mt-1 flex items-baseline gap-1.5">
            <span className="text-xl font-bold font-mono text-zinc-900">{testMetrics.retries}</span>
            <span className="text-xs text-zinc-400">/ 3 max</span>
          </div>
        </div>

        <div className="bg-white border border-zinc-200/80 rounded-xl p-3.5 shadow-2xs">
          <div className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">Target Scope</div>
          <div className="mt-1 text-xs font-mono font-medium text-zinc-800 truncate">
            {plan?.target_files?.length ? `${plan.target_files.length} files targeted` : 'Pending plan'}
          </div>
        </div>

        <div className="bg-white border border-zinc-200/80 rounded-xl p-3.5 shadow-2xs">
          <div className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">PR Target</div>
          <div className="mt-1 text-xs font-mono font-medium text-zinc-800 truncate">
            {prUrl ? (
              <a href={prUrl} target="_blank" rel="noreferrer" className="text-emerald-700 underline font-semibold">
                View PR
              </a>
            ) : (
              'forge/auto-patch'
            )}
          </div>
        </div>
      </div>

      {/* INSPECTION PANELS (Tabs: Diff, Logs, RAG, Plan) */}
      <div className="bg-white border border-zinc-200 rounded-2xl shadow-xs overflow-hidden flex flex-col flex-1 min-h-[460px]">
        {/* Navigation Tabs Header */}
        <div className="flex items-center justify-between px-4 border-b border-zinc-200 bg-zinc-50/50">
          <div className="flex items-center space-x-1">
            <button
              type="button"
              onClick={() => setActiveTab('diff')}
              className={`flex items-center gap-2 px-3.5 py-3 text-xs font-semibold border-b-2 transition ${
                activeTab === 'diff'
                  ? 'border-zinc-900 text-zinc-950 bg-white'
                  : 'border-transparent text-zinc-500 hover:text-zinc-800'
              }`}
            >
              <Code2 className="h-4 w-4" />
              <span>Unified Diff {currentDiff && '•'}</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab('logs')}
              className={`flex items-center gap-2 px-3.5 py-3 text-xs font-semibold border-b-2 transition ${
                activeTab === 'logs'
                  ? 'border-zinc-900 text-zinc-950 bg-white'
                  : 'border-transparent text-zinc-500 hover:text-zinc-800'
              }`}
            >
              <Terminal className="h-4 w-4" />
              <span>Event Stream ({logs.length})</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab('rag')}
              className={`flex items-center gap-2 px-3.5 py-3 text-xs font-semibold border-b-2 transition ${
                activeTab === 'rag'
                  ? 'border-zinc-900 text-zinc-950 bg-white'
                  : 'border-transparent text-zinc-500 hover:text-zinc-800'
              }`}
            >
              <Search className="h-4 w-4" />
              <span>RAG Explorer</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab('plan')}
              className={`flex items-center gap-2 px-3.5 py-3 text-xs font-semibold border-b-2 transition ${
                activeTab === 'plan'
                  ? 'border-zinc-900 text-zinc-950 bg-white'
                  : 'border-transparent text-zinc-500 hover:text-zinc-800'
              }`}
            >
              <ShieldCheck className="h-4 w-4" />
              <span>Execution Plan</span>
            </button>
          </div>

          {/* Contextual actions on tab header */}
          <div className="flex items-center gap-2">
            {activeTab === 'diff' && currentDiff && (
              <button
                type="button"
                onClick={handleCopyDiff}
                className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-lg border border-zinc-200 bg-white hover:bg-zinc-50 text-zinc-700 shadow-2xs transition"
              >
                {copiedDiff ? (
                  <>
                    <Check className="h-3.5 w-3.5 text-emerald-600" />
                    <span className="text-emerald-700 font-semibold">Copied!</span>
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5 text-zinc-500" />
                    <span>Copy Diff</span>
                  </>
                )}
              </button>
            )}

            {prUrl && (
              <a
                href={prUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold rounded-lg bg-emerald-50 border border-emerald-300 text-emerald-800 hover:bg-emerald-100 transition"
              >
                <GitPullRequest className="h-3.5 w-3.5" />
                <span>Open GitHub PR</span>
              </a>
            )}
          </div>
        </div>

        {/* TAB 1: UNIFIED DIFF VIEWER */}
        {activeTab === 'diff' && (
          <div className="flex-1 p-4 font-mono text-xs overflow-auto bg-zinc-950 text-zinc-200">
            {currentDiff ? (
              <pre className="whitespace-pre font-mono leading-relaxed">
                {currentDiff.split('\n').map((line, idx) => {
                  let lineClass = 'text-zinc-400';
                  if (line.startsWith('+') && !line.startsWith('+++')) {
                    lineClass = 'bg-emerald-950/60 text-emerald-300 block px-1 -mx-1 border-l-2 border-emerald-500';
                  } else if (line.startsWith('-') && !line.startsWith('---')) {
                    lineClass = 'bg-rose-950/60 text-rose-300 block px-1 -mx-1 border-l-2 border-rose-500';
                  } else if (line.startsWith('@@')) {
                    lineClass = 'text-cyan-400 font-bold block pt-1';
                  } else if (line.startsWith('diff --git')) {
                    lineClass = 'text-amber-400 font-bold border-t border-zinc-800 pt-2 block';
                  }
                  return (
                    <span key={idx} className={lineClass}>
                      {line}
                      {'\n'}
                    </span>
                  );
                })}
              </pre>
            ) : (
              <div className="flex flex-col items-center justify-center h-64 text-zinc-500 space-y-2">
                <Code2 className="h-8 w-8 text-zinc-700" />
                <p className="text-xs">No patch diff generated yet.</p>
                <p className="text-[11px] text-zinc-600">
                  Run an autonomous task to produce an AST-validated patch candidate.
                </p>
              </div>
            )}
          </div>
        )}

        {/* TAB 2: LIVE LOGS STREAM */}
        {activeTab === 'logs' && (
          <div className="flex-1 p-4 font-mono text-xs overflow-auto bg-zinc-950 text-zinc-200 space-y-1">
            {logs.length > 0 ? (
              <>
                {logs.map((log, i) => (
                  <div key={i} className="leading-relaxed flex items-start gap-2">
                    <span className="text-zinc-600 select-none">{String(i + 1).padStart(3, '0')}</span>
                    <span
                      className={
                        log.includes('[Error]')
                          ? 'text-rose-400'
                          : log.includes('[Reviewer]')
                          ? 'text-amber-300'
                          : log.includes('passed') || log.includes('approved')
                          ? 'text-emerald-400'
                          : 'text-zinc-300'
                      }
                    >
                      {log}
                    </span>
                  </div>
                ))}
                <div ref={logsEndRef} />
              </>
            ) : (
              <div className="flex flex-col items-center justify-center h-64 text-zinc-500 space-y-2">
                <Terminal className="h-8 w-8 text-zinc-700" />
                <p className="text-xs">No execution events recorded yet.</p>
              </div>
            )}
          </div>
        )}

        {/* TAB 3: RAG SEARCH & INSPECTION */}
        {activeTab === 'rag' && (
          <div className="flex-1 p-5 space-y-4 overflow-auto bg-zinc-50">
            {/* Search Input Bar */}
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <Search className="absolute left-3.5 top-3 h-4 w-4 text-zinc-400 pointer-events-none" />
                <input
                  type="text"
                  value={ragQuery}
                  onChange={(e) => setRagQuery(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleTestSearch()}
                  placeholder="Search indexed AST functions, methods, classes..."
                  className="w-full pl-10 pr-4 py-2.5 bg-white border border-zinc-200 rounded-xl text-xs font-mono text-zinc-900 focus:outline-none focus:border-zinc-400 focus:ring-1 focus:ring-zinc-300 shadow-2xs"
                />
              </div>
              <button
                type="button"
                disabled={isSearchingRag || !ragQuery.trim()}
                onClick={handleTestSearch}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-zinc-900 hover:bg-zinc-800 text-white text-xs font-bold shadow-xs transition disabled:opacity-50"
              >
                {isSearchingRag ? (
                  <Loader2 className="h-4 w-4 animate-spin text-amber-400" />
                ) : (
                  <Search className="h-4 w-4 text-amber-400" />
                )}
                <span>Test Search</span>
              </button>
            </div>

            {ragSearchError && (
              <div className="p-3 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-800 font-mono">
                Search Error: {ragSearchError}
              </div>
            )}

            {/* Results Display */}
            {ragResults.length > 0 ? (
              <div className="space-y-3">
                <div className="text-xs font-semibold text-zinc-500">
                  Top Reranked Matches ({ragResults.length} chunks via Reciprocal Rank Fusion & FlashRank)
                </div>
                {ragResults.map((chunk, idx) => (
                  <div
                    key={idx}
                    className="bg-white border border-zinc-200 rounded-xl p-4 shadow-2xs space-y-2 hover:border-zinc-300 transition"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs font-bold text-zinc-900">
                          {chunk.symbol_name}
                        </span>
                        <span className="px-2 py-0.5 rounded text-[10px] font-mono bg-zinc-100 text-zinc-700 uppercase">
                          {chunk.symbol_type || 'code'}
                        </span>
                        <span className="text-xs font-mono text-zinc-400">{chunk.file_path}</span>
                      </div>
                      <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-50 text-amber-900 border border-amber-200">
                        Score: {chunk.score.toFixed(3)}
                      </span>
                    </div>

                    <pre className="p-3 rounded-lg bg-zinc-900 text-zinc-200 font-mono text-[11px] overflow-x-auto max-h-48 leading-relaxed">
                      {chunk.content}
                    </pre>

                    {chunk.dependencies && chunk.dependencies.length > 0 && (
                      <div className="pt-1 border-t border-zinc-100">
                        <span className="text-[10px] font-bold uppercase text-zinc-400">1-Hop AST Neighbors:</span>
                        <div className="flex flex-wrap gap-1.5 mt-1">
                          {chunk.dependencies.map((dep, dIdx) => (
                            <span
                              key={dIdx}
                              className="px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-50 text-indigo-800 border border-indigo-200"
                            >
                              {dep.symbol} ({dep.file_path})
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              !isSearchingRag && (
                <div className="text-center py-12 text-zinc-400 text-xs font-mono">
                  Enter a symbol name or query and click &ldquo;Test Search&rdquo; to test RAG retrieval.
                </div>
              )
            )}
          </div>
        )}

        {/* TAB 4: EXECUTION PLAN */}
        {activeTab === 'plan' && (
          <div className="flex-1 p-5 space-y-4 overflow-auto bg-zinc-50">
            {plan ? (
              <div className="space-y-4">
                <div className="bg-white border border-zinc-200 rounded-xl p-4 shadow-2xs space-y-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-500">Target Files</h4>
                  <ul className="list-disc pl-5 font-mono text-xs text-zinc-800 space-y-1">
                    {plan.target_files.map((f, i) => (
                      <li key={i}>{f}</li>
                    ))}
                  </ul>
                </div>

                <div className="bg-white border border-zinc-200 rounded-xl p-4 shadow-2xs space-y-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-500">Logic Steps</h4>
                  <ol className="list-decimal pl-5 text-xs text-zinc-800 space-y-1.5 leading-relaxed">
                    {plan.logic_steps.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ol>
                </div>

                <div className="bg-white border border-zinc-200 rounded-xl p-4 shadow-2xs space-y-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-500">Test Strategy</h4>
                  <p className="text-xs text-zinc-800 leading-relaxed font-mono">{plan.test_strategy}</p>
                </div>
              </div>
            ) : (
              <div className="text-center py-12 text-zinc-400 text-xs">
                No active execution plan generated yet.
              </div>
            )}
          </div>
        )}
      </div>

      {/* REJECT & REQUEST CHANGES FEEDBACK MODAL */}
      {activeModal === 'reject' && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-zinc-950/60 backdrop-blur-xs animate-in fade-in">
          <div className="bg-white rounded-2xl border border-zinc-200 shadow-xl max-w-lg w-full p-6 space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <RotateCcw className="h-5 w-5 text-amber-600" />
                <h3 className="text-base font-bold text-zinc-900">Request Changes</h3>
              </div>
              <button
                type="button"
                onClick={() => setActiveModal(null)}
                className="text-zinc-400 hover:text-zinc-600"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <p className="text-xs text-zinc-600 leading-relaxed">
              Provide feedback or specify required test cases. The LangGraph state machine will resume the repair loop with your instructions.
            </p>

            <form onSubmit={onSubmitReject} className="space-y-4">
              <textarea
                rows={4}
                required
                value={rejectFeedback}
                onChange={(e) => setRejectFeedback(e.target.value)}
                placeholder="E.g., The exponential backoff should handle socket drops during reconnect. Add a pytest case for disconnection timeout."
                className="w-full rounded-xl border border-zinc-200 bg-zinc-50 p-3 text-xs font-mono text-zinc-900 focus:bg-white focus:outline-none focus:ring-2 focus:ring-amber-400/40"
              />

              <div className="flex items-center justify-end gap-2.5">
                <button
                  type="button"
                  onClick={() => setActiveModal(null)}
                  className="px-4 py-2 rounded-xl border border-zinc-200 text-xs font-semibold text-zinc-700 hover:bg-zinc-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isSubmitting || !rejectFeedback.trim()}
                  className="flex items-center gap-2 px-4 py-2 rounded-xl bg-amber-500 hover:bg-amber-600 text-amber-950 text-xs font-bold shadow-xs transition disabled:opacity-50"
                >
                  {isSubmitting ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <ArrowRight className="h-4 w-4" />
                  )}
                  <span>Submit Changes</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* REPOSITORY INGESTION MODAL */}
      {ingestModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-zinc-950/60 backdrop-blur-xs animate-in fade-in">
          <div className="bg-white rounded-2xl border border-zinc-200 shadow-xl max-w-md w-full p-6 space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <FolderGit2 className="h-5 w-5 text-indigo-600" />
                <h3 className="text-base font-bold text-zinc-900">Ingest Code Repository</h3>
              </div>
              <button
                type="button"
                onClick={() => setIngestModalOpen(false)}
                className="text-zinc-400 hover:text-zinc-600"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <p className="text-xs text-zinc-600 leading-relaxed">
              Clone and parse any Git repository via Tree-sitter AST, extract classes, functions, and dependency edges, and generate embeddings for hybrid search.
            </p>

            <form onSubmit={onSubmitIngest} className="space-y-4">
              <div>
                <label className="text-xs font-semibold text-zinc-700 block mb-1">
                  Repository Git URL
                </label>
                <input
                  type="text"
                  required
                  value={ingestUrl}
                  onChange={(e) => setIngestUrl(e.target.value)}
                  placeholder="https://github.com/org/repo.git"
                  className="w-full px-3 py-2 rounded-xl border border-zinc-200 text-xs font-mono text-zinc-900 focus:outline-none focus:ring-2 focus:ring-zinc-400"
                />
              </div>

              <div>
                <label className="text-xs font-semibold text-zinc-700 block mb-1">
                  Target Branch
                </label>
                <input
                  type="text"
                  value={ingestBranch}
                  onChange={(e) => setIngestBranch(e.target.value)}
                  placeholder="main"
                  className="w-full px-3 py-2 rounded-xl border border-zinc-200 text-xs font-mono text-zinc-900 focus:outline-none focus:ring-2 focus:ring-zinc-400"
                />
              </div>

              {ingestModalError && (
                <div className="p-3 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-800 font-mono">
                  {ingestModalError}
                </div>
              )}

              <div className="flex items-center justify-end gap-2.5 pt-2">
                <button
                  type="button"
                  onClick={() => setIngestModalOpen(false)}
                  className="px-4 py-2 rounded-xl border border-zinc-200 text-xs font-semibold text-zinc-700 hover:bg-zinc-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isIngesting || !ingestUrl.trim()}
                  className="flex items-center gap-2 px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-bold shadow-xs transition disabled:opacity-50"
                >
                  {isIngesting ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Download className="h-4 w-4" />
                  )}
                  <span>{isIngesting ? 'Parsing AST Chunks...' : 'Ingest & Index'}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
