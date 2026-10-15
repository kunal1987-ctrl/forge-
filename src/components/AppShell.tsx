import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  FolderGit2,
  GitBranch,
  Flame,
  LayoutDashboard,
  PlusSquare,
  Activity as ActivityIcon,
  GitPullRequest,
  X,
  Moon,
  Sun,
} from 'lucide-react';
import { useTask } from '../context/TaskContext';
import { useTheme } from '../context/ThemeContext';
import { Workspace } from './Workspace';
import { NewTaskView } from './NewTaskView';
import { Activity } from './Activity';
import { Review } from './Review';
import { ErrorBoundary } from './ErrorBoundary';
import { normalizeView, type NavigationTab } from './Navigation';

export interface AppShellProps {
  children?: React.ReactNode;
  activeView?: NavigationTab | string;
  onNavigate?: (tab: NavigationTab) => void;
  className?: string;
}

export const AppShell: React.FC<AppShellProps> = ({
  children,
  activeView: controlledActiveView,
  onNavigate: controlledOnNavigate,
  className = '',
}) => {
  const {
    repository,
    currentRepo,
    setCurrentRepo,
    taskId,
    status,
    taskStatus,
    activeStep,
    completedSteps,
    rawStatus,
    currentStep,
    logs,
    toasts,
    dismissToast,
  } = useTask();
  const { theme, toggleTheme } = useTheme();
  const ThemeIcon = theme === 'dark' ? Sun : Moon;

  // Navigation state: supports controlled props or internal self-contained state
  const [internalView, setInternalView] = useState<NavigationTab>('workspace');
  const activeTab = normalizeView(controlledActiveView ?? internalView);

  const safeNavigate = useCallback(
    (targetView?: string | null) => {
      const validatedView = normalizeView(targetView);
      if (typeof controlledOnNavigate === 'function') {
        controlledOnNavigate(validatedView);
      } else {
        setInternalView(validatedView);
      }
    },
    [controlledOnNavigate]
  );

  // Automatically switch to review tab when review gate is awaiting user decision
  useEffect(() => {
    if (rawStatus === 'awaiting_approval' && activeTab !== 'workspace' && activeTab !== 'reviews') {
      safeNavigate('reviews');
    }
  }, [rawStatus, activeTab, safeNavigate]);

  // Dynamic Global Repository & Branch Binding
  const activeRepo = repository || currentRepo;
  const isRepoConnected = Boolean(
    activeRepo && (activeRepo.url || (activeRepo.name && activeRepo.name !== 'default-repo'))
  );
  const repoUrl = activeRepo?.url || (isRepoConnected ? `https://github.com/org/${activeRepo?.name}` : '');
  const repoBranch = activeRepo?.branch || 'main';
  const repoSymbols = activeRepo?.ast_symbol_count ?? activeRepo?.total_chunks ?? 0;
  const repoDisplayName = isRepoConnected
    ? repoUrl.replace(/^https?:\/\/(www\.)?github\.com\//i, '').replace(/\.git$/i, '') || activeRepo?.name || 'connected-repo'
    : 'No Repository Connected';

  // Format activity logs safely for Activity tab
  const activityLogs = useMemo(() => {
    const safeLogs = Array.isArray(logs) ? logs : [];
    return safeLogs.map((msg, index) => {
      const safeMsg = typeof msg === 'string' ? msg : String(msg || '');
      const stepMatch = safeMsg.match(/\[Step (\d+)\]/i);
      const step = stepMatch ? stepMatch[1] : String(currentStep || 1);
      return {
        id: `log-${index}-${safeMsg.slice(0, 8)}`,
        step,
        message: safeMsg,
        timestamp: new Date().toLocaleTimeString(),
      };
    });
  }, [logs, currentStep]);

  // Main navigation items
  const navItems: Array<{
    id: NavigationTab;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    hasBadge?: boolean;
  }> = [
    {
      id: 'workspace',
      label: 'Workspace',
      icon: LayoutDashboard,
    },
    {
      id: 'new-task',
      label: 'New Task',
      icon: PlusSquare,
    },
    {
      id: 'activity',
      label: 'Activity',
      icon: ActivityIcon,
    },
    {
      id: 'reviews',
      label: 'Reviews',
      icon: GitPullRequest,
      hasBadge: rawStatus === 'awaiting_approval',
    },
  ];

  return (
    <div className={`min-h-screen flex flex-col bg-zinc-50 text-zinc-950 font-sans dark:bg-zinc-950 dark:text-zinc-100 ${className}`}>
      {/* =======================================================================
          STICKY TOP NAVIGATION HEADER
          Anchors main navigation bar, persistent repository status, and pipeline telemetry
          ======================================================================= */}
      <header className="sticky top-0 z-50 border-b border-zinc-200/90 bg-white/90 backdrop-blur-md shadow-2xs dark:border-zinc-800 dark:bg-zinc-900/90">
        <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-3 sm:px-6 gap-2 sm:gap-4">
          {/* Brand Identity & Repository Context Badge */}
          <div className="flex items-center gap-2 sm:gap-3 shrink-0">
            {/* Forge Logo */}
            <div
              onClick={() => safeNavigate('workspace')}
              className="flex items-center gap-2 cursor-pointer group"
              title="Return to Workspace"
            >
              <div className="flex h-7 w-7 sm:h-8 sm:w-8 items-center justify-center rounded-lg bg-zinc-950 text-white shadow-2xs group-hover:bg-zinc-800 transition">
                <Flame className="h-4 w-4 text-indigo-400" />
              </div>
              <span className="font-bold text-sm sm:text-base tracking-wider text-zinc-950">
                FORGE
              </span>
            </div>

            {/* Persistent Repository Context Badge (Connected vs Idle) */}
            <div
              className={`flex items-center gap-1.5 sm:gap-2 px-2 sm:px-2.5 py-1 rounded-lg border text-xs font-mono transition cursor-default ${
                isRepoConnected
                  ? 'bg-zinc-100/80 border-zinc-200 text-zinc-800 hover:bg-zinc-200/70'
                  : 'bg-zinc-100 border-dashed border-zinc-300 text-zinc-500'
              }`}
              title={
                isRepoConnected
                  ? `Active Repository: ${repoUrl || repoDisplayName}\nTarget Branch: ${repoBranch}${
                      repoSymbols > 0 ? `\nIndexed AST Symbols: ${repoSymbols.toLocaleString()}` : ''
                    }`
                  : 'No active repository connected. Ingest a repository in Workspace or New Task to begin.'
              }
            >
              {/* Status Indicator Dot */}
              {isRepoConnected ? (
                <span className="relative flex h-2 w-2 shrink-0">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
                </span>
              ) : (
                <span className="h-2 w-2 rounded-full bg-zinc-400 shrink-0" />
              )}

              <FolderGit2
                className={`h-3.5 w-3.5 shrink-0 ${
                  isRepoConnected ? 'text-zinc-600' : 'text-zinc-400'
                }`}
              />

              {/* Status Pill */}
              <span
                className={`hidden md:inline-flex px-1.5 py-0.2 rounded text-[10px] font-bold uppercase tracking-wider ${
                  isRepoConnected
                    ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                    : 'bg-zinc-200/70 text-zinc-600'
                }`}
              >
                {isRepoConnected ? 'Connected' : 'Idle'}
              </span>

              {/* Repository Name or Idle Label */}
              <span className="truncate max-w-[100px] sm:max-w-[160px] lg:max-w-[210px] font-semibold text-zinc-900">
                {repoDisplayName}
              </span>

              {/* Indexed Symbols Count */}
              {isRepoConnected && repoSymbols > 0 && (
                <span className="hidden xl:inline-flex items-center px-1.5 py-0.5 rounded bg-zinc-200/60 text-zinc-700 text-[10px] font-mono font-medium">
                  {repoSymbols.toLocaleString()} symbols
                </span>
              )}
            </div>
          </div>

          {/* Main Navigation Bar (Workspace, New Task, Activity, Reviews) */}
          <nav
            className="flex items-center gap-1 sm:gap-1.5 bg-zinc-100/80 p-1 rounded-xl border border-zinc-200/80"
            role="navigation"
            aria-label="Main Navigation"
          >
            {navItems.map((item) => {
              const Icon = item.icon;
              const isActive = activeTab === item.id;

              return (
                <button
                  key={item.id}
                  type="button"
                  id={`top-nav-${item.id}`}
                  onClick={() => safeNavigate(item.id)}
                  className={`relative flex items-center gap-1.5 rounded-lg px-2 sm:px-3 py-1.5 text-xs font-medium transition active:scale-[0.98] ${
                    isActive
                      ? 'bg-white text-zinc-950 font-semibold shadow-2xs border border-zinc-200/60'
                      : 'text-zinc-600 hover:text-zinc-900 hover:bg-white/60'
                  }`}
                  aria-current={isActive ? 'page' : undefined}
                >
                  <Icon
                    className={`h-3.5 w-3.5 shrink-0 ${
                      isActive ? 'text-zinc-950' : 'text-zinc-500'
                    }`}
                  />
                  <span className="hidden sm:inline">{item.label}</span>
                  {item.hasBadge && (
                    <span className="relative flex h-2 w-2 shrink-0">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                      <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
                    </span>
                  )}
                </button>
              );
            })}
          </nav>

          {/* Right Controls: Engine Status & Branch Selector */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={toggleTheme}
              className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-zinc-200 bg-white text-zinc-700 shadow-2xs transition hover:bg-zinc-100 hover:text-zinc-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200 dark:hover:bg-zinc-700 dark:hover:text-white"
              aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
              title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
            >
              <ThemeIcon className="h-4 w-4" aria-hidden="true" />
            </button>

            {/* Live Pipeline Execution Status Badge */}
            <div className="flex items-center gap-1.5">
              <span
                className={`font-mono text-[10px] px-2 py-0.5 rounded-full border font-bold uppercase tracking-wider ${
                  status === 'RUNNING'
                    ? 'bg-amber-50 text-amber-800 border-amber-300 animate-pulse'
                    : status === 'COMPLETED'
                    ? 'bg-emerald-50 text-emerald-800 border-emerald-300'
                    : status === 'FAILED'
                    ? 'bg-rose-50 text-rose-800 border-rose-300'
                    : status === 'ABORTED'
                    ? 'bg-zinc-200 text-zinc-800 border-zinc-300'
                    : 'bg-zinc-100 text-zinc-600 border-zinc-200'
                }`}
              >
                {status || 'IDLE'}
              </span>

              {activeStep && status === 'RUNNING' && (
                <span className="hidden lg:inline font-mono text-[10px] px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200">
                  {activeStep} {(completedSteps || []).length > 0 ? `(${completedSteps.length}/6)` : ''}
                </span>
              )}

              {taskId && typeof taskId === 'string' && (
                <span className="hidden xl:inline font-mono text-[10px] text-zinc-500">
                  #{taskId.slice(0, 8)}
                </span>
              )}
            </div>

            {/* Target Branch Selector */}
            <div className="relative hidden md:flex items-center">
              <GitBranch className="pointer-events-none absolute left-2 h-3.5 w-3.5 text-zinc-500" />
              <select
                value={repoBranch}
                onChange={(e) => {
                  if (typeof setCurrentRepo === 'function') {
                    setCurrentRepo({ branch: e.target.value });
                  }
                }}
                title="Target Branch"
                aria-label="Target Branch"
                className="h-8 rounded-lg border border-zinc-200 bg-white pl-7 pr-6 text-xs font-mono font-medium text-zinc-800 shadow-2xs transition hover:border-zinc-300 focus:border-zinc-400 focus:outline-none focus:ring-1 focus:ring-zinc-400"
              >
                <option value="main">main</option>
                <option value="dev">dev</option>
                <option value="staging">staging</option>
              </select>
            </div>
          </div>
        </div>
      </header>

      {/* =======================================================================
          MAIN WORKSPACE VIEW CONTAINER WRAPPED IN GLOBAL ERROR BOUNDARY
          ======================================================================= */}
      <main className="flex-1 overflow-y-auto px-4 py-8 sm:px-6">
        <div className="mx-auto max-w-5xl">
          <ErrorBoundary
            key={activeTab}
            componentName={
              activeTab === 'new-task'
                ? 'New Task'
                : activeTab === 'activity'
                ? 'Activity'
                : activeTab === 'reviews'
                ? 'Reviews'
                : 'Workspace'
            }
            onReset={() => safeNavigate('workspace')}
          >
            {children ? (
              children
            ) : (
              <>
                {activeTab === 'workspace' && <Workspace />}
                {activeTab === 'new-task' && (
                  <NewTaskView
                    onStartRun={() => safeNavigate('workspace')}
                    onNavigate={safeNavigate}
                    initialBranch={repoBranch}
                  />
                )}
                {activeTab === 'activity' && (
                  <Activity
                    logs={activityLogs}
                    currentStep={currentStep}
                    onNavigate={(tab) => safeNavigate(tab)}
                    taskId={taskId}
                    taskStatus={taskStatus}
                  />
                )}
                {activeTab === 'reviews' && (
                  <Review
                    onNavigate={(tab) => safeNavigate(tab)}
                    taskId={taskId}
                  />
                )}
              </>
            )}
          </ErrorBoundary>
        </div>
      </main>

      {/* =======================================================================
          GLOBAL FLOATING TOAST NOTIFICATIONS
          ======================================================================= */}
      {toasts && toasts.length > 0 && (
        <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm w-full pointer-events-none">
          {toasts.map((toast) => (
            <div
              key={toast.id}
              className={`pointer-events-auto flex items-center justify-between gap-3 p-3 rounded-xl border shadow-lg text-xs font-medium animate-in slide-in-from-bottom-2 ${
                toast.type === 'success'
                  ? 'bg-emerald-50 text-emerald-900 border-emerald-200'
                  : toast.type === 'error'
                  ? 'bg-rose-50 text-rose-900 border-rose-200'
                  : 'bg-zinc-900 text-zinc-100 border-zinc-800'
              }`}
            >
              <span>{toast.message}</span>
              <button
                type="button"
                onClick={() => dismissToast(toast.id)}
                className="text-current opacity-70 hover:opacity-100 transition p-0.5 rounded"
                aria-label="Dismiss toast"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default AppShell;
