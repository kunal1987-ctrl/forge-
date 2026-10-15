import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  FolderGit2,
  GitBranch,
  Database,
  Sparkles,
  Zap,
  CheckCircle2,
  ArrowRight,
  Lock,
  Terminal,
  FileCode,
  Paperclip,
  Mic,
  AlertCircle,
  ExternalLink,
  ChevronRight,
  ShieldCheck,
  Code2,
  Loader2,
  Copy,
  Check,
  Layers,
} from 'lucide-react';
import { apiClient, ApiError } from '../services/apiClient';
import { useTask } from '../context/TaskContext';
import type { ExecutionMode, RepoIngestResponse } from '../types/api';
import { WorkflowTimer } from './WorkflowTimer';
import { WorkflowStepper } from './WorkflowStepper';
import { ExecutionLog } from './ExecutionLog';
import { MetricsCard } from './MetricsCard';
import { AbortButton } from './AbortButton';
import { ExecutionPlanTab } from './ExecutionPlanTab';

export type WizardPhase = 'ingestion' | 'task_input' | 'execution';

export interface ForgeWizardProps {
  /** Optional initial phase override */
  initialPhase?: WizardPhase;
  /** Optional callback fired when task is dispatched */
  onTaskDispatched?: (taskId: string) => void;
  /** Optional callback to navigate to other views */
  onNavigate?: (tab: 'workspace' | 'new' | 'activity' | 'review') => void;
  /** Custom wrapper classes */
  className?: string;
}

/**
 * Preset repository templates for quick demo and onboarding.
 */
const REPO_PRESETS = [
  {
    name: 'fastapi/fastapi',
    url: 'https://github.com/fastapi/fastapi',
    branch: 'main',
    description: 'High-performance Python ASGI web framework',
  },
  {
    name: 'pallets/flask',
    url: 'https://github.com/pallets/flask',
    branch: 'main',
    description: 'Lightweight WSGI Python microframework',
  },
  {
    name: 'psf/requests',
    url: 'https://github.com/psf/requests',
    branch: 'main',
    description: 'Simple, elegant HTTP library for Python',
  },
];

/**
 * Common task objective templates for rapid testing.
 */
const TASK_PRESETS = [
  {
    label: 'Async Race Condition',
    text: 'Fix race condition in async telemetry dispatcher when socket drops during handshake. Add exponential backoff retry buffer and unit tests.',
  },
  {
    label: 'Input Validation',
    text: 'Add strict payload boundary validation for incoming webhook endpoints to reject malformed JSON with HTTP 422 and structured error messages.',
  },
  {
    label: 'Connection Leak',
    text: 'Resolve connection pool resource leak when database transactions timeout during high concurrency. Ensure cleanup in finally block.',
  },
];

