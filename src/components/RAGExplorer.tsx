import React, { useState } from 'react';
import {
  Search,
  Loader2,
  Copy,
  Check,
  FileCode,
  Sparkles,
  GitFork,
  FileQuestion,
  AlertCircle,
  X,
} from 'lucide-react';
import { apiClient } from '../services/apiClient';
import type { RAGSearchResultItem } from '../types/api';

export interface RAGExplorerProps {
  repoId?: string;
  initialQuery?: string;
  className?: string;
}

export const RAGExplorer: React.FC<RAGExplorerProps> = ({
  repoId = 'default-repo',
  initialQuery = '',
  className = '',
}) => {
  const [query, setQuery] = useState<string>(initialQuery);
  const [isSearching, setIsSearching] = useState<boolean>(false);
  const [results, setResults] = useState<RAGSearchResultItem[]>([]);
  const [hasSearched, setHasSearched] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  const handleSearch = async (overrideQuery?: string) => {
    const q = (overrideQuery ?? query).trim();
    if (!q) return;

    if (overrideQuery) {
      setQuery(overrideQuery);
    }

    setIsSearching(true);
    setError(null);

    try {
      const data = await apiClient.searchRAG(q, repoId, 6);
      setResults(data);
      setHasSearched(true);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Graph-RAG search failed';
      setError(msg);
      setResults([]);
      setHasSearched(true);
    } finally {
      setIsSearching(false);
    }
  };

  const handleCopySnippet = (snippet: string, index: number) => {
    if (!snippet) return;
    navigator.clipboard.writeText(snippet);
    setCopiedIndex(index);
    setTimeout(() => {
      setCopiedIndex(null);
    }, 2000);
  };

  const sampleChips = [
    'flush_all',
    'TelemetryDispatcher',
    'ASTGuard',
    'CodeParser',
    'execute_rag_search',
  ];

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Search Input Bar */}
      <div className="bg-white border border-zinc-200/90 rounded-2xl p-4 shadow-sm space-y-3">
        <div className="flex gap-2">
          <div className="relative flex-1">
            <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-zinc-400">
              {isSearching ? (
                <Loader2 className="h-4 w-4 animate-spin text-amber-500" />
              ) : (
                <Search className="h-4 w-4 text-zinc-400" />
              )}
            </div>
            <input
              type="text"
              id="rag-search-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              placeholder="Search indexed AST functions, methods, classes (e.g. flush_all, TelemetryDispatcher)..."
              className="w-full pl-10 pr-9 py-2.5 bg-zinc-50 border border-zinc-200 rounded-xl text-xs font-mono text-zinc-900 placeholder:text-zinc-400 focus:outline-none focus:border-zinc-400 focus:bg-white focus:ring-2 focus:ring-zinc-100 transition shadow-inner"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery('')}
                className="absolute inset-y-0 right-0 pr-3 flex items-center text-zinc-400 hover:text-zinc-600 transition"
                title="Clear input"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          <button
            type="button"
            id="rag-test-search-btn"
            disabled={isSearching || !query.trim()}
            onClick={() => handleSearch()}
            className="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-zinc-900 hover:bg-zinc-800 active:scale-[0.99] text-white text-xs font-semibold shadow-sm transition disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
          >
            {isSearching ? (
              <Loader2 className="h-4 w-4 animate-spin text-amber-400" />
            ) : (
              <Search className="h-4 w-4 text-amber-400" />
            )}
            <span>Test Search</span>
          </button>
        </div>

        {/* Quick Sample Query Chips */}
        <div className="flex items-center gap-1.5 flex-wrap pt-0.5">
          <span className="text-[11px] font-mono text-zinc-400 mr-1 flex items-center gap-1">
            <Sparkles className="h-3 w-3 text-amber-500" />
            Suggested:
          </span>
          {sampleChips.map((chip) => (
            <button
              key={chip}
              type="button"
              onClick={() => handleSearch(chip)}
              className="text-[11px] font-mono px-2 py-0.5 rounded-md bg-zinc-100 hover:bg-zinc-200 text-zinc-700 border border-zinc-200 transition"
            >
              {chip}
            </button>
          ))}
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="p-3.5 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-800 font-mono flex items-center gap-2">
          <AlertCircle className="h-4 w-4 text-rose-600 shrink-0" />
          <span>Search Error: {error}</span>
        </div>
      )}

      {/* Dynamic Results Container */}
      {isSearching ? (
        <div className="py-16 text-center flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-zinc-200/80 p-6">
          <Loader2 className="h-6 w-6 animate-spin text-amber-500" />
          <div className="space-y-1">
            <p className="text-xs font-mono font-medium text-zinc-700">
              Querying AST graph index &amp; executing pgvector RRF...
            </p>
            <p className="text-[11px] text-zinc-400 font-mono">
              Running FlashRank cross-encoder reranking
            </p>
          </div>
        </div>
      ) : results.length > 0 ? (
        <div className="space-y-3" id="rag-results-container">
          <div className="flex items-center justify-between px-1">
            <span className="text-xs font-semibold text-zinc-700 font-mono">
              Top Reranked Matches ({results.length} chunks via Hybrid AST + pgvector &amp; FlashRank)
            </span>
            <span className="text-[11px] font-mono text-zinc-400">
              model: ms-marco-MiniLM-L-12-v2
            </span>
          </div>

          {results.map((chunk, idx) => {
            const snippetText = chunk.snippet || chunk.content || '';
            const codeLines = snippetText.split('\n');
            const startLine = chunk.start_line || 1;

            return (
              <div
                key={`${chunk.file_path}-${chunk.symbol_name}-${idx}`}
                className="bg-white border border-zinc-200/90 rounded-2xl p-4 shadow-sm space-y-3 hover:border-zinc-300 transition"
              >
                {/* Header Metadata */}
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="flex items-center gap-2 min-w-0">
                    <FileCode className="h-4 w-4 text-amber-600 shrink-0" />
                    <span className="font-mono text-xs font-bold text-zinc-900 truncate">
                      {chunk.symbol_name}
                    </span>
                    <span className="px-2 py-0.5 rounded text-[10px] font-mono bg-zinc-100 text-zinc-700 uppercase font-medium border border-zinc-200">
                      {chunk.symbol_type || 'function'}
                    </span>
                    <span className="text-xs font-mono text-zinc-400 truncate">
                      {chunk.file_path}
                      {chunk.start_line ? `:${chunk.start_line}` : ''}
                    </span>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-50 text-amber-900 border border-amber-200/80">
                      Score: {chunk.score.toFixed(3)}
                    </span>
                    <button
                      type="button"
                      onClick={() => handleCopySnippet(snippetText, idx)}
                      className="p-1 rounded-md text-zinc-400 hover:text-zinc-700 hover:bg-zinc-100 transition"
                      title="Copy code snippet"
                    >
                      {copiedIndex === idx ? (
                        <Check className="h-3.5 w-3.5 text-emerald-600 stroke-[3]" />
                      ) : (
                        <Copy className="h-3.5 w-3.5" />
                      )}
                    </button>
                  </div>
                </div>

                {/* Syntax-Highlighted Code Snippet with Line Numbers */}
                <div className="rounded-xl bg-zinc-950 text-zinc-200 border border-zinc-800/80 overflow-hidden shadow-inner">
                  <div className="flex items-center justify-between px-3 py-1.5 bg-zinc-900 border-b border-zinc-800/80 text-[10px] font-mono text-zinc-400">
                    <span className="truncate">{chunk.file_path}</span>
                    <span>Lines {startLine}–{startLine + Math.max(0, codeLines.length - 1)}</span>
                  </div>

                  <div className="p-3 font-mono text-xs overflow-x-auto max-h-56 leading-relaxed flex">
                    {/* Line numbers gutter */}
                    <div className="select-none text-zinc-600 pr-3.5 text-right font-mono text-xs border-r border-zinc-800/60 shrink-0">
                      {codeLines.map((_, lineIdx) => (
                        <div key={lineIdx}>{startLine + lineIdx}</div>
                      ))}
                    </div>
                    {/* Code body */}
                    <pre className="pl-3.5 font-mono text-xs text-zinc-200 flex-1 whitespace-pre">
                      {snippetText}
                    </pre>
                  </div>
                </div>

                {/* 1-Hop AST Neighbors (Call and Dependency graph) */}
                {chunk.dependencies && chunk.dependencies.length > 0 && (
                  <div className="pt-2 border-t border-zinc-100 space-y-1.5">
                    <div className="flex items-center gap-1.5 text-[10px] font-mono uppercase font-bold text-zinc-400">
                      <GitFork className="h-3 w-3 text-indigo-500" />
                      <span>1-Hop AST Neighbors &amp; Graph Callees</span>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {chunk.dependencies.map((dep, dIdx) => (
                        <span
                          key={dIdx}
                          className="px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-50 text-indigo-800 border border-indigo-200/80"
                        >
                          {dep.symbol} ({dep.file_path})
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ) : hasSearched ? (
        /* Empty State: Zero matches found */
        <div
          id="rag-empty-state"
          className="py-14 text-center flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-zinc-200/80 p-6"
        >
          <div className="w-12 h-12 rounded-full bg-zinc-100 flex items-center justify-center text-zinc-400 border border-zinc-200">
            <FileQuestion className="h-6 w-6" />
          </div>
          <div className="space-y-1 max-w-sm">
            <p className="text-xs font-mono font-medium text-zinc-700">
              No matching code symbols found in repository index
            </p>
            <p className="text-[11px] text-zinc-400 font-mono">
              Try testing symbols like <code className="text-zinc-600 bg-zinc-100 px-1 py-0.5 rounded">flush_all</code>, <code className="text-zinc-600 bg-zinc-100 px-1 py-0.5 rounded">TelemetryDispatcher</code>, or ingest a repository in the Workspace tab.
            </p>
          </div>
        </div>
      ) : (
        /* Initial Standby Prompt */
        <div
          id="rag-initial-standby"
          className="py-14 text-center flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-zinc-200/80 p-6"
        >
          <div className="w-12 h-12 rounded-full bg-amber-50 flex items-center justify-center text-amber-600 border border-amber-200">
            <Search className="h-6 w-6" />
          </div>
          <div className="space-y-1 max-w-sm">
            <p className="text-xs font-mono font-medium text-zinc-700">
              Graph-RAG AST &amp; Vector Explorer Ready
            </p>
            <p className="text-[11px] text-zinc-400 font-mono">
              Enter a symbol name or query and click &ldquo;Test Search&rdquo; to test RAG retrieval.
            </p>
          </div>
        </div>
      )}
    </div>
  );
};

export default RAGExplorer;
