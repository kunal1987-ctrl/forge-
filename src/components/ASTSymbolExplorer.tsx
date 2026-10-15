/**
 * ASTSymbolExplorer
 *
 * Read-only inspector for the Tree-sitter AST index produced during repository
 * ingestion. Renders the flat symbol projection persisted to `code_chunks`
 * (`file_path`, `symbol_name`, `symbol_type`, `line_number`) grouped by file,
 * with a KPI row summarising index coverage.
 *
 * Backend contract (see `apiClient.getRepoASTSymbols`):
 *   GET /api/repos/{repo_id}/symbols?commit_sha&symbol_type&query&limit
 *   -> { repo_id, commit_sha, summary: { total_files_parsed, total_nodes_indexed, by_type }, symbols: ASTSymbol[] }
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Box,
  Braces,
  Check,
  ChevronRight,
  Copy,
  FileCode,
  FileQuestion,
  Layers,
  ListTree,
  Loader2,
  RefreshCw,
  Search,
  SquareFunction,
  X,
} from 'lucide-react';
import { apiClient } from '../services/apiClient';
import type { ASTSymbol, ASTSymbolsIndexResponse } from '../types/api';

export interface ASTSymbolExplorerProps {
  /** Repository UUID returned by the ingestion endpoint. */
  repoId?: string;
  /** Pin the view to a single indexed revision. */
  commitSha?: string;
  /** `panel` renders a full tab body; `sidebar` renders a narrow single-column rail. */
  variant?: 'panel' | 'sidebar';
  /** Fetch on mount and whenever `repoId` / `commitSha` change. */
  autoLoad?: boolean;
  /** Upper bound on symbols requested from the API. */
  limit?: number;
  onSelectSymbol?: (symbol: ASTSymbol) => void;
  className?: string;
}

type SymbolTypeFilter = 'all' | 'class' | 'function' | 'module';

interface FileGroup {
  filePath: string;
  symbols: ASTSymbol[];
}

interface IndexStats {
  files: number;
  nodes: number;
  classes: number;
  functions: number;
}

const CLASS_SYMBOL_TYPES = new Set(['class', 'interface', 'struct', 'trait', 'enum', 'type']);
const FUNCTION_SYMBOL_TYPES = new Set(['function', 'method', 'constructor', 'func']);

/** Rows rendered per page so multi-thousand-symbol indexes stay responsive. */
const PAGE_SIZE = 60;

const compactFormatter = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

/** Keeps small counts exact (1,284) and compacts large ones (12.9K). */
const formatCount = (value: number): string => {
  if (!Number.isFinite(value) || value <= 0) return '0';
  return value >= 10_000 ? compactFormatter.format(value) : value.toLocaleString('en-US');
};

const isClassSymbol = (symbolType: string): boolean =>
  CLASS_SYMBOL_TYPES.has(symbolType.toLowerCase());

const isFunctionSymbol = (symbolType: string): boolean =>
  FUNCTION_SYMBOL_TYPES.has(symbolType.toLowerCase());

const resolveSymbolIcon = (
  symbolType: string
): React.ComponentType<{ className?: string }> => {
  if (isClassSymbol(symbolType)) return Box;
  if (isFunctionSymbol(symbolType)) return SquareFunction;
  return Braces;
};

