import React, { useState, useEffect } from 'react';
import {
  GitBranch,
  PlusSquare,
  Activity as ActivityIcon,
  GitPullRequest,
  LayoutDashboard,
  FolderGit2,
} from 'lucide-react';
import { TaskProvider, useTask } from './context/TaskContext';
import { Workspace } from './components/Workspace';
import { NewTask } from './components/NewTask';
import { Activity } from './components/Activity';
import { Review } from './components/Review';

type View = 'workspace' | 'new' | 'activity' | 'review';

export const AppContent: React.FC = () => {
  const {
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
  } = useTask();

  const [currentView, setCurrentView] = useState<View>('workspace');

  const handleStartRun = () => {
    setCurrentView('workspace');
  };

  // Automatically navigate to review tab when review gate is active
  useEffect(() => {
    if (rawStatus === 'awaiting_approval' && currentView !== 'workspace') {
      // oxlint-disable-next-line react/set-state-in-effect
      setCurrentView('review');
    }
  }, [rawStatus, currentView]);

  // Convert raw log strings into LogEntry structure expected by Activity tab
  const activityLogs = logs.map((msg, index) => {
    const stepMatch = msg.match(/\[Step (\d+)\]/i);
    const step = stepMatch ? stepMatch[1] : String(currentStep || 1);
    return {
      id: `log-${index}-${msg.slice(0, 8)}`,
      step,
      message: msg,
      timestamp: new Date().toLocaleTimeString(),
    };
  });

  return (
    <div className="min-h-screen flex flex-col bg-zinc-50 text-zinc-950 font-sans">
      {/* Top Header with Synchronous Repo, Branch, and Live Task Status */}
      <header className="sticky top-0 z-40 border-b border-zinc-200 bg-white/80 backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <span className="font-semibold text-base tracking-wider text-zinc-950">
              FORGE
            </span>

            {/* Global Active Repository Pill */}
            <div className="hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-zinc-100 border border-zinc-200 text-xs font-mono text-zinc-700">
              <FolderGit2 className="h-3.5 w-3.5 text-zinc-500" />
              <span className="truncate max-w-[160px] font-semibold">{currentRepo.name || currentRepo.id}</span>
            </div>

            {/* Global Live Task Execution Status Indicator */}
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
                {status}
              </span>
              {activeStep && status === 'RUNNING' && (
                <span className="hidden sm:inline font-mono text-[10px] px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200">
                  node: {activeStep} {completedSteps.length > 0 ? `(${completedSteps.length}/6)` : ''}
                </span>
              )}
              {taskId && (
                <span className="hidden md:inline font-mono text-[10px] text-zinc-500">
                  {taskId.slice(0, 8)}
                </span>
              )}
            </div>
          </div>

          {/* Synchronous Target Branch Selector */}
          <div className="flex items-center gap-2">
            <div className="relative flex items-center">
              <GitBranch className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-zinc-500" />
              <select
                value={currentRepo.branch || 'main'}
                onChange={(e) => setCurrentRepo({ branch: e.target.value })}
                className="h-8 rounded-md border border-zinc-200 bg-white pl-8 pr-7 text-xs font-mono font-medium text-zinc-800 shadow-sm transition hover:border-zinc-300 focus:border-zinc-400 focus:outline-none focus:ring-1 focus:ring-zinc-400"
              >
                <option value="main">main</option>
                <option value="dev">dev</option>
                <option value="staging">staging</option>
              </select>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content Area: All Tabs Read Synchronously From Global Store */}
      <main className="flex-1 overflow-y-auto px-4 py-8 sm:px-6">
        <div className="mx-auto max-w-5xl">
          {currentView === 'workspace' && <Workspace />}
          {currentView === 'new' && (
            <NewTask
              onStartRun={handleStartRun}
              onNavigate={setCurrentView}
              initialBranch={currentRepo.branch || 'main'}
            />
          )}
          {currentView === 'activity' && (
            <Activity
              logs={activityLogs}
              currentStep={currentStep}
              onNavigate={setCurrentView}
              taskId={taskId}
              taskStatus={taskStatus}
            />
          )}
          {currentView === 'review' && (
            <Review onNavigate={setCurrentView} taskId={taskId} />
          )}
        </div>
      </main>

      {/* Bottom Navigation Bar */}
      <nav className="sticky bottom-0 z-40 border-t border-zinc-200 bg-white/90 backdrop-blur-md">
        <div className="mx-auto flex max-w-lg items-center justify-around px-4 py-2 sm:py-2.5">
          <button
            type="button"
            onClick={() => setCurrentView('workspace')}
            className={`flex flex-col items-center gap-1 rounded-lg px-4 py-1.5 text-xs font-medium transition ${
              currentView === 'workspace'
                ? 'bg-zinc-100 text-zinc-950 font-semibold'
                : 'text-zinc-500 hover:text-zinc-800 hover:bg-zinc-50'
            }`}
          >
            <LayoutDashboard className="h-4 w-4" />
            <span>Workspace</span>
          </button>

          <button
            type="button"
            onClick={() => setCurrentView('new')}
            className={`flex flex-col items-center gap-1 rounded-lg px-4 py-1.5 text-xs font-medium transition ${
              currentView === 'new'
                ? 'bg-zinc-100 text-zinc-950 font-semibold'
                : 'text-zinc-500 hover:text-zinc-800 hover:bg-zinc-50'
            }`}
          >
            <PlusSquare className="h-4 w-4" />
            <span>New Task</span>
          </button>

          <button
            type="button"
            onClick={() => setCurrentView('activity')}
            className={`flex flex-col items-center gap-1 rounded-lg px-4 py-1.5 text-xs font-medium transition ${
              currentView === 'activity'
                ? 'bg-zinc-100 text-zinc-950 font-semibold'
                : 'text-zinc-500 hover:text-zinc-800 hover:bg-zinc-50'
            }`}
          >
            <ActivityIcon className="h-4 w-4" />
            <span>Activity</span>
          </button>

          <button
            type="button"
            onClick={() => setCurrentView('review')}
            className={`flex flex-col items-center gap-1 rounded-lg px-4 py-1.5 text-xs font-medium transition ${
              currentView === 'review'
                ? 'bg-zinc-100 text-zinc-950 font-semibold'
                : 'text-zinc-500 hover:text-zinc-800 hover:bg-zinc-50'
            }`}
          >
            <GitPullRequest className="h-4 w-4" />
            <span>Reviews</span>
          </button>
        </div>
      </nav>
    </div>
  );
};

export const App: React.FC = () => {
  return (
    <TaskProvider>
      <AppContent />
    </TaskProvider>
  );
};

export default App;
