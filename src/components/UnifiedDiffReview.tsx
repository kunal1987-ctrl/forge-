import React, { useState, useMemo } from 'react';
import {
  FileCode,
  Copy,
  Check,
  ChevronDown,
  ChevronRight,
  GitCommit,
  Loader2,
  Search,
  Code2,
} from 'lucide-react';
import { useTask } from '../context/TaskContext';
import type { TaskStateObject } from '../types/api';

export interface DiffLine {
  type: 'add' | 'delete' | 'context' | 'hunk' | 'meta';
  content: string;
  oldLineNumber?: number;
  newLineNumber?: number;
}

export interface DiffHunk {
  header: string;
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  lines: DiffLine[];
}

export interface ParsedDiffFile {
  oldPath: string;
  newPath: string;
  filePath: string;
  rawHeader: string;
  hunks: DiffHunk[];
  linesAdded: number;
  linesDeleted: number;
}

export interface UnifiedDiffReviewProps {
  /** Optional direct patch override. If omitted, consumes task.patch from TaskContext */
  patch?: string | null;
  /** Optional direct task state object override */
  task?: TaskStateObject | null;
  /** Custom extra CSS classes */
  className?: string;
  /** Whether the task is currently actively executing */
  isRunning?: boolean;
  /** Optional file filter search query */
  searchQuery?: string;
  /** Optional title for the review panel */
  title?: string;
}

/**
 * Robust Unified Diff Parser
 * Parses standard `diff --git`, `--- / +++`, and `@@ -x,y +x,y @@` unified diffs,
 * assigning exact line numbers to deletions and additions. Also parses search/replace blocks.
 */