export const ForgeWizard: React.FC<ForgeWizardProps> = ({
  initialPhase,
  onTaskDispatched,
  onNavigate,
  className = '',
}) => {
  const {
    taskId,
    status,
    activeStep,
    currentRepo,
    setCurrentRepo,
    startTask,
    resetTask,
    logs,
    currentDiff,
    plan,
    prUrl,
    addToast,
    addLog,
  } = useTask();

  // ---------------------------------------------------------------------------
  // 1. STRICT 3-PHASE VIEW STATE MANAGEMENT
  // ---------------------------------------------------------------------------
  // Determine initial phase:
  // - Phase 3 (execution) if active task exists
  // - Phase 2 (task_input) if repository has already been indexed in context
  // - Phase 1 (ingestion) strictly by default
  const determineDefaultPhase = (): WizardPhase => {
    if (initialPhase) return initialPhase;
    if (taskId && (status === 'RUNNING' || status === 'COMPLETED' || status === 'FAILED' || status === 'ABORTED')) {
      return 'execution';
    }
    if (currentRepo.url && (currentRepo.total_chunks ?? 0) > 0) {
      return 'task_input';
    }
    return 'ingestion';
  };

  const [phase, setPhase] = useState<WizardPhase>(determineDefaultPhase);

  // Sync phase automatically if task lifecycle changes globally
  useEffect(() => {
    if (status === 'RUNNING' && phase !== 'execution') {
      // oxlint-disable-next-line react/set-state-in-effect
      setPhase('execution');
    }
  }, [status, phase]);

  // ---------------------------------------------------------------------------
  // PHASE 1: INGESTION SCREEN STATE (Bound to Persistent Global Store)
  // ---------------------------------------------------------------------------
  const targetRepoUrl = currentRepo.url || 'https://github.com/fastapi/fastapi';
  const targetBranch = currentRepo.branch || 'main';
  const targetChunks = currentRepo.total_chunks || 0;
  const targetId = currentRepo.id || '';

  const [repoUrl, setRepoUrl] = useState<string>(targetRepoUrl);
  const [branch, setBranch] = useState<string>(targetBranch);
  const [customBranch, setCustomBranch] = useState<string>('');
  const [isCustomBranch, setIsCustomBranch] = useState<boolean>(false);
  const [isIngesting, setIsIngesting] = useState<boolean>(false);
  const [ingestionError, setIngestionError] = useState<string | null>(null);
  const [ingestionProgressStep, setIngestionProgressStep] = useState<string>('');

  // ---------------------------------------------------------------------------
  // PHASE 2: TASK INPUT SCREEN STATE
  // ---------------------------------------------------------------------------
  const [prompt, setPrompt] = useState<string>('');
  const [executionMode, setExecutionMode] = useState<ExecutionMode>('fast');
  const [isDispatching, setIsDispatching] = useState<boolean>(false);
  const [dispatchError, setDispatchError] = useState<string | null>(null);

  // AST Symbol count verified from ingestion
  const [indexedSymbolCount, setIndexedSymbolCount] = useState<number>(targetChunks);
  const [indexedRepoId, setIndexedRepoId] = useState<string>(targetId);

  // Synchronize state when global repository updates
  const [prevRepoUrl, setPrevRepoUrl] = useState(targetRepoUrl);
  if (targetRepoUrl !== prevRepoUrl) {
    setPrevRepoUrl(targetRepoUrl);
    setRepoUrl(targetRepoUrl);
  }

  const [prevBranch, setPrevBranch] = useState(targetBranch);
  if (targetBranch !== prevBranch) {
    setPrevBranch(targetBranch);
    setBranch(targetBranch);
  }

  const [prevChunks, setPrevChunks] = useState(targetChunks);
  if (targetChunks !== prevChunks) {
    setPrevChunks(targetChunks);
    setIndexedSymbolCount(targetChunks);
  }

  const [prevId, setPrevId] = useState(targetId);
  if (targetId !== prevId) {
    setPrevId(targetId);
    setIndexedRepoId(targetId);
  }

  // File attachment & Voice dictation helpers
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<string[]>([]);
  const [isListening, setIsListening] = useState<boolean>(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const recognitionRef = useRef<any>(null);

  // ---------------------------------------------------------------------------
  // PHASE 3: EXECUTION DASHBOARD STATE
  // ---------------------------------------------------------------------------
  const [executionTab, setExecutionTab] = useState<'terminal' | 'diff' | 'plan' | 'metrics'>('terminal');
  const [copiedDiff, setCopiedDiff] = useState<boolean>(false);

  // ===========================================================================
  // HANDLERS: PHASE 1 (INGESTION)
  // ===========================================================================
  const handleIngestRepository = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();

    const targetUrl = repoUrl.trim();
    const targetBranch = (isCustomBranch ? customBranch.trim() : branch.trim()) || 'main';

    if (!targetUrl) {
      setIngestionError('Please provide a valid Git repository URL.');
      return;
    }

    if (!targetUrl.startsWith('http://') && !targetUrl.startsWith('https://') && !targetUrl.startsWith('git@')) {
      setIngestionError('Repository URL must begin with https:// or git@');
      return;
    }

    setIsIngesting(true);
    setIngestionError(null);
    setIngestionProgressStep('Cloning repository worktree into container sandbox...');

    try {
      // Step simulation for rich telemetry feedback
      const progressTimer1 = setTimeout(() => {
        setIngestionProgressStep('Running Tree-sitter AST symbol extraction on Python/TS files...');
      }, 1200);

      const progressTimer2 = setTimeout(() => {
        setIngestionProgressStep('Building pgvector semantic embeddings & Graph-RAG symbol graph...');
      }, 2500);

      const res: RepoIngestResponse = await apiClient.ingestRepository({
        repo_url: targetUrl,
        branch: targetBranch,
      });

      clearTimeout(progressTimer1);
      clearTimeout(progressTimer2);

      const repoName = targetUrl.split('/').pop()?.replace('.git', '') || 'repository';
      const symbolCount = res.total_chunks || 0;

      // Update Global State
      setCurrentRepo({
        id: res.repo_id,
        name: repoName,
        url: targetUrl,
        branch: targetBranch,
        total_chunks: symbolCount,
      });

      setIndexedRepoId(res.repo_id);
      setIndexedSymbolCount(symbolCount);

      addToast('success', `Repository indexed: ${symbolCount} AST symbols stored in Graph-RAG!`);
      addLog(`[Wizard] Successfully indexed repository ${repoName} (${res.repo_id}) with ${symbolCount} code chunks.`);

      // Seamlessly transition to Phase 2
      setPhase('task_input');
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Ingestion failed';
      setIngestionError(msg);
      addToast('error', `Repository ingestion error: ${msg}`);
    } finally {
      setIsIngesting(false);
      setIngestionProgressStep('');
    }
  };

  const handleSelectPresetRepo = (preset: typeof REPO_PRESETS[0]) => {
    setRepoUrl(preset.url);
    setBranch(preset.branch);
    setIsCustomBranch(false);
    setIngestionError(null);
  };

  // ===========================================================================
  // HANDLERS: PHASE 2 (TASK OBJECTIVE INPUT)
  // ===========================================================================
  const handleStartRun = useCallback(async () => {
    if (!prompt.trim() || isDispatching) return;

    if (!indexedRepoId && !currentRepo.id) {
      setDispatchError('Missing indexed repository context. Please re-run ingestion.');
      setPhase('ingestion');
      return;
    }

    setIsDispatching(true);
    setDispatchError(null);
    addToast('info', 'Dispatching autonomous workflow to FORGE orchestrator...');

    try {
      const effectiveRepoId = indexedRepoId || currentRepo.id || 'default-repo';
      const spawnedTaskId = await startTask(prompt.trim(), effectiveRepoId, executionMode);

      addToast('success', `Task #${spawnedTaskId.slice(0, 8)} dispatched successfully!`);
      addLog(`[Wizard] Task #${spawnedTaskId} spawned with mode: ${executionMode}`);

      if (onTaskDispatched) {
        onTaskDispatched(spawnedTaskId);
      }

      // Transition to Phase 3: Live Execution Dashboard
      setPhase('execution');
      setExecutionTab('terminal');
    } catch (err: unknown) {
      const msg = err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Dispatch failed';
      setDispatchError(msg);
      addToast('error', `Task dispatch failed: ${msg}`);
    } finally {
      setIsDispatching(false);
    }
  }, [prompt, isDispatching, indexedRepoId, currentRepo.id, executionMode, startTask, addToast, addLog, onTaskDispatched]);

  // Keyboard shortcut: Cmd/Ctrl + Enter to run
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        if (phase === 'task_input') {
          e.preventDefault();
          void handleStartRun();
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [phase, handleStartRun]);

  // Paperclip: Attach file or stack trace
  const handleAttachFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (event) => {
      const content = event.target?.result as string;
      const snippet = `\n\n--- [Attached: ${file.name}] ---\n${content.slice(0, 4000)}${
        content.length > 4000 ? '\n... (truncated)' : ''
      }`;
      setPrompt((prev) => `${prev}${snippet}`);
      setAttachedFiles((prev) => [...prev, file.name]);
      addToast('success', `Attached "${file.name}" to task instructions.`);
    };
    reader.onerror = () => {
      addToast('error', `Failed to read attachment "${file.name}".`);
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  // Voice dictation toggle
  const handleToggleVoice = () => {
    if (isListening) {
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
      setIsListening(false);
      addToast('info', 'Speech recognition stopped.');
      return;
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const windowWithSpeech = window as any;
    const SpeechRecognition =
      windowWithSpeech.SpeechRecognition || windowWithSpeech.webkitSpeechRecognition;

    if (!SpeechRecognition) {
      addToast('error', 'Voice dictation is not supported by this browser.');
      return;
    }

    try {
      const recognition = new SpeechRecognition();
      recognition.lang = 'en-US';
      recognition.continuous = false;
      recognition.interimResults = false;

      recognition.onstart = () => {
        setIsListening(true);
        addToast('info', 'Listening... Describe the task clearly.');
      };

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      recognition.onresult = (event: any) => {
        const transcript = event.results?.[0]?.[0]?.transcript;
        if (transcript) {
          setPrompt((prev) => (prev ? `${prev} ${transcript}` : transcript));
          addToast('success', 'Voice transcript appended to objective.');
        }
      };

      recognition.onerror = () => {
        setIsListening(false);
        addToast('error', 'Voice dictation error.');
      };

      recognition.onend = () => {
        setIsListening(false);
      };

      recognitionRef.current = recognition;
      recognition.start();
    } catch {
      setIsListening(false);
      addToast('error', 'Could not start voice recognition.');
    }
  };

  // ===========================================================================
  // HANDLERS: PHASE 3 (EXECUTION DASHBOARD)
  // ===========================================================================
  const handleCopyDiff = async () => {
    if (!currentDiff) return;
    try {
      await navigator.clipboard.writeText(currentDiff);
      setCopiedDiff(true);
      setTimeout(() => setCopiedDiff(false), 2000);
      addToast('info', 'Unified diff copied to clipboard.');
    } catch {
      setCopiedDiff(false);
    }
  };

  const handleStartNewTaskSameRepo = () => {
    resetTask();
    setPhase('task_input');
    setPrompt('');
  };

  const handleStartNewRepo = () => {
    resetTask();
    setPhase('ingestion');
    setPrompt('');
  };

  // ===========================================================================
  // RENDER HELPERS
  // ===========================================================================
  const isRunning = status === 'RUNNING';
  const isCompleted = status === 'COMPLETED';
  const isFailed = status === 'FAILED';
  const isAborted = status === 'ABORTED';

  return (
    <div className={`w-full mx-auto max-w-5xl space-y-6 pb-12 font-sans ${className}`}>
      {/* Hidden File Input for Paperclip */}
      <input
        type="file"
        ref={fileInputRef}
        onChange={handleAttachFile}
        className="hidden"
        accept=".txt,.log,.py,.ts,.js,.json,.md,.diff,.patch,*"
      />

      {/* =====================================================================
          TOP SEQUENTIAL WIZARD STEPPER (Visual progress through 3 phases)
          ===================================================================== */}
      <nav aria-label="Workflow Wizard Steps" className="bg-white border border-zinc-200/90 rounded-2xl p-3 shadow-xs">
        <ol className="flex items-center justify-between gap-2 sm:gap-4">
          {/* Step 1: Ingestion */}
          <li className="flex-1 flex items-center gap-2">
            <button
              type="button"
              disabled={isRunning || phase === 'ingestion'}
              onClick={() => setPhase('ingestion')}
              className={`group flex items-center gap-2.5 text-left w-full transition ${
                phase === 'ingestion'
                  ? 'opacity-100 cursor-default'
                  : 'opacity-70 hover:opacity-100'
              }`}
            >
              <div
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl font-mono text-xs font-bold transition-all ${
                  phase === 'ingestion'
                    ? 'bg-zinc-950 text-white shadow-sm ring-2 ring-zinc-900/10'
                    : indexedSymbolCount > 0
                    ? 'bg-emerald-600 text-white'
                    : 'bg-zinc-100 text-zinc-600 border border-zinc-200'
                }`}
              >
                {indexedSymbolCount > 0 && phase !== 'ingestion' ? (
                  <Check className="h-4 w-4" />
                ) : (
                  '1'
                )}
              </div>
              <div className="hidden sm:flex flex-col min-w-0">
                <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-900 truncate">
                  1. Repository Ingestion
                </span>
                <span className="text-[11px] text-zinc-500 font-mono truncate">
                  {indexedSymbolCount > 0 ? `${indexedSymbolCount} symbols indexed` : 'Git URL & Branch'}
                </span>
              </div>
            </button>
            <ChevronRight className="h-4 w-4 text-zinc-300 shrink-0 hidden sm:block" />
          </li>

          {/* Step 2: Task Input */}
          <li className="flex-1 flex items-center gap-2">
            <button
              type="button"
              disabled={indexedSymbolCount === 0 || isRunning || phase === 'task_input'}
              onClick={() => setPhase('task_input')}
              className={`group flex items-center gap-2.5 text-left w-full transition ${
                phase === 'task_input'
                  ? 'opacity-100 cursor-default'
                  : indexedSymbolCount > 0
                  ? 'opacity-70 hover:opacity-100'
                  : 'opacity-40 cursor-not-allowed'
              }`}
            >
              <div
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl font-mono text-xs font-bold transition-all ${
                  phase === 'task_input'
                    ? 'bg-zinc-950 text-white shadow-sm ring-2 ring-zinc-900/10'
                    : phase === 'execution'
                    ? 'bg-emerald-600 text-white'
                    : 'bg-zinc-100 text-zinc-400 border border-zinc-200'
                }`}
              >
                {phase === 'execution' ? <Check className="h-4 w-4" /> : '2'}
              </div>
              <div className="hidden sm:flex flex-col min-w-0">
                <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-900 truncate">
                  2. Task Objective
                </span>
                <span className="text-[11px] text-zinc-500 truncate">
                  {indexedSymbolCount > 0 ? 'Spec & Mode Config' : 'Locked (Pending Ingestion)'}
                </span>
              </div>
            </button>
            <ChevronRight className="h-4 w-4 text-zinc-300 shrink-0 hidden sm:block" />
          </li>

          {/* Step 3: Execution Dashboard */}
          <li className="flex-1 flex items-center gap-2">
            <div
              className={`flex items-center gap-2.5 text-left w-full transition ${
                phase === 'execution' ? 'opacity-100' : 'opacity-40'
              }`}
            >
              <div
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl font-mono text-xs font-bold ${
                  phase === 'execution'
                    ? isRunning
                      ? 'bg-emerald-600 text-white animate-pulse'
                      : isCompleted
                      ? 'bg-blue-600 text-white'
                      : isFailed
                      ? 'bg-rose-600 text-white'
                      : 'bg-zinc-950 text-white'
                    : 'bg-zinc-100 text-zinc-400 border border-zinc-200'
                }`}
              >
                {phase === 'execution' && isRunning ? (
                  <Zap className="h-4 w-4" />
                ) : (
                  '3'
                )}
              </div>
              <div className="hidden sm:flex flex-col min-w-0">
                <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-900 truncate">
                  3. Execution Dashboard
                </span>
                <span className="text-[11px] text-zinc-500 font-mono truncate">
                  {phase === 'execution' ? `Status: ${status}` : 'Locked (Pending Dispatch)'}
                </span>
              </div>
            </div>
          </li>
        </ol>
      </nav>

      {/* =====================================================================
          PHASE 1: INGESTION SCREEN (Prominent card, strictly hiding task prompt)
          ===================================================================== */}
      {phase === 'ingestion' && (
        <section
          aria-labelledby="ingestion-screen-heading"
          className="bg-white border border-zinc-200/90 rounded-2xl p-6 sm:p-8 shadow-xs space-y-6 transition-all"
        >
          {/* Header */}
          <div className="flex items-start justify-between gap-4 border-b border-zinc-100 pb-5">
            <div className="space-y-1">
              <div className="inline-flex items-center gap-2 px-2.5 py-1 rounded-full bg-blue-50 border border-blue-200 text-blue-700 text-xs font-semibold">
                <Database className="h-3.5 w-3.5" />
                <span>Phase 1 of 3: Codebase Indexing</span>
              </div>
              <h1 id="ingestion-screen-heading" className="text-xl sm:text-2xl font-bold tracking-tight text-zinc-950">
                Repository Ingestion & AST Indexing
              </h1>
              <p className="text-sm text-zinc-600 max-w-2xl">
                Before FORGE can reason, generate surgical diffs, or execute sandboxed tests, your codebase must be ingested.
                Tree-sitter extracts AST symbols into Postgres and pgvector constructs the Graph-RAG topology.
              </p>
            </div>

            <div className="hidden md:flex flex-col items-end text-xs font-mono text-zinc-400">
              <span className="flex items-center gap-1">
                <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
                Sandboxed Docker Worktrees
              </span>
              <span>pgvector Graph-RAG Active</span>
            </div>
          </div>

          {/* Ingestion Form */}
          <form onSubmit={handleIngestRepository} className="space-y-5">
            {/* Repository Git URL Input */}
            <div className="space-y-2">
              <label htmlFor="repo-git-url" className="block text-xs font-bold uppercase tracking-wider text-zinc-700">
                Repository Git URL <span className="text-rose-500">*</span>
              </label>
              <div className="relative flex items-center">
                <FolderGit2 className="pointer-events-none absolute left-3.5 h-4 w-4 text-zinc-400" />
                <input
                  id="repo-git-url"
                  type="text"
                  required
                  disabled={isIngesting}
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/fastapi/fastapi.git"
                  className="w-full h-11 rounded-xl border border-zinc-300 bg-white pl-10 pr-4 text-sm font-mono text-zinc-900 placeholder-zinc-400 shadow-xs transition hover:border-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 disabled:opacity-50"
                />
              </div>
            </div>

            {/* Target Branch Selector */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="space-y-2">
                <label htmlFor="target-branch-select" className="block text-xs font-bold uppercase tracking-wider text-zinc-700">
                  Target Branch
                </label>
                <div className="relative flex items-center">
                  <GitBranch className="pointer-events-none absolute left-3.5 h-4 w-4 text-zinc-400" />
                  <select
                    id="target-branch-select"
                    disabled={isIngesting}
                    value={isCustomBranch ? 'custom' : branch}
                    onChange={(e) => {
                      if (e.target.value === 'custom') {
                        setIsCustomBranch(true);
                      } else {
                        setIsCustomBranch(false);
                        setBranch(e.target.value);
                      }
                    }}
                    className="w-full h-11 rounded-xl border border-zinc-300 bg-white pl-10 pr-8 text-sm font-mono text-zinc-900 shadow-xs transition hover:border-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 disabled:opacity-50"
                  >
                    <option value="main">main</option>
                    <option value="master">master</option>
                    <option value="dev">dev</option>
                    <option value="staging">staging</option>
                    <option value="custom">Other / Custom Branch...</option>
                  </select>
                </div>
              </div>

              {isCustomBranch && (
                <div className="space-y-2">
                  <label htmlFor="custom-branch-input" className="block text-xs font-bold uppercase tracking-wider text-zinc-700">
                    Custom Branch Name
                  </label>
                  <input
                    id="custom-branch-input"
                    type="text"
                    disabled={isIngesting}
                    value={customBranch}
                    onChange={(e) => setCustomBranch(e.target.value)}
                    placeholder="e.g., feature/telemetry-fix"
                    className="w-full h-11 rounded-xl border border-zinc-300 bg-white px-3.5 text-sm font-mono text-zinc-900 placeholder-zinc-400 shadow-xs transition hover:border-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                  />
                </div>
              )}
            </div>

            {/* Ingestion Error Alert */}
            {ingestionError && (
              <div className="flex items-start gap-3 p-3.5 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 text-xs">
                <AlertCircle className="h-4 w-4 text-rose-600 shrink-0 mt-0.5" />
                <div className="flex-1">
                  <span className="font-semibold">Ingestion Failed:</span> {ingestionError}
                </div>
              </div>
            )}

            {/* Ingestion Active Progress Banner */}
            {isIngesting && (
              <div className="flex items-center gap-3 p-4 rounded-xl bg-blue-50 border border-blue-200 text-blue-900 text-xs shadow-xs animate-pulse">
                <Loader2 className="h-5 w-5 text-blue-600 animate-spin shrink-0" />
                <div className="space-y-0.5 min-w-0">
                  <p className="font-semibold text-blue-950">
                    Cloning & Indexing Codebase...
                  </p>
                  <p className="text-[11px] text-blue-700 font-mono truncate">
                    {ingestionProgressStep || 'Parsing AST and computing vectors...'}
                  </p>
                </div>
              </div>
            )}

            {/* Quick Demo Presets */}
            <div className="space-y-2 pt-2">
              <span className="block text-[11px] font-bold uppercase tracking-wider text-zinc-400">
                Or select a popular open-source repository preset:
              </span>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
                {REPO_PRESETS.map((preset) => (
                  <button
                    key={preset.name}
                    type="button"
                    disabled={isIngesting}
                    onClick={() => handleSelectPresetRepo(preset)}
                    className={`flex flex-col text-left p-3 rounded-xl border transition ${
                      repoUrl === preset.url
                        ? 'border-zinc-900 bg-zinc-50/80 ring-1 ring-zinc-900'
                        : 'border-zinc-200 hover:border-zinc-300 hover:bg-zinc-50'
                    }`}
                  >
                    <span className="text-xs font-bold text-zinc-900 font-mono truncate">
                      {preset.name}
                    </span>
                    <span className="text-[11px] text-zinc-500 mt-0.5 line-clamp-1">
                      {preset.description}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            {/* Primary Action Button */}
            <div className="pt-4 flex flex-col sm:flex-row items-center justify-between gap-3 border-t border-zinc-100">
              <div className="flex items-center gap-2 text-xs text-zinc-500">
                <Lock className="h-3.5 w-3.5 text-zinc-400" />
                <span>Task objective input is locked until repository indexing completes.</span>
              </div>

              <button
                id="btn-ingest-repository"
                type="submit"
                disabled={isIngesting || !repoUrl.trim()}
                className="w-full sm:w-auto flex items-center justify-center gap-2 px-6 py-3 rounded-xl bg-zinc-950 hover:bg-zinc-800 text-white text-xs font-bold tracking-wide shadow-sm transition active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isIngesting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>Indexing Codebase...</span>
                  </>
                ) : (
                  <>
                    <Database className="h-4 w-4" />
                    <span>Ingest & Index Codebase</span>
                    <ArrowRight className="h-3.5 w-3.5 ml-1" />
                  </>
                )}
              </button>
            </div>
          </form>
        </section>
      )}

      {/* =====================================================================
          PHASE 2: TASK INPUT SCREEN (Unlocked once ingestion returns success)
          ===================================================================== */}
      {phase === 'task_input' && (
        <section
          aria-labelledby="task-input-screen-heading"
          className="bg-white border border-zinc-200/90 rounded-2xl p-6 sm:p-8 shadow-xs space-y-6 transition-all"
        >
          {/* Header & Ingestion Success Banner */}
          <div className="space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-950 shadow-xs">
              <div className="flex items-center gap-3 min-w-0">
                <div className="h-9 w-9 rounded-xl bg-emerald-600 text-white flex items-center justify-center shrink-0 font-bold shadow-xs">
                  <Check className="h-5 w-5" />
                </div>
                <div className="space-y-0.5 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-bold text-sm text-emerald-950 truncate">
                      {currentRepo.name || currentRepo.url?.split('/').pop()?.replace('.git', '') || 'Repository'}
                    </span>
                    <span className="font-mono text-xs text-emerald-700 bg-emerald-100/80 px-2 py-0.5 rounded-md border border-emerald-300">
                      branch: {currentRepo.branch || branch}
                    </span>
                  </div>
                  <p className="text-xs text-emerald-800 font-mono">
                    ✓ Indexed {indexedSymbolCount || currentRepo.total_chunks || 0} AST code symbols into Graph-RAG vector space
                  </p>
                </div>
              </div>

              <button
                type="button"
                onClick={() => setPhase('ingestion')}
                className="self-start sm:self-auto px-3 py-1.5 rounded-lg border border-emerald-300 bg-white hover:bg-emerald-100/50 text-emerald-900 text-xs font-semibold shadow-xs transition"
              >
                Change Repository
              </button>
            </div>

            <div className="border-b border-zinc-100 pb-4">
              <div className="inline-flex items-center gap-2 px-2.5 py-1 rounded-full bg-zinc-100 border border-zinc-200 text-zinc-800 text-xs font-semibold mb-2">
                <Sparkles className="h-3.5 w-3.5 text-zinc-600" />
                <span>Phase 2 of 3: Define Objective</span>
              </div>
              <h2 id="task-input-screen-heading" className="text-xl sm:text-2xl font-bold tracking-tight text-zinc-950">
                Task Objective & Execution Parameters
              </h2>
              <p className="text-sm text-zinc-600">
                Describe the bug, feature, or refactoring task. FORGE multi-agent pipeline will formulate a surgical execution plan,
                retrieve Graph-RAG context, synthesize a unified patch, and run containerized verification tests.
              </p>
            </div>
          </div>

          {/* Quick Objective Presets */}
          <div className="space-y-1.5">
            <span className="text-[11px] font-bold uppercase tracking-wider text-zinc-400">
              Quick Task Templates:
            </span>
            <div className="flex items-center gap-2 flex-wrap">
              {TASK_PRESETS.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  onClick={() => setPrompt(preset.text)}
                  className="px-2.5 py-1 rounded-lg border border-zinc-200 bg-zinc-50 hover:bg-zinc-100 hover:border-zinc-300 text-xs font-medium text-zinc-700 transition"
                >
                  {preset.label}
                </button>
              ))}
            </div>
          </div>

          {/* Task Objective Textarea */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label htmlFor="task-prompt-textarea" className="text-xs font-bold uppercase tracking-wider text-zinc-700">
                Bug Description / Task Objective <span className="text-rose-500">*</span>
              </label>

              {/* Utility Tools: Paperclip & Mic */}
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-zinc-200 bg-white hover:bg-zinc-100 text-xs text-zinc-600 transition"
                  title="Attach Stack Trace or Log Snippet"
                >
                  <Paperclip className="h-3.5 w-3.5 text-zinc-500" />
                  <span>Attach Trace</span>
                </button>

                <button
                  type="button"
                  onClick={handleToggleVoice}
                  className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border text-xs transition ${
                    isListening
                      ? 'bg-rose-50 border-rose-300 text-rose-700 animate-pulse'
                      : 'border-zinc-200 bg-white hover:bg-zinc-100 text-zinc-600'
                  }`}
                  title="Voice Dictation"
                >
                  <Mic className="h-3.5 w-3.5 text-zinc-500" />
                  <span>{isListening ? 'Listening...' : 'Voice Dictate'}</span>
                </button>
              </div>
            </div>

            <div className="relative">
              <textarea
                id="task-prompt-textarea"
                rows={5}
                required
                disabled={isDispatching}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="Describe the issue in detail, specify target files if known, or paste error trace..."
                className="w-full rounded-xl border border-zinc-300 bg-white p-4 font-mono text-xs sm:text-sm text-zinc-900 placeholder-zinc-400 shadow-xs transition hover:border-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 disabled:opacity-50"
              />
              <div className="absolute right-3 bottom-3 flex items-center gap-2 text-[10px] text-zinc-400 font-mono select-none">
                <span>{prompt.length} chars</span>
                <span>•</span>
                <kbd className="px-1.5 py-0.5 rounded bg-zinc-100 border border-zinc-200 text-zinc-600">
                  Cmd/Ctrl + Enter
                </kbd>
              </div>
            </div>

            {attachedFiles.length > 0 && (
              <div className="flex items-center gap-2 pt-1 flex-wrap">
                <span className="text-[11px] font-medium text-zinc-500">Attached files:</span>
                {attachedFiles.map((fn, idx) => (
                  <span
                    key={`${fn}-${idx}`}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-blue-50 border border-blue-200 text-blue-700 font-mono text-[11px]"
                  >
                    <FileCode className="h-3 w-3" />
                    {fn}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Execution Mode Selector */}
          <div className="space-y-2">
            <span className="block text-xs font-bold uppercase tracking-wider text-zinc-700">
              Agent Execution Mode
            </span>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <button
                type="button"
                onClick={() => setExecutionMode('fast')}
                className={`flex items-start gap-3 p-3.5 rounded-xl border text-left transition ${
                  executionMode === 'fast'
                    ? 'border-zinc-900 bg-zinc-50/80 ring-1 ring-zinc-900'
                    : 'border-zinc-200 hover:border-zinc-300 hover:bg-zinc-50'
                }`}
              >
                <div className="h-8 w-8 rounded-lg bg-emerald-100 text-emerald-800 flex items-center justify-center shrink-0 font-bold">
                  <Zap className="h-4 w-4" />
                </div>
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-bold text-zinc-900">Standard / Fast Mode</span>
                    <span className="text-[10px] uppercase font-mono px-1.5 py-0.2 rounded bg-emerald-100 text-emerald-800 font-semibold">
                      Recommended
                    </span>
                  </div>
                  <p className="text-[11px] text-zinc-500">
                    Direct AST retrieval, single reproduction test gate, and rapid patch synthesis. Ideal for standard bug fixes.
                  </p>
                </div>
              </button>

              <button
                type="button"
                onClick={() => setExecutionMode('deep')}
                className={`flex items-start gap-3 p-3.5 rounded-xl border text-left transition ${
                  executionMode === 'deep'
                    ? 'border-zinc-900 bg-zinc-50/80 ring-1 ring-zinc-900'
                    : 'border-zinc-200 hover:border-zinc-300 hover:bg-zinc-50'
                }`}
              >
                <div className="h-8 w-8 rounded-lg bg-blue-100 text-blue-800 flex items-center justify-center shrink-0 font-bold">
                  <Layers className="h-4 w-4" />
                </div>
                <div className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-bold text-zinc-900">Deep / Audit Mode</span>
                    <span className="text-[10px] uppercase font-mono px-1.5 py-0.2 rounded bg-blue-100 text-blue-800 font-semibold">
                      Comprehensive
                    </span>
                  </div>
                  <p className="text-[11px] text-zinc-500">
                    Multi-hop Graph-RAG dependency tracing, extensive retry verifier loop (up to 3 iterations), and deep static analysis.
                  </p>
                </div>
              </button>
            </div>
          </div>

          {/* Dispatch Error Alert */}
          {dispatchError && (
            <div className="flex items-start gap-3 p-3.5 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 text-xs">
              <AlertCircle className="h-4 w-4 text-rose-600 shrink-0 mt-0.5" />
              <div className="flex-1">
                <span className="font-semibold">Dispatch Failed:</span> {dispatchError}
              </div>
            </div>
          )}

          {/* Bottom Action Footer */}
          <div className="pt-4 flex flex-col sm:flex-row items-center justify-between gap-3 border-t border-zinc-100">
            <button
              type="button"
              onClick={() => setPhase('ingestion')}
              className="w-full sm:w-auto px-4 py-2.5 rounded-xl border border-zinc-200 hover:bg-zinc-50 text-xs font-semibold text-zinc-700 transition"
            >
              ← Back to Ingestion
            </button>

            <button
              id="btn-start-autonomous-run"
              type="button"
              disabled={isDispatching || !prompt.trim()}
              onClick={() => void handleStartRun()}
              className="w-full sm:w-auto flex items-center justify-center gap-2 px-7 py-3 rounded-xl bg-zinc-950 hover:bg-zinc-800 text-white text-xs font-bold tracking-wide shadow-md transition active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isDispatching ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>Dispatching Workflow...</span>
                </>
              ) : (
                <>
                  <Sparkles className="h-4 w-4 text-amber-300" />
                  <span>Start Autonomous Run</span>
                  <ArrowRight className="h-3.5 w-3.5 ml-1" />
                </>
              )}
            </button>
          </div>
        </section>
      )}

      {/* =====================================================================
          PHASE 3: EXECUTION DASHBOARD (Locked inputs, live workflow progress)
          ===================================================================== */}
      {phase === 'execution' && (
        <section
          aria-labelledby="execution-dashboard-heading"
          className="bg-white border border-zinc-200/90 rounded-2xl p-6 sm:p-8 shadow-xs space-y-6 transition-all"
        >
          {/* Locked Inputs & Telemetry Banner */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-zinc-100 pb-5">
            <div className="space-y-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-zinc-100 border border-zinc-200 text-zinc-800 text-xs font-bold font-mono">
                  <Lock className="h-3 w-3 text-zinc-500" />
                  Inputs Locked
                </span>

                {taskId && (
                  <span className="font-mono text-xs text-zinc-600 bg-zinc-50 border border-zinc-200 px-2 py-0.5 rounded-full">
                    Task #{taskId.slice(0, 8)}
                  </span>
                )}

                <span className="font-mono text-xs text-zinc-500 truncate max-w-[200px]">
                  repo: {currentRepo.name || currentRepo.id}
                </span>

                {activeStep && isRunning && (
                  <span className="font-mono text-xs text-blue-700 bg-blue-50 border border-blue-200 px-2 py-0.5 rounded-full">
                    node: {activeStep}
                  </span>
                )}
              </div>

              <h2 id="execution-dashboard-heading" className="text-xl sm:text-2xl font-bold tracking-tight text-zinc-950 truncate">
                {isRunning
                  ? 'Autonomous Execution in Progress'
                  : isCompleted
                  ? 'Workflow Completed Successfully'
                  : isFailed
                  ? 'Execution Failed'
                  : isAborted
                  ? 'Workflow Aborted'
                  : 'Orchestrating Workflow'}
              </h2>
            </div>

            {/* Live Timer and Abort Controls */}
            <div className="flex items-center gap-3 shrink-0">
              <WorkflowTimer size="md" variant="pill" showStatusBadge={true} />

              {isRunning && (
                <AbortButton
                  taskId={taskId}
                  variant="danger"
                  size="md"
                  label="Abort"
                />
              )}
            </div>
          </div>

          {/* Locked Task Objective Summary Card */}
          <div className="p-4 rounded-xl bg-zinc-50 border border-zinc-200 text-xs font-mono space-y-2">
            <div className="flex items-center justify-between text-zinc-500">
              <span className="font-bold uppercase tracking-wider text-[10px]">Active Objective Spec:</span>
              <span className="text-[10px] capitalize">Mode: {executionMode}</span>
            </div>
            <p className="text-zinc-800 font-sans text-xs leading-relaxed line-clamp-3">
              {prompt}
            </p>
          </div>

          {/* Live Workflow Stepper Pipeline */}
          <div className="space-y-2">
            <span className="block text-xs font-bold uppercase tracking-wider text-zinc-500">
              Orchestration Node Pipeline:
            </span>
            <WorkflowStepper showDescriptions={true} />
          </div>

          {/* Metrics Summary Card (Diff size, latency delta, confidence) */}
          <MetricsCard />

          {/* Multi-Tab Telemetry Inspector (Terminal Logs, Unified Diff, Plan) */}
          <div className="border border-zinc-200 rounded-xl overflow-hidden shadow-xs">
            {/* Tabs Header */}
            <div className="flex items-center justify-between bg-zinc-100/80 border-b border-zinc-200 px-3 py-2">
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setExecutionTab('terminal')}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                    executionTab === 'terminal'
                      ? 'bg-white text-zinc-950 shadow-xs border border-zinc-200/80'
                      : 'text-zinc-600 hover:text-zinc-900'
                  }`}
                >
                  <Terminal className="h-3.5 w-3.5" />
                  <span>Execution Logs ({logs.length})</span>
                </button>

                <button
                  type="button"
                  onClick={() => setExecutionTab('diff')}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                    executionTab === 'diff'
                      ? 'bg-white text-zinc-950 shadow-xs border border-zinc-200/80'
                      : 'text-zinc-600 hover:text-zinc-900'
                  }`}
                >
                  <Code2 className="h-3.5 w-3.5" />
                  <span>Unified Diff {currentDiff ? '✓' : ''}</span>
                </button>

                <button
                  type="button"
                  onClick={() => setExecutionTab('plan')}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                    executionTab === 'plan'
                      ? 'bg-white text-zinc-950 shadow-xs border border-zinc-200/80'
                      : 'text-zinc-600 hover:text-zinc-900'
                  }`}
                >
                  <FileCode className="h-3.5 w-3.5" />
                  <span>Plan Spec {plan ? '✓' : ''}</span>
                </button>
              </div>

              {executionTab === 'diff' && currentDiff && (
                <button
                  type="button"
                  onClick={() => void handleCopyDiff()}
                  className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md bg-white border border-zinc-200 hover:bg-zinc-50 text-[11px] font-medium text-zinc-700 shadow-xs transition"
                >
                  {copiedDiff ? <Check className="h-3.5 w-3.5 text-emerald-600" /> : <Copy className="h-3.5 w-3.5" />}
                  <span>{copiedDiff ? 'Copied' : 'Copy Patch'}</span>
                </button>
              )}
            </div>

            {/* Tab 1: Terminal Logs */}
            {executionTab === 'terminal' && (
              <div className="bg-zinc-950">
                <ExecutionLog
                  logs={logs}
                  status={status}
                  maxHeight="20rem"
                  className="border-0 rounded-none shadow-none"
                />
              </div>
            )}

            {/* Tab 2: Unified Diff */}
            {executionTab === 'diff' && (
              <div className="bg-zinc-900 text-zinc-100 p-4 font-mono text-xs max-h-80 overflow-y-auto">
                {currentDiff ? (
                  <pre className="whitespace-pre-wrap leading-relaxed">
                    {currentDiff.split('\n').map((line, idx) => {
                      let colorClass = 'text-zinc-300';
                      let bgClass = '';
                      if (line.startsWith('+') && !line.startsWith('+++')) {
                        colorClass = 'text-emerald-400';
                        bgClass = 'bg-emerald-950/40';
                      } else if (line.startsWith('-') && !line.startsWith('---')) {
                        colorClass = 'text-rose-400';
                        bgClass = 'bg-rose-950/40';
                      } else if (line.startsWith('@@')) {
                        colorClass = 'text-cyan-400 font-bold';
                      }
                      return (
                        <div key={idx} className={`${colorClass} ${bgClass} px-1 rounded-xs`}>
                          {line}
                        </div>
                      );
                    })}
                  </pre>
                ) : (
                  <div className="text-zinc-500 py-6 text-center italic">
                    Awaiting patch generation from implementer node...
                  </div>
                )}
              </div>
            )}

            {/* Tab 3: Execution Plan */}
            {executionTab === 'plan' && (
              <div className="p-4 bg-zinc-50 overflow-y-auto max-h-96">
                <ExecutionPlanTab isRunning={isRunning} activeStep={activeStep} />
              </div>
            )}
          </div>

          {/* PR Created Banner if complete */}
          {prUrl && (
            <div className="flex items-center justify-between gap-4 p-4 rounded-xl bg-emerald-50 border border-emerald-300 text-emerald-950">
              <div className="flex items-center gap-3">
                <CheckCircle2 className="h-6 w-6 text-emerald-600 shrink-0" />
                <div className="space-y-0.5">
                  <p className="text-xs font-bold text-emerald-950">
                    GitHub Pull Request Ready & Verified
                  </p>
                  <p className="text-[11px] text-emerald-800 font-mono">
                    All unit tests passed within sandbox and branch cleanly pushed.
                  </p>
                </div>
              </div>

              <a
                href={prUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold shadow-xs transition"
              >
                <span>Open Pull Request</span>
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            </div>
          )}

          {/* Post-Execution Actions */}
          <div className="pt-4 flex flex-col sm:flex-row items-center justify-between gap-3 border-t border-zinc-100">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleStartNewRepo}
                className="px-4 py-2 rounded-xl border border-zinc-200 hover:bg-zinc-50 text-xs font-semibold text-zinc-700 transition"
              >
                Ingest Different Repo
              </button>

              <button
                type="button"
                onClick={handleStartNewTaskSameRepo}
                className="px-4 py-2 rounded-xl border border-zinc-200 hover:bg-zinc-50 text-xs font-semibold text-zinc-700 transition"
              >
                New Task (Same Repo)
              </button>
            </div>

            {onNavigate && (
              <button
                type="button"
                onClick={() => onNavigate('review')}
                className="w-full sm:w-auto px-5 py-2.5 rounded-xl bg-zinc-950 hover:bg-zinc-800 text-white text-xs font-bold shadow-sm transition"
              >
                Navigate to Review Gate →
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
};

export default ForgeWizard;
