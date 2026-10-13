import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Sparkles,
  FolderCode,
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
  FolderGit2,
  Download,
  X,
  AlertCircle,
} from 'lucide-react';
import { apiClient } from '../services/apiClient';
import { EngineStatusBadge } from './EngineStatusBadge';

interface NewTaskProps {
  onStartRun?: (taskId?: string) => void;
  onNavigate?: (tab: 'workspace' | 'new' | 'activity' | 'review') => void;
  initialBranch?: string;
}

export const NewTask: React.FC<NewTaskProps> = ({
  onStartRun,
  onNavigate,
  initialBranch = 'main',
}) => {
  const [prompt, setPrompt] = useState<string>(
    'Fix race condition in async telemetry dispatcher when socket drops during handshake. Add exponential backoff retry buffer and unit tests.'
  );
  const [mode, setMode] = useState<'standard' | 'audit'>('standard');
  const [runState, setRunState] = useState<'idle' | 'spawning' | 'dispatched'>('idle');

  // Interactive Target Branch Selector State
  const [selectedBranch, setSelectedBranch] = useState<string>(initialBranch);
  const [isBranchMenuOpen, setIsBranchMenuOpen] = useState<boolean>(false);
  const [customBranchInput, setCustomBranchInput] = useState<string>('');
  const [isAddingCustomBranch, setIsAddingCustomBranch] = useState<boolean>(false);

  // Repository & Ingestion State
  const [repoInput, setRepoInput] = useState<string>('https://github.com/fastapi/fastapi');
  const [repoId, setRepoId] = useState<string>('default-repo');
  const [isIngestModalOpen, setIsIngestModalOpen] = useState<boolean>(false);
  const [isIngesting, setIsIngesting] = useState<boolean>(false);
  const [ingestStats, setIngestStats] = useState<{ chunks: number; repoId: string } | null>(null);

  // Stack Trace Attachment State
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<string[]>([]);

  // Speech Dictation State
  const [isListening, setIsListening] = useState<boolean>(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const recognitionRef = useRef<any>(null);

  // Toast / Feedback State
  const [feedbackNotice, setFeedbackNotice] = useState<{
    type: 'success' | 'error' | 'info';
    message: string;
  } | null>(null);

  const showNotice = (type: 'success' | 'error' | 'info', message: string) => {
    setFeedbackNotice({ type, message });
    setTimeout(() => {
      setFeedbackNotice(null);
    }, 4500);
  };

  // 1. PAPERCLIP HANDLER: Attach file or stack trace
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
      showNotice('success', `Attached "${file.name}" to task instructions.`);
    };
    reader.onerror = () => {
      showNotice('error', `Failed to read file "${file.name}".`);
    };
    reader.readAsText(file);
    // Reset file input so re-selecting same file triggers onChange
    e.target.value = '';
  };

  // 2. MICROPHONE HANDLER: Native Speech Recognition with fallback
  const handleToggleVoiceDictation = () => {
    if (isListening) {
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
      setIsListening(false);
      showNotice('info', 'Speech dictation paused.');
      return;
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const windowWithSpeech = window as any;
    const SpeechRecognition =
      windowWithSpeech.SpeechRecognition || windowWithSpeech.webkitSpeechRecognition;

    if (!SpeechRecognition) {
      // Browser fallback simulation
      showNotice(
        'info',
        'SpeechRecognition API not available in current browser engine. Voice dictation simulated.'
      );
      setPrompt((prev) => `${prev} [Voice Dictation: Ensure socket timeout handler triggers reconnection with exponential backoff.]`);
      return;
    }

    try {
      const recognition = new SpeechRecognition();
      recognition.lang = 'en-US';
      recognition.continuous = false;
      recognition.interimResults = false;

      recognition.onstart = () => {
        setIsListening(true);
        showNotice('info', 'Listening... Speak your task requirements clearly.');
      };

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      recognition.onresult = (event: any) => {
        const transcript = event.results?.[0]?.[0]?.transcript;
        if (transcript) {
          setPrompt((prev) => `${prev} ${transcript}`);
          showNotice('success', `Transcribed: "${transcript}"`);
        }
      };

      recognition.onerror = () => {
        setIsListening(false);
        showNotice('error', 'Voice dictation encountered an error or was denied microphone permission.');
      };

      recognition.onend = () => {
        setIsListening(false);
      };

      recognitionRef.current = recognition;
      recognition.start();
    } catch {
      setIsListening(false);
      showNotice('error', 'Could not access microphone.');
    }
  };

  // 3. INGESTION HANDLER: Clones & parses repository
  const handleIngestSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!repoInput.trim() || isIngesting) return;

    setIsIngesting(true);
    showNotice('info', `Ingesting and parsing AST symbols for ${repoInput}...`);
    try {
      const res = await apiClient.ingestRepository({
        repo_url: repoInput.trim(),
        branch: selectedBranch,
      });
      setRepoId(res.repo_id);
      setIngestStats({ chunks: res.total_chunks, repoId: res.repo_id });
      setIsIngestModalOpen(false);
      showNotice('success', `Repository successfully indexed! Extracted ${res.total_chunks} symbols into Graph-RAG.`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Ingestion failed';
      showNotice('error', `Repository ingestion error: ${msg}`);
    } finally {
      setIsIngesting(false);
    }
  };

  // 4. TASK DISPATCH HANDLER: Real task creation
  const handleStartRun = useCallback(async () => {
    if (!prompt.trim() || runState !== 'idle') return;

    setRunState('spawning');
    showNotice('info', 'Dispatching autonomous task to FORGE backend...');

    try {
      const res = await apiClient.createTask({
        prompt: prompt.trim(),
        repo_id: repoId,
        mode: mode === 'audit' ? 'deep' : 'fast',
      });

      setRunState('dispatched');
      showNotice('success', `Task ${res.task_id.slice(0, 8)} accepted by orchestrator.`);

      if (onStartRun) {
        onStartRun(res.task_id);
      }

      setTimeout(() => {
        setRunState('idle');
        if (onNavigate) {
          onNavigate('workspace');
        }
      }, 900);
    } catch (err: unknown) {
      setRunState('idle');
      const msg = err instanceof Error ? err.message : 'Failed to create task';
      showNotice('error', `Task dispatch failed: ${msg}`);
    }
  }, [prompt, runState, repoId, mode, onStartRun, onNavigate]);

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
    <div className="flex flex-col w-full pb-8 space-y-4 font-sans">
      {/* Hidden File Input for Paperclip */}
      <input
        type="file"
        ref={fileInputRef}
        onChange={handleFileChange}
        className="hidden"
        accept=".txt,.log,.py,.ts,.js,.json,.md,.diff,.patch,*"
      />

      {/* Floating In-Component Notification */}
      {feedbackNotice && (
        <div
          className={`flex items-center justify-between gap-3 p-3 rounded-xl border text-xs font-medium shadow-md transition-all animate-in fade-in slide-in-from-top-2 ${
            feedbackNotice.type === 'success'
              ? 'bg-emerald-50 border-emerald-300 text-emerald-900'
              : feedbackNotice.type === 'error'
              ? 'bg-rose-50 border-rose-300 text-rose-900'
              : 'bg-zinc-900 border-zinc-800 text-zinc-100'
          }`}
        >
          <div className="flex items-center gap-2">
            {feedbackNotice.type === 'success' && <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />}
            {feedbackNotice.type === 'error' && <AlertCircle className="h-4 w-4 text-rose-500 shrink-0" />}
            {feedbackNotice.type === 'info' && <Database className="h-4 w-4 text-amber-400 shrink-0" />}
            <span>{feedbackNotice.message}</span>
          </div>
          <button
            onClick={() => setFeedbackNotice(null)}
            className="text-zinc-400 hover:text-zinc-600 transition"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

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
      <div className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-xs flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-medium">
            Repository
          </span>
          <div className="flex items-center gap-2">
            {ingestStats && (
              <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200/60 font-mono text-[11px]">
                <Check className="w-3 h-3 text-emerald-600 stroke-[3]" />
                {ingestStats.chunks} symbols indexed
              </span>
            )}
            <EngineStatusBadge repoId={repoId !== 'default-repo' ? repoId : undefined} />
          </div>
        </div>

        <div className="flex items-center gap-2 bg-zinc-50 border border-zinc-200/80 px-3 py-2 rounded-lg focus-within:bg-white focus-within:border-zinc-400 transition">
          <FolderCode className="h-4 w-4 text-zinc-500 shrink-0" />
          <input
            className="bg-transparent font-mono text-xs text-zinc-800 w-full focus:outline-none"
            type="text"
            value={repoInput}
            onChange={(e) => setRepoInput(e.target.value)}
            placeholder="e.g. fastapi/fastapi or https://github.com/org/repo"
          />
          <button
            type="button"
            onClick={() => setIsIngestModalOpen(true)}
            className="px-2 py-1 rounded-md text-[11px] font-semibold bg-white hover:bg-zinc-100 border border-zinc-200 text-zinc-700 shadow-2xs transition shrink-0"
          >
            Ingest Repo
          </button>
        </div>

        {/* Target Branch Selector Row */}
        <div className="flex items-center justify-between pt-0.5 relative">
          <span className="font-mono text-[11px] text-zinc-500 uppercase tracking-wider font-medium">
            Target Branch
          </span>

          <div className="relative">
            <button
              type="button"
              onClick={() => setIsBranchMenuOpen((prev) => !prev)}
              className="inline-flex items-center gap-1.5 bg-zinc-100 hover:bg-zinc-200/70 border border-zinc-200 px-2.5 py-1 rounded-lg text-zinc-800 transition-colors shadow-2xs"
            >
              <GitBranch className="h-3.5 w-3.5 text-emerald-600" />
              <span className="font-mono text-xs font-semibold">{selectedBranch}</span>
              <ChevronDown className="h-3.5 w-3.5 text-zinc-500" />
            </button>

            {/* Interactive Branch Dropdown Menu */}
            {isBranchMenuOpen && (
              <div className="absolute right-0 top-9 z-30 w-48 bg-white border border-zinc-200 rounded-xl shadow-lg p-1.5 space-y-1 animate-in fade-in slide-in-from-top-1 font-mono text-xs">
                {['main', 'dev', 'staging', 'feature/autonomous-fix'].map((b) => (
                  <button
                    key={b}
                    type="button"
                    onClick={() => {
                      setSelectedBranch(b);
                      setIsBranchMenuOpen(false);
                      showNotice('info', `Switched target branch to "${b}".`);
                    }}
                    className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg text-left transition ${
                      selectedBranch === b
                        ? 'bg-zinc-100 text-zinc-950 font-bold'
                        : 'text-zinc-600 hover:bg-zinc-50 hover:text-zinc-900'
                    }`}
                  >
                    <span>{b}</span>
                    {selectedBranch === b && <Check className="h-3.5 w-3.5 text-emerald-600" />}
                  </button>
                ))}

                <div className="border-t border-zinc-100 pt-1">
                  {!isAddingCustomBranch ? (
                    <button
                      type="button"
                      onClick={() => setIsAddingCustomBranch(true)}
                      className="w-full text-left px-2.5 py-1 text-[11px] text-indigo-600 hover:text-indigo-800 font-semibold"
                    >
                      + Custom branch...
                    </button>
                  ) : (
                    <div className="flex items-center gap-1 px-1 py-0.5">
                      <input
                        type="text"
                        value={customBranchInput}
                        onChange={(e) => setCustomBranchInput(e.target.value)}
                        placeholder="branch-name"
                        className="w-full px-1.5 py-1 text-xs border rounded bg-zinc-50 focus:outline-none"
                      />
                      <button
                        type="button"
                        onClick={() => {
                          if (customBranchInput.trim()) {
                            setSelectedBranch(customBranchInput.trim());
                            setIsBranchMenuOpen(false);
                            setIsAddingCustomBranch(false);
                            setCustomBranchInput('');
                          }
                        }}
                        className="px-2 py-1 bg-zinc-900 text-white rounded text-[10px]"
                      >
                        Set
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Task Objective / Prompt Card */}
      <div className="bg-white rounded-xl p-4 border border-zinc-200/80 shadow-xs">
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
            className="w-full bg-transparent text-sm text-zinc-900 placeholder:text-zinc-400 resize-none focus:outline-none min-h-[80px]"
            id="task-prompt"
            placeholder="Describe issue or paste error log..."
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />

          <div className="flex items-center justify-between mt-3 pt-2.5 border-t border-zinc-200/60">
            <div className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-zinc-200/60 text-zinc-700 text-xs font-mono">
              <FolderCode className="h-3 w-3 text-zinc-500" />
              <span>
                {attachedFiles.length > 0
                  ? `${attachedFiles.length} file(s) attached`
                  : 'Context symbols active'}
              </span>
            </div>

            {/* ATTACHMENT & MICROPHONE BUTTONS */}
            <div className="flex items-center gap-1.5 text-zinc-500">
              {/* Paperclip Button */}
              <button
                type="button"
                onClick={handleAttachClick}
                className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-zinc-200/80 hover:text-zinc-900 transition-colors border border-transparent hover:border-zinc-300"
                title="Attach Stack Trace or Code File"
                aria-label="Attach File"
              >
                <Paperclip className="h-4 w-4" />
              </button>

              {/* Microphone Speech Dictation Button */}
              <button
                type="button"
                onClick={handleToggleVoiceDictation}
                className={`w-8 h-8 rounded-lg flex items-center justify-center transition-all border ${
                  isListening
                    ? 'bg-rose-50 text-rose-600 border-rose-300 animate-pulse ring-2 ring-rose-400/30'
                    : 'hover:bg-zinc-200/80 hover:text-zinc-900 border-transparent hover:border-zinc-300'
                }`}
                title={isListening ? 'Stop Listening' : 'Voice Dictation (Speech to Text)'}
                aria-label="Speech Dictation"
              >
                <Mic className="h-4 w-4" />
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
          {/* Strategy Option 1: Standard (Fast) */}
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
                  ⚡ gpt-4o-mini
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
                  🧠 DeepSeek R1
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
        disabled={runState !== 'idle' || !prompt.trim()}
        className="w-full h-11 bg-zinc-900 hover:bg-zinc-800 disabled:opacity-50 text-white rounded-xl flex items-center justify-center gap-2 shadow-md active:scale-[0.99] transition-all font-medium text-sm"
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
            <span>Spawning Container &amp; Dispatching...</span>
          </>
        )}
        {runState === 'dispatched' && (
          <>
            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
            <span>Dispatched to Workspace</span>
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
          <span
            onClick={() => onNavigate?.('activity')}
            className="font-mono text-xs text-zinc-900 font-medium hover:underline cursor-pointer"
          >
            View Live Stream
          </span>
        </div>

        <div className="flex flex-col gap-2">
          {/* Item 1 */}
          <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-2xs flex items-center justify-between gap-3">
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
          <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-2xs flex items-center justify-between gap-3">
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
          <div className="bg-white rounded-xl p-3 border border-zinc-200/80 shadow-2xs flex items-center justify-between gap-3">
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

      {/* REPOSITORY INGESTION MODAL */}
      {isIngestModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-zinc-950/60 backdrop-blur-xs animate-in fade-in">
          <div className="bg-white rounded-2xl border border-zinc-200 shadow-xl max-w-md w-full p-6 space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <FolderGit2 className="h-5 w-5 text-indigo-600" />
                <h3 className="text-base font-bold text-zinc-900">Ingest Code Repository</h3>
              </div>
              <button
                type="button"
                onClick={() => setIsIngestModalOpen(false)}
                className="text-zinc-400 hover:text-zinc-600"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <p className="text-xs text-zinc-600 leading-relaxed">
              Clone and parse any Git repository via Tree-sitter AST, extract classes, functions, and dependency edges, and generate embeddings for hybrid search.
            </p>

            <form onSubmit={handleIngestSubmit} className="space-y-4">
              <div>
                <label className="text-xs font-semibold text-zinc-700 block mb-1">
                  Repository Git URL / Shorthand
                </label>
                <input
                  type="text"
                  required
                  value={repoInput}
                  onChange={(e) => setRepoInput(e.target.value)}
                  placeholder="https://github.com/org/repo or org/repo"
                  className="w-full px-3 py-2 rounded-xl border border-zinc-200 text-xs font-mono text-zinc-900 focus:outline-none focus:ring-2 focus:ring-zinc-400"
                />
              </div>

              <div>
                <label className="text-xs font-semibold text-zinc-700 block mb-1">
                  Target Branch
                </label>
                <input
                  type="text"
                  value={selectedBranch}
                  onChange={(e) => setSelectedBranch(e.target.value)}
                  placeholder="main"
                  className="w-full px-3 py-2 rounded-xl border border-zinc-200 text-xs font-mono text-zinc-900 focus:outline-none focus:ring-2 focus:ring-zinc-400"
                />
              </div>

              <div className="flex items-center justify-end gap-2.5 pt-2">
                <button
                  type="button"
                  onClick={() => setIsIngestModalOpen(false)}
                  className="px-4 py-2 rounded-xl border border-zinc-200 text-xs font-semibold text-zinc-700 hover:bg-zinc-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isIngesting || !repoInput.trim()}
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