export function parseUnifiedDiff(diffString: string): ParsedDiffFile[] {
  if (!diffString || !diffString.trim()) {
    return [];
  }

  const files: ParsedDiffFile[] = [];
  const rawLines = diffString.replace(/\r\n/g, '\n').split('\n');

  let currentFile: ParsedDiffFile | null = null;
  let currentHunk: DiffHunk | null = null;
  let curOldLine = 1;
  let curNewLine = 1;

  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i];

    // 1. Detect standard `diff --git a/... b/...` header
    const diffGitMatch = line.match(/^diff --git a\/(.+) b\/(.+)$/);
    if (diffGitMatch) {
      if (currentHunk && currentFile) {
        currentFile.hunks.push(currentHunk);
        currentHunk = null;
      }
      if (currentFile) {
        files.push(currentFile);
      }

      currentFile = {
        oldPath: diffGitMatch[1],
        newPath: diffGitMatch[2],
        filePath: diffGitMatch[2] || diffGitMatch[1],
        rawHeader: line,
        hunks: [],
        linesAdded: 0,
        linesDeleted: 0,
      };
      continue;
    }

    // Fallback: Detect `### File: path/to/file` header
    const fileHeaderMatch = line.match(/^###\s+File:\s*(.+)$/i);
    if (fileHeaderMatch) {
      if (currentHunk && currentFile) {
        currentFile.hunks.push(currentHunk);
        currentHunk = null;
      }
      if (currentFile) {
        files.push(currentFile);
      }

      const path = fileHeaderMatch[1].trim();
      currentFile = {
        oldPath: path,
        newPath: path,
        filePath: path,
        rawHeader: `diff --git a/${path} b/${path}`,
        hunks: [],
        linesAdded: 0,
        linesDeleted: 0,
      };
      continue;
    }

    // 2. Detect `--- a/...` or `+++ b/...`
    if (line.startsWith('--- ') || line.startsWith('+++ ')) {
      if (!currentFile) {
        const path = line.slice(4).replace(/^[ab]\//, '').trim();
        currentFile = {
          oldPath: path,
          newPath: path,
          filePath: path,
          rawHeader: `diff --git a/${path} b/${path}`,
          hunks: [],
          linesAdded: 0,
          linesDeleted: 0,
        };
      }
      continue;
    }

    // 3. Detect Hunk Header: `@@ -oldStart,oldCount +newStart,newCount @@`
    const hunkMatch = line.match(/^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$/);
    if (hunkMatch) {
      if (!currentFile) {
        currentFile = {
          oldPath: 'unknown.file',
          newPath: 'unknown.file',
          filePath: 'unknown.file',
          rawHeader: 'diff --git a/unknown.file b/unknown.file',
          hunks: [],
          linesAdded: 0,
          linesDeleted: 0,
        };
      }

      if (currentHunk) {
        currentFile.hunks.push(currentHunk);
      }

      const oldStart = parseInt(hunkMatch[1], 10);
      const oldCount = hunkMatch[2] ? parseInt(hunkMatch[2], 10) : 1;
      const newStart = parseInt(hunkMatch[3], 10);
      const newCount = hunkMatch[4] ? parseInt(hunkMatch[4], 10) : 1;

      curOldLine = oldStart;
      curNewLine = newStart;

      currentHunk = {
        header: line,
        oldStart,
        oldCount,
        newStart,
        newCount,
        lines: [
          {
            type: 'hunk',
            content: line,
          },
        ],
      };
      continue;
    }

    // 4. Lines within a hunk or file
    if (!currentFile) {
      // Create fallback file wrapper if raw diff without git headers
      currentFile = {
        oldPath: 'patch.diff',
        newPath: 'patch.diff',
        filePath: 'patch.diff',
        rawHeader: 'diff --git a/patch.diff b/patch.diff',
        hunks: [],
        linesAdded: 0,
        linesDeleted: 0,
      };
    }

    if (!currentHunk) {
      currentHunk = {
        header: '@@ -1,1 +1,1 @@',
        oldStart: 1,
        oldCount: 1,
        newStart: 1,
        newCount: 1,
        lines: [],
      };
      curOldLine = 1;
      curNewLine = 1;
    }

    if (line.startsWith('+') && !line.startsWith('+++')) {
      currentFile.linesAdded++;
      currentHunk.lines.push({
        type: 'add',
        content: line.slice(1),
        newLineNumber: curNewLine++,
      });
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      currentFile.linesDeleted++;
      currentHunk.lines.push({
        type: 'delete',
        content: line.slice(1),
        oldLineNumber: curOldLine++,
      });
    } else if (line.startsWith(' ')) {
      currentHunk.lines.push({
        type: 'context',
        content: line.slice(1),
        oldLineNumber: curOldLine++,
        newLineNumber: curNewLine++,
      });
    } else if (line.startsWith('\\ No newline at end of file')) {
      currentHunk.lines.push({
        type: 'meta',
        content: line,
      });
    } else {
      // Unprefixed context or raw code line
      currentHunk.lines.push({
        type: 'context',
        content: line,
        oldLineNumber: curOldLine++,
        newLineNumber: curNewLine++,
      });
    }
  }

  if (currentHunk && currentFile) {
    currentFile.hunks.push(currentHunk);
  }
  if (currentFile) {
    files.push(currentFile);
  }

  return files;
}

/**
 * UnifiedDiffReview Component
 *
 * Syntax-highlighted GitHub PR style unified diff viewer:
 * 1. Consumes `task.patch` from the backend task state.
 * 2. Renders exact line numbers with:
 *    - Soft green highlight for added lines (`+`).
 *    - Soft red highlight for deleted lines (`-`).
 *    - Clear file headers (e.g. `diff --git a/utils.py b/utils.py`).
 * 3. Displays a clean helper state (*"Awaiting implementer patch generation..."*) when no patch is available.
 */
