import React, { useState, useEffect } from 'react';
import {
  GitBranch,
  PlusSquare,
  Activity as ActivityIcon,
  GitPullRequest,
  LayoutDashboard,
} from 'lucide-react';
import { Workspace } from './components/Workspace';
import { NewTask } from './components/NewTask';
import { Activity } from './components/Activity';
import { Review } from './components/Review';
import { useAgentStream } from './hooks/useAgentStream';

type View = 'workspace' | 'new' | 'activity' | 'review';

export const App: React.FC = () => {
  const { status, currentStep, logs, startRun, taskId: streamTaskId } = useAgentStream();
  const [currentView, setCurrentView] = useState<View>('workspace');
  const [selectedBranch, setSelectedBranch] = useState<string>('main');
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  const handleStartRun = (taskId?: string) => {
    if (taskId) {
      setActiveTaskId(taskId);
      try {
        localStorage.setItem('forge_active_task_id', taskId);
      } catch {
        // ignore
      }
    } else {
      startRun();
    }
    setCurrentView('workspace');
  };

  useEffect(() => {
    if (status === 'awaiting_approval' && currentView !== 'workspace') {
      // oxlint-disable-next-line react/set-state-in-effect
      setCurrentView('review');
    }
  }, [status, currentView]);

  return (
    <div className="min-h-screen flex flex-col bg-zinc-50 text-zinc-950 font-sans">
      {/* Top Header */}
      <header className="sticky top-0 z-40 border-b border-zinc-200 bg-white/80 backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <span className="font-semibold text-base tracking-wider text-zinc-950">
              FORGE
            </span>
            <div className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
              {status === 'running'
                ? `RUNNING (${currentStep}/5)`
                : status === 'awaiting_approval'
                ? 'REVIEW REQUIRED'
                : 'ENGINE ACTIVE'}
            </div>
          </div>

          {/* Target Branch Selector */}
          <div className="flex items-center gap-2">
            <div className="relative flex items-center">
              <GitBranch className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-zinc-500" />
              <select
                value={selectedBranch}
                onChange={(e) => setSelectedBranch(e.target.value)}
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

      {/* Main Content Area */}
      <main className="flex-1 overflow-y-auto px-4 py-8 sm:px-6">
        <div className="mx-auto max-w-5xl">
          {currentView === 'workspace' && <Workspace />}
          {currentView === 'new' && (
            <NewTask onStartRun={handleStartRun} onNavigate={setCurrentView} initialBranch={selectedBranch} />
          )}
          {currentView === 'activity' && (
            <Activity logs={logs} currentStep={currentStep} onNavigate={setCurrentView} />
          )}
          {currentView === 'review' && (
            <Review onNavigate={setCurrentView} taskId={activeTaskId || streamTaskId} />
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

export default App;