export const ASTSymbolExplorer: React.FC<ASTSymbolExplorerProps> = ({
  repoId,
  commitSha,
  variant = 'panel',
  autoLoad = true,
  limit,
  onSelectSymbol,
  className = '',
}) => {
  const [symbols, setSymbols] = useState<ASTSymbol[]>([]);
  const [response, setResponse] = useState<ASTSymbolsIndexResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [hasLoaded, setHasLoaded] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState<string>('');
  const [typeFilter, setTypeFilter] = useState<SymbolTypeFilter>('all');
  const [renderLimit, setRenderLimit] = useState<number>(PAGE_SIZE);
  const [collapsedFiles, setCollapsedFiles] = useState<Set<string>>(new Set());
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const isSidebar = variant === 'sidebar';

  // --------------------------------------------------------------------------
  // DATA LOADING
  // --------------------------------------------------------------------------

  const loadSymbols = useCallback(async () => {
    const targetRepo = (repoId || '').trim();

    if (!targetRepo) {
      setSymbols([]);
      setResponse(null);
      setError(null);
      setHasLoaded(false);
      setIsLoading(false);
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      const data = await apiClient.getRepoASTSymbols(targetRepo, { commitSha, limit });
      setResponse(data || null);
      setSymbols(Array.isArray(data?.symbols) ? data.symbols : []);
      setHasLoaded(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load the AST symbol index');
      setSymbols([]);
      setResponse(null);
      setHasLoaded(true);
    } finally {
      setIsLoading(false);
    }
  }, [repoId, commitSha, limit]);

  useEffect(() => {
    if (autoLoad) {
      void loadSymbols();
    }
  }, [autoLoad, loadSymbols]);

  // Reset pagination whenever the working set changes
  useEffect(() => {
    setRenderLimit(PAGE_SIZE);
  }, [query, typeFilter, symbols.length]);

  // --------------------------------------------------------------------------
  // SUMMARY STATS
  // --------------------------------------------------------------------------

  // Derived client-side so the KPI row still renders if the API omits `summary`.
  const derivedStats = useMemo<IndexStats>(() => {
    const files = new Set<string>();
    let classes = 0;
    let functions = 0;

    for (const symbol of symbols) {
      files.add(symbol.file_path);
      const symbolType = (symbol.symbol_type || '').toLowerCase();
      if (isClassSymbol(symbolType)) {
        classes += 1;
      } else if (isFunctionSymbol(symbolType)) {
        functions += 1;
      }
    }

    return { files: files.size, nodes: symbols.length, classes, functions };
  }, [symbols]);

  const stats = useMemo<IndexStats>(() => {
    const summary = response?.summary;
    const byType = summary?.by_type || {};

    const summarisedClasses = Object.entries(byType)
      .filter(([type]) => isClassSymbol(type))
      .reduce((total, [, count]) => total + (Number(count) || 0), 0);
    const summarisedFunctions = Object.entries(byType)
      .filter(([type]) => isFunctionSymbol(type))
      .reduce((total, [, count]) => total + (Number(count) || 0), 0);

    return {
      files: summary?.total_files_parsed ?? derivedStats.files,
      nodes: summary?.total_nodes_indexed ?? derivedStats.nodes,
      classes: summarisedClasses || derivedStats.classes,
      functions: summarisedFunctions || derivedStats.functions,
    };
  }, [response, derivedStats]);

  const statTiles: Array<{
    label: string;
    value: number;
    icon: React.ComponentType<{ className?: string }>;
  }> = [
    { label: 'Files parsed', value: stats.files, icon: FileCode },
    { label: 'AST nodes indexed', value: stats.nodes, icon: ListTree },
    { label: 'Classes', value: stats.classes, icon: Box },
    { label: 'Functions', value: stats.functions, icon: SquareFunction },
  ];

  // --------------------------------------------------------------------------
  // FILTERING & GROUPING
  // --------------------------------------------------------------------------

  const filteredSymbols = useMemo(() => {
    const needle = query.trim().toLowerCase();

    return symbols.filter((symbol) => {
      const symbolType = (symbol.symbol_type || '').toLowerCase();

      if (typeFilter === 'class' && !isClassSymbol(symbolType)) return false;
      if (typeFilter === 'function' && !isFunctionSymbol(symbolType)) return false;
      if (
        typeFilter === 'module' &&
        (isClassSymbol(symbolType) || isFunctionSymbol(symbolType))
      ) {
        return false;
      }

      if (!needle) return true;
      return (
        (symbol.symbol_name || '').toLowerCase().includes(needle) ||
        (symbol.file_path || '').toLowerCase().includes(needle)
      );
    });
  }, [symbols, query, typeFilter]);

  const fileGroups = useMemo<FileGroup[]>(() => {
    const grouped = new Map<string, ASTSymbol[]>();
    for (const symbol of filteredSymbols) {
      const bucket = grouped.get(symbol.file_path);
      if (bucket) {
        bucket.push(symbol);
      } else {
        grouped.set(symbol.file_path, [symbol]);
      }
    }
    return Array.from(grouped, ([filePath, groupedSymbols]) => ({
      filePath,
      symbols: groupedSymbols,
    }));
  }, [filteredSymbols]);

  // Slice groups down to the pagination window
  const { visibleGroups, renderedCount } = useMemo(() => {
    const groups: FileGroup[] = [];
    let count = 0;

    for (const group of fileGroups) {
      if (count >= renderLimit) break;
      const remaining = renderLimit - count;
      if (group.symbols.length <= remaining) {
        groups.push(group);
        count += group.symbols.length;
      } else {
        groups.push({ filePath: group.filePath, symbols: group.symbols.slice(0, remaining) });
        count += remaining;
      }
    }

    return { visibleGroups: groups, renderedCount: count };
  }, [fileGroups, renderLimit]);

  const toggleFile = (filePath: string) => {
    setCollapsedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(filePath)) {
        next.delete(filePath);
      } else {
        next.add(filePath);
      }
      return next;
    });
  };

  const handleCopyReference = (symbol: ASTSymbol) => {
    const reference =
      symbol.line_number != null
        ? `${symbol.file_path}:${symbol.line_number}`
        : symbol.file_path;
    const key = `${symbol.file_path}:${symbol.symbol_name}:${symbol.line_number ?? 'x'}`;

    navigator.clipboard.writeText(reference);
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  const activeFilterCount =
    (query.trim() ? 1 : 0) + (typeFilter === 'all' ? 0 : 1);

  const filterChips: Array<{ id: SymbolTypeFilter; label: string }> = [
    { id: 'all', label: 'All' },
    { id: 'class', label: 'Classes' },
    { id: 'function', label: 'Functions' },
    { id: 'module', label: 'Modules & files' },
  ];

  // --------------------------------------------------------------------------
  // RENDER
  // --------------------------------------------------------------------------

  return (
    <div
      className={`flex flex-col ${isSidebar ? 'gap-3' : 'gap-4'} ${className}`}
      id="ast-symbol-explorer"
    >
      {/* =====================================================================
          SUMMARY KPI ROW
          ===================================================================== */}
      <div className={`grid gap-3 ${isSidebar ? 'grid-cols-2' : 'grid-cols-2 lg:grid-cols-4'}`}>
        {statTiles.map((tile) => {
          const Icon = tile.icon;
          return (
            <div
              key={tile.label}
              className="bg-white border border-zinc-200/90 rounded-2xl px-4 py-3 shadow-sm"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] font-medium text-zinc-500">{tile.label}</span>
                <Icon className="h-3.5 w-3.5 text-zinc-400 shrink-0" />
              </div>
              <div className="mt-1 text-2xl font-semibold text-zinc-900 leading-none">
                {formatCount(tile.value)}
              </div>
            </div>
          );
        })}
      </div>

      {/* =====================================================================
          FILTER TOOLBAR
          ===================================================================== */}
      <div className="bg-white border border-zinc-200/90 rounded-2xl p-3.5 shadow-sm space-y-3">
        <div className="flex gap-2">
          <div className="relative flex-1">
            <div className="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none">
              <Search className="h-3.5 w-3.5 text-zinc-400" />
            </div>
            <input
              type="text"
              id="ast-symbol-filter-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter by symbol or file path (e.g. CodeParser, engine/nodes.py)..."
              className="w-full pl-10 pr-9 py-2 bg-zinc-50 border border-zinc-200 rounded-xl text-xs font-mono text-zinc-900 placeholder:text-zinc-400 focus:outline-none focus:border-zinc-400 focus:bg-white focus:ring-2 focus:ring-zinc-100 transition shadow-inner"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery('')}
                className="absolute inset-y-0 right-0 pr-3 flex items-center text-zinc-400 hover:text-zinc-600 transition"
                title="Clear filter"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          <button
            type="button"
            id="ast-symbol-refresh-btn"
            disabled={isLoading || !(repoId || '').trim()}
            onClick={() => void loadSymbols()}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-zinc-900 hover:bg-zinc-800 active:scale-[0.99] text-white text-xs font-semibold shadow-sm transition disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
            title="Re-fetch the symbol index"
          >
            {isLoading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-400" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5 text-amber-400" />
            )}
            <span>Refresh</span>
          </button>
        </div>

        {/* Symbol type filter chips */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[11px] font-mono text-zinc-400 mr-1 flex items-center gap-1">
            <Layers className="h-3 w-3 text-amber-500" />
            Type:
          </span>
          {filterChips.map((chip) => (
            <button
              key={chip.id}
              type="button"
              onClick={() => setTypeFilter(chip.id)}
              className={`text-[11px] font-mono px-2 py-0.5 rounded-md border transition ${
                typeFilter === chip.id
                  ? 'bg-zinc-900 text-white border-zinc-900 font-semibold'
                  : 'bg-zinc-100 hover:bg-zinc-200 text-zinc-700 border-zinc-200'
              }`}
              aria-pressed={typeFilter === chip.id}
            >
              {chip.label}
            </button>
          ))}
        </div>
      </div>

      {/* =====================================================================
          ERROR BANNER
          ===================================================================== */}
      {error && (
        <div className="p-3.5 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-800 font-mono flex items-center gap-2">
          <AlertCircle className="h-4 w-4 text-rose-600 shrink-0" />
          <span>Symbol index unavailable: {error}</span>
        </div>
      )}

      {/* =====================================================================
          RESULT BODY
          ===================================================================== */}
      {isLoading && !hasLoaded ? (
        <div className="py-16 text-center flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-zinc-200/80 p-6">
          <Loader2 className="h-6 w-6 animate-spin text-amber-500" />
          <div className="space-y-1">
            <p className="text-xs font-mono font-medium text-zinc-700">
              Loading Tree-sitter AST symbol index...
            </p>
            <p className="text-[11px] text-zinc-400 font-mono">
              Projecting classes, functions and modules from code_chunks
            </p>
          </div>
        </div>
      ) : !(repoId || '').trim() ? (
        /* No repository bound to this view */
        <div
          id="ast-symbol-no-repo"
          className="py-14 text-center flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-zinc-200/80 p-6"
        >
          <div className="w-12 h-12 rounded-full bg-zinc-100 flex items-center justify-center text-zinc-400 border border-zinc-200">
            <FileQuestion className="h-6 w-6" />
          </div>
          <div className="space-y-1 max-w-sm">
            <p className="text-xs font-mono font-medium text-zinc-700">
              No repository connected
            </p>
            <p className="text-[11px] text-zinc-400 font-mono">
              Ingest a repository to populate the AST index.
            </p>
          </div>
        </div>
      ) : filteredSymbols.length === 0 ? (
        /* Index empty, or filters excluded everything */
        <div
          id="ast-symbol-empty-state"
          className="py-14 text-center flex flex-col items-center justify-center gap-3 bg-white rounded-2xl border border-zinc-200/80 p-6"
        >
          <div className="w-12 h-12 rounded-full bg-zinc-100 flex items-center justify-center text-zinc-400 border border-zinc-200">
            <ListTree className="h-6 w-6" />
          </div>
          <div className="space-y-1 max-w-sm">
            <p className="text-xs font-mono font-medium text-zinc-700">
              {symbols.length === 0
                ? 'No symbols indexed for this repository yet'
                : 'No symbols match the current filters'}
            </p>
            <p className="text-[11px] text-zinc-400 font-mono">
              {symbols.length === 0
                ? 'Run repository ingestion to extract classes, functions and dependency edges.'
                : 'Adjust the search term or symbol type filter.'}
            </p>
          </div>
        </div>
      ) : (
        <>
          {/* Result count strip */}
          <div className="flex items-center justify-between px-1">
            <span className="text-xs font-semibold text-zinc-700 font-mono">
              {filteredSymbols.length.toLocaleString('en-US')} symbols across{' '}
              {fileGroups.length.toLocaleString('en-US')} files
            </span>
            {(activeFilterCount > 0 || response?.commit_sha) && (
              <span className="text-[11px] font-mono text-zinc-400 truncate max-w-[50%]">
                {response?.commit_sha ? `commit ${response.commit_sha.slice(0, 8)}` : ''}
              </span>
            )}
          </div>

          {/* Grouped symbol list */}
          <div
            className="space-y-2"
            id="ast-symbol-groups"
            role="list"
            aria-label="Indexed AST symbols grouped by file"
          >
            {visibleGroups.map((group) => {
              const isCollapsed = collapsedFiles.has(group.filePath);

              return (
                <div
                  key={group.filePath}
                  className="bg-white border border-zinc-200/90 rounded-2xl overflow-hidden shadow-sm"
                >
                  {/* File header */}
                  <button
                    type="button"
                    onClick={() => toggleFile(group.filePath)}
                    aria-expanded={!isCollapsed}
                    className="w-full flex items-center gap-2 px-3 py-2 bg-zinc-50 hover:bg-zinc-100 transition text-left"
                  >
                    <ChevronRight
                      className={`h-3.5 w-3.5 text-zinc-400 shrink-0 transition-transform ${
                        isCollapsed ? '' : 'rotate-90'
                      }`}
                    />
                    <FileCode className="h-3.5 w-3.5 text-amber-600 shrink-0" />
                    <span className="font-mono text-xs font-semibold text-zinc-800 truncate flex-1 min-w-0">
                      {group.filePath}
                    </span>
                    <span className="text-[10px] font-mono font-medium px-1.5 py-0.5 rounded bg-zinc-200/70 text-zinc-600 shrink-0">
                      {group.symbols.length}
                    </span>
                  </button>

                  {/* Symbol rows */}
                  {!isCollapsed && (
                    <div className="divide-y divide-zinc-100">
                      {group.symbols.map((symbol, index) => {
                        const Icon = resolveSymbolIcon(symbol.symbol_type || '');
                        const copyKey = `${symbol.file_path}:${symbol.symbol_name}:${
                          symbol.line_number ?? 'x'
                        }`;
                        const isCopied = copiedKey === copyKey;

                        return (
                          <div
                            key={`${copyKey}:${index}`}
                            role="listitem"
                            onClick={onSelectSymbol ? () => onSelectSymbol(symbol) : undefined}
                            className={`group flex items-center gap-2.5 px-3 py-1.5 pl-8 transition ${
                              onSelectSymbol
                                ? 'cursor-pointer hover:bg-amber-50/60'
                                : 'hover:bg-zinc-50'
                            }`}
                          >
                            <Icon className="h-3.5 w-3.5 shrink-0 text-zinc-400" />
                            <span className="font-mono text-xs font-medium text-zinc-900 truncate flex-1 min-w-0">
                              {symbol.symbol_name}
                            </span>
                            {symbol.parent_symbol && (
                              <span className="hidden md:inline font-mono text-[10px] text-zinc-400 truncate max-w-[140px]">
                                in {symbol.parent_symbol}
                              </span>
                            )}
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase font-medium bg-zinc-100 text-zinc-700 border border-zinc-200 shrink-0">
                              {symbol.symbol_type || 'symbol'}
                            </span>
                            <span
                              className="font-mono text-[11px] text-zinc-400 tabular-nums w-12 text-right shrink-0"
                              title={
                                symbol.line_number != null
                                  ? `Defined at line ${symbol.line_number}`
                                  : 'Line number not recorded for this revision'
                              }
                            >
                              {symbol.line_number != null ? `L${symbol.line_number}` : '—'}
                            </span>
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleCopyReference(symbol);
                              }}
                              className="p-1 rounded-md text-zinc-300 group-hover:text-zinc-500 hover:!text-zinc-700 hover:bg-zinc-100 transition shrink-0"
                              title="Copy file path and line reference"
                              aria-label={`Copy reference for ${symbol.symbol_name}`}
                            >
                              {isCopied ? (
                                <Check className="h-3.5 w-3.5 text-emerald-600 stroke-[3]" />
                              ) : (
                                <Copy className="h-3.5 w-3.5" />
                              )}
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Pagination */}
          {renderedCount < filteredSymbols.length && (
            <button
              type="button"
              onClick={() => setRenderLimit((prev) => prev + PAGE_SIZE)}
              className="w-full py-2.5 rounded-xl border border-zinc-200 bg-white hover:bg-zinc-50 text-xs font-semibold text-zinc-700 shadow-2xs transition"
            >
              Show {Math.min(PAGE_SIZE, filteredSymbols.length - renderedCount)} more (
              {renderedCount.toLocaleString('en-US')} of{' '}
              {filteredSymbols.length.toLocaleString('en-US')} shown)
            </button>
          )}
        </>
      )}
    </div>
  );
};

export default ASTSymbolExplorer;