export const UnifiedDiffReview: React.FC<UnifiedDiffReviewProps> = ({
  patch: propPatch,
  task: propTask,
  className = '',
  isRunning: propIsRunning,
  searchQuery = '',
  title = 'Unified Git Diff',
}) => {
  const context = useTask();

  // 1. Resolve effective task and patch
  const effectiveTask = propTask ?? context.task;
  const rawPatch =
    propPatch ??
    effectiveTask?.patch ??
    effectiveTask?.currentDiff ??
    context.currentDiff ??
    null;

  const isRunning = propIsRunning ?? (context.status === 'RUNNING');
  const activeStep = context.activeStep;

  // 2. Parse diff into structured files & hunks
  const parsedFiles = useMemo(() => {
    return parseUnifiedDiff(rawPatch || '');
  }, [rawPatch]);

  // Overall statistics
  const totalAdded = useMemo(
    () => parsedFiles.reduce((acc, f) => acc + f.linesAdded, 0),
    [parsedFiles]
  );
  const totalDeleted = useMemo(
    () => parsedFiles.reduce((acc, f) => acc + f.linesDeleted, 0),
    [parsedFiles]
  );

  // File expansion state
  const [collapsedFiles, setCollapsedFiles] = useState<Record<string, boolean>>({});
  const [copiedPatch, setCopiedPatch] = useState<boolean>(false);
  const [copiedFile, setCopiedFile] = useState<string | null>(null);
  const [localSearch, setLocalSearch] = useState<string>(searchQuery);

  const toggleCollapse = (filePath: string) => {
    setCollapsedFiles((prev) => ({
      ...prev,
      [filePath]: !prev[filePath],
    }));
  };

  const handleCopyFullPatch = async () => {
    if (!rawPatch) return;
    try {
      await navigator.clipboard.writeText(rawPatch);
      setCopiedPatch(true);
      setTimeout(() => setCopiedPatch(false), 2000);
      context.addToast('info', 'Unified diff patch copied to clipboard.');
    } catch {
      setCopiedPatch(false);
    }
  };

  const handleCopyFilePatch = async (file: ParsedDiffFile) => {
    const filePatch = `${file.rawHeader}\n--- a/${file.oldPath}\n+++ b/${file.newPath}\n` +
      file.hunks
        .map((h) => `${h.header}\n` + h.lines.map((l) => (l.type === 'add' ? `+${l.content}` : l.type === 'delete' ? `-${l.content}` : ` ${l.content}`)).join('\n'))
        .join('\n');
    try {
      await navigator.clipboard.writeText(filePatch);
      setCopiedFile(file.filePath);
      setTimeout(() => setCopiedFile(null), 2000);
      context.addToast('info', `Diff for ${file.filePath} copied.`);
    } catch {
      setCopiedFile(null);
    }
  };

  // Filtered files based on search query
  const filteredFiles = useMemo(() => {
    if (!localSearch.trim()) return parsedFiles;
    const query = localSearch.toLowerCase().trim();
    return parsedFiles.filter(
      (f) =>
        f.filePath.toLowerCase().includes(query) ||
        f.hunks.some((h) =>
          h.lines.some((l) => l.content.toLowerCase().includes(query))
        )
    );
  }, [parsedFiles, localSearch]);

  // ===========================================================================
  // STATE 1: AWAITING IMPLEMENTER PATCH GENERATION (CLEAN HELPER STATE)
  // ===========================================================================
  if (!rawPatch || !rawPatch.trim() || parsedFiles.length === 0) {
    return (
      <div
        className={`flex flex-col items-center justify-center p-8 sm:p-12 text-center bg-white border border-zinc-200/90 rounded-2xl space-y-4 shadow-xs ${className}`}
      >
        <div className="relative flex items-center justify-center h-12 w-12 rounded-2xl bg-zinc-100 text-zinc-400 border border-zinc-200">
          {isRunning ? (
            <Loader2 className="h-6 w-6 animate-spin text-indigo-600" />
          ) : (
            <GitCommit className="h-6 w-6" />
          )}
        </div>

        <div className="space-y-1.5 max-w-md">
          <h3 className="text-sm font-bold text-zinc-900 tracking-tight">
            Awaiting implementer patch generation...
          </h3>
          <p className="text-xs text-zinc-500 leading-relaxed">
            {isRunning
              ? activeStep === 'implementer'
                ? 'The implementer agent is currently synthesizing surgical AST search-and-replace diffs.'
                : 'The orchestration pipeline is completing planning and context retrieval before generating patches.'
              : 'Once dispatched, the implementer node will generate unified code modifications and display line-by-line diffs here.'}
          </p>
        </div>

        {isRunning && (
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-50 border border-indigo-200 text-indigo-700 text-[11px] font-mono">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-indigo-500" />
            </span>
            <span>node: {activeStep || 'orchestrating'}</span>
          </div>
        )}
      </div>
    );
  }

  // ===========================================================================
  // STATE 2: REAL GITHUB PR SYNTAX-HIGHLIGHTED DIFF VIEWER
  // ===========================================================================
  return (
    <div className={`flex flex-col w-full space-y-4 font-sans ${className}`}>
      {/* Top Toolbar & Summary Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 rounded-xl bg-white border border-zinc-200 shadow-xs">
        <div className="flex items-center gap-3">
          <div className="h-9 w-9 rounded-xl bg-zinc-950 text-white flex items-center justify-center shrink-0 shadow-xs">
            <Code2 className="h-5 w-5" />
          </div>
          <div className="space-y-0.5">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-bold text-zinc-950">{title}</h3>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-zinc-100 text-zinc-700 border border-zinc-200 font-semibold">
                {parsedFiles.length} {parsedFiles.length === 1 ? 'file' : 'files'} changed
              </span>
            </div>
            <div className="flex items-center gap-2 font-mono text-xs">
              <span className="text-emerald-700 font-semibold">+{totalAdded} additions</span>
              <span className="text-zinc-300">•</span>
              <span className="text-rose-700 font-semibold">-{totalDeleted} deletions</span>
            </div>
          </div>
        </div>

        {/* Search & Copy Actions */}
        <div className="flex items-center gap-2">
          <div className="relative flex items-center">
            <Search className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-zinc-400" />
            <input
              type="text"
              value={localSearch}
              onChange={(e) => setLocalSearch(e.target.value)}
              placeholder="Filter file or line..."
              className="h-8 w-40 sm:w-48 rounded-lg border border-zinc-200 bg-zinc-50 pl-8 pr-3 text-xs font-mono text-zinc-800 placeholder-zinc-400 shadow-xs transition hover:border-zinc-300 focus:bg-white focus:border-zinc-900 focus:outline-none"
            />
          </div>

          <button
            type="button"
            onClick={handleCopyFullPatch}
            className="flex items-center gap-1.5 h-8 px-3 rounded-lg border border-zinc-200 bg-white hover:bg-zinc-50 text-xs font-semibold text-zinc-800 shadow-xs transition active:scale-[0.98]"
            title="Copy entire unified diff patch to clipboard"
          >
            {copiedPatch ? (
              <>
                <Check className="h-3.5 w-3.5 text-emerald-600" />
                <span className="text-emerald-700">Copied</span>
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5 text-zinc-500" />
                <span>Copy Patch</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Files List */}
      <div className="space-y-4">
        {filteredFiles.map((file) => {
          const isCollapsed = Boolean(collapsedFiles[file.filePath]);

          return (
            <div
              key={file.filePath}
              className="rounded-xl border border-zinc-200/90 bg-white shadow-xs overflow-hidden transition-all"
            >
              {/* GitHub File Header Bar */}
              <div className="flex items-center justify-between px-3.5 py-2.5 bg-zinc-50/90 border-b border-zinc-200/80 text-xs select-none">
                <div className="flex items-center gap-2 min-w-0">
                  <button
                    type="button"
                    onClick={() => toggleCollapse(file.filePath)}
                    className="p-0.5 rounded text-zinc-400 hover:text-zinc-800 hover:bg-zinc-200/60 transition"
                    title={isCollapsed ? 'Expand file diff' : 'Collapse file diff'}
                    aria-label="Toggle file collapse"
                  >
                    {isCollapsed ? (
                      <ChevronRight className="h-4 w-4" />
                    ) : (
                      <ChevronDown className="h-4 w-4" />
                    )}
                  </button>

                  <FileCode className="h-4 w-4 text-zinc-600 shrink-0" />

                  {/* File Header clearly displayed */}
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-mono font-bold text-zinc-900 truncate" title={file.filePath}>
                      {file.filePath}
                    </span>
                    <span className="hidden sm:inline font-mono text-[11px] text-zinc-400 truncate">
                      {file.rawHeader}
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-2.5 shrink-0">
                  {/* File Change Stats */}
                  <div className="flex items-center gap-1.5 font-mono text-[11px] font-semibold">
                    <span className="text-emerald-700 bg-emerald-50 px-1.5 py-0.2 rounded border border-emerald-200">
                      +{file.linesAdded}
                    </span>
                    <span className="text-rose-700 bg-rose-50 px-1.5 py-0.2 rounded border border-rose-200">
                      -{file.linesDeleted}
                    </span>
                  </div>

                  {/* Copy File Diff */}
                  <button
                    type="button"
                    onClick={() => handleCopyFilePatch(file)}
                    className="p-1 rounded text-zinc-400 hover:text-zinc-800 hover:bg-zinc-200/60 transition"
                    title="Copy this file's diff"
                    aria-label="Copy file diff"
                  >
                    {copiedFile === file.filePath ? (
                      <Check className="h-3.5 w-3.5 text-emerald-600" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                  </button>
                </div>
              </div>

              {/* Scrollable Code Block with Line Numbers */}
              {!isCollapsed && (
                <div className="overflow-x-auto bg-zinc-950 text-zinc-100 font-mono text-xs max-h-[32rem] overflow-y-auto">
                  <div className="min-w-full inline-block">
                    {file.hunks.map((hunk, hIdx) => (
                      <div key={hIdx} className="divide-y divide-zinc-900">
                        {hunk.lines.map((line, lIdx) => {
                          // Chunk / Hunk header row
                          if (line.type === 'hunk') {
                            return (
                              <div
                                key={lIdx}
                                className="flex items-center bg-sky-950/40 text-sky-300 font-bold px-3 py-1 text-[11px] select-none border-y border-sky-900/40 tracking-tight"
                              >
                                <span className="font-mono">{line.content}</span>
                              </div>
                            );
                          }

                          // Meta row (e.g. \ No newline at end of file)
                          if (line.type === 'meta') {
                            return (
                              <div
                                key={lIdx}
                                className="flex items-center bg-zinc-900/60 text-zinc-500 italic px-3 py-0.5 text-[11px] select-none"
                              >
                                <span>{line.content}</span>
                              </div>
                            );
                          }

                          const isAdd = line.type === 'add';
                          const isDelete = line.type === 'delete';

                          return (
                            <div
                              key={lIdx}
                              className={`flex items-stretch hover:brightness-110 transition-colors ${
                                isAdd
                                  ? 'bg-emerald-950/35 border-l-2 border-emerald-500 text-emerald-200'
                                  : isDelete
                                  ? 'bg-rose-950/35 border-l-2 border-rose-500 text-rose-200'
                                  : 'bg-transparent text-zinc-300'
                              }`}
                            >
                              {/* Old Line Number Gutter */}
                              <div className="w-10 sm:w-12 px-2 py-0.5 text-right select-none font-mono text-[11px] text-zinc-500 shrink-0">
                                {line.oldLineNumber !== undefined ? line.oldLineNumber : ''}
                              </div>

                              {/* New Line Number Gutter */}
                              <div className="w-10 sm:w-12 px-2 py-0.5 text-right select-none font-mono text-[11px] text-zinc-500 shrink-0 border-r border-zinc-800">
                                {line.newLineNumber !== undefined ? line.newLineNumber : ''}
                              </div>

                              {/* Add / Delete Symbol Marker Gutter */}
                              <div className="w-6 px-1.5 py-0.5 text-center select-none font-mono text-xs font-bold shrink-0">
                                {isAdd ? (
                                  <span className="text-emerald-400">+</span>
                                ) : isDelete ? (
                                  <span className="text-rose-400">-</span>
                                ) : (
                                  <span className="text-zinc-600"> </span>
                                )}
                              </div>

                              {/* Code Line Content */}
                              <div className="flex-1 px-2 py-0.5 whitespace-pre overflow-x-visible leading-relaxed break-all">
                                {line.content || ' '}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default UnifiedDiffReview;